# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 0 — Compute Contexts & Code Execution tools."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import COMPUTE_SESSION_ID, CONTEXT_NAME, SAS_MCP_EXPECT_HOST, SAS_MCP_PROFILE, VIYA_ENDPOINT
from ..helpers.telemetry_helpers import server_version
from ..token_info import can_renew, token_lifetime
from ..user_log import resolve_user_id
from ..viya_client import (
    contains_filter,
    delete_resource,
    get_paged_items,
    logger,
    make_client,
    return_items,
)
from ..viya_utils import forget_cached_session, list_compute_sessions, reset_cached_session, run_one_snippet
from ._access import host_check
from ._common import SasCode, make_session_helpers


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 0 (Compute Contexts & Code Execution) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def execute_sas_code(
        sas_code: SasCode, ctx: Context, fresh_session: bool = False, html_results: bool = True
    ) -> dict[str, str]:
        """
        Executes the provided SAS code in the Viya environment and returns information about the completed Job.
        This will create a job definition for the SAS code, execute it, and then retrieve the results.

        IMPORTANT — state persists between calls: the code runs in a reusable
        compute session that is kept warm and shared across calls (per user),
        so SAS state — WORK tables, macro variables, and assigned librefs —
        survives between successive ``execute_sas_code`` calls. A re-run can
        therefore see leftovers from earlier calls (e.g. a check that counts
        results twice). Pass ``fresh_session=True`` (or call
        ``reset_compute_session``) when the code must start from a clean slate.

        Limits of the session:

        - It runs one job at a time. A second call waits until the first ends,
          so a long job blocks every other call. Run long work with
          ``submit_batch_job``, which gets a session of its own, and keep this
          tool free for short steps. Do not pass ``fresh_session`` while
          another call is still running: it deletes the session under it.
        - Its memory is capped by the deployment's compute pod. A PROC SQL
          join or a hash object can exhaust it on tables well under a million
          rows and end the session. Sort both tables and MERGE them in a DATA
          step, or do the join in CAS.

        Tip: to reach CAS data, prefer ``libname casuser cas;`` (or a targeted
        ``caslib`` statement) over ``caslib _all_ assign;`` — on tenants with
        many caslibs the latter floods the log with assignment NOTEs.

        Args:
            sas_code (str): the SAS code snippet to be executed using the Viya Job Execution API Service
            fresh_session: When True, discard any cached compute session first
                so the code runs with no inherited SAS state (equivalent to
                calling ``reset_compute_session`` immediately before).
            html_results: When True (the default), the output is also written
                as an HTML page, as SAS Data and AI Studio shows it, graphs
                included, and
                saved so the person can open it in a browser. The code runs
                between an ``ods html5`` statement and its close, preceded by
                ``;*';*";*/;run;quit;`` so that an unterminated statement or
                quote cannot swallow the close; neither appears in the log.
                Pass False to submit the code exactly as given.

        Returns:
            A dictionary of string fields describing the executed job:
            ``snippet_id`` (the job's snippet identifier), ``state`` (the final
            job state, e.g. ``completed``/``error``/``warning``), ``log`` (the
            full SAS log — execution details, notes, and any errors/warnings),
            and ``listing`` (the SAS listing output, i.e. the intended results
            when the code ran successfully). When the code printed output and
            ``html_results`` is on, also ``html_results_url``: the same output
            as an HTML page, as SAS Data and AI Studio shows it. Put this URL in
            your reply so the person can open it; they may not see the tool
            result itself.
            It opens in their browser after they sign in to SAS Viya, for them
            only. Do not fetch it yourself: ``listing`` already holds the same
            output as text. ``html_results_error`` instead says why the page
            could not be saved.
        """
        logger.info("--- TOOL USED: execute_sas_code ---")
        token = await get_token(ctx)
        if fresh_session and not COMPUTE_SESSION_ID:
            # Fixed externally managed sessions can't be reset; run_one_snippet
            # creates a new cached session transparently after the reset.
            async with make_client(token) as client:
                await reset_cached_session(client, CONTEXT_NAME, token)
        return await run_one_snippet(sas_code, "1", token, html_results=html_results)

    @mcp.tool()
    async def server_status(ctx: Context) -> dict[str, Any]:
        """Report which SAS Viya deployment this server is connected to, and as whom.

        Call it before the first write or job of a task, and whenever results
        look as if they came from another environment. It returns no secrets.

        Returns:
            ``viya_endpoint`` and ``host``: where every tool call goes.
            ``profile``: the environment profile that started the server
            (``SAS_MCP_PROFILE``), or null when the launcher did not say.
            ``expected_host`` and ``host_matches``: the ``SAS_MCP_EXPECT_HOST``
            check, null when it is not set. ``writes_refused`` is true when the
            check fails: tools that write or run code then refuse, and read
            tools keep working.
            ``user``: the SAS Viya user id the calls run as, null when it could
            not be looked up.
            ``token``: ``expires_at`` and ``seconds_left`` of the access token,
            and ``renewal_available`` (whether this server can renew it
            itself), or ``error`` when no token could be obtained.
            ``server_version``.
        """
        logger.info("--- TOOL USED: server_status ---")
        check = host_check(VIYA_ENDPOINT, SAS_MCP_EXPECT_HOST)
        status: dict[str, Any] = {
            "viya_endpoint": VIYA_ENDPOINT,
            "host": check["connected_host"],
            "profile": SAS_MCP_PROFILE or None,
            "expected_host": check["expected_host"],
            "host_matches": check["matches"],
            "writes_refused": check["matches"] is False,
            "user": None,
            "server_version": server_version(),
        }
        try:
            token = await get_token(ctx)
        except Exception as exc:  # noqa: BLE001 - the endpoint is worth reporting without a token
            status["token"] = {"error": f"{type(exc).__name__}: {exc}"}
            return status
        status["user"] = await resolve_user_id(token)
        status["token"] = {**(token_lifetime(token) or {}), "renewal_available": can_renew()}
        return status

    @mcp.tool()
    async def list_compute_contexts(
        ctx: Context, limit: int = 50, start: int = 0, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List available compute contexts on the Viya environment."""
        if COMPUTE_SESSION_ID:
            return [{
                "name": f"fixed-session:{COMPUTE_SESSION_ID}",
                "description": (
                    "Fixed compute session mode enabled via COMPUTE_SESSION_ID; "
                    "context discovery is bypassed."
                ),
            }]
        async with viya_session("list_compute_contexts", ctx) as client:
            filters = contains_filter(filter_name)
            items, _ = await get_paged_items("/compute/contexts", client, limit=limit, start=start, filters=filters)
            return return_items(items, ["name", "description"])

    @mcp.tool(name="list_compute_sessions")
    async def list_compute_sessions_tool(ctx: Context, limit: int = 50, start: int = 0) -> dict[str, Any]:
        """List your SAS compute sessions: the warm one this server reuses and any batch job's.

        A job that was cancelled, or whose record is gone, can leave its
        session running, and the session holds a pod and any database
        connections it opened. Use this to find such sessions and
        ``delete_compute_session`` to end one.

        Args:
            limit: Maximum sessions to return (default 50).
            start: Offset of the first session, for paging.

        Returns:
            ``sessions``: ``id``, ``name``, ``state`` (``idle``, ``running``,
            ...), ``owner``, ``created``, ``context_id`` and
            ``cached_by_this_server`` (true for the session that
            ``execute_sas_code`` reuses). ``count``: the total Viya reports.
            ``has_more``: whether another page follows.
        """
        async with viya_session("list_compute_sessions", ctx) as client:
            sessions, count = await list_compute_sessions(client, limit=limit, start=start)
        return {"sessions": sessions, "count": count, "has_more": start + len(sessions) < count}

    @mcp.tool()
    async def delete_compute_session(session_id: str, ctx: Context) -> dict[str, Any]:
        """End a SAS compute session, stopping whatever it is running.

        Its SAS process exits, so its pod goes away and the database
        connections it held close. Any job still running in it fails. Find
        session ids with ``list_compute_sessions``. Deleting the session that
        ``execute_sas_code`` reuses discards its WORK tables and macro
        variables, like ``reset_compute_session``; the next call starts a new
        one.

        Args:
            session_id: Id of the compute session to delete.

        Returns:
            ``status``: ``deleted``, ``not_found`` (already gone) or
            ``refused`` (the fixed ``COMPUTE_SESSION_ID`` session).
            ``was_cached_session``: whether it was the session this server
            reused.
        """
        if COMPUTE_SESSION_ID and session_id == COMPUTE_SESSION_ID:
            return {
                "status": "refused",
                "session_id": session_id,
                "message": "This is the fixed COMPUTE_SESSION_ID session; it is managed outside this server.",
            }
        async with viya_session("delete_compute_session", ctx) as client:
            try:
                await delete_resource(f"/compute/sessions/{session_id}", client)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                forget_cached_session(session_id)
                return {"status": "not_found", "session_id": session_id, "was_cached_session": False}
        return {
            "status": "deleted",
            "session_id": session_id,
            "was_cached_session": forget_cached_session(session_id),
        }

    @mcp.tool()
    async def reset_compute_session(ctx: Context, compute_context_name: str | None = None) -> dict[str, str]:
        """Reset (delete) the cached compute session for a compute context.

        The server keeps one reusable SAS compute session per user and compute
        context so repeat calls skip the slow session spin-up; SAS state (WORK
        tables, macro variables, assigned librefs) therefore persists across
        ``execute_sas_code`` and ``list_compute_*`` calls. Call this to discard
        that state — the next compute tool call transparently creates a fresh
        session.

        Args:
            compute_context_name: Compute context whose session to reset.
                Defaults to the server's configured execution context (the one
                ``execute_sas_code`` uses).
        """
        context_name = compute_context_name or CONTEXT_NAME
        if COMPUTE_SESSION_ID:
            return {
                "status": "fixed_session_mode",
                "compute_context": context_name,
                "message": (
                    "COMPUTE_SESSION_ID is configured; the session is externally "
                    "managed and cannot be reset by this server."
                ),
            }
        logger.info("--- TOOL USED: reset_compute_session ---")
        token = await get_token(ctx)
        async with make_client(token) as client:
            sid = await reset_cached_session(client, context_name, token)
        if sid is None:
            return {
                "status": "no_active_session",
                "compute_context": context_name,
                "message": "No cached compute session to reset for this context.",
            }
        return {
            "status": "reset",
            "compute_context": context_name,
            "deleted_session": sid,
        }
