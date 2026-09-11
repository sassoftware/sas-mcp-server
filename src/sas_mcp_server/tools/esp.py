# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 9 — Event Stream Processing (ESP) tools.

Wraps the SAS Event Stream Processing REST/XML layer
(``/eventStreamProcessing/v1/...``) the same way the other tiers wrap their
Viya microservice: reuse the shared ``viya_session`` helper for the
authenticated ``httpx.AsyncClient``, so ESP calls ride the same SASLogon
bearer token as every other tool in this server.

IMPORTANT — verify the endpoint paths against your environment first.
Unlike the other tiers (catalog, casManagement, reports, ...), ESP's REST API
has historically been documented as living directly on an ESP *server*
process (``http://ESPhost:http_port/eventStreamProcessing/v1/...``) rather
than uniformly behind the shared Viya API gateway used by
``VIYA_ENDPOINT``. Whether your deployment fronts it at
``{VIYA_ENDPOINT}/eventStreamProcessing/v1/...`` (typical when SAS Event
Stream Manager proxies ESP servers under Viya, e.g. as a "module" alongside
Visual Analytics / Model Manager) depends on your specific Viya
configuration. Call ``discover_esp_api`` first — it fetches the service's
own API-documentation resource so you (or the calling model) can confirm the
real base path and operation names before trusting any other tool below. If
paths differ in your environment, only the URL fragments built inside this
module need to change — the auth/session plumbing does not.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import get_json, post_json
from ._common import make_session_helpers

# Base path for the ESP REST/XML layer. Override here (or via env var, if you
# add one) if `discover_esp_api` shows your environment mounts it elsewhere.
_ESP_BASE = "/eventStreamProcessing/v1"


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 9 (Event Stream Processing) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def discover_esp_api(ctx: Context) -> dict[str, Any]:
        """Fetch the live ESP API description to confirm real paths for this environment.

        Call this FIRST, before any other esp_* tool. SAS's ESP REST API has
        varied its mount point across versions/deployments (standalone ESP
        server vs. Event Stream Manager proxying it under Viya). This hits a
        couple of likely candidate URLs under VIYA_ENDPOINT and reports which
        one (if any) responded, plus the raw body, so you can confirm or
        correct the base path this module assumes
        (``{VIYA_ENDPOINT}/eventStreamProcessing/v1``).
        """
        candidates = [
            f"{_ESP_BASE}/apiDoc?format=json",
            f"{_ESP_BASE}",
            "/eventStreamProcessing",
            "/SASEventStreamProcessingServer",
        ]
        async with viya_session("discover_esp_api", ctx) as client:
            results = []
            for path in candidates:
                url = f"{VIYA_ENDPOINT}{path}"
                try:
                    resp = await client.get(url, headers={"Accept": "application/json"})
                    results.append({
                        "path": path,
                        "status_code": resp.status_code,
                        "content_type": resp.headers.get("content-type", ""),
                        "body_preview": resp.text[:1000],
                    })
                except httpx.HTTPError as e:
                    results.append({"path": path, "error": str(e)})
            return {
                "viya_endpoint": VIYA_ENDPOINT,
                "assumed_base_path": _ESP_BASE,
                "candidates_tried": results,
                "message": (
                    "Look for a 2xx response with a JSON/Swagger-shaped body. "
                    "If none of these succeed, ESP is likely not exposed through "
                    "this Viya gateway/token — check with your Viya admin for the "
                    "correct host, or whether Event Stream Manager is deployed."
                ),
            }

    @mcp.tool()
    async def list_esp_servers(ctx: Context) -> dict[str, Any]:
        """List ESP server instances known to this Viya environment.

        Returns the raw response alongside a best-effort parsed item list,
        since the exact collection shape (name/id fields) is not verified
        against a live environment — inspect ``raw`` if ``items`` looks wrong.
        """
        async with viya_session("list_esp_servers", ctx) as client:
            data = await get_json(f"{_ESP_BASE}/servers", client)
            items = data.get("items", data if isinstance(data, list) else [])
            return {"items": items, "raw": data}

    @mcp.tool()
    async def list_esp_projects(ctx: Context, server_id: str | None = None) -> dict[str, Any]:
        """List ESP projects deployed on a server (or across all servers if omitted).

        Args:
            server_id: Optional ESP server name/id to scope the listing (see
                list_esp_servers). Omit to list projects across all servers.
        """
        params = {"server": server_id} if server_id else None
        async with viya_session("list_esp_projects", ctx) as client:
            resp = await client.get(
                f"{VIYA_ENDPOINT}{_ESP_BASE}/projects",
                params=params or {},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            items = data.get("items", data if isinstance(data, list) else [])
            return {"items": items, "raw": data}

    @mcp.tool()
    async def get_esp_project(project_name: str, ctx: Context, server_id: str | None = None) -> dict[str, Any]:
        """Get an ESP project's model and status.

        Args:
            project_name: Name of the deployed ESP project.
            server_id: Optional ESP server name/id the project runs on, if
                required to disambiguate in your environment.
        """
        params = {"server": server_id} if server_id else None
        async with viya_session("get_esp_project", ctx) as client:
            resp = await client.get(
                f"{VIYA_ENDPOINT}{_ESP_BASE}/projects/{project_name}",
                params=params or {},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    @mcp.tool()
    async def deploy_esp_project(
        project_name: str,
        project_xml: str,
        ctx: Context,
        server_id: str | None = None,
    ) -> dict[str, Any]:
        """Deploy (load and start) an ESP project from its XML model definition.

        Args:
            project_name: Name to deploy the project under.
            project_xml: The full ESP project XML (as produced by ESP Studio,
                or hand-authored per the ESP XML modeling-language reference).
            server_id: Optional target ESP server name/id (see list_esp_servers).
        """
        params = {"server": server_id} if server_id else None
        async with viya_session("deploy_esp_project", ctx) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}{_ESP_BASE}/projects/{project_name}",
                params=params or {},
                content=project_xml.encode(),
                headers={"Content-Type": "application/xml", "Accept": "application/json"},
            )
            resp.raise_for_status()
            return {
                "status": "deployed" if resp.status_code < 300 else "unknown",
                "status_code": resp.status_code,
                "project_name": project_name,
                "response_body": resp.text[:2000],
            }

    @mcp.tool()
    async def undeploy_esp_project(project_name: str, ctx: Context, server_id: str | None = None) -> dict[str, str]:
        """Stop and remove a deployed ESP project.

        Args:
            project_name: Name of the ESP project to remove.
            server_id: Optional ESP server name/id the project runs on.
        """
        params = {"server": server_id} if server_id else None
        async with viya_session("undeploy_esp_project", ctx) as client:
            resp = await client.delete(
                f"{VIYA_ENDPOINT}{_ESP_BASE}/projects/{project_name}",
                params=params or {},
            )
            resp.raise_for_status()
            return {"status": "undeployed", "project_name": project_name}

    @mcp.tool()
    async def get_esp_window_schema(
        project_name: str, cq_name: str, window_name: str, ctx: Context
    ) -> dict[str, Any]:
        """Get a window's schema (field names/types) within a running ESP project.

        Args:
            project_name: Name of the deployed ESP project.
            cq_name: Name of the continuous query containing the window.
            window_name: Name of the window.
        """
        async with viya_session("get_esp_window_schema", ctx) as client:
            return await get_json(
                f"{_ESP_BASE}/projects/{project_name}/queries/{cq_name}/windows/{window_name}/schema",
                client,
            )

    @mcp.tool()
    async def publish_esp_events(
        project_name: str,
        cq_name: str,
        window_name: str,
        events: list[dict[str, Any]],
        ctx: Context,
    ) -> dict[str, Any]:
        """Publish a batch of events into a source window of a running ESP project.

        Args:
            project_name: Name of the deployed ESP project.
            cq_name: Name of the continuous query containing the target window.
            window_name: Name of the source window to publish into.
            events: List of event field/value dicts, one per event, matching
                the window's schema (see get_esp_window_schema).
        """
        body = {"events": events}
        async with viya_session("publish_esp_events", ctx) as client:
            return await post_json(
                f"{_ESP_BASE}/projects/{project_name}/queries/{cq_name}/windows/{window_name}/events",
                client,
                body=body,
            )
