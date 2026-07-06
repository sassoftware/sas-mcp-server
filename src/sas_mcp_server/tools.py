# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared tool registration for both HTTP and stdio MCP servers.
All tools are registered via ``register_tools(mcp, get_token)``.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from sas_mcp_server.helpers import report_export_helpers

from .config import CONTEXT_NAME, SSL_VERIFY, VIYA_ENDPOINT
from .env import env_bool
from .viya_client import (
    delete_resource,
    get_json,
    get_paged_items,
    logger,
    make_client,
    post_json,
    return_items,
)
from .viya_utils import get_cached_session, reset_cached_session, run_one_snippet


# --- upload_data / upload_inline_data: source-format registry -----------------
# One registry of DataFormat objects is the single place file formats are
# described: each maps a logical format to the casManagement ``uploadTable``
# ``format`` value plus the per-format flags. Adding a format (or enabling one,
# e.g. parquet once an endpoint accepts it) is a one-line change here — the
# lookup tables below are derived from it, so nothing is kept in sync by hand.
# Per the uploadTable API the accepted ``format`` values are csv, xls, xlsx,
# sas7bdat and sashdat; ``tsv`` is csv with a tab delimiter and ``xlsm`` uploads
# as ``xlsx``.
@dataclass(frozen=True)
class DataFormat:
    key: str  # logical name used in data_format / detection (e.g. "tsv")
    cas_format: str  # value sent in the multipart ``format`` field (e.g. "csv")
    extensions: tuple[str, ...] = ()  # file extensions that map here (with the dot)
    aliases: tuple[str, ...] = ()  # accepted ``data_format`` synonyms
    delimiter: str | None = None  # delimiter override (e.g. a tab for tsv)
    header_row: bool = False  # accepts the containsHeaderRow flag
    excel_format: bool = False  # accepts a sheetName
    binary: bool = False  # non-text; must come from file_path/url, not inline
    supported: bool = True  # accepted by the uploadTable endpoint
    unsupported_reason: str | None = None  # guidance shown when supported is False


_DATA_FORMATS: tuple[DataFormat, ...] = (
    DataFormat("csv", "csv", extensions=(".csv",), header_row=True),
    DataFormat(
        "tsv", "csv", extensions=(".tsv", ".tab"), aliases=("tab",),
        delimiter="\t", header_row=True,
    ),
    DataFormat("xls", "xls", extensions=(".xls",), header_row=True,
               excel_format=True, binary=True),
    DataFormat("xlsx", "xlsx", extensions=(".xlsx",), aliases=("excel",),
               header_row=True, excel_format=True, binary=True),
    DataFormat("xlsm", "xlsx", extensions=(".xlsm",), header_row=True,
               excel_format=True, binary=True),
    DataFormat("sas7bdat", "sas7bdat", extensions=(".sas7bdat",),
               aliases=("sas",), binary=True),
    DataFormat("sashdat", "sashdat", extensions=(".sashdat",), binary=True),
    # Recognized but not accepted by the upload endpoint: detected so we can fail
    # fast with guidance rather than a guaranteed HTTP 400. Flip ``supported`` to
    # True if/when a deployment's endpoint accepts parquet.
    DataFormat(
        "parquet", "parquet", extensions=(".parquet", ".parq"), binary=True,
        supported=False,
        unsupported_reason=(
            "The casManagement file-upload endpoint does not accept parquet "
            "(it supports csv, tsv, xls, xlsx, sas7bdat, sashdat). Load parquet via a "
            "path-based caslib and promote_table_to_memory, or convert it to "
            "csv/sas7bdat first."
        ),
    ),
)


def _index_formats_by_name() -> dict[str, DataFormat]:
    """Map every format key and alias to its DataFormat (built once at import)."""
    out: dict[str, DataFormat] = {}
    for fmt in _DATA_FORMATS:
        out[fmt.key] = fmt
        for alias in fmt.aliases:
            out[alias] = fmt
    return out


_FORMAT_BY_NAME = _index_formats_by_name()
_FORMAT_BY_EXT = {ext: fmt for fmt in _DATA_FORMATS for ext in fmt.extensions}
_SUPPORTED_FORMATS = tuple(fmt.key for fmt in _DATA_FORMATS if fmt.supported)


def _resolve_data_format(
    data_format: str | None, file_path: str | None, url: str | None
) -> tuple[DataFormat | None, dict[str, Any] | None]:
    """Resolve the source to a *supported* ``DataFormat``, or a structured error.

    An explicit ``data_format`` (key or alias) wins; otherwise the format is
    inferred from the ``file_path``/``url`` extension. Returns ``(format, None)``
    on success, or ``(None, error)`` for an unknown extension, an unrecognized
    ``data_format``, or a recognized-but-unsupported format (e.g. parquet).
    """
    if data_format:
        name = data_format.strip().lower().lstrip(".")
        fmt = _FORMAT_BY_NAME.get(name)
        if fmt is None:
            return None, {
                "status": "unsupported_format",
                "data_format": name,
                "message": (
                    f"Unsupported data_format '{name}'. Supported: "
                    f"{', '.join(_SUPPORTED_FORMATS)}."
                ),
            }
    else:
        fmt = None
        ref = file_path or url
        if ref:
            # Drop any URL query/fragment before reading the suffix.
            clean = ref.split("?", 1)[0].split("#", 1)[0]
            fmt = _FORMAT_BY_EXT.get(Path(clean).suffix.lower())
        if fmt is None:
            return None, {
                "status": "unknown_format",
                "message": (
                    "Could not infer the data format from the file/URL extension. "
                    "Pass data_format (csv, tsv, xls, xlsx, sas7bdat, sashdat)."
                ),
            }
    if not fmt.supported:
        return None, {
            "status": "format_not_supported",
            "data_format": fmt.key,
            "message": fmt.unsupported_reason
            or f"Format '{fmt.key}' is not accepted by the upload endpoint.",
        }
    return fmt, None


async def _resolve_source_bytes(
    file_path: str | None, url: str | None
) -> tuple[bytes | None, dict[str, Any] | None]:
    """Materialize the upload bytes server-side from ``file_path`` or ``url``.

    Assumes exactly one of the two is set (the caller's exactly-one-source
    guard). The bytes are read off the server's disk or fetched over HTTP, never
    routed through the model context. Returns ``(bytes, None)`` or ``(None, error)``.
    """
    if file_path:
        if not env_bool("ALLOW_LOCAL_FILE_UPLOAD", True):
            return None, {
                "status": "file_upload_disabled",
                "message": (
                    "Server-side file reads are disabled "
                    "(ALLOW_LOCAL_FILE_UPLOAD=false). Use url or the upload_inline_data tool."
                ),
            }
        path = Path(file_path).expanduser()
        if not path.is_file():
            return None, {
                "status": "file_not_found",
                "file_path": file_path,
                "message": (
                    f"No readable file at '{file_path}' on the server host. In "
                    "stdio mode that host is your local machine; pass an absolute path."
                ),
            }
        try:
            return path.read_bytes(), None
        except OSError as exc:
            return None, {
                "status": "file_unreadable",
                "file_path": file_path,
                "message": str(exc),
            }
    # url — fetch with a plain client (no Viya bearer on an external URL).
    assert url is not None
    try:
        async with httpx.AsyncClient(
            timeout=120.0, follow_redirects=True, verify=SSL_VERIFY
        ) as fetch_client:
            fetch_resp = await fetch_client.get(url)
            fetch_resp.raise_for_status()
            return fetch_resp.content, None
    except httpx.HTTPError as exc:
        return None, {"status": "fetch_failed", "url": url, "message": str(exc)}


async def _post_cas_upload(
    client: httpx.AsyncClient,
    server_id: str,
    caslib_name: str,
    table_name: str,
    fmt: DataFormat,
    file_bytes: bytes,
    source: str,
    *,
    sheet_name: str | None = None,
    contains_header_row: bool = True,
) -> dict[str, Any]:
    """POST resolved bytes to the casManagement uploadTable endpoint, shape the result.

    Shared by ``upload_data`` (file_path/url) and ``upload_inline_data`` (inline
    text). *fmt* is a resolved, supported :class:`DataFormat`; *source* is echoed
    back in the result so callers can tell how the bytes arrived.
    """
    fields: dict[str, str] = {"tableName": table_name, "format": fmt.cas_format}
    if fmt.delimiter is not None:
        fields["delimiter"] = fmt.delimiter
    if fmt.header_row:
        fields["containsHeaderRow"] = "true" if contains_header_row else "false"
    if sheet_name and fmt.excel_format:
        fields["sheetName"] = sheet_name
    content_type = "application/octet-stream" if fmt.binary else "text/csv"
    # The uploadTable API requires the ``file`` part to come *last*; httpx
    # serializes ``data`` fields before ``files``, so this satisfies that.
    resp = await client.post(
        f"{VIYA_ENDPOINT}/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
        data=fields,
        files={"file": (f"data.{fmt.key}", file_bytes, content_type)},
    )
    if resp.status_code == 409:
        return {
            "status": "table_already_exists",
            "table_name": table_name,
            "caslib": caslib_name,
            "message": (
                f"Table '{table_name}' already exists in caslib '{caslib_name}'. "
                "Drop or rename before re-uploading."
            ),
        }
    if resp.status_code >= 400:
        # Surface why CAS refused (bad caslib, malformed file, scope/perm) as a
        # structured error instead of raising an opaque one.
        return {
            "status": "upload_failed",
            "http_status": resp.status_code,
            "data_format": fmt.key,
            "message": (
                f"CAS rejected the {fmt.key} upload (HTTP {resp.status_code}). "
                f"Viya said: {resp.text[:400]}"
            ),
        }
    body = resp.json()
    return {
        "status": "success",
        "source": source,
        "data_format": fmt.key,
        "table_name": body.get("name"),
        "rows_uploaded": body.get("rowCount", 0),
        "column_count": body.get("columnCount", 0),
        "caslib": body.get("caslibName"),
        "scope": body.get("scope"),
    }


