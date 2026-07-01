# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Helpers for the ``export_report`` tool.

Holds the Visual Analytics export-format registry plus the request validation
and execution logic, so the ``@mcp.tool`` in ``tools.py`` stays a thin wrapper
(matching the ``helpers/`` pattern used elsewhere in the server).
"""

from dataclasses import dataclass, field
from typing import Any

import httpx
from fastmcp.utilities.types import File, Image

from sas_mcp_server.config import MAX_EXPORT_INLINE_BYTES, VIYA_ENDPOINT


# One registry describing every synchronous Visual Analytics report export the
# ``/visualAnalytics/reports/{id}/{suffix}`` endpoints expose. Each entry pins
# the URL suffix, the Accept media type, and how report objects are addressed,
# so the single ``export_report`` tool can validate inputs and shape its result
# (text inline, image content, or an embedded binary file) per format. Adding a
# format the VA service later supports is a one-line change here.
@dataclass(frozen=True)
class ReportExportFormat:
    """A single synchronous VA report export endpoint."""

    key: str  # value callers pass as ``export_format``
    suffix: str  # path segment after the report id (the VA endpoint name)
    file_ext: str  # download extension for the result (differs from suffix for zip/txt)
    accept: str  # Accept media type requested and advertised on the result
    object_param: str | None  # query param naming report object(s), or None
    multi_object: bool  # True -> comma-joined list; False -> single label
    object_required: bool  # at least one object label is mandatory
    needs_size: bool  # image_size required (image endpoints)
    is_text: bool  # response is UTF-8 text, returned inline
    is_image: bool  # response is a raster image, returned as Image content


REPORT_EXPORT_FORMATS: dict[str, ReportExportFormat] = {
    fmt.key: fmt
    for fmt in (
        # key      suffix     ext    accept                            obj param       multi  req    size   text   image
        ReportExportFormat("package", "package", "zip", "application/zip",
            "reportObjects", True, False, False, False, False),
        ReportExportFormat("pdf", "pdf", "pdf", "application/pdf",
            "reportObjects", True, False, False, False, False),
        ReportExportFormat("png", "png", "png", "image/png",
            "reportObject", False, False, True, False, True),
        ReportExportFormat("svg", "svg", "svg", "image/svg+xml",
            "reportObject", False, False, True, True, False),
        ReportExportFormat("csv", "csv", "csv", "text/csv",
            "reportObject", False, True, False, True, False),
        ReportExportFormat("tsv", "tsv", "tsv", "text/tsv",
            "reportObject", False, True, False, True, False),
        ReportExportFormat("xlsx", "xlsx", "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "reportObject", False, True, False, False, False),
        ReportExportFormat("summary", "summary", "txt", "text/plain",
            None, False, False, False, True, False),
    )
}


@dataclass
class ReportExportRequest:
    """A normalised ``export_report`` invocation."""

    report_id: str
    export_format: str
    report_objects: list[str] | None = field(default_factory=list)
    image_size: str | None = None
    options: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # Tolerate ``None`` and a single label passed as a bare string.
        if self.report_objects is None:
            self.report_objects = []
        elif isinstance(self.report_objects, str):
            self.report_objects = [self.report_objects]


def validate_export_request(req: ReportExportRequest) -> dict[str, Any] | None:
    """Return a structured error dict if *req* is invalid, else ``None``.

    Enforces the per-format rules the VA endpoints impose (object cardinality
    and the required ``image_size``) before any HTTP call is made.
    """
    fmt = REPORT_EXPORT_FORMATS.get(req.export_format)
    if fmt is None:
        return {
            "status": "unsupported_format",
            "export_format": req.export_format,
            "supported": sorted(REPORT_EXPORT_FORMATS),
        }
    objects = req.report_objects
    if fmt.object_param is None and objects:
        return {
            "status": "invalid_request",
            "message": f"export_format '{fmt.key}' does not take report objects.",
        }
    if objects and not fmt.multi_object and len(objects) > 1:
        return {
            "status": "invalid_request",
            "message": (
                f"export_format '{fmt.key}' exports a single report object; "
                f"got {len(objects)}. Pass exactly one label."
            ),
        }
    if fmt.object_required and not objects:
        return {
            "status": "invalid_request",
            "message": (
                f"export_format '{fmt.key}' requires one report object label "
                "in report_objects."
            ),
        }
    if fmt.needs_size and not req.image_size:
        return {
            "status": "invalid_request",
            "message": (
                f"export_format '{fmt.key}' requires image_size, "
                'e.g. "1200px,800px".'
            ),
        }
    return None


def build_export_params(fmt: ReportExportFormat, req: ReportExportRequest) -> dict[str, str]:
    """Build the query parameters for the VA export request."""
    params: dict[str, str] = {}
    if fmt.object_param and req.report_objects:
        params[fmt.object_param] = (
            ",".join(req.report_objects) if fmt.multi_object else req.report_objects[0]
        )
    if fmt.needs_size and req.image_size:
        params["size"] = req.image_size
    if fmt.key == "pdf" and req.options:
        params.update({key: str(value) for key, value in req.options.items()})
    return params


async def execute_export(req: ReportExportRequest, client: httpx.AsyncClient) -> Any:
    """Call the VA export endpoint for a *validated* request and shape the result.

    Returns text inline for text formats, ``Image`` content for ``png``, and an
    embedded binary file (with the correct MIME type) for ``package``/``pdf``/
    ``xlsx``. Binary results over ``MAX_EXPORT_INLINE_BYTES`` are refused with
    guidance rather than streamed through the model context. A Viya HTTP error
    is surfaced as a structured ``export_failed`` dict rather than raised.
    """
    fmt = REPORT_EXPORT_FORMATS[req.export_format]
    params = build_export_params(fmt, req)
    resp = await client.get(
        f"{VIYA_ENDPOINT}/visualAnalytics/reports/{req.report_id}/{fmt.suffix}",
        params=params,
        headers={"Accept": fmt.accept},
        follow_redirects=True,
    )
    if resp.status_code >= 400:
        # Surface why Viya refused (bad object label, an object type that the
        # exporter can't render, a server fault) as a structured error instead
        # of raising an opaque one. Some VA export faults (e.g. data export of a
        # non-tabular object) come back as a bare 500 with no body.
        return {
            "status": "export_failed",
            "export_format": fmt.key,
            "http_status": resp.status_code,
            "report_id": req.report_id,
            "message": (
                f"Viya rejected the {fmt.key} export of report {req.report_id} "
                f"(HTTP {resp.status_code}). The report or object may not support "
                f"this export. Viya said: {resp.text[:400] or '(no response body)'}"
            ),
        }

    if fmt.is_text:
        return resp.text

    content = resp.content
    if len(content) > MAX_EXPORT_INLINE_BYTES:
        return {
            "status": "export_too_large",
            "export_format": fmt.key,
            "size_bytes": len(content),
            "limit_bytes": MAX_EXPORT_INLINE_BYTES,
            "message": (
                f"The {fmt.key} export is {len(content):,} bytes, over the "
                f"{MAX_EXPORT_INLINE_BYTES:,}-byte inline limit. Narrow the "
                "export via report_objects, choose a lighter format, or "
                "raise MAX_EXPORT_INLINE_BYTES."
            ),
        }

    if fmt.is_image:
        return Image(data=content, format=fmt.file_ext)
    # ``format`` sets the embedded resource's filename extension; the explicit
    # mime_type override gives it the real media type (the File helper would
    # otherwise advertise ``application/<format>``).
    return File(
        data=content, format=fmt.file_ext, name=req.report_id
    ).to_resource_content(mime_type=fmt.accept)
