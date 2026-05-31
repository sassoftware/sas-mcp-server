# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SAS Viya compute session and job orchestration.

These helpers drive the Compute service end to end: resolve a compute context,
open a session, submit code, poll for completion, and tear the session down.
``run_one_snippet`` is the public entry point used by the ``execute_sas_code``
MCP tool. Generic REST helpers and the shared client/logger live in
:mod:`sas_mcp_server.viya_client`.
"""

import asyncio

import httpx

from .config import CONTEXT_NAME, VIYA_ENDPOINT
from .viya_client import logger, make_client


async def get_context_id(client: httpx.AsyncClient, context_name: str) -> str:
    """Return the id of the named compute context, raising if it is absent."""
    url = f"{VIYA_ENDPOINT}/compute/contexts?name={context_name}"
    resp = await client.get(url)
    coll = resp.json()
    items = coll.get("items", [])
    if not items:
        raise RuntimeError(f"Compute context not found: {context_name}")
    return items[0]["id"]


async def create_session(
    client: httpx.AsyncClient, context_id: str, name: str = "py-parallel"
) -> str:
    """Create a compute session in *context_id* and return its id."""
    url = f"{VIYA_ENDPOINT}/compute/contexts/{context_id}/sessions"
    resp = await client.post(url, json={"name": name})
    return resp.json()["id"]


async def submit_job(client: httpx.AsyncClient, session_id: str, code: str) -> str:
    """Submit *code* as a job in *session_id* and return the job id."""
    body = {"code": code.splitlines()}
    url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs"
    resp = await client.post(url, json=body)
    job = resp.json()
    return job["id"]


async def wait_job(
    client: httpx.AsyncClient, session_id: str, job_id: str, poll: float = 2
) -> tuple[str, str, str]:
    """Poll *job_id* until it reaches a terminal state; return (state, log, listing)."""
    while True:
        state_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/state"
        resp = await client.get(state_url)
        state = resp.text.strip()
        if state in ("completed", "error", "warning", "canceled"):
            # Fetch log
            log_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/log"
            log_resp = await client.get(log_url)
            log = log_resp.json()
            lines = [item["line"] for item in log.get("items", [])]
            log_text = "\n".join(lines)

            # Fetch listing (plain text output)
            listing_url = (
                f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/listing"
            )
            listing_resp = await client.get(listing_url)
            listing_json = listing_resp.json()
            listing_lines = [item["line"] for item in listing_json.get("items", [])]
            listing_text = (
                "\n".join(listing_lines) if listing_lines else "(no listing output)"
            )

            return state, log_text, listing_text
        await asyncio.sleep(poll)


async def run_one_snippet(
    snippet_data: str, snippet_id: str, token: str
) -> dict[str, str]:
    """Execute one SAS snippet end to end and return its structured result.

    Returns a dict with keys ``snippet_id``, ``state``, ``log`` and ``listing``.
    The compute session is always torn down in a ``finally`` block.
    """
    code = snippet_data

    logger.info("Creating session with token (length: %d)", len(token))

    async with make_client(token) as client:
        ctx_id = await get_context_id(client, CONTEXT_NAME)
        sid = await create_session(client, ctx_id, name="py-parallel")
        logger.info("Session created: %s", sid)
        try:
            jid = await submit_job(client, sid, code)
            logger.info("Job submitted: %s", jid)
            state, log_text, listing_text = await wait_job(client, sid, jid)
            logger.info("Job completed: %s", state)
            return {
                "snippet_id": snippet_id,
                "state": state,
                "log": log_text,
                "listing": listing_text,
            }
        except Exception:
            logger.exception("Error executing SAS job")
            raise
        finally:
            try:
                delete_url = f"{VIYA_ENDPOINT}/compute/sessions/{sid}"
                await client.delete(delete_url)
                logger.info("Session %s deleted successfully", sid)
            except Exception:
                logger.exception("Failed to delete session %s", sid)
                raise
