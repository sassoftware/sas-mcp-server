# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generic SAS Viya REST helpers.

These functions wrap the common request shapes used by the MCP tools (GET a
JSON document, GET a paginated collection, POST JSON, DELETE a resource) and
build the authenticated :class:`httpx.AsyncClient`. They are the public,
cross-module API of this package — ``tools.py`` and ``viya_utils.py`` both
depend on them. The shared :data:`logger` lives here as the lowest-level module
(above only :mod:`sas_mcp_server.config`) so every other module can import it
without creating an import cycle.
"""

from typing import Any

import httpx
from fastmcp.utilities.logging import get_logger

from .config import SSL_VERIFY, VIYA_ENDPOINT

logger = get_logger(__name__)

# Viya REST calls can be slow (compute session spin-up, large log fetches);
# give them a generous client timeout.
_CLIENT_TIMEOUT = 300.0

JSONDict = dict[str, Any]


async def get_json(
    url: str,
    client: httpx.AsyncClient,
    params: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> JSONDict:
    """GET a JSON response from a Viya REST endpoint."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.get(full_url, headers={"Accept": accept}, params=params or {})
    resp.raise_for_status()
    return resp.json()


async def get_paged_items(
    url: str,
    client: httpx.AsyncClient,
    limit: int = 20,
    start: int = 0,
    filters: str | None = None,
    extra_params: dict[str, Any] | None = None,
) -> tuple[list[JSONDict], int]:
    """GET a paginated collection and return the items list plus total count."""
    params: dict[str, Any] = {"start": start, "limit": limit}
    if filters:
        params["filter"] = filters
    if extra_params:
        params.update(extra_params)
    data = await get_json(
        url, client, params=params, accept="application/vnd.sas.collection+json"
    )
    return data.get("items", []), data.get("count", 0)


async def post_json(
    url: str,
    client: httpx.AsyncClient,
    body: Any | None = None,
    params: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> JSONDict:
    """POST JSON to a Viya REST endpoint and return the response JSON."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.post(
        full_url,
        json=body,
        headers={"Content-Type": "application/json", "Accept": accept},
        params=params or {},
    )
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


async def delete_resource(url: str, client: httpx.AsyncClient) -> None:
    """DELETE a Viya REST resource."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.delete(full_url)
    resp.raise_for_status()


def make_client(token: str) -> httpx.AsyncClient:
    """Create an :class:`httpx.AsyncClient` with auth headers for Viya API calls."""
    if not token.startswith("Bearer "):
        token = f"Bearer {token}"
    headers = {"Authorization": token}
    return httpx.AsyncClient(
        headers=headers, verify=SSL_VERIFY, timeout=_CLIENT_TIMEOUT
    )


def return_items(
    items: list[JSONDict], prop_selection: list[str]
) -> list[dict[str, Any]]:
    """Return a list of items matching the selection criteria.

    Args:
        items: A list of JSON dictionaries representing the items.
        prop_selection: A list of property names to include in the result.
    """
    results = []
    for item in items:
        if not any(prop in item for prop in prop_selection):
            raise ValueError(
                "None of the specified properties are present in the item."
            )
        result = {prop: item.get(prop, "") for prop in prop_selection}
        results.append(result)
    return results
