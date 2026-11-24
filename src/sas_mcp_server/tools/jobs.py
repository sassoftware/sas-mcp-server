# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 4 — Batch Jobs & Async Execution tools."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import CONTEXT_NAME, VIYA_ENDPOINT
from ..token_info import ensure_token_lifetime
from ..viya_client import (
    delete_resource,
    get_json,
    get_paged_items,
    logger,
    make_client,
    post_json,
    raise_for_viya_status,
    return_items,
)
from ..viya_utils import list_compute_sessions
from ._common import SasCode, make_session_helpers

# Sessions listed beside a job record that is gone; enough to spot a leftover.
_GONE_JOB_SESSION_LIMIT = 20


async def _job_record_gone(client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    """What the server can say about a job whose record no longer exists."""
    result: dict[str, Any] = {
        "status": "not_found",
        "job_id": job_id,
        "message": (
            "No Job Execution record has this id. cancel_job deletes the record, and Job Execution "
            "deletes a job once it expires. The compute session the job ran in can still be running: "
            "compute_sessions lists your sessions other than the one execute_sas_code reuses. End a "
            "leftover with delete_compute_session."
        ),
    }
    try:
        sessions, _ = await list_compute_sessions(client, limit=_GONE_JOB_SESSION_LIMIT)
    except httpx.HTTPError as exc:
        result["compute_sessions_error"] = str(exc)
    else:
        result["compute_sessions"] = [s for s in sessions if not s["cached_by_this_server"]]
    return result


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 4 (Batch Jobs & Async Execution) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def submit_batch_job(
        sas_code: SasCode,
        ctx: Context,
        job_name: str | None = None,
        expected_runtime_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Submit a SAS job for asynchronous execution via the Job Execution service.

        The job runs in a compute session of its own, not the one
        ``execute_sas_code`` reuses, so interactive calls keep working while it
        runs. Poll it with ``get_job_status`` and read its log with
        ``get_job_log``.

        A job can outlive the access token it was submitted with, and then
        steps that call CAS or other SAS Viya services fail. Before submitting,
        the server renews a token with less than 30 minutes left, or less than
        ``expected_runtime_minutes`` when given, if it can renew by itself
        (stdio with a cached login). The result says how long the token has.

        Args:
            sas_code: SAS code to execute.
            job_name: Optional descriptive name for the job.
            expected_runtime_minutes: How long the job is expected to run. Used
                to decide whether to renew the token first, and to warn when
                even a renewed token expires sooner.

        Returns:
            The Job Execution job record (``id``, ``state``, ...), plus
            ``token_lifetime``: ``expires_at``, ``seconds_left``, ``renewed``,
            ``renewal_available`` and, when the token may expire before the job
            ends, ``warning``. Absent when the token carries no expiry.
        """
        body = {
            "name": job_name or "mcp-batch-job",
            "jobDefinition": {
                "type": "Compute",
                "code": sas_code,
            },
            "arguments": {
                "_contextName": CONTEXT_NAME,
            },
        }
        logger.info("--- TOOL USED: submit_batch_job ---")
        token, lifetime = await ensure_token_lifetime(await get_token(ctx), expected_runtime_minutes)
        async with make_client(token) as client:
            job = await post_json("/jobExecution/jobs", client, body=body)
        if lifetime is not None:
            job["token_lifetime"] = lifetime
        return job

    @mcp.tool()
    async def get_job_status(job_id: str, ctx: Context) -> dict[str, Any]:
        """Check job state. A missing record returns status="not_found" and the requested job ID.

        Args:
            job_id: ID of the job.

        Returns:
            The Job Execution job record (``state``, ``results``, ...). When
            the record no longer exists: ``status`` ``not_found``, a message,
            and ``compute_sessions``, your sessions that may still be running
            the job's code.
        """
        async with viya_session("get_job_status", ctx) as client:
            try:
                return await get_json(f"/jobExecution/jobs/{job_id}", client)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                return await _job_record_gone(client, job_id)

    @mcp.tool()
    async def list_jobs(ctx: Context, limit: int = 20) -> list[dict[str, Any]]:
        """List recent jobs from the Job Execution service.

        Args:
            limit: Maximum jobs to return (default 20).
        """
        async with viya_session("list_jobs", ctx) as client:
            items, _ = await get_paged_items("/jobExecution/jobs", client, limit=limit)
            return return_items(items, ["id", "name", "state", "creationTimeStamp"])

    @mcp.tool()
    async def cancel_job(job_id: str, ctx: Context) -> str:
        """Delete the Job Execution record. The compute session may still be running.

        Args:
            job_id: ID of the job to cancel.
        """
        async with viya_session("cancel_job", ctx) as client:
            await delete_resource(f"/jobExecution/jobs/{job_id}", client)
            return f"Deleted the record for job {job_id}. Its compute session may still be running."

    @mcp.tool()
    async def get_job_log(job_id: str, ctx: Context) -> str:
        """Retrieve the log of a completed job.

        Args:
            job_id: ID of the job.
        """
        async with viya_session("get_job_log", ctx) as client:
            data = await get_json(f"/jobExecution/jobs/{job_id}", client)
            results = data.get("results", {})

            log_uri = None
            for key, value in results.items():
                if key.endswith(".log.txt"):
                    log_uri = value
                    break
            if not log_uri:
                for key, value in results.items():
                    if key.endswith(".log"):
                        log_uri = value
                        break

            if not log_uri:
                state = data.get("state", "unknown")
                error = data.get("error", {})
                if error:
                    return f"Job {state}: {error.get('message', 'No error details')}"
                return f"No log available. Job state: {state}"

            resp = await client.get(f"{VIYA_ENDPOINT}{log_uri}/content")
            raise_for_viya_status(resp)
            return resp.text
