# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 8 — Workbench (Execute Code Only)."""

from collections.abc import Awaitable, Callable

from fastmcp import Context, FastMCP

from ..viya_client import logger
from ..viya_utils import run_one_snippet
from ._common import SasCode


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 8 (Workbench) tools on *mcp*."""

    @mcp.tool()
    async def execute_sas_code(sas_code: SasCode, ctx: Context, html_results: bool = True) -> dict[str, str]:
        """Execute SAS code in a reusable Viya compute session.

        Limits of the session:

        - It runs one job at a time. A second call waits until the first ends,
          so a long job blocks every other call. Where the
          deployment offers ``submit_batch_job``, run long work there: it gets
          a session of its own.
        - Its memory is capped by the deployment's compute pod. A PROC SQL
          join or a hash object can exhaust it on tables well under a million
          rows and end the session. Sort both tables and MERGE them in a DATA
          step, or do the join in CAS.

        Args:
            sas_code (str): the SAS code snippet to be executed using the Viya Job Execution API Service
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
        return await run_one_snippet(sas_code, "1", token, html_results=html_results)
