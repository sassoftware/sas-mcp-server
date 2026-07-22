# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 8 — Workbench (Execute Code Only)."""

from collections.abc import Awaitable, Callable

from fastmcp import Context, FastMCP

from ..viya_client import logger
from ..viya_utils import run_one_snippet


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 8 (Workbench) tools on *mcp*."""

    @mcp.tool()
    async def execute_sas_code(sas_code: str, ctx: Context) -> dict[str, str]:
        """Execute SAS code in a reusable Viya compute session.

        Args:
            sas_code (str): the SAS code snippet to be executed using the Viya Job Execution API Service

        Returns:
            A dictionary with four string fields describing the executed job:
            ``snippet_id`` (the job's snippet identifier), ``state`` (the final
            job state, e.g. ``completed``/``error``/``warning``), ``log`` (the
            full SAS log — execution details, notes, and any errors/warnings),
            and ``listing`` (the SAS listing output, i.e. the intended results
            when the code ran successfully).
        """
        logger.info("--- TOOL USED: execute_sas_code ---")
        token = await get_token(ctx)
        return await run_one_snippet(sas_code, "1", token)
