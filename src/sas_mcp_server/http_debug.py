# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in trace of every HTTP request this server sends to SAS Viya.

A tool failure reaches the person as the model's summary of an error; the
request that caused it — a wrong path, a missing ``Accept`` media type, a body
of the wrong shape — is nowhere to be seen, and one tool call can make several
requests (paging, polling a compute job, fetching its log). ``HTTP_DEBUG=true``
writes each of them to a separate JSONL file (``HTTP_DEBUG_LOG_PATH``), never to
the server log: under stdio, stdout is the MCP protocol stream, and the server
log is often shared with a client's own logs.

Two records per exchange, ``"event": "request"`` then ``"event": "response"``,
share an ``id``. They are written as each happens rather than as one record at
the end, because a request that never gets a response — a timeout, a refused
connection, a TLS failure — is exactly the case worth seeing, and httpx's
response hook never fires for it: such a request shows up as a ``request`` with
no matching ``response``.

Redaction reuses :mod:`sas_mcp_server.usage_logger`, so the two logs mask the
same things: credential-shaped header names, query parameters and body keys
(``Authorization``, ``Cookie``, ``token``, ``password`` ...) plus inline
Bearer/JWT strings. Bodies are capped at ``HTTP_DEBUG_MAX_BODY_BYTES``
(``0`` records none). Like collection mode, redaction does not detect PII in
data values — table rows can appear in a body — so review the file before
sharing it.

Only clients built by :func:`sas_mcp_server.viya_client.make_client` are traced
— that is every Viya REST call the tools make. The OAuth token exchanges
(sign-in, refresh) are deliberately not: their bodies are credentials.

Like :func:`sas_mcp_server.telemetry.install_telemetry`, the tracer is
installed once at startup by the server entry points; when ``HTTP_DEBUG`` is off
:func:`event_hooks` returns ``None`` and clients are built exactly as before.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl

import httpx

from .usage_logger import REDACT_KEY_RE, UsageLogger, bounded_redact

module_logger = logging.getLogger(__name__)

__all__ = [
    "HttpDebugTracer",
    "body_preview",
    "event_hooks",
    "install_http_debug",
    "redact_headers",
    "redact_url",
    "uninstall_http_debug",
]

_REDACTED = "[REDACTED]"
# Where the request hook leaves the id and start time for the response hook.
# httpx carries request.extensions through to response.request untouched.
_EXT_KEY = "sas_mcp_http_debug"

# Media types whose bodies are recorded as text. Anything else (report images,
# exports, uploads) is recorded as its size and type only.
_TEXT_MARKERS = ("json", "text/", "xml", "x-www-form-urlencoded", "javascript", "csv")


def _is_text(content_type: str) -> bool:
    ct = content_type.lower()
    return any(marker in ct for marker in _TEXT_MARKERS)


def redact_headers(headers: httpx.Headers) -> dict[str, str]:
    """Headers as a dict, credential-shaped names masked (``Authorization``,
    ``Cookie``/``Set-Cookie`` and whatever :data:`REDACT_KEY_RE` matches)."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in ("cookie", "set-cookie") or REDACT_KEY_RE.search(lowered):
            out[name] = _REDACTED
        else:
            out[name] = value
    return out


def redact_url(url: httpx.URL) -> str:
    """The URL with any credential-shaped query parameter's value masked."""
    if not url.query:
        return str(url)
    params = [
        (key, _REDACTED if REDACT_KEY_RE.search(key) else value)
        for key, value in url.params.multi_items()
    ]
    return str(url.copy_with(params=params))


