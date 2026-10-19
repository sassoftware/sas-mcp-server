# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AML model build/validate tools (read + AutoML orchestration).

Model BUILD/SCORE primitives already exist (create_ml_project / run_ml_project /
score_data). Here: project status polling, champion registration, model detail
and the validation confusion matrix. Deterministic; the only write is champion
registration (idempotent, into the Model Repository).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import bound_text, get_json, logger, make_client
from ..viya_utils import run_one_snippet
from ._vi_links import model_url


def register_aml_models(mcp: FastMCP, get_token) -> None:
    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    async def _run_fixed_sas(sas: str, ctx: Context) -> dict[str, Any]:
        token = await get_token(ctx)
        res = await run_one_snippet(sas, "1", token)
        listing = (res.get("listing") or "").strip()
        # Return the listing whenever there is one; a benign WARNING (e.g. OUTOBS=
        # truncation) sets state="warning" but the results are still valid. Only
        # fall back to the raw log when there is genuinely no listing output.
        if listing and listing != "(no listing output)":
            return {"state": res.get("state"), "output": bound_text(listing)}
        return {"state": res.get("state"), "output": bound_text(res.get("log", ""))}

    @mcp.tool()
    async def get_model(model_id: str, ctx: Context) -> dict[str, Any]:
        """Get a registered model's detail from the Model Repository (for validation). Read-only.

        Args:
            model_id: Model id (from ``list_registered_models``).
        """
        async with viya_session("get_model", ctx) as client:
            return await get_json(f"/modelRepository/models/{model_id}", client)

    @mcp.tool()
    async def get_ml_project_status(project_id: str, ctx: Context) -> dict[str, Any]:
        """Get an AutoML pipeline project's current state — poll after create_ml_project. Read-only.

        An AutoML project moves through states like ``constructingPipeline`` /
        ``modeling`` to ``completed``. Flow: create_ml_project (auto_run=False =
        construct only) → poll to completed → run_ml_project (train) → poll to
        completed → register_champion_model.

        Args:
            project_id: The project id from ``create_ml_project``.
        """
        url = f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}/state"
        async with viya_session("get_ml_project_status", ctx) as client:
            resp = await client.get(url, headers={"Accept": "text/plain"})
            resp.raise_for_status()
            return {"project_id": project_id, "state": resp.text.strip()}

    @mcp.tool()
    async def register_champion_model(project_id: str, ctx: Context) -> dict[str, Any]:
        """Register a completed AutoML project's champion model into the Model Repository.

        Run after training (create_ml_project → run_ml_project → status 'completed').
        Makes the champion visible to ``list_registered_models`` / ``get_model`` for
        validation and deployment.

        Args:
            project_id: The AutoML project id.
        """
        url = f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}/models/@championModel"
        async with viya_session("register_champion_model", ctx) as client:
            resp = await client.put(
                url, params={"action": "register"}, content=b" ",
                headers={"Accept": "application/json", "Content-Type": "application/json"})
            resp.raise_for_status()
            registered = resp.json()
            mid = (registered.get("id") or registered.get("modelId")
                   or registered.get("championModelId"))
            result = {**registered, **model_url(mid)}
            if mid:
                # Record lineage: registered model depends on its AutoML project.
                # Wrapped in try/except — lineage.py is wired separately as a hook
                # in mcp_server.py; this silently activates once it is integrated.
                try:
                    from ..lineage import ml_project_uri, model_uri, record_failsoft
                    await record_failsoft(
                        client, result,
                        [(model_uri(mid), ml_project_uri(project_id))],
                    )
                except ImportError:
                    pass
            return result

    @mcp.tool()
    async def get_model_confusion_matrix(ctx: Context) -> dict[str, Any]:
        """Model performance confusion matrix (APPLICATION_CONFUSION_MATRIX). Read-only.

        Fixed read of the model validation confusion-matrix table (per-model
        TP/TN/FP/FN) for model-effectiveness assessment.
        """
        sas = """options validvarname=any;
cas s; caslib _all_ assign;
proc casutil; load casdata="APPLICATION_CONFUSION_MATRIX.sashdat" incaslib="Public"
  casout="_t" outcaslib="CASUSER" replace; quit;
title "Model confusion matrix (APPLICATION_CONFUSION_MATRIX)";
proc print data=CASUSER._t noobs; run;
title; cas s terminate;"""
        return await _run_fixed_sas(sas, ctx)
