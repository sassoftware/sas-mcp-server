# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 4 — Batch Jobs & Async Execution tools."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context, FastMCP

from ..config import CONTEXT_NAME, VIYA_ENDPOINT
from ..viya_client import delete_resource, get_json, get_paged_items, post_json, return_items
from ._common import make_session_helpers


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 4 (Batch Jobs & Async Execution) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def submit_batch_job(sas_code: str, ctx: Context, job_name: str | None = None) -> dict[str, Any]:
        """Submit a SAS job for asynchronous execution via the Job Execution service.

        Args:
            sas_code: SAS code to execute.
            job_name: Optional descriptive name for the job.
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
        async with viya_session("submit_batch_job", ctx) as client:
            return await post_json("/jobExecution/jobs", client, body=body)

    @mcp.tool()
    async def get_job_status(job_id: str, ctx: Context) -> dict[str, Any]:
        """Check the status of a submitted job.

        Args:
            job_id: ID of the job.
        """
        async with viya_session("get_job_status", ctx) as client:
            return await get_json(f"/jobExecution/jobs/{job_id}", client)

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
        """Cancel a running job.

        Args:
            job_id: ID of the job to cancel.
        """
        async with viya_session("cancel_job", ctx) as client:
            await delete_resource(f"/jobExecution/jobs/{job_id}", client)
            return f"Job {job_id} cancelled."

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
            resp.raise_for_status()
            return resp.text