def _build_decision_flow_body(
    name: str, signature: list[dict[str, Any]], rule_set_steps: list[dict[str, Any]], description: str | None
) -> dict[str, Any]:
    """Build a SAS Decisions flow body from ruleSetSteps shorthand.

    Each entry in *rule_set_steps* is ``{"ruleSetId", "versionId", "mappings"}``
    (mappings: ``[{"stepTermName", "direction", "targetDecisionTermName"}, ...]``)
    and is expanded into the ``application/vnd.sas.decision.step.ruleset`` step
    shape the API expects — including the lowercase ``ruleset`` key, which does
    not match the OpenAPI schema's documented ``ruleSet``.
    """
    steps = []
    for i, step in enumerate(rule_set_steps):
        missing = [k for k in ("ruleSetId", "versionId", "mappings") if k not in step]
        if missing:
            raise ValueError(
                f"rule_set_steps[{i}] is missing required key(s): {', '.join(missing)}. "
                "Each step needs ruleSetId, versionId (a locked rule set revision), and mappings."
            )
        steps.append({
            "type": "application/vnd.sas.decision.step.ruleset",
            "ruleset": {"id": step["ruleSetId"], "versionId": step["versionId"]},
            "mappings": step["mappings"],
        })
    return {
        "name": name,
        "description": description or "",
        "signature": signature,
        "flow": {"steps": steps},
    }