def body_preview(content: bytes, content_type: str, max_bytes: int) -> tuple[Any, bool]:
    """Render a body for the log: redacted, capped, and typed where possible.

    Returns ``(value, truncated)``. JSON is parsed so that a secret-shaped *key*
    is masked, not just an inline token, and stays an object in the JSONL
    record; a form body is treated the same way. Other text is scrubbed of
    Bearer/JWT strings and clipped. Binary content is described, not recorded.
    """
    if not content:
        return None, False
    if max_bytes <= 0:
        return f"<{len(content)} bytes, not recorded: HTTP_DEBUG_MAX_BODY_BYTES=0>", False
    if not _is_text(content_type):
        return f"<{len(content)} bytes of {content_type or 'unknown type'}>", False
    text = content.decode("utf-8", errors="replace")
    ct = content_type.lower()
    if "json" in ct:
        try:
            return bounded_redact(json.loads(text), max_bytes)
        except ValueError:
            pass  # declared JSON but is not: record it as text
    elif "x-www-form-urlencoded" in ct:
        return bounded_redact(dict(parse_qsl(text, keep_blank_values=True)), max_bytes)
    return bounded_redact(text, max_bytes)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class HttpDebugTracer:
    """httpx event hooks that append request/response records to a JSONL file.

    One tracer serves every client (``make_client`` builds one per tool call),
    so ids are unique for the life of the process and concurrent tool calls
    stay separable. The hooks never raise: a tracing fault must not fail the
    Viya call it was watching.
    """

    def __init__(self, writer: UsageLogger, *, max_body_bytes: int) -> None:
        self.writer = writer
        self.max_body_bytes = max_body_bytes
        self._ids = itertools.count(1)

    def event_hooks(self) -> dict[str, list[Any]]:
        return {"request": [self.on_request], "response": [self.on_response]}

    def _request_body(self, request: httpx.Request) -> tuple[Any, bool]:
        try:
            content = request.content
        except httpx.RequestNotRead:
            # A multipart upload is a stream; reading it here would buffer the
            # whole file (up to MAX_UPLOAD_BYTES) a second time.
            return "<streamed body, not recorded>", False
        return body_preview(
            content, request.headers.get("content-type", ""), self.max_body_bytes
        )

    async def on_request(self, request: httpx.Request) -> None:
        try:
            trace_id = next(self._ids)
            request.extensions[_EXT_KEY] = (trace_id, time.perf_counter())
            body, truncated = self._request_body(request)
            record: dict[str, Any] = {
                "ts": _now(),
                "event": "request",
                "id": trace_id,
                "method": request.method,
                "url": redact_url(request.url),
                "headers": redact_headers(request.headers),
                "body": body,
            }
            if truncated:
                record["body_truncated"] = True
            self.writer.write(record)
        except Exception as exc:  # noqa: BLE001 - tracing must never break a call
            module_logger.debug("HTTP debug request hook failed: %s", exc)

    async def on_response(self, response: httpx.Response) -> None:
        try:
            request = response.request
            trace_id, started = request.extensions.get(_EXT_KEY, (None, None))
            # Safe to read here: make_client's callers never stream, and every
            # one of them reads the whole body anyway.
            await response.aread()
            content_type = response.headers.get("content-type", "")
            body, truncated = body_preview(
                response.content, content_type, self.max_body_bytes
            )
            record: dict[str, Any] = {
                "ts": _now(),
                "event": "response",
                "id": trace_id,
                "method": request.method,
                "url": redact_url(request.url),
                "status": response.status_code,
                "elapsed_ms": (
                    round((time.perf_counter() - started) * 1000, 1)
                    if started is not None
                    else None
                ),
                "headers": redact_headers(response.headers),
                "body": body,
            }
            if truncated:
                record["body_truncated"] = True
            self.writer.write(record)
        except Exception as exc:  # noqa: BLE001 - tracing must never break a call
            module_logger.debug("HTTP debug response hook failed: %s", exc)


_tracer: HttpDebugTracer | None = None


def install_http_debug() -> HttpDebugTracer | None:
    """Open the trace file and start tracing iff ``HTTP_DEBUG`` is on.

    config is imported LAZILY, as in ``install_telemetry`` (config.py raises
    ConfigError when VIYA_ENDPOINT is unset). An unusable path disables tracing
    with a warning; the server runs exactly as it would without it.
    """
    global _tracer
    from . import config

    if not config.HTTP_DEBUG:
        _tracer = None
        return None
    try:
        writer = UsageLogger(
            path=os.path.expanduser(config.HTTP_DEBUG_LOG_PATH),
            max_log_bytes=config.HTTP_DEBUG_MAX_LOG_BYTES,
            backup_count=config.HTTP_DEBUG_LOG_BACKUPS,
            max_field_bytes=config.HTTP_DEBUG_MAX_BODY_BYTES,
        )
    except OSError as exc:
        module_logger.warning(
            "HTTP_DEBUG requested but log path unusable (%s); HTTP tracing disabled",
            exc,
        )
        _tracer = None
        return None
    _tracer = HttpDebugTracer(writer, max_body_bytes=config.HTTP_DEBUG_MAX_BODY_BYTES)
    # Say so where the operator looks: a trace file that grows unnoticed holds
    # every request and response body the server has sent, table rows included.
    module_logger.warning(
        "HTTP_DEBUG is on: every SAS Viya API request and response is written to %s "
        "(credentials redacted, bodies capped at %d bytes). Turn it off when done.",
        writer.path,
        config.HTTP_DEBUG_MAX_BODY_BYTES,
    )
    return _tracer


def uninstall_http_debug() -> None:
    """Stop tracing. For tests; the servers never turn it off once on."""
    global _tracer
    _tracer = None


def event_hooks() -> dict[str, list[Any]] | None:
    """The ``event_hooks`` argument for a new client: the tracer's, or ``None``."""
    return _tracer.event_hooks() if _tracer is not None else None
