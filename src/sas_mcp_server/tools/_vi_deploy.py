# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AML model deployment tools (publish to a destination). GATED WRITES.

Publish a registered model or an AutoML champion to a publishing destination
(e.g. 'maslocal' = MAS). Deploy tools default to dry_run=True (preview only);
production deployment must be approved (ask the user) before dry_run=False.

Note: use ``list_publishing_destinations`` (Tier 6) to find a valid
``destination_name`` — it hits the same endpoint and is already registered.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import get_json, logger, make_client
from ._vi_links import ml_project_url, model_url


def _mas_module_name(raw: str) -> str:
    """Sanitize a name to a MAS-safe module name (alphanumeric/underscore)."""
    mod = "".join(c if (c.isalnum() or c == "_") else "_" for c in raw).strip("_")
    if not mod:
        mod = "model"
    if mod[0].isdigit():
        mod = "m_" + mod
    return mod


def register_aml_deploy(mcp: FastMCP, get_token) -> None:
    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    def _dry_run_preview(method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "dry_run": True,
            "would_send": {"method": method, "url": f"{VIYA_ENDPOINT}{path}", "body": body},
            "note": ("Preview only — no changes made. "
                     "Get approval, then rerun with dry_run=False. "
                     "Do not run against a shared/demo environment without approval."),
        }

    @mcp.tool()
    async def publish_model(model_id: str, destination_name: str, ctx: Context,
                            module_name: str | None = None,
                            dry_run: bool = True) -> dict[str, Any]:
        """Publish/deploy a registered model to a destination (e.g. 'maslocal'=MAS). GATED WRITE.

        Fetches the model's score code from the Model Repository (the ``role=score``
        file) and publishes via ``POST /modelPublish/models`` — the correct path on
        this deployment (``/modelManagement/publishModels`` does not exist here).
        The MAS module name is sanitized to alphanumerics/underscore (spaces /
        parentheses are rejected by MAS). Inline score code is required (a
        sourceURI alone yields "no code to publish").

        Defaults to dry_run (preview only). Do NOT run dry_run=False against a
        shared/demo environment without approval.

        Use ``list_publishing_destinations`` (Tier 6) to find a valid
        ``destination_name``.

        Args:
            model_id: Registered model id (from ``list_registered_models``).
            destination_name: Destination (from ``list_publishing_destinations``, e.g. 'maslocal').
            module_name: Optional MAS module name; defaults to a sanitized model name.
            dry_run: Keep True to preview only. False executes the publish (requires approval).
        """
        mod = _mas_module_name(module_name or f"model_{model_id.replace('-', '')[:12]}")
        path = "/modelPublish/models"
        async with viya_session("publish_model", ctx) as client:
            contents = await get_json(
                f"/modelRepository/models/{model_id}/contents", client,
                accept="application/vnd.sas.collection+json")
            score_id = next((it.get("id") for it in contents.get("items", [])
                             if it.get("role") == "score"), None)
            if not score_id:
                return {"error": "no score-code file (role=score) found for this model",
                        "model_id": model_id}
            if dry_run:
                return _dry_run_preview("POST", path, {
                    "name": mod, "destinationName": destination_name,
                    "modelContents": [{"modelName": mod, "modelID": model_id,
                                       "codeType": "ds2",
                                       "code": f"<score code from content {score_id}>"}]})
            code_resp = await client.get(
                f"{VIYA_ENDPOINT}/modelRepository/models/{model_id}/contents/{score_id}/content",
                headers={"Accept": "text/plain"})
            code_resp.raise_for_status()
            body = {"name": mod, "note": "AML model publish (agent)",
                    "destinationName": destination_name,
                    "modelContents": [{"modelName": mod, "modelID": model_id,
                                       "code": code_resp.text, "codeType": "ds2"}]}
            resp = await client.post(
                f"{VIYA_ENDPOINT}{path}", json=body,
                headers={"Content-Type": "application/vnd.sas.models.publishing.request+json",
                         "Accept": "application/json"})
            resp.raise_for_status()
            return {**resp.json(), **model_url(model_id)}

    @mcp.tool()
    async def publish_champion_model(project_id: str, destination_name: str, ctx: Context,
                                     dry_run: bool = True) -> dict[str, Any]:
        """Publish/deploy an AutoML project's champion model to a destination (e.g. MAS). GATED WRITE.

        Defaults to dry_run (returns the request it would send). Deployment is
        approval-gated; do not run dry_run=False against a shared/demo environment
        without approval.

        Use ``list_publishing_destinations`` (Tier 6) to find a valid
        ``destination_name``.

        Args:
            project_id: The AutoML project id.
            destination_name: Target publishing destination (e.g. a MAS destination).
            dry_run: Keep True to preview only. False executes the publish.
        """
        path = f"/mlPipelineAutomation/projects/{project_id}/models/@championModel"
        if dry_run:
            return _dry_run_preview(
                "PUT", f"{path}?action=publish&destinationName={destination_name}", {})
        url = f"{VIYA_ENDPOINT}{path}"
        async with viya_session("publish_champion_model", ctx) as client:
            resp = await client.put(
                url, params={"action": "publish", "destinationName": destination_name},
                content=b" ",
                headers={"Accept": "application/json", "Content-Type": "application/json"})
            resp.raise_for_status()
            return {**resp.json(), **ml_project_url(project_id)}