# --- export_report: Visual Analytics export-format registry -------------------
def register_tools(
    mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]
) -> None:
    """Register all tools on *mcp*.

    Parameters
    ----------
    mcp : FastMCP
        The server instance to register tools on.
    get_token : callable
        ``async def get_token(ctx: Context) -> str`` — returns a Viya access
        token.  HTTP mode pulls it from context state; stdio mode reads a
        token cached by ``sas-viya auth loginCode`` or runs an RFC 8628
        device-code flow.
    """

    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        """Log tool usage, resolve a Viya token, and yield an authed client.

        Collapses the per-tool preamble (log line + token fetch + client
        construction) into one context manager shared by every tool.
        """
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    @asynccontextmanager
    async def compute_tool_session(
        name: str, ctx: Context, context_name: str
    ) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
        """Like :func:`viya_session` but also resolves the cached compute session.

        Yields ``(client, session_id)`` where *session_id* is the reusable
        per-user compute session for *context_name* — created on first use and
        reused (not torn down) on later calls. Use ``reset_compute_session`` to
        discard it.
        """
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            session_id = await get_cached_session(client, context_name, token)
            yield client, session_id

    # ------------------------------------------------------------------
    # Original tool
    # ------------------------------------------------------------------

    @mcp.tool()
    async def execute_sas_code(sas_code: str, ctx: Context) -> dict[str, str]:
        """
        Executes the provided SAS code in the Viya environment and returns information about the completed Job.
        This will create a job definition for the SAS code, execute it, and then retrieve the results.

        The code runs in a reusable compute session that is kept warm and shared
        across calls (per user), so SAS state — WORK tables, macro variables, and
        assigned librefs — persists between successive ``execute_sas_code`` calls.
        Call ``reset_compute_session`` to discard that state and start fresh.

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

    # ------------------------------------------------------------------
    # Tier 1 — Data Discovery (CAS Management)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_cas_servers(ctx: Context) -> list[dict[str, Any]]:
        """List available CAS servers on the Viya environment."""
        async with viya_session("list_cas_servers", ctx) as client:
            items, _ = await get_paged_items("/casManagement/servers", client)
            return return_items(items, ["name", "id", "description"])


    @mcp.tool()
    async def list_caslibs(
        server_id: str, ctx: Context, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List CAS libraries (caslibs) available on a CAS server.

        Args:
            server_id: CAS server name or ID (e.g. 'cas-shared-default').
            limit: Maximum number of caslibs to return (default 50).
        """
        async with viya_session("list_caslibs", ctx) as client:
            items, _ = await get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs", client, limit=limit
            )
            return return_items(items, ["name", "type", "description"])

    @mcp.tool()
    async def list_castables(
        server_id: str, caslib_name: str, ctx: Context, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List tables in a CAS library.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            limit: Maximum number of tables to return (default 50).
        """
        async with viya_session("list_castables", ctx) as client:
            items, _ = await get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
                client,
                limit=limit,
            )
            return return_items(items, ["name", "rowCount", "columnCount"])

    @mcp.tool()
    async def list_source_tables(
        server_id: str, caslib_name: str, ctx: Context, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List source tables that are NOT yet loaded into memory in a CAS library.

        These are the candidates for ``promote_table_to_memory`` — tables that
        exist on the caslib's data source but are not in CAS memory yet.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            limit: Maximum number of tables to return (default 50).
        """
        async with viya_session("list_source_tables", ctx) as client:
            items, _ = await get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
                client,
                limit=limit,
                extra_params={"state": "unloaded"},
            )
            return return_items(items, ["name", "sourceTableName", "scope", "state"])

    @mcp.tool()
    async def get_castable_info(
        server_id: str, caslib_name: str, table_name: str, ctx: Context
    ) -> dict[str, Any]:
        """Get metadata for a CAS table (row count, column count, size, etc.).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
        """
        async with viya_session("get_castable_info", ctx) as client:
            return await get_json(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}",
                client,
            )

    @mcp.tool()
    async def get_castable_columns(
        server_id: str,
        caslib_name: str,
        table_name: str,
        ctx: Context,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Get column metadata for a CAS table (names, types, labels, formats).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
            limit: Maximum columns to return (default 200).
        """
        async with viya_session("get_castable_columns", ctx) as client:
            items, _ = await get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}/columns",
                client,
                limit=limit,
            )
            return return_items(items, ["name", "type", "rawLength", "label", "format"])

    @mcp.tool()
    async def get_castable_data(
        server_id: str,
        caslib_name: str,
        table_name: str,
        ctx: Context,
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        """Fetch rows from a CAS table with column names.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
            limit: Maximum rows to return (default 100).
            start: Row offset (default 0).
        """
        data_source_id = f"cas~fs~{server_id}~fs~{caslib_name}"
        table_id = f"cas~fs~{server_id}~fs~{caslib_name}~fs~{table_name}"
        async with viya_session("get_castable_data", ctx) as client:
            columns: list[dict[str, Any]] = []
            col_start = 0
            col_limit = 100
            while True:
                col_resp = await client.get(
                    f"{VIYA_ENDPOINT}/dataTables/dataSources/{data_source_id}/tables/{table_name}/columns",
                    params={"start": col_start, "limit": col_limit},
                    follow_redirects=True,
                )
                col_resp.raise_for_status()
                col_data = col_resp.json()
                for item in col_data.get("items", []):
                    columns.append(
                        {
                            "name": item.get("name"),
                            "type": item.get("type"),
                            "index": item.get("index"),
                        }
                    )
                total = col_data.get("count", 0)
                col_start += col_limit
                if col_start >= total:
                    break

            row_resp = await client.get(
                f"{VIYA_ENDPOINT}/rowSets/tables/{table_id}/rows",
                params={"start": start, "limit": limit},
                follow_redirects=True,
            )
            row_resp.raise_for_status()
            row_data = row_resp.json()

            col_names = [c["name"] for c in columns]
            rows = []
            for item in row_data.get("items", []):
                cells = item.get("cells", [])
                rows.append(dict(zip(col_names, cells, strict=False)))

            return {
                "columns": col_names,
                "rows": rows,
                "count": row_data.get("count", len(rows)),
                "start": start,
                "limit": limit,
            }

    # ------------------------------------------------------------------
    # Tier 2 — Data Operations & Files
    # ------------------------------------------------------------------

    @mcp.tool()
    async def upload_data(
        server_id: str,
        caslib_name: str,
        table_name: str,
        ctx: Context,
        file_path: str | None = None,
        url: str | None = None,
        data_format: str | None = None,
        sheet_name: str | None = None,
        contains_header_row: bool = True,
    ) -> dict[str, Any]:
        """Upload a data file into a CAS table — read **by the server**, not the model.

        Provide the data by reference through **exactly one** of:

        * ``file_path`` — the server reads the file off its own disk (in stdio mode
          that's your machine). Disable with ``ALLOW_LOCAL_FILE_UPLOAD=false``.
        * ``url`` — the server fetches it over HTTP.

        Either way the bytes are read server-side and never pass through the calling
        model's context window. To create a *small* table you are building inline (no
        file or URL), use the ``upload_inline_data`` tool instead.

        The casManagement uploadTable endpoint only accepts an uploaded file (multipart
        form-data) and has no URL parameter, so ``url`` is fetched and sent on as the
        multipart file part.

        **Formats.** Per the uploadTable API: csv, xls, xlsx (single sheet), sas7bdat,
        sashdat; ``tsv`` is csv with a tab delimiter. parquet is **not** accepted and is
        rejected up front with guidance (load via a path-based caslib +
        promote_table_to_memory, or convert to csv/sas7bdat). The format is auto-detected
        from the ``file_path``/``url`` extension; pass ``data_format`` to override (needed
        for URLs with no clean suffix).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Target caslib name.
            table_name: Name for the new table.
            file_path: Path to a data file the server reads directly from disk.
            url: HTTP(S) URL the server fetches the file from.
            data_format: Override format detection. One of csv, tsv, xls, xlsx,
                sas7bdat, sashdat (aliases: excel→xlsx, tab→tsv, sas→sas7bdat).
            sheet_name: For Excel sources, the worksheet to import (first sheet by default).
            contains_header_row: Whether the first row holds column names — applies
                to csv/tsv/Excel (default True).
        """
        provided = [n for n, v in (("file_path", file_path), ("url", url)) if v]
        if len(provided) != 1:
            return {
                "status": "invalid_source",
                "provided": provided,
                "message": (
                    "Provide exactly one of file_path or url. To upload inline text "
                    "use the upload_inline_data tool."
                ),
            }
        source = provided[0]

        # Resolve the format first (fails cheaply before any disk/URL/Viya I/O),
        # then materialize the bytes server-side; each helper returns a structured
        # error or its result.
        fmt, fmt_error = _resolve_data_format(data_format, file_path, url)
        if fmt_error is not None:
            return fmt_error
        assert fmt is not None  # paired with fmt_error by _resolve_data_format

        file_bytes, source_error = await _resolve_source_bytes(file_path, url)
        if source_error is not None:
            return source_error
        assert file_bytes is not None  # paired with source_error

        async with viya_session("upload_data", ctx) as client:
            return await _post_cas_upload(
                client,
                server_id,
                caslib_name,
                table_name,
                fmt,
                file_bytes,
                source,
                sheet_name=sheet_name,
                contains_header_row=contains_header_row,
            )

    @mcp.tool()
    async def upload_inline_data(
        server_id: str,
        caslib_name: str,
        table_name: str,
        data: str,
        ctx: Context,
        data_format: str = "csv",
        contains_header_row: bool = True,
    ) -> dict[str, Any]:
        """Create a small CAS table from inline delimited text passed as a string.

        Use this only for **tiny, hand-built tables** — a lookup/mapping table the model
        constructs on the fly, or a quick test table — because the whole payload travels
        through the model's context as a tool argument. For anything larger, or any file
        you already have, use ``upload_data`` (file_path/url), which reads the bytes
        server-side instead.

        Text formats only: ``csv`` (default) or ``tsv`` (tab-separated). For binary
        formats (Excel, sas7bdat, sashdat) use ``upload_data``.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Target caslib name.
            table_name: Name for the new table.
            data: The delimited text, including the header row.
            data_format: 'csv' (default) or 'tsv' (alias 'tab').
            contains_header_row: Whether the first row holds column names (default True).
        """
        fmt = _FORMAT_BY_NAME.get(data_format.strip().lower())
        # Inline text can only carry the text formats (csv/tsv) — never a binary
        # or endpoint-unsupported format.
        if fmt is None or fmt.binary or not fmt.supported:
            return {
                "status": "text_only",
                "data_format": data_format,
                "message": (
                    "upload_inline_data accepts only csv or tsv text. For binary formats "
                    "(xls, xlsx, sas7bdat, sashdat) use upload_data with file_path or url."
                ),
            }
        async with viya_session("upload_inline_data", ctx) as client:
            return await _post_cas_upload(
                client,
                server_id,
                caslib_name,
                table_name,
                fmt,
                data.encode("utf-8"),
                "inline",
                contains_header_row=contains_header_row,
            )

    @mcp.tool()
    async def promote_table_to_memory(
        server_id: str, caslib_name: str, table_name: str, ctx: Context
    ) -> dict[str, Any]:
        """Load a source table into CAS memory at global scope (visible to all sessions).

        Loads the table from its caslib data source and promotes it to global
        scope via the casManagement ``updateTableState`` API. Idempotent: if the
        table is already loaded in global scope it is left untouched. Use
        ``list_source_tables`` to discover unloaded tables that can be promoted.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Caslib containing the table.
            table_name: Table to load and promote.
        """
        table_path = f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}"
        async with viya_session("promote_table_to_memory", ctx) as client:
            # Idempotency: skip if the table is already loaded in global scope.
            try:
                info = await get_json(table_path, client)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return {
                        "status": "not_found",
                        "table": f"{caslib_name}.{table_name}",
                        "message": (
                            f"No table '{table_name}' in caslib '{caslib_name}'. "
                            "Use list_source_tables to find loadable source tables."
                        ),
                    }
                raise
            if info.get("state") == "loaded" and info.get("scope") == "global":
                return {
                    "status": "already_global",
                    "table": f"{caslib_name}.{table_name}",
                    "state": "loaded",
                    "scope": "global",
                }

            # Load from source and promote to global scope. The updateTableState
            # endpoint responds with text/plain (the new state), not JSON.
            resp = await client.put(
                f"{VIYA_ENDPOINT}{table_path}/state",
                params={"value": "loaded", "scope": "global"},
                headers={"Accept": "*/*"},
            )
            resp.raise_for_status()
            return {
                "status": "promoted",
                "table": f"{caslib_name}.{table_name}",
                "state": resp.text.strip() or "loaded",
                "scope": "global",
            }

    @mcp.tool()
    async def list_files(
        ctx: Context, limit: int = 50, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List files in the Viya Files Service.

        Args:
            limit: Maximum files to return (default 50).
            filter_name: Optional name filter (substring match).
        """
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with viya_session("list_files", ctx) as client:
            items, _ = await get_paged_items(
                "/files/files", client, limit=limit, filters=filters
            )
            return return_items(items, ["id", "name", "contentType", "size"])

    @mcp.tool()
    async def upload_file(
        file_name: str, content: str, ctx: Context, content_type: str = "text/plain"
    ) -> dict[str, Any]:
        """Upload a file to the Viya Files Service.

        Args:
            file_name: Name for the file.
            content: File content as a string.
            content_type: MIME type (default 'text/plain').
        """
        async with viya_session("upload_file", ctx) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}/files/files",
                content=content.encode("utf-8"),
                headers={
                    "Content-Type": content_type,
                    "Content-Disposition": f'attachment; filename="{file_name}"',
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def download_file(file_id: str, ctx: Context) -> str:
        """Download file content from the Viya Files Service.

        Args:
            file_id: ID of the file to download.
        """
        async with viya_session("download_file", ctx) as client:
            resp = await client.get(f"{VIYA_ENDPOINT}/files/files/{file_id}/content")
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Tier 3 — Reports & Visualization
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_reports(
        ctx: Context, limit: int = 50, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List Visual Analytics reports.

        Args:
            limit: Maximum reports to return (default 50).
            filter_name: Optional name filter (substring match).
        """
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with viya_session("list_reports", ctx) as client:
            items, _ = await get_paged_items(
                "/reports/reports", client, limit=limit, filters=filters
            )
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

    # ------------------------------------------------------------------
    # Tier 4 — Batch Jobs & Async Execution
    # ------------------------------------------------------------------

    @mcp.tool()
    async def submit_batch_job(
        sas_code: str, ctx: Context, job_name: str | None = None
    ) -> dict[str, Any]:
        """Submit a SAS job for asynchronous execution via the Job Execution service.

        Args:
            sas_code: SAS code to execute.
            job_name: Optional descriptive name for the job.
        """
        body = {
            "name": job_name or "mcp-batch-job",
            "jobDefinition": {
                "type": "Compute",
                "code": sas_code,
            },
            "arguments": {
                "_contextName": CONTEXT_NAME,
            },
        }
        async with viya_session("submit_batch_job", ctx) as client:
            return await post_json("/jobExecution/jobs", client, body=body)

    @mcp.tool()
    async def get_job_status(job_id: str, ctx: Context) -> dict[str, Any]:
        """Check the status of a submitted job.

        Args:
            job_id: ID of the job.
        """
        async with viya_session("get_job_status", ctx) as client:
            return await get_json(f"/jobExecution/jobs/{job_id}", client)

    @mcp.tool()
    async def list_jobs(ctx: Context, limit: int = 20) -> list[dict[str, Any]]:
        """List recent jobs from the Job Execution service.

        Args:
            limit: Maximum jobs to return (default 20).
        """
        async with viya_session("list_jobs", ctx) as client:
            items, _ = await get_paged_items("/jobExecution/jobs", client, limit=limit)
            return return_items(items, ["id", "name", "state", "creationTimeStamp"])

    @mcp.tool()
    async def cancel_job(job_id: str, ctx: Context) -> str:
        """Cancel a running job.

        Args:
            job_id: ID of the job to cancel.
        """
        async with viya_session("cancel_job", ctx) as client:
            await delete_resource(f"/jobExecution/jobs/{job_id}", client)
            return f"Job {job_id} cancelled."

    @mcp.tool()
    async def get_job_log(job_id: str, ctx: Context) -> str:
        """Retrieve the log of a completed job.

        Args:
            job_id: ID of the job.
        """
        async with viya_session("get_job_log", ctx) as client:
            data = await get_json(f"/jobExecution/jobs/{job_id}", client)
            results = data.get("results", {})

            log_uri = None
            for key, value in results.items():
                if key.endswith(".log.txt"):
                    log_uri = value
                    break
            if not log_uri:
                for key, value in results.items():
                    if key.endswith(".log"):
                        log_uri = value
                        break

            if not log_uri:
                state = data.get("state", "unknown")
                error = data.get("error", {})
                if error:
                    return f"Job {state}: {error.get('message', 'No error details')}"
                return f"No log available. Job state: {state}"

            resp = await client.get(f"{VIYA_ENDPOINT}{log_uri}/content")
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Tier 5 — Model Management & Scoring
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_ml_projects(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List AutoML pipeline automation projects.

        Args:
            limit: Maximum projects to return (default 50).
        """
        async with viya_session("list_ml_projects", ctx) as client:
            items, _ = await get_paged_items(
                "/mlPipelineAutomation/projects", client, limit=limit
            )
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
            "classSelectionStatistic": (
                "ks" if prediction_type in ("binary", "nominal") else "ase"
            ),
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

    @mcp.tool()
    async def list_registered_models(
        ctx: Context, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List models in the Model Repository.

        Args:
            limit: Maximum models to return (default 50).
        """
        async with viya_session("list_registered_models", ctx) as client:
            items, _ = await get_paged_items(
                "/modelRepository/models", client, limit=limit
            )
            return return_items(items, ["id", "name", "description", "modelVersionName"])

    @mcp.tool()
    async def list_models_and_decisions(
        ctx: Context, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List published scoring models and decisions (MAS modules).

        Args:
            limit: Maximum modules to return (default 50).
        """
        async with viya_session("list_models_and_decisions", ctx) as client:
            items, _ = await get_paged_items(
                "/microanalyticScore/modules", client, limit=limit
            )
            return return_items(items, ["id", "name", "description"])

    @mcp.tool()
    async def score_data(
        module_id: str, step_id: str, input_data: dict, ctx: Context
    ) -> dict[str, Any]:
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

    @mcp.tool()
    async def create_business_ruleset(
        name: str, signature: list[dict[str, Any]], ctx: Context, description: str | None = None
    ) -> dict[str, Any]:
        """Create a new SAS Business Rules rule set.

        A rule set with no rules cannot be used in a decision flow — follow up
        with ``create_business_rule`` to populate it.

        Args:
            name: Rule set name (max 30 chars).
            signature: Input/output/inOut variables the rules operate on, each
                ``{"name", "dataType", "direction"}`` — dataType one of string,
                decimal, integer, date, datetime, dataGrid, boolean, any;
                direction one of input, output, inOut.
            description: Optional description.
        """
        body = {"name": name, "description": description, "signature": signature}
        async with viya_session("create_business_ruleset", ctx) as client:
            return await post_json("/businessRules/ruleSets", client, body=body)

    @mcp.tool()
    async def update_business_ruleset(
        ruleset_id: str, name: str, signature: list[dict[str, Any]], ctx: Context, description: str | None = None
    ) -> dict[str, Any]:
        """Update an existing SAS Business Rules rule set's name/description/signature.

        Changing the signature can invalidate existing rules that reference
        removed variables — check with ``get_business_ruleset`` first if unsure.

        Args:
            ruleset_id: The existing rule set UUID.
            name: Rule set name (max 30 chars).
            signature: Input/output/inOut variables the rules operate on.
            description: Optional description.
        """
        body = {"name": name, "description": description, "signature": signature}
        async with viya_session("update_business_ruleset", ctx) as client:
            get_resp = await client.get(f"{VIYA_ENDPOINT}/businessRules/ruleSets/{ruleset_id}")
            get_resp.raise_for_status()
            etag = get_resp.headers.get("etag", "")
            resp = await client.put(
                f"{VIYA_ENDPOINT}/businessRules/ruleSets/{ruleset_id}",
                content=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "If-Match": etag},
            )
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def get_business_ruleset(ruleset_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch a single SAS Business Rules rule set by ID.

        Args:
            ruleset_id: The rule set UUID.
        """
        async with viya_session("get_business_ruleset", ctx) as client:
            return await get_json(f"/businessRules/ruleSets/{ruleset_id}", client)

    @mcp.tool()
    async def list_business_rulesets(
        ctx: Context, limit: int = 20, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List SAS Business Rules rule sets, optionally filtered by name substring.

        Args:
            limit: Maximum number of results to return (default 20).
            filter_name: Optional substring to match against rule set names.
        """
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with viya_session("list_business_rulesets", ctx) as client:
            items, _ = await get_paged_items("/businessRules/ruleSets", client, limit=limit, filters=filters)
            return return_items(items, ["id", "name", "status"])

    @mcp.tool()
    async def delete_business_ruleset(ruleset_id: str, ctx: Context) -> str:
        """Permanently delete a SAS Business Rules rule set.

        Only call this once the rule set is confirmed unused by any decision
        flow — deleting a rule set still referenced by a decision fails.

        Args:
            ruleset_id: The rule set UUID to delete.
        """
        async with viya_session("delete_business_ruleset", ctx) as client:
            await delete_resource(f"/businessRules/ruleSets/{ruleset_id}", client)
            return f"Rule set {ruleset_id} deleted."

    @mcp.tool()
    async def lock_business_ruleset_revision(
        ruleset_id: str, ctx: Context, revision_type: str = "minor"
    ) -> dict[str, Any]:
        """Lock the current state of a rule set as an immutable revision.

        Decision steps reference a specific rule set revision (versionId), not
        the live working copy, so a revision must exist before wiring a rule
        set into a decision flow — call again after editing rules if a
        decision needs to pick up the changes.

        The revision-creation request replaces the rule set's full content
        from the body sent, so this fetches the rule set with its rules
        included (``application/vnd.sas.business.rule.set.integral+json``)
        and resends them — omitting them would wipe the live rule set's rules,
        not just the new revision.

        Args:
            ruleset_id: The rule set UUID.
            revision_type: "minor" for iterative changes, "major" for a
                significant/approved milestone (default "minor").
        """
        integral_type = "application/vnd.sas.business.rule.set.integral+json"
        async with viya_session("lock_business_ruleset_revision", ctx) as client:
            get_resp = await client.get(
                f"{VIYA_ENDPOINT}/businessRules/ruleSets/{ruleset_id}",
                headers={"Accept": integral_type},
            )
            get_resp.raise_for_status()
            current = get_resp.json()
            body = {
                "name": current["name"],
                "description": current.get("description"),
                "signature": current.get("signature"),
                "rules": current.get("rules"),
            }
            resp = await client.post(
                f"{VIYA_ENDPOINT}/businessRules/ruleSets/{ruleset_id}/revisions",
                params={"revisionType": revision_type},
                content=json.dumps(body).encode(),
                headers={"Content-Type": integral_type},
            )
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def list_business_ruleset_revisions(
        ruleset_id: str, ctx: Context, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List all locked revisions of a rule set.

        Args:
            ruleset_id: The rule set UUID.
            limit: Maximum number of results to return (default 20).
        """
        async with viya_session("list_business_ruleset_revisions", ctx) as client:
            items, _ = await get_paged_items(f"/businessRules/ruleSets/{ruleset_id}/revisions", client, limit=limit)
            return return_items(items, ["id", "majorRevision", "minorRevision"])

    @mcp.tool()
    async def create_business_rule(
        ruleset_id: str,
        name: str,
        conditional: str,
        rule_fired_tracking_enabled: bool,
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        ctx: Context,
    ) -> dict[str, Any]:
        """Create a new rule inside an existing SAS Business Rules rule set.

        A rule set can hold multiple rules, each evaluated per its conditional
        type. Condition/action expressions must include the variable name
        directly (e.g. ``"credit_score < 650"``, not just ``"< 650"``) — the
        API accepts the latter as valid but generates DS2 code with a missing
        left-hand operand. Boolean signature variables must be compared with
        ``= 0``/``= 1`` in expressions, not ``= false``/``= true``.

        Args:
            ruleset_id: The rule set UUID to add the rule to.
            name: Rule name (max 30 chars).
            conditional: "if" starts a new independent rule chain, "elseif"
                continues the previous rule's chain, "or" ORs into it.
            rule_fired_tracking_enabled: Whether to record when this rule fires.
            conditions: List of conditions (multiple conditions AND together),
                each ``{"type": "complex", "expression", "term": {"name",
                "dataType", "direction"}}``.
            actions: List of actions, each ``{"type": "assignment"|"return",
                "term": {"name", "dataType", "direction"}, "expression"}``.
        """
        body = {
            "name": name,
            "conditional": conditional,
            "ruleFiredTrackingEnabled": rule_fired_tracking_enabled,
            "conditions": conditions,
            "actions": actions,
        }
        async with viya_session("create_business_rule", ctx) as client:
            return await post_json(
                f"/businessRules/ruleSets/{ruleset_id}/rules",
                client,
                body=body,
                params={"createVariables": 1},
            )

    @mcp.tool()
    async def update_business_rule(
        ruleset_id: str,
        rule_id: str,
        name: str,
        conditional: str,
        rule_fired_tracking_enabled: bool,
        conditions: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        ctx: Context,
    ) -> dict[str, Any]:
        """Update an existing rule inside a SAS Business Rules rule set.

        Args:
            ruleset_id: The parent rule set UUID.
            rule_id: The specific rule UUID to update.
            name: Rule name (max 30 chars).
            conditional: "if" starts a new independent rule chain, "elseif"
                continues the previous rule's chain, "or" ORs into it.
            rule_fired_tracking_enabled: Whether to record when this rule fires.
            conditions: List of conditions (multiple conditions AND together).
            actions: List of assignment/return actions to perform when matched.
        """
        body = {
            "name": name,
            "conditional": conditional,
            "ruleFiredTrackingEnabled": rule_fired_tracking_enabled,
            "conditions": conditions,
            "actions": actions,
        }
        async with viya_session("update_business_rule", ctx) as client:
            get_resp = await client.get(f"{VIYA_ENDPOINT}/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}")
            get_resp.raise_for_status()
            etag = get_resp.headers.get("etag", "")
            resp = await client.put(
                f"{VIYA_ENDPOINT}/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}",
                content=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "If-Match": etag},
            )
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def get_business_rule(ruleset_id: str, rule_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch a single rule's definition from a SAS Business Rules rule set.

        Args:
            ruleset_id: The parent rule set UUID.
            rule_id: The rule UUID.
        """
        async with viya_session("get_business_rule", ctx) as client:
            return await get_json(f"/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}", client)

    @mcp.tool()
    async def list_business_rules(
        ruleset_id: str, ctx: Context, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List all rules inside a SAS Business Rules rule set.

        Args:
            ruleset_id: The rule set UUID.
            limit: Maximum number of results to return (default 100).
        """
        async with viya_session("list_business_rules", ctx) as client:
            items, _ = await get_paged_items(f"/businessRules/ruleSets/{ruleset_id}/rules", client, limit=limit)
            return return_items(items, ["id", "name", "status"])

    @mcp.tool()
    async def delete_business_rule(ruleset_id: str, rule_id: str, ctx: Context) -> str:
        """Permanently delete a rule from a SAS Business Rules rule set.

        Args:
            ruleset_id: The parent rule set UUID.
            rule_id: The rule UUID to delete.
        """
        async with viya_session("delete_business_rule", ctx) as client:
            await delete_resource(f"/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}", client)
            return f"Rule {rule_id} deleted from rule set {ruleset_id}."

    @mcp.tool()
    async def create_decision_flow(
        name: str,
        signature: list[dict[str, Any]],
        rule_set_steps: list[dict[str, Any]],
        ctx: Context,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a new SAS Intelligent Decisioning flow chaining rule set steps.

        Args:
            name: Decision name (max 60 chars).
            signature: Flow-level input/output variables, each ``{"name",
                "direction", "dataType"}`` — direction input or output;
                dataType string, decimal, integer, date, datetime, boolean.
            rule_set_steps: Ordered list of rule set steps to execute in
                sequence, each ``{"ruleSetId", "versionId", "mappings"}`` —
                versionId is a locked rule set revision (see
                ``lock_business_ruleset_revision``); mappings is a list of
                ``{"stepTermName", "direction", "targetDecisionTermName"}``
                connecting the rule set's terms to this decision's signature.
                A term produced as output by an earlier step can be consumed
                as input by a later step via a shared signature entry.
            description: Optional description.
        """
        body = _build_decision_flow_body(name, signature, rule_set_steps, description)
        async with viya_session("create_decision_flow", ctx) as client:
            return await post_json("/decisions/flows", client, body=body)

    @mcp.tool()
    async def update_decision_flow(
        decision_id: str,
        name: str,
        signature: list[dict[str, Any]],
        rule_set_steps: list[dict[str, Any]],
        ctx: Context,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing SAS Intelligent Decisioning flow.

        Pass ALL rule set steps (existing + new) — the full flow is replaced
        on update, it is not a partial patch.

        Args:
            decision_id: The existing decision flow UUID.
            name: Decision name (max 60 chars).
            signature: Flow-level input/output variables.
            rule_set_steps: Ordered list of rule set steps (see
                ``create_decision_flow`` for the shape).
            description: Optional description.
        """
        body = _build_decision_flow_body(name, signature, rule_set_steps, description)
        async with viya_session("update_decision_flow", ctx) as client:
            get_resp = await client.get(f"{VIYA_ENDPOINT}/decisions/flows/{decision_id}")
            get_resp.raise_for_status()
            etag = get_resp.headers.get("etag", "")
            resp = await client.put(
                f"{VIYA_ENDPOINT}/decisions/flows/{decision_id}",
                content=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "If-Match": etag},
            )
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def get_decision_flow(decision_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch the current state of a SAS Intelligent Decisioning flow.

        Args:
            decision_id: The decision flow UUID.
        """
        async with viya_session("get_decision_flow", ctx) as client:
            return await get_json(f"/decisions/flows/{decision_id}", client)

    @mcp.tool()
    async def list_decision_flows(
        ctx: Context, limit: int = 20, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List SAS Intelligent Decisioning flows, optionally filtered by name substring.

        Args:
            limit: Maximum number of results to return (default 20).
            filter_name: Optional substring to match against decision names.
        """
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with viya_session("list_decision_flows", ctx) as client:
            items, _ = await get_paged_items("/decisions/flows", client, limit=limit, filters=filters)
            return return_items(items, ["id", "name", "majorRevision"])

    @mcp.tool()
    async def delete_decision_flow(decision_id: str, ctx: Context) -> str:
        """Permanently delete a SAS Intelligent Decisioning flow.

        Args:
            decision_id: The decision flow UUID to delete.
        """
        async with viya_session("delete_decision_flow", ctx) as client:
            await delete_resource(f"/decisions/flows/{decision_id}", client)
            return f"Decision {decision_id} deleted."

    @mcp.tool()
    async def get_decision_flow_code(decision_id: str, ctx: Context) -> str:
        """Retrieve the generated DS2 execution code for a decision flow.

        Args:
            decision_id: The decision flow UUID.
        """
        async with viya_session("get_decision_flow_code", ctx) as client:
            resp = await client.get(
                f"{VIYA_ENDPOINT}/decisions/flows/{decision_id}/code",
                headers={"Accept": "text/vnd.sas.source.ds2"},
            )
            resp.raise_for_status()
            return resp.text

    @mcp.tool()
    async def lock_decision_flow_revision(decision_id: str, ctx: Context) -> dict[str, Any]:
        """Lock the current state of a decision flow as an immutable revision.

        Call after a successful create/update to freeze the approved state as
        a point-in-time snapshot referenceable by ``publish_decision_flow``.

        Args:
            decision_id: The decision flow UUID.
        """
        async with viya_session("lock_decision_flow_revision", ctx) as client:
            current = await get_json(f"/decisions/flows/{decision_id}", client)
            body = {
                "name": current["name"],
                "description": current.get("description"),
                "signature": current.get("signature"),
                "flow": current.get("flow"),
            }
            return await post_json(f"/decisions/flows/{decision_id}/revisions", client, body=body)

    @mcp.tool()
    async def list_decision_flow_revisions(
        decision_id: str, ctx: Context, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List all locked revisions of a decision flow.

        Args:
            decision_id: The decision flow UUID.
            limit: Maximum number of results to return (default 20).
        """
        async with viya_session("list_decision_flow_revisions", ctx) as client:
            items, _ = await get_paged_items(f"/decisions/flows/{decision_id}/revisions", client, limit=limit)
            return return_items(items, ["id", "creationTimeStamp"])

    @mcp.tool()
    async def get_decision_flow_revision(decision_id: str, revision_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch the content of a specific locked decision revision.

        Args:
            decision_id: The decision flow UUID.
            revision_id: The revision UUID.
        """
        async with viya_session("get_decision_flow_revision", ctx) as client:
            return await get_json(f"/decisions/flows/{decision_id}/revisions/{revision_id}", client)

    @mcp.tool()
    async def publish_decision_flow(
        decision_id: str,
        revision_id: str,
        publish_name: str,
        ctx: Context,
        destination_name: str = "maslocal",
        poll_timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Publish a locked decision revision to a Micro Analytic Score (MAS) destination.

        Required before ``score_data`` can execute the decision — MAS runs
        published modules, not decision flows directly. Requires the DS2 code
        generation service to be healthy for this decision's rule sets; an
        error mentioning rule set code generation is an environment-level
        issue, not a bad payload.

        Publishing is asynchronous and the resulting MAS module ID is
        server-generated — it is NOT ``publish_name``. This polls the
        publish job (``properties.masModules[0].jobUri``) until it reaches a
        terminal state and returns the real ``moduleId`` alongside the
        publish record, so the result is directly usable with
        ``get_mas_module_step_signature``/``score_data`` without a separate
        lookup via ``list_models_and_decisions``.

        Args:
            decision_id: The decision flow UUID.
            revision_id: The locked revision UUID (see
                ``lock_decision_flow_revision``).
            publish_name: The published name shown in Model Publish (not the
                MAS module ID — see above).
            destination_name: The configured MAS publishing destination
                (default "maslocal").
            poll_timeout: Max seconds to wait for the publish job to reach a
                terminal state before giving up (default 60.0).
        """
        async with viya_session("publish_decision_flow", ctx) as client:
            code_resp = await client.get(
                f"{VIYA_ENDPOINT}/decisions/flows/{decision_id}/revisions/{revision_id}/code",
                headers={"Accept": "text/vnd.sas.source.ds2"},
            )
            code_resp.raise_for_status()
            body = {
                "name": publish_name,
                "destinationName": destination_name,
                "modelContents": [
                    {
                        "modelName": publish_name,
                        "code": code_resp.text,
                        "codeType": "ds2",
                        "sourceUri": f"/decisions/flows/{decision_id}/revisions/{revision_id}",
                    }
                ],
            }
            resp = await client.post(
                f"{VIYA_ENDPOINT}/modelPublish/models",
                content=json.dumps(body).encode(),
                headers={"Content-Type": "application/vnd.sas.models.publishing.request+json"},
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            published = items[0] if items else data
            model_id = published.get("id")

            poll_interval = 1.0
            elapsed = 0.0

            # Phase 1: resolve the async publish job's URI. It is often absent
            # from the initial POST response (properties is {} for a moment),
            # so re-fetch the model until masModules[0].jobUri appears. Check
            # what's already in hand before sleeping so poll_timeout=0 still
            # returns any immediately-available result.
            job_uri = ""
            while True:
                mas_modules = (published.get("properties") or {}).get("masModules") or []
                job_uri = mas_modules[0].get("jobUri", "") if mas_modules else ""
                if job_uri or elapsed >= poll_timeout:
                    break
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                model_resp = await client.get(f"{VIYA_ENDPOINT}/modelPublish/models/{model_id}")
                model_resp.raise_for_status()
                published = model_resp.json()

            if not job_uri:
                published["moduleState"] = "pending"
                return published

            # Phase 2: poll the job to a terminal state. Query once before
            # checking the deadline so this phase always runs at least once,
            # even if phase 1 consumed the whole budget.
            terminal_states = ("completed", "failed", "error", "canceled", "cancelled")
            while True:
                job_resp = await client.get(f"{VIYA_ENDPOINT}{job_uri}")
                job_resp.raise_for_status()
                job = job_resp.json()
                state = job.get("state")
                if state in terminal_states:
                    published["moduleId"] = job.get("moduleId")
                    published["moduleState"] = state
                    if job.get("errors"):
                        published["moduleErrors"] = job["errors"]
                    return published
                if elapsed >= poll_timeout:
                    break
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            published["moduleState"] = "pending"
            return published

    @mcp.tool()
    async def get_mas_module_step_signature(module_id: str, ctx: Context, step_id: str = "execute") -> dict[str, Any]:
        """Fetch a MAS module step's input/output variable signature.

        Call before ``score_data`` to know the exact variable names, types,
        and order to pass as inputs, and what outputs to expect.

        Args:
            module_id: The MAS module ID (see ``list_models_and_decisions``).
            step_id: The step within the module to inspect (default "execute").
        """
        async with viya_session("get_mas_module_step_signature", ctx) as client:
            return await get_json(f"/microanalyticScore/modules/{module_id}/steps/{step_id}", client)

    # ------------------------------------------------------------------
    # Tier 6 — Compute Contexts & Code Execution
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_compute_contexts(
        ctx: Context, limit: int = 50, start: int = 0, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List available compute contexts on the Viya environment."""
        async with viya_session("list_compute_contexts", ctx) as client:
            filters = f"contains(name,'{filter_name}')" if filter_name else None
            items, _ = await get_paged_items(
                "/compute/contexts", client, limit=limit, start=start, filters=filters
            )
            return return_items(items, ["name", "description"])

    @mcp.tool()
    async def list_compute_libraries(
        compute_context_name: str,
        ctx: Context,
        limit: int = 50,
        start: int = 0,
        filter_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the SAS libraries (librefs) assigned in a compute context.

        Runs in the reusable per-user compute session for the context, so it
        also sees libraries created by prior ``execute_sas_code`` calls.

        Args:
            compute_context_name: Name of the compute context (see list_compute_contexts).
            limit: Maximum number of libraries to return (default 50).
            start: Offset of the first library to return (default 0).
            filter_name: Optional name filter (substring match).
        """
        async with compute_tool_session(
            "list_compute_libraries", ctx, compute_context_name
        ) as (client, session_id):
            filters = f"contains(name,'{filter_name}')" if filter_name else None
            items, _ = await get_paged_items(
                f"/compute/sessions/{session_id}/data",
                client,
                limit=limit,
                start=start,
                filters=filters,
            )
            return return_items(items, ["name", "description"])

    @mcp.tool()
    async def list_compute_tables(
        compute_context_name: str,
        library_name: str,
        ctx: Context,
        limit: int = 50,
        start: int = 0,
        filter_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the tables in a SAS library within a compute context.

        These are SAS/Compute tables (e.g. WORK or an assigned libref), distinct
        from in-memory CAS tables (see list_castables). Runs in the reusable
        per-user compute session for the context.

        Args:
            compute_context_name: Name of the compute context (see list_compute_contexts).
            library_name: Name of the SAS library/libref (e.g. 'WORK', 'SASHELP').
            limit: Maximum number of tables to return (default 50).
            start: Offset of the first table to return (default 0).
            filter_name: Optional name filter (substring match).
        """
        async with compute_tool_session(
            "list_compute_tables", ctx, compute_context_name
        ) as (client, session_id):
            filters = f"contains(name,'{filter_name}')" if filter_name else None
            items, _ = await get_paged_items(
                f"/compute/sessions/{session_id}/data/{library_name}",
                client,
                limit=limit,
                start=start,
                filters=filters,
            )
            return return_items(items, ["name", "description"])

    @mcp.tool()
    async def list_compute_columns(
        compute_context_name: str,
        library_name: str,
        table_name: str,
        ctx: Context,
        limit: int = 50,
        start: int = 0,
        filter_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the columns of a table in a SAS library within a compute context.

        Runs in the reusable per-user compute session for the context.

        Args:
            compute_context_name: Name of the compute context (see list_compute_contexts).
            library_name: Name of the SAS library/libref (e.g. 'WORK', 'SASHELP').
            table_name: Name of the table within the library.
            limit: Maximum number of columns to return (default 50).
            start: Offset of the first column to return (default 0).
            filter_name: Optional name filter (substring match).
        """
        async with compute_tool_session(
            "list_compute_columns", ctx, compute_context_name
        ) as (client, session_id):
            filters = f"contains(name,'{filter_name}')" if filter_name else None
            items, _ = await get_paged_items(
                f"/compute/sessions/{session_id}/data/{library_name}/{table_name}/columns",
                client,
                limit=limit,
                start=start,
                filters=filters,
            )
            return return_items(items, ["id", "name", "label", "type", "length"])

    @mcp.tool()
    async def reset_compute_session(
        ctx: Context, compute_context_name: str | None = None
    ) -> dict[str, str]:
        """Reset (delete) the cached compute session for a compute context.

        The server keeps one reusable SAS compute session per user and compute
        context so repeat calls skip the slow session spin-up; SAS state (WORK
        tables, macro variables, assigned librefs) therefore persists across
        ``execute_sas_code`` and ``list_compute_*`` calls. Call this to discard
        that state — the next compute tool call transparently creates a fresh
        session.

        Args:
            compute_context_name: Compute context whose session to reset.
                Defaults to the server's configured execution context (the one
                ``execute_sas_code`` uses).
        """
        context_name = compute_context_name or CONTEXT_NAME
        logger.info("--- TOOL USED: reset_compute_session ---")
        token = await get_token(ctx)
        async with make_client(token) as client:
            sid = await reset_cached_session(client, context_name, token)
        if sid is None:
            return {
                "status": "no_active_session",
                "compute_context": context_name,
                "message": "No cached compute session to reset for this context.",
            }
        return {
            "status": "reset",
            "compute_context": context_name,
            "deleted_session": sid,
        }

    # ------------------------------------------------------------------
    # Tier 7 — Information Catalog (metadata discovery & profiling)
    # ------------------------------------------------------------------

    catalog = "/catalog"
    search_collection_type = "application/vnd.sas.metadata.search.collection+json"
    # The adhoc job's status lives only in the full representation; the summary
    # (application/json) omits it.
    adhoc_media = "application/vnd.sas.metadata.bot.adhoc+json"
    profile_levels = ("dataDictionary", "dataDictionaryAndProfile", "detailedMetrics")

    def resource_uri_of(item: dict[str, Any]) -> str:
        """Pull the source-asset URI (rel='resource') from a catalog item's links."""
        for link in item.get("links", []) or []:
            if link.get("rel") == "resource":
                return link.get("href", "") or link.get("uri", "")
        return ""

    async def instance_for_resource_uri(
        client: httpx.AsyncClient, resource_uri: str
    ) -> dict[str, Any] | None:
        """Resolve the catalog instance for a source-asset URI, or None if absent.

        Filters ``/catalog/instances`` by ``resourceId`` — the reliable way to
        locate the instance that profiling writes its results onto, rather than
        assuming the instance id equals a search hit's id.
        """
        data = await get_json(
            f"{catalog}/instances",
            client,
            params={"filter": f'eq(resourceId,"{resource_uri}")'},
        )
        items = data.get("items", []) or []
        return items[0] if items else None

    @mcp.tool()
    async def catalog_search(
        query: str,
        ctx: Context,
        indices: str = "catalog",
        limit: int = 20,
        start: int = 0,
    ) -> dict[str, Any]:
        """Search the SAS Information Catalog for assets (tables, columns, reports, ...).

        The catalog is a metadata index across the whole Viya environment, so this
        finds assets without needing to know their server/library first. Each hit
        includes the asset's ``resource_uri`` — the URI you can hand to the matching
        tool (e.g. get_report, get_castable_data) to act on the live asset — and an
        ``attributes`` map with whatever metadata the catalog holds for it (commonly
        ``library``, ``rowCount``, ``columnCount``, ``completenessPercent``,
        ``reviewStatus``, ``informationPrivacy``, and ``analysisTimeStamp``).

        The ``query`` uses the SAS catalog search grammar:
          * Free text matches names, with wildcards ``*`` (0+ chars) and ``?`` (1 char): ``cust*``.
          * Facets constrain fields, e.g. ``AssetType:Report``, ``Name:sales``,
            ``Library.name:PUBLIC``, ``Column.informationPrivacy:Sensitive``.
          * Ranges ``DateModified:[2024-01-01 TO 2024-12-31]`` and ``+`` to require a term.
            Combine freely: ``AssetType:"CAS Table" +Name:cust*``.
        Use ``catalog_search_helper`` to discover valid facet names and values.

        Args:
            query: The catalog search query (see grammar above). Use ``*`` to match all names.
            indices: Comma-separated index name(s) to search (default 'catalog').
            limit: Maximum hits to return (default 20).
            start: Offset of the first hit (default 0).
        """
        async with viya_session("catalog_search", ctx) as client:
            data = await get_json(
                f"{catalog}/search",
                client,
                params={"q": query, "indices": indices, "start": start, "limit": limit},
                accept=search_collection_type,
            )
            raw_items = data.get("items", [])
            items = return_items(
                raw_items,
                [
                    "id",
                    "type",
                    "typeLabel",
                    "label",
                    "name",
                    "description",
                    "score",
                    "attributes",
                ],
            )
            # resource_uri is derived from the item's links, not a flat field.
            for out, src in zip(items, raw_items, strict=True):
                out["resource_uri"] = resource_uri_of(src)
            return {
                "count": data.get("count", len(items)),
                "start": data.get("start", start),
                "limit": data.get("limit", limit),
                "items": items,
            }

    @mcp.tool()
    async def catalog_search_helper(
        ctx: Context, facet: str | None = None, query: str = "", limit: int = 50
    ) -> dict[str, Any]:
        """Discover how to search the catalog: list facets, or values for one facet.

        Call with no ``facet`` to list the available facets — the fields you can
        constrain in a ``catalog_search`` query. Call with a ``facet`` name to get the
        suggested/valid values for that facet (e.g. the asset types or review
        statuses that actually exist). Use the results to build precise
        ``catalog_search`` queries.

        Args:
            facet: Facet name to get suggested values for (e.g. 'AssetType'). If
                omitted, returns the list of available facets instead.
            query: Optional filter — when listing facets, matches facet names; when
                listing values, matches value prefixes.
            limit: Maximum entries to return (default 50).
        """
        async with viya_session("catalog_search_helper", ctx) as client:
            if facet:
                data = await get_json(
                    f"{catalog}/search/suggestions",
                    client,
                    params={"facet": facet, "q": query, "limit": limit},
                )
                return {"facet": facet, "values": data.get("items", [])}
            data = await get_json(
                f"{catalog}/search/facets",
                client,
                params={"q": query, "start": 0, "limit": limit},
            )
            facets = return_items(
                data.get("items", []), ["name", "type", "indices"]
            )
            return {"facets": facets}

    @mcp.tool()
    async def catalog_find_instance(
        resource_uri: str, ctx: Context
    ) -> dict[str, Any]:
        """Resolve the catalog instance for a source-asset URI.

        ``catalog_search`` finds assets by free text and facets, but the
        profiling and download tools key off a catalog *instance id*. When you
        already hold a resource URI — the ``resource_uri`` from a search hit, or
        a CAS table path — this looks the instance up directly by ``resourceId``
        (the same filter the profiling workflow uses) and returns its id plus
        the key profile attributes. Use it to tell at a glance whether the asset
        has been profiled (``analysisTimeStamp``) and what semantic metadata it
        carries (``informationPrivacy``, ``nlpTerms``, ``nlpTags``,
        ``mostImportantFields``) before calling ``catalog_download_table_profile``.

        Args:
            resource_uri: Source URI of the asset (e.g.
                '/dataTables/dataSources/cas~fs~.../tables/MYTABLE').
        """
        async with viya_session("catalog_find_instance", ctx) as client:
            inst = await instance_for_resource_uri(client, resource_uri)
            if inst is None:
                return {
                    "status": "not_found",
                    "resource_uri": resource_uri,
                    "message": (
                        "No catalog instance indexes that URI yet. Confirm the URI "
                        "with catalog_search, or run a discovery agent "
                        "(catalog_run_agent) to populate it."
                    ),
                }
            attrs = inst.get("attributes", {}) or {}
            return {
                "status": "ok",
                "instance_id": inst.get("id"),
                "name": inst.get("name", ""),
                "type": inst.get("type", ""),
                "resource_uri": inst.get("resourceId", resource_uri),
                "profiled": bool(attrs.get("analysisTimeStamp")),
                "attributes": attrs,
            }

    @mcp.tool()
    async def catalog_list_agents(
        ctx: Context, limit: int = 50, start: int = 0, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List SAS Information Catalog discovery agents.

        Agents crawl a data source (server/library) to discover assets and collect
        their metadata into the catalog. Use ``catalog_run_agent`` to start one and
        ``catalog_get_agent_history`` to see what a run produced.

        Args:
            limit: Maximum agents to return (default 50).
            start: Offset of the first agent (default 0).
            filter_name: Optional name filter (substring match).
        """
        params: dict[str, Any] = {"start": start, "limit": limit}
        if filter_name:
            params["filter"] = f"contains(name,'{filter_name}')"
        async with viya_session("catalog_list_agents", ctx) as client:
            data = await get_json(f"{catalog}/bots", client, params=params)
            return return_items(
                data.get("items", []),
                ["id", "name", "description", "agentType", "provider"],
            )

    @mcp.tool()
    async def catalog_run_agent(agent_id: str, ctx: Context) -> dict[str, str]:
        """Start a catalog discovery agent run (asynchronous).

        Triggers the agent to crawl its data source and populate/refresh catalog
        metadata. The run is asynchronous — results are applied to the catalog in
        the background; poll ``catalog_get_agent_history`` to track completion.
        Note: the Catalog API can only *start* an agent, not stop one already running.

        Args:
            agent_id: ID of the agent to run (see catalog_list_agents).
        """
        async with viya_session("catalog_run_agent", ctx) as client:
            resp = await client.put(
                f"{VIYA_ENDPOINT}{catalog}/bots/{agent_id}/state",
                params={"value": "running"},
                headers={"Accept": "text/plain"},
            )
            resp.raise_for_status()
            return {
                "status": resp.text.strip() or "running",
                "agent_id": agent_id,
                "message": (
                    "Agent started; metadata is applied to the catalog asynchronously. "
                    "Poll catalog_get_agent_history for completion."
                ),
            }

    @mcp.tool()
    async def catalog_get_agent_history(
        agent_id: str, ctx: Context, limit: int = 20, start: int = 0
    ) -> list[dict[str, Any]]:
        """Get the execution history of a catalog agent's runs.

        Each record reports a run's status and how much metadata it populated
        (tables enumerated/added/updated/removed), so you can confirm a run started
        by ``catalog_run_agent`` finished and what it changed.

        Args:
            agent_id: ID of the agent (see catalog_list_agents).
            limit: Maximum run records to return (default 20).
            start: Offset of the first record (default 0).
        """
        async with viya_session("catalog_get_agent_history", ctx) as client:
            data = await get_json(
                f"{catalog}/bots/{agent_id}/history",
                client,
                params={"start": start, "limit": limit},
            )
            return return_items(
                data.get("items", []),
                [
                    "id",
                    "status",
                    "creationTimeStamp",
                    "endTimeStamp",
                    "nEnumerated",
                    "nAdded",
                    "nUpdated",
                    "nRemoved",
                ],
            )

    @mcp.tool()
    async def catalog_run_adhoc_analysis(
        resource_uri: str,
        name: str,
        ctx: Context,
        resource_type: str = "",
        description: str = "",
        provider: str = "TABLE-BOT",
        identify_language: bool = True,
        analyze_sentiment: bool = True,
        get_nlp_semantic_id: bool = True,
    ) -> dict[str, Any]:
        """Submit an ad-hoc analysis (profiling) job for a table in the catalog.

        Profiles the table — computing the data dictionary, column statistics, and
        data-quality metrics that ``catalog_download_table_profile`` returns. The job
        runs asynchronously and may take a while; poll ``catalog_get_adhoc_analysis``
        with the returned job id until the profile is ready.

        The three NLP job parameters are enabled by default — they drive the
        semantic enrichment that populates an asset's ``informationPrivacy``,
        ``nlpTerms``, ``nlpTags``, and ``mostImportantFields`` (the privacy and
        keyword signals the catalog is most useful for). Leave them on unless you
        only need a plain column profile and want the job to finish faster.

        Args:
            resource_uri: Source URI of the table to analyze (the ``resource_uri`` from
                a catalog_search hit, e.g. '/dataTables/dataSources/cas~fs~.../tables/MYTABLE').
            name: A name for the analysis job.
            resource_type: Catalog entity type of the resource. Defaults to
                'CASMEMTable' when the URI is a CAS table (contains 'cas~fs~');
                pass it explicitly for other asset types.
            description: Optional description for the job.
            provider: Job provider (default 'TABLE-BOT').
            identify_language: Detect each text column's language (default True).
            analyze_sentiment: Score sentiment on text columns (default True).
            get_nlp_semantic_id: Derive semantic types / privacy classification
                (informationPrivacy, nlpTerms, nlpTags) (default True).
        """
        rtype = resource_type or (
            "CASMEMTable" if "cas~fs~" in resource_uri else ""
        )
        if not rtype:
            return {
                "status": "missing_resource_type",
                "resource_uri": resource_uri,
                "message": (
                    "Could not infer resource_type from the URI. Pass resource_type "
                    "explicitly (e.g. 'CASMEMTable' for a CAS table)."
                ),
            }
        job_parameters: dict[str, str] = {}
        if identify_language:
            job_parameters["identifyLanguage"] = "1"
        if analyze_sentiment:
            job_parameters["analyzeSentiment"] = "1"
        if get_nlp_semantic_id:
            job_parameters["getNLPSemanticID"] = "1"
        body = {
            "provider": provider,
            "name": name,
            "description": description,
            "resources": [{"uri": resource_uri, "type": rtype}],
            "jobParameters": job_parameters,
        }
        async with viya_session("catalog_run_adhoc_analysis", ctx) as client:
            job = await post_json(
                f"{catalog}/bots/adhocAnalysisJobs",
                client,
                body=body,
                accept=adhoc_media,
            )
            return {
                "id": job.get("id"),
                "status": job.get("status", ""),
                "name": job.get("name", name),
                "message": (
                    "Analysis submitted. Poll catalog_get_adhoc_analysis until status "
                    "is 'completed', then catalog_download_table_profile."
                ),
            }

    @mcp.tool()
    async def catalog_get_adhoc_analysis(job_id: str, ctx: Context) -> dict[str, Any]:
        """Get the status of an ad-hoc analysis job, and whether its profile is ready.

        The job reaching a terminal ``status`` is *not* sufficient: the profile
        attributes are written onto the asset a little later, so a download fired
        the instant the job completes can come back empty. To close that gap, when
        the job carries a resource this also resolves the target catalog instance
        and reports ``profile_ready`` (the asset's ``analysisTimeStamp`` is
        populated — the same gate ``catalog_download_table_profile`` uses) and
        ``information_privacy`` (non-empty once the NLP semantic enrichment has
        landed). Poll until ``profile_ready`` is true, then download.

        Args:
            job_id: The analysis job id returned by catalog_run_adhoc_analysis.
        """
        async with viya_session("catalog_get_adhoc_analysis", ctx) as client:
            try:
                job = await get_json(
                    f"{catalog}/bots/adhocAnalysisJobs/{job_id}",
                    client,
                    accept=adhoc_media,
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    # Adhoc jobs can be purged once terminal — report it as such
                    # rather than raising, so polling loops can stop cleanly.
                    return {
                        "id": job_id,
                        "status": "not_found",
                        "message": (
                            "No such analysis job — it may have finished and been "
                            "purged, or the id is wrong."
                        ),
                    }
                raise
            resources = job.get("resources", []) or []
            resource_uri = ""
            if resources:
                resource_uri = resources[0].get("uri", "") or resources[0].get(
                    "resourceId", ""
                )
            # Cross-check the asset itself: the job can be terminal while the
            # profile is still being written onto the instance.
            instance_id = ""
            profile_ready = False
            information_privacy = ""
            if resource_uri:
                inst = await instance_for_resource_uri(client, resource_uri)
                if inst:
                    instance_id = inst.get("id", "")
                    inst_attrs = inst.get("attributes", {}) or {}
                    profile_ready = bool(inst_attrs.get("analysisTimeStamp"))
                    information_privacy = inst_attrs.get("informationPrivacy", "") or ""
            return {
                "id": job.get("id", job_id),
                "status": job.get("status", ""),
                "name": job.get("name", ""),
                "creationTimeStamp": job.get("creationTimeStamp", ""),
                "endTimeStamp": job.get("endTimeStamp", ""),
                "resources": resources,
                "instance_id": instance_id,
                "profile_ready": profile_ready,
                "information_privacy": information_privacy,
                "message": (
                    f"Profile ready — download with catalog_download_table_profile "
                    f"(instance_id='{instance_id}')."
                    if profile_ready
                    else "Profile not written to the asset yet — poll again before "
                    "downloading."
                ),
            }

    @mcp.tool()
    async def catalog_download_table_profile(
        ctx: Context,
        instance_id: str = "",
        resource_uri: str = "",
        level: str = "dataDictionaryAndProfile",
    ) -> dict[str, Any]:
        """Download a catalog table's data dictionary and profile as CSV.

        Returns the table's column metadata plus, by default, its profile (column
        statistics and data-quality metrics). If the table has not been profiled yet,
        this returns a recommendation to run ``catalog_run_adhoc_analysis`` (pre-filled
        with the table's URI and type) instead of an empty profile.

        Identify the table by either ``instance_id`` or ``resource_uri`` (give one).
        Passing ``resource_uri`` lets you run search → profile → download without ever
        handling an instance id: the asset is resolved by ``resourceId`` the same way
        ``catalog_find_instance`` does. ``instance_id`` takes precedence if both are given.

        Args:
            instance_id: Catalog instance id of the table (the ``id`` from a catalog_search hit).
            resource_uri: Source URI of the table (the ``resource_uri`` from a search hit,
                e.g. '/dataTables/dataSources/cas~fs~.../tables/MYTABLE'). Used when
                ``instance_id`` is omitted.
            level: Detail level — 'dataDictionaryAndProfile' (default; columns + profile),
                'detailedMetrics' (full per-column metrics), or 'dataDictionary'
                (column metadata only).
        """
        if level not in profile_levels:
            return {
                "status": "invalid_level",
                "message": f"level must be one of {', '.join(profile_levels)}.",
            }
        if not instance_id and not resource_uri:
            return {
                "status": "missing_identifier",
                "message": "Pass either instance_id or resource_uri.",
            }
        async with viya_session("catalog_download_table_profile", ctx) as client:
            # Resolve the instance first to identify the asset and whether it is profiled.
            if instance_id:
                try:
                    inst = await get_json(
                        f"{catalog}/instances/{instance_id}",
                        client,
                        accept="application/vnd.sas.metadata.instance.entity+json",
                    )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        return {
                            "status": "not_found",
                            "instance_id": instance_id,
                            "message": (
                                f"No catalog instance '{instance_id}'. "
                                "Use catalog_search to find one."
                            ),
                        }
                    raise
            else:
                inst = await instance_for_resource_uri(client, resource_uri)
                if inst is None:
                    return {
                        "status": "not_found",
                        "resource_uri": resource_uri,
                        "message": (
                            f"No catalog instance indexes '{resource_uri}'. "
                            "Use catalog_search or catalog_find_instance to confirm it."
                        ),
                    }
                instance_id = inst.get("id", "")
            attrs = inst.get("attributes", {}) or {}
            resource_uri = inst.get("resourceId", "") or resource_uri
            resource_type = inst.get("type", "")
            wants_profile = level in ("dataDictionaryAndProfile", "detailedMetrics")
            if wants_profile and not attrs.get("analysisTimeStamp"):
                return {
                    "status": "not_profiled",
                    "instance_id": instance_id,
                    "resource_uri": resource_uri,
                    "resource_type": resource_type,
                    "message": (
                        "This table has no profile yet. Run catalog_run_adhoc_analysis "
                        f"with resource_uri='{resource_uri}' and "
                        f"resource_type='{resource_type}', poll catalog_get_adhoc_analysis "
                        "until completed, then retry."
                    ),
                }
            resp = await client.get(
                f"{VIYA_ENDPOINT}{catalog}/instances",
                params={"level": level, "filter": f"eq(id,'{instance_id}')"},
                headers={"Accept": "text/csv"},
                follow_redirects=True,
            )
            resp.raise_for_status()
            return {
                "status": "ok",
                "instance_id": instance_id,
                "resource_uri": resource_uri,
                "level": level,
                "csv": resp.text,
            }
