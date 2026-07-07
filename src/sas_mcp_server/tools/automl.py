# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 5 — Automated Machine Learning tools."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..helpers import auto_ml_helpers
from ..viya_client import get_json, get_paged_items, post_json, return_items
from ._common import make_session_helpers


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 5 (Automated Machine Learning) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def list_ml_projects(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List AutoML pipeline automation projects.

        Args:
            limit: Maximum projects to return (default 50).
        """
        async with viya_session("list_ml_projects", ctx) as client:
            items, _ = await get_paged_items("/mlPipelineAutomation/projects", client, limit=limit)
            return return_items(items, ["id", "name", "state", "description"])

    @mcp.tool()
    async def create_ml_project(
        project_name: str,
        caslib_name: str,
        table_name: str,
        target_variable: str,
        ctx: Context,
        server_id: str = "cas-shared-default",
        description: str = "",
        prediction_type: str = "binary",
        target_event_level: str = "1",
        auto_run: bool = True,
    ) -> dict[str, Any]:
        """Create a new AutoML pipeline automation project from a CAS table.

        The training table must already be loaded into CAS memory at **global**
        scope. This tool verifies that first and returns an actionable error
        otherwise (use ``promote_table_to_memory`` to load + promote a source
        table, and ``list_source_tables`` to find one). The data-table URI is
        built from ``server_id``/``caslib_name``/``table_name``.

        Args:
            project_name: Name for the project.
            caslib_name: Caslib containing the training table.
            table_name: Name of the (loaded, global) training table.
            target_variable: Name of the target/response variable.
            server_id: CAS server name or ID (default 'cas-shared-default').
            description: Optional project description.
            prediction_type: 'binary', 'interval', or 'nominal' (default 'binary').
            target_event_level: Target event level for binary/nominal classification (default '1').
            auto_run: Whether to automatically run pipelines after creation (default True).
        """
        table_path = f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}"
        data_table_uri = f"/dataTables/dataSources/cas~fs~{server_id}~fs~{caslib_name}/tables/{table_name}"
        analytics_attrs: dict[str, Any] = {
            "targetVariable": target_variable,
            "targetLevel": prediction_type,
            "partitionEnabled": True,
            "classSelectionStatistic": ("ks" if prediction_type in ("binary", "nominal") else "ase"),
        }
        if prediction_type in ("binary", "nominal"):
            analytics_attrs["targetEventLevel"] = target_event_level
        body = {
            "name": project_name,
            "description": description,
            "type": "predictive",
            "dataTableUri": data_table_uri,
            "pipelineBuildMethod": "automatic",
            "settings": {
                "applyGlobalMetadata": True,
                "autoRun": auto_run,
                "numberOfModels": 5,
            },
            "analyticsProjectAttributes": analytics_attrs,
        }
        async with viya_session("create_ml_project", ctx) as client:
            # Pre-flight: the training table must be loaded in global scope, or
            # mlPipelineAutomation fails opaquely later.
            try:
                info = await get_json(table_path, client)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return {
                        "status": "table_not_found",
                        "table": f"{caslib_name}.{table_name}",
                        "message": (
                            f"Table '{table_name}' not found in caslib '{caslib_name}' on "
                            f"server '{server_id}'. Load and promote it with "
                            "promote_table_to_memory (see list_source_tables)."
                        ),
                    }
                raise
            if not (info.get("state") == "loaded" and info.get("scope") == "global"):
                return {
                    "status": "table_not_global",
                    "table": f"{caslib_name}.{table_name}",
                    "state": info.get("state"),
                    "scope": info.get("scope"),
                    "message": (
                        "The training table must be loaded in global scope before "
                        "creating an ML project. Use promote_table_to_memory to load "
                        "and promote it."
                    ),
                }
            return await post_json("/mlPipelineAutomation/projects", client, body=body)

    @mcp.tool()
    async def register_ml_champion_model(project_id: str, ctx: Context) -> dict[str, Any]:
        """Register the champion model from an AutoML pipeline automation project to the Model Repository.

        Args:
            project_id: ID of the ML pipeline automation project.
        """
        async with viya_session("register_ml_champion_model", ctx) as client:
            props = auto_ml_helpers.MLRegisterProps(project_id=project_id)
            return await auto_ml_helpers.ml_register_publish(props, client)

    @mcp.tool()
    async def publish_ml_champion_model(project_id: str, destination_name: str, ctx: Context) -> dict[str, Any]:
        """Publish the champion model from an AutoML pipeline automation project to the Model Repository.

        Args:
            project_id: ID of the ML pipeline automation project.
            destination_name: Name of the destination to publish to.
        """
        async with viya_session("publish_ml_champion_model", ctx) as client:
            props = auto_ml_helpers.MLPublishProps(project_id=project_id, destination_name=destination_name)
            return await auto_ml_helpers.ml_register_publish(props, client)

    @mcp.tool()
    async def run_ml_project(project_id: str, ctx: Context) -> dict[str, Any]:
        """Run an AutoML pipeline automation project.

        Args:
            project_id: ID of the project to run.
        """
        mlpa_type = "application/vnd.sas.analytics.ml.pipeline.automation.project+json"
        async with viya_session("run_ml_project", ctx) as client:
            get_resp = await client.get(
                f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                headers={"Accept": mlpa_type},
            )
            get_resp.raise_for_status()
            project_body = get_resp.json()
            etag = get_resp.headers.get("etag", "")
            resp = await client.put(
                f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                params={"action": "retrainProject"},
                content=json.dumps(project_body).encode(),
                headers={
                    "Content-Type": mlpa_type,
                    "Accept": mlpa_type,
                    "If-Match": etag,
                    "Accept-Language": "en",
                },
            )
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return {"status": "running", "projectId": project_id}
            return resp.json()
