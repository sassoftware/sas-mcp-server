# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 6 — Model Management & Scoring tools."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context, FastMCP

from ..viya_client import get_json, get_paged_items, post_json, return_items
from ._common import make_session_helpers


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 6 (Model Management & Scoring) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def list_registered_models(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List models in the Model Repository.

        Args:
            limit: Maximum models to return (default 50).
        """
        async with viya_session("list_registered_models", ctx) as client:
            items, _ = await get_paged_items("/modelRepository/models", client, limit=limit)
            return return_items(items, ["id", "name", "description", "modelVersionName"])

    @mcp.tool()
    async def list_publishing_destinations(
        ctx: Context, limit: int = 50, start: int = 0, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List available publishing destinations.

        Args:
            ctx: FastMCP context.
            limit: Maximum destinations to return (default 50).
            start: Row offset (default 0).
            filter_name: Optional filter for destination names.
        """
        async with viya_session("list_publishing_destinations", ctx) as client:
            items, _ = await get_paged_items(
                "/modelPublish/destinations",
                client,
                limit=limit,
                start=start,
                filters=f"contains(name,'{filter_name}')" if filter_name else None,
            )
            return return_items(items, ["id", "name", "description", "destinationType"])

    @mcp.tool()
    async def list_mas_modules(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List published scoring models and decisions (MAS modules).

        Args:
            limit: Maximum modules to return (default 50).
        """
        async with viya_session("list_mas_modules", ctx) as client:
            items, _ = await get_paged_items("/microanalyticScore/modules", client, limit=limit)
            return return_items(items, ["id", "name", "description"])

    @mcp.tool()
    async def get_mas_module_step_signature(module_id: str, ctx: Context, step_id: str = "execute") -> dict[str, Any]:
        """Fetch a MAS module step's input/output variable signature.

        Call before ``score_data`` to know the exact variable names, types,
        and order to pass as inputs, and what outputs to expect.

        Args:
            module_id: The MAS module ID (see ``list_mas_modules``).
            step_id: The step within the module to inspect (default "execute").
        """
        async with viya_session("get_mas_module_step_signature", ctx) as client:
            return await get_json(f"/microanalyticScore/modules/{module_id}/steps/{step_id}", client)

    @mcp.tool()
    async def score_data(module_id: str, step_id: str, input_data: dict, ctx: Context) -> dict[str, Any]:
        """Score data against a published model or decision (MAS module).

        Args:
            module_id: MAS module ID.
            step_id: Step ID within the module (usually 'score' or 'execute').
            input_data: Dictionary of input variable name-value pairs.
        """
        body = {"inputs": [{"name": k, "value": v} for k, v in input_data.items()]}
        async with viya_session("score_data", ctx) as client:
            return await post_json(
                f"/microanalyticScore/modules/{module_id}/steps/{step_id}",
                client,
                body=body,
            )
