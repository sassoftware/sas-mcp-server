# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 2 — Data Operations & Files tools."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import SSL_VERIFY, VIYA_ENDPOINT
from ..env import env_bool
from ..viya_client import get_json, get_paged_items, return_items
from ._common import make_session_helpers


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
        "tsv",
        "csv",
        extensions=(".tsv", ".tab"),
        aliases=("tab",),
        delimiter="\t",
        header_row=True,
    ),
    DataFormat("xls", "xls", extensions=(".xls",), header_row=True, excel_format=True, binary=True),
    DataFormat(
        "xlsx", "xlsx", extensions=(".xlsx",), aliases=("excel",), header_row=True, excel_format=True, binary=True
    ),
    DataFormat("xlsm", "xlsx", extensions=(".xlsm",), header_row=True, excel_format=True, binary=True),
    DataFormat("sas7bdat", "sas7bdat", extensions=(".sas7bdat",), aliases=("sas",), binary=True),
    DataFormat("sashdat", "sashdat", extensions=(".sashdat",), binary=True),
    # Recognized but not accepted by the upload endpoint: detected so we can fail
    # fast with guidance rather than a guaranteed HTTP 400. Flip ``supported`` to
    # True if/when a deployment's endpoint accepts parquet.
    DataFormat(
        "parquet",
        "parquet",
        extensions=(".parquet", ".parq"),
        binary=True,
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
                "message": (f"Unsupported data_format '{name}'. Supported: {', '.join(_SUPPORTED_FORMATS)}."),
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
            "message": fmt.unsupported_reason or f"Format '{fmt.key}' is not accepted by the upload endpoint.",
        }
    return fmt, None


async def _resolve_source_bytes(file_path: str | None, url: str | None) -> tuple[bytes | None, dict[str, Any] | None]:
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
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True, verify=SSL_VERIFY) as fetch_client:
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
                f"Table '{table_name}' already exists in caslib '{caslib_name}'. Drop or rename before re-uploading."
            ),
        }
    if resp.status_code >= 400:
        # Surface why CAS refused (bad caslib, malformed file, scope/perm) as a
        # structured error instead of raising an opaque one.
        return {
            "status": "upload_failed",
            "http_status": resp.status_code,
            "data_format": fmt.key,
            "message": (f"CAS rejected the {fmt.key} upload (HTTP {resp.status_code}). Viya said: {resp.text[:400]}"),
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


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 2 (Data Operations & Files) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

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
                    "Provide exactly one of file_path or url. To upload inline text use the upload_inline_data tool."
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
    async def list_files(ctx: Context, limit: int = 50, filter_name: str | None = None) -> list[dict[str, Any]]:
        """List files in the Viya Files Service.

        Args:
            limit: Maximum files to return (default 50).
            filter_name: Optional name filter (substring match).
        """
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with viya_session("list_files", ctx) as client:
            items, _ = await get_paged_items("/files/files", client, limit=limit, filters=filters)
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
