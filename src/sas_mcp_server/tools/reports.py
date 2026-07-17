# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 3 — Reports & Visualization tools."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from ..helpers import report_authoring_helpers, report_export_helpers
from ..viya_client import contains_filter, get_json, get_paged_items, logger, return_items
from ._common import coerce_json_dict, coerce_json_list, coerce_str_or_json_list, make_session_helpers

# Tolerant aliases for params some MCP clients deliver as JSON-encoded strings
# (see _common.coerce_json_list). The published schema is unchanged.
OperationsParam = Annotated[list[dict[str, Any]], BeforeValidator(coerce_json_list)]
ReportObjectsParam = Annotated[list[str], BeforeValidator(coerce_str_or_json_list)]
OptionsParam = Annotated[dict[str, Any], BeforeValidator(coerce_json_dict)]


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
        filters = contains_filter(filter_name)
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
        report_objects: ReportObjectsParam | None = None,
        image_size: str | None = None,
        options: OptionsParam | None = None,
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

        Verifying freshly authored reports: a WHOLE-report ``png`` can render
        blank (API-created reports keep an empty default "Page 1" first) —
        export page-by-page instead by passing a page label in
        ``report_objects``. Text objects 404 on per-object image export; use
        their page. ``pdf`` with no ``report_objects`` is the reliable
        whole-report fallback. ``get_report_outline`` lists the page/object
        labels to pass here.
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

    @mcp.tool()
    async def describe_report_objects(
        ctx: Context,
        object_type: str | None = None,
        category: str | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        """Discover what a Visual Analytics report can contain — operations and objects.

        Call this to learn how to build a report before calling
        ``apply_report_operations``. It reads a bundled catalog (no network), so
        it is the cheap way to look up an object's data roles instead of guessing.

        Modes:
          * No arguments — an index: the eight report operations, every addable
            object (schema key, one-line purpose, category, addable/updatable),
            an intent→object map (e.g. "single KPI number" → ``keyValue``), the
            placement guide, layout recipes, and the API's hard limits.
          * ``category`` — the index filtered to one category (Tables, Controls,
            Containers, Content, Graphs, Geo Maps, Analytics, Statistics,
            Machine Learning).
          * ``object_type`` — one object's full contract: its data roles (each
            marked single vs list), the roles it commonly needs, its common
            options (``options.object.title`` etc.), and a ready-to-send
            ``addObject`` example. Colloquial names resolve (``"kpi"`` →
            ``keyValue``); for a VA UI object with no API support (Text Topics,
            Decision tree, GAM, GLM) it returns the nearest addable alternative;
            for an unknown key it returns ``did_you_mean``.
          * ``operation`` — one operation's full shape with a worked example and
            notes; ``operation="addData"`` documents ``dataItems`` (column
            renames, SAS formats, aggregations, geography classification — the
            polish layer most reports need).

        Pair it with ``get_castable_columns`` to see which columns are categories
        vs measures, then map those columns onto the roles reported here.

        Args:
            object_type: A schema key (e.g. ``"barChart"``, ``"scatterPlot"``)
                or colloquial alias for its contract and example.
            category: Restrict the catalog to one category.
            operation: An operation key (e.g. ``"addData"``, ``"applyDataView"``)
                for its full shape, example, and notes.
        """
        logger.info("--- TOOL USED: describe_report_objects ---")
        return report_authoring_helpers.describe(object_type=object_type, category=category, operation=operation)

    @mcp.tool()
    async def create_report(
        name: str,
        ctx: Context,
        folder: str | None = None,
        on_conflict: str = "rename",
        operations: OperationsParam | None = None,
    ) -> dict[str, Any]:
        """Create a Visual Analytics report and return its id for further edits.

        Creates an empty report shell, or — if you pass ``operations`` — builds
        the whole report in one atomic call (bind data, add pages, add objects).
        Building at creation avoids leaving an empty report behind if a later
        edit fails. Returns ``{"status": "created", "id": ..., "name": ...}``
        plus a ``created`` summary whose object names/labels are what follow-up
        placement and exports target; feed the id to ``apply_report_operations``
        to keep editing. Note: VA prepends an empty default "Page 1" before any
        pages your operations add, so verify page-by-page with ``export_report``
        (see the result's ``verify_hint``) rather than a whole-report export.

        Args:
            name: Report name (``resultReportName``). Must be unique in the folder
                unless ``on_conflict`` resolves it.
            folder: Optional target folder URI (``resultFolder``); omit for the
                caller's My Folder.
            on_conflict: Name-conflict policy — ``rename`` (default), ``abort``,
                or ``replace``.
            operations: Optional native operations array to apply at creation, in
                the same shape ``apply_report_operations`` takes. Call
                ``describe_report_objects`` for the operation and object formats.
        """
        ops = report_authoring_helpers.normalize_operations(operations) if operations else None
        req = report_authoring_helpers.CreateReportRequest(
            name=name, folder=folder, on_conflict=on_conflict, operations=ops
        )
        error = report_authoring_helpers.validate_create(req)
        if error is not None:
            return error
        warnings = report_authoring_helpers.warn_operations(ops) if ops else []
        async with viya_session("create_report", ctx) as client:
            result = await report_authoring_helpers.execute_create(req, client)
        if warnings and result.get("status") == "created":
            result["warnings"] = warnings
        return result

    @mcp.tool()
    async def apply_report_operations(
        report_id: str,
        operations: OperationsParam,
        ctx: Context,
        dry_run: bool = False,
        response_format: str = "concise",
        result_report_name: str | None = None,
        result_folder: str | None = None,
        result_name_conflict: str = "rename",
    ) -> dict[str, Any]:
        """Apply an ordered batch of operations to a report — the authoring workhorse.

        This is how you add pages, add objects (any of the ~60 VA visual, control,
        and content types), set parameters, and swap data sources. ``operations``
        is the native SAS Visual Analytics operations array; the whole batch is
        applied atomically (all succeed or nothing changes).

        Operation keys (one per array element): ``addData``, ``addPage``,
        ``addObject``, ``updateObject``, ``setParameterValue``, ``updateData``,
        ``changeData``, ``applyDataView``. Call ``describe_report_objects`` for
        each operation's shape (``operation="addData"`` covers formats,
        aggregations, and geography via ``dataItems``) and each object's data
        roles, and ``get_castable_columns`` to map columns onto those roles.

        Layout & titles (see ``describe_report_objects`` → ``placement`` /
        ``layout_recipes`` for details):
          * Page title — give ``addPage`` a ``title`` (e.g. ``{"addPage":
            {"pageName": "Overview", "title": "Sales Overview"}}``); it becomes a
            text band at the top of that page's body. Page/report headers accept
            ONLY control objects — never text or visuals.
          * Chart titles — pass ``{"options": {"object": {"title": "..."}}}``
            inside the object spec at add time (all types except
            ``standardContainer``, which takes no options at add time).
          * One-batch multi-page — create pages inline with placement
            ``{"report": {"context": "new_page", "pageName": "Trends",
            "pagePosition": 1}}`` (numeric position) and target that pageName
            from later operations in the same batch.
          * Grids/columns — ``relativeToObject`` with ``left/right/top/bottom``
            (geometric) or ``before/after`` (flow order) against an EXISTING
            object's name; same-batch forward references fail, so chain across
            calls using the names each result returns. Objects are auto-named
            and auto-sized; placement and dataRoles are write-once
            (``updateObject`` changes options only).
          * Read structure back anytime with ``get_report_outline``; verify
            visually with ``export_report`` page-by-page (see ``verify_hint``
            in the result).

        The tool validates every operation against the object catalog before any
        HTTP call (unknown/typo'd object type, non-addable object, bad data-role
        names or arity, disallowed object/placement keys) and reports ALL invalid
        operations at once. It also handles the ETag optimistic-concurrency
        handshake for you, retrying once transparently on a concurrent edit.

        Args:
            report_id: Target report id (from ``create_report`` or ``list_reports``).
            operations: Ordered native operations array. Example element:
                ``{"addObject": {"object": {"barChart": {"dataSource": "CARS",
                "dataRoles": {"category": "Origin", "measures": ["MSRP"]},
                "options": {"object": {"title": "MSRP by Origin"}}}},
                "placement": {"page": {"target": "Overview"}}}}``.
            dry_run: Validate and return the normalized payload (plus any
                soft warnings about missing common roles) without writing anything.
            response_format: ``concise`` (default) returns the created page/object/
                data-source names+labels; ``detailed`` also echoes the full VA
                response.
            result_report_name: Save-as — apply the operations to a NEW report
                with this name, leaving the source report untouched (atomic
                template instantiation; pairs with ``changeData``).
            result_folder: Save-as target folder URI; omit for My Folder.
            result_name_conflict: Save-as name-conflict policy — ``rename``
                (default), ``abort``, or ``replace``.
        """
        if result_name_conflict not in report_authoring_helpers.CONFLICT_VALUES:
            return {
                "status": "invalid_request",
                "message": f"result_name_conflict must be one of {sorted(report_authoring_helpers.CONFLICT_VALUES)}.",
            }
        for param_name, value in (("result_report_name", result_report_name), ("result_folder", result_folder)):
            # A blank save-as target would silently fall back to editing the
            # SOURCE report in place — refuse it instead.
            if value is not None and not str(value).strip():
                return {
                    "status": "invalid_request",
                    "message": f"{param_name}, if given, must be non-empty (blank would edit the source in place).",
                }
        ops = report_authoring_helpers.normalize_operations(operations)
        error = report_authoring_helpers.validate_operations(ops)
        if error is not None:
            return error
        warnings = report_authoring_helpers.warn_operations(ops)
        if dry_run:
            return {"status": "valid", "warnings": warnings, "normalized_operations": ops}
        async with viya_session("apply_report_operations", ctx) as client:
            result = await report_authoring_helpers.execute_operations(
                report_id,
                ops,
                client,
                response_format=response_format,
                result_report_name=result_report_name,
                result_folder=result_folder,
                result_name_conflict=result_name_conflict,
            )
        if warnings and result.get("status") == "applied":
            result["warnings"] = warnings
        return result

    @mcp.tool()
    async def get_report_outline(report_id: str, ctx: Context) -> dict[str, Any]:
        """Read a report's structure: pages → objects with the handles other tools need.

        Reduces the stored report definition to a compact outline —
        per page its internal name and label, per object its name (``ve*``),
        label, type, and any text content. Use it to edit an existing report,
        to recover object names after an apply, or to check what a batch
        actually produced:

          * object ``name`` → the target for ``relativeToObject``/``container``
            placement and ``updateObject``;
          * object ``label`` → what ``export_report`` ``report_objects`` takes;
          * page ``label`` → the ``page`` placement target.

        Returns ``{"status": "ok", "pages": [...], "hint": ...}`` (or
        ``not_found`` / ``outline_failed``).

        Args:
            report_id: The report id to outline.
        """
        async with viya_session("get_report_outline", ctx) as client:
            return await report_authoring_helpers.execute_outline(report_id, client)

    @mcp.tool()
    async def copy_report(
        report_id: str,
        ctx: Context,
        name: str | None = None,
        folder: str | None = None,
        on_conflict: str = "rename",
    ) -> dict[str, Any]:
        """Copy a Visual Analytics report to a new report, returning the copy's id.

        Useful for tailoring a report to a new audience or for the copy-and-replace
        pattern — copy, then ``apply_report_operations`` with a ``changeData`` op to
        point the copy at a different table. Returns
        ``{"status": "copied", "id": ..., "name": ..., "source_report_id": ...}``.

        Args:
            report_id: The source report id to copy.
            name: Optional name for the copy (``resultReportName``); omit to let
                Viya name it.
            folder: Optional target folder URI (``resultFolder``); omit for the
                caller's My Folder.
            on_conflict: Name-conflict policy — ``rename`` (default), ``abort``,
                or ``replace``.
        """
        error = report_authoring_helpers.validate_copy(name, on_conflict)
        if error is not None:
            return error
        async with viya_session("copy_report", ctx) as client:
            return await report_authoring_helpers.execute_copy(
                report_id, name, folder, on_conflict, client
            )

    @mcp.tool()
    async def delete_report(report_id: str, ctx: Context) -> dict[str, Any]:
        """Delete a Visual Analytics report and its content.

        There is no per-object undo in the report API, so deleting and rebuilding
        (or copying first) is how you discard an unwanted report. Returns
        ``{"status": "deleted", "report_id": ...}`` (or ``not_found`` /
        ``delete_failed``).

        Args:
            report_id: The report id to delete.
        """
        async with viya_session("delete_report", ctx) as client:
            return await report_authoring_helpers.execute_delete(report_id, client)
