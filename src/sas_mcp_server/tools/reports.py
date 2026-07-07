# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 3 — Reports & Visualization tools."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context, FastMCP

from ..helpers import report_export_helpers
from ..viya_client import get_json, get_paged_items, return_items
from ._common import make_session_helpers


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 3 (Reports & Visualization) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def list_reports(ctx: Context, limit: int = 50, filter_name: str | None = None) -> list[dict[str, Any]]:
        """List Visual Analytics reports.

        Args:
            limit: Maximum reports to return (default 50).
            filter_name: Optional name filter (substring match).
        """
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with viya_session("list_reports", ctx) as client:
            items, _ = await get_paged_items("/reports/reports", client, limit=limit, filters=filters)
            return return_items(items, ["id", "name", "description", "createdBy"])

    @mcp.tool()
    async def get_report(report_id: str, ctx: Context) -> dict[str, Any]:
        """Get a Visual Analytics report's metadata and definition.

        Args:
            report_id: ID of the report.
        """
        async with viya_session("get_report", ctx) as client:
            return await get_json(f"/reports/reports/{report_id}", client)

    @mcp.tool()
    async def export_report(
        report_id: str,
        export_format: str,
        ctx: Context,
        report_objects: list[str] | None = None,
        image_size: str | None = None,
        options: dict[str, Any] | None = None,
    ):
        """Export a Visual Analytics report (or specific report objects) in any
        format the VA service exposes, via its synchronous export endpoints.

        Formats (``export_format``):
          * ``package`` — full report bundle as a ``.zip`` (source files, query
            results, and rendered content); whole report or selected objects.
          * ``pdf`` — rendered PDF; whole report or selected objects. Pass
            rendering overrides (e.g. ``orientation``, ``paperSize``, ``margin``,
            ``includeCoverPage``) via ``options``.
          * ``png`` / ``svg`` — image of the report or a single object;
            ``image_size`` is required, e.g. ``"1200px,800px"``.
          * ``csv`` / ``tsv`` / ``xlsx`` — the data behind a single report
            object; exactly one object label is required.
          * ``summary`` — the report's text summary.

        Args:
            report_id: ID of the report.
            export_format: One of package, pdf, png, svg, csv, tsv, xlsx, summary.
            report_objects: Report object labels to export. ``package``/``pdf``
                accept several; image and data formats accept exactly one;
                ``summary`` accepts none. Omit to export the whole report where
                the format allows it.
            image_size: Required for ``png``/``svg``; format ``"<w>px,<h>px"``.
            options: Optional ``pdf`` rendering overrides, passed through as query
                parameters (e.g. ``{"orientation": "landscape"}``).

        Returns text inline for text formats, image content for ``png``, and an
        embedded binary file (carrying the right MIME type) for ``package`` /
        ``pdf`` / ``xlsx``. Binary results larger than ``MAX_EXPORT_INLINE_BYTES``
        are refused with guidance rather than streamed through the model context.
        """
        req = report_export_helpers.ReportExportRequest(
            report_id=report_id,
            export_format=export_format,
            report_objects=report_objects,
            image_size=image_size,
            options=options,
        )
        error = report_export_helpers.validate_export_request(req)
        if error is not None:
            return error
        async with viya_session("export_report", ctx) as client:
            return await report_export_helpers.execute_export(req, client)
