# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in collection-mode telemetry middleware for the SAS MCP server.

Injects a required ``goal`` parameter into every published tool schema and logs
each tool call's input/output/session-id/goal/status/latency to a JSONL file.
Default OFF; requires ZERO changes to existing tools; works identically in HTTP
and stdio (it relies solely on ``context.message`` + a guarded
``context.fastmcp_context.session_id`` and NEVER calls get_http_request).

The disk write is offloaded to a worker thread via ``anyio.to_thread`` so the
blocking file I/O and any RotatingFileHandler rollover never run on the asyncio
event loop; the handler's own lock keeps each append atomic across threads.

REJECTED ALTERNATIVES (do not "simplify" into these):
  * ToolTransform / ArgTransform CANNOT add a brand-new ``goal`` arg
    (ArgTransform only forwards EXISTING parent properties), would force
    re-registering all ~45 tools, inherits ``additionalProperties: false`` +
    the parent output_schema, and runs INNERMOST so it could not observe auth
    failures. Middleware is the right layer.
  * A Context wrapper is impossible: Context is built internally per request.
  * A QueueHandler/QueueListener would move I/O off-loop too, but needs
    lifespan shutdown wiring in both entry points to avoid a lost final flush;
    anyio.to_thread keeps per-call flush semantics with no extra wiring.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import anyio.to_thread
import mcp.types
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# Tool/ToolResult live in fastmcp.tools.base — the path FastMCP's own middleware
# imports. A sys.modules shim in fastmcp.tools defeats Pyright's static resolver
# for this submodule, so the import is runtime-correct but flagged; ignore it.
from fastmcp.tools.base import Tool, ToolResult  # pyright: ignore[reportMissingImports]

from .usage_logger import GOAL_KEY, UsageLogger, bounded_redact

module_logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

GOAL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Before the other arguments, state in ONE sentence WHY you are calling "
        "THIS specific tool for the user's current request — the "
        "underlying goal it serves."
    ),
}


class TelemetryMiddleware(Middleware):
    """Injects ``goal`` into every listed tool schema and logs each call."""

    def __init__(
        self,
        logger: UsageLogger,
        *,
        require_goal: bool,
        transport: str,
        log_results: bool = True,
    ) -> None:
        self.logger = logger
        self.require_goal = require_goal
        self.transport = transport
        self.log_results = log_results
        # Per-process fallback session id (used only if the transport session
        # id is unavailable).
        self._proc_session = str(uuid4())

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp.types.ListToolsRequest],
        call_next: CallNext[mcp.types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        out: list[Tool] = []
        for t in tools:
            try:
                params = dict(t.parameters or {})
                props = dict(params.get("properties", {}))
                if GOAL_KEY not in props:  # idempotent across repeated lists
                    props = {GOAL_KEY: GOAL_SCHEMA, **props}  # goal FIRST
                    params["properties"] = props
                    if self.require_goal:
                        req = [
                            r for r in params.get("required", []) if r != GOAL_KEY
                        ]
                        params["required"] = [GOAL_KEY, *req]
                    # NEW Tool; never mutate the shared registry singleton.
                    out.append(t.model_copy(update={"parameters": params}))
                else:
                    out.append(t)
            except Exception:  # noqa: BLE001 - per-tool isolation
                out.append(t)
        return out

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp.types.CallToolRequestParams],
        call_next: CallNext[mcp.types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        raw = dict(context.message.arguments or {})
        # LOAD-BEARING: a real tool's TypeAdapter raises
        # unexpected_keyword_argument if 'goal' leaks through, so strip it
        # from a COPY of the arguments before forwarding.
        goal = raw.pop(GOAL_KEY, None)
        cleaned_ctx = context.copy(
            message=context.message.model_copy(update={"arguments": raw})
        )
        session_id = self._resolve_session(context)
        status, is_error, error, result_obj = "success", False, None, None
        t0 = time.perf_counter()
        try:
            result = await call_next(cleaned_ctx)
            is_error = bool(getattr(result, "is_error", False))
            if is_error:
                status = "error"
                error = self._extract_error_text(result)
            result_obj = self._extract_output(result)
            return result
        except Exception as exc:  # noqa: BLE001 - record then re-raise UNCHANGED
            status, is_error = "error", True
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                dur_ms = (time.perf_counter() - t0) * 1000.0
                record = self._build_record(
                    context.message.name,
                    goal,
                    raw,
                    result_obj,
                    status,
                    is_error,
                    error,
                    session_id,
                    dur_ms,
                )
                # Offload the blocking write (and any rollover) off the event
                # loop; the handler's own lock keeps each append atomic across
                # worker threads.
                await anyio.to_thread.run_sync(self.logger.write, record)
            except Exception:  # noqa: BLE001 - logging must never break the call
                pass

    # -- helpers ---------------------------------------------------------- #

    def _resolve_session(self, context: MiddlewareContext[Any]) -> str:
        fc = getattr(context, "fastmcp_context", None)
        if fc is not None:
            try:
                return fc.session_id
            except RuntimeError:
                pass
            except Exception:  # noqa: BLE001 - never let session read break us
                pass
        return self._proc_session

    def _extract_output(self, result: Any) -> Any:
        # Cap joined text to a bounded prefix so a huge result is not fully
        # materialized here before bounded_redact enforces the exact cap.
        cap = getattr(self.logger, "max_result_bytes", 8192)
        try:
            sc = getattr(result, "structured_content", None)
            if sc is not None:
                return sc
            content = getattr(result, "content", None)
            if content:
                texts: list[str] = []
                total = 0
                for block in content:
                    text = getattr(block, "text", None)
                    if text is None:
                        continue
                    texts.append(text)
                    total += len(text)
                    if total > cap * 2:  # generous prefix; capped exactly later
                        break
                if texts:
                    return "\n".join(texts)
            return None
        except Exception:  # noqa: BLE001
            return None

    def _extract_error_text(self, result: Any) -> str | None:
        try:
            out = self._extract_output(result)
            if isinstance(out, str):
                return out
            if out is not None:
                return json.dumps(out, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _result_shape(result_obj: Any) -> Any:
        """Content-free description of a result (used when log_results=False)."""
        if result_obj is None:
            return None
        try:
            if isinstance(result_obj, dict):
                return {"_type": "object", "_keys": len(result_obj)}
            if isinstance(result_obj, (list, tuple)):
                return {"_type": "array", "_items": len(result_obj)}
            if isinstance(result_obj, str):
                return {"_type": "string", "_bytes": len(result_obj.encode("utf-8"))}
            return {"_type": type(result_obj).__name__}
        except Exception:  # noqa: BLE001
            return {"_type": "unknown"}

    def _build_record(
        self,
        tool: str,
        goal: Any,
        arguments: dict[str, Any],
        result_obj: Any,
        status: str,
        is_error: bool,
        error: Any,
        session_id: str,
        dur_ms: float,
    ) -> dict[str, Any]:
        field_bytes = self.logger.max_field_bytes
        result_bytes = getattr(self.logger, "max_result_bytes", field_bytes)

        args_val, args_trunc = bounded_redact(arguments, field_bytes)
        # goal is model-authored free text that can restate the user's request
        # (incl. anything they pasted) -> redact + bound it like other fields.
        goal_val, _ = (
            bounded_redact(goal, field_bytes) if goal is not None else (None, False)
        )
        if self.log_results:
            res_val, res_trunc = bounded_redact(result_obj, result_bytes)
        else:
            res_val, res_trunc = self._result_shape(result_obj), False
        err_val, _ = (
            bounded_redact(error, field_bytes) if error is not None else (None, False)
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "tool": tool,
            "goal": goal_val,
            "arguments": args_val,
            "arguments_truncated": args_trunc,
            "result": res_val,
            "result_truncated": res_trunc,
            "result_logged": self.log_results,
            "status": status,
            "is_error": is_error,
            "error": err_val,
            "duration_ms": dur_ms,
            "transport": self.transport,
        }


def install_telemetry(mcp: Any, transport: str) -> TelemetryMiddleware | None:
    """Add the telemetry middleware to ``mcp`` iff collection mode is enabled.

    config is imported LAZILY (config.py raises ConfigError when VIYA_ENDPOINT
    is unset, which would otherwise make this module unimportable in tests)."""
    from . import config

    if not config.COLLECTION_MODE:
        return None
    try:
        logger = UsageLogger(
            path=os.path.expanduser(config.COLLECTION_LOG_PATH),
            max_log_bytes=config.COLLECTION_MAX_LOG_BYTES,
            backup_count=config.COLLECTION_LOG_BACKUPS,
            max_field_bytes=config.COLLECTION_MAX_FIELD_BYTES,
            max_result_bytes=config.COLLECTION_MAX_RESULT_BYTES,
        )
    except OSError as exc:
        module_logger.warning(
            "Collection mode requested but log path unusable (%s); "
            "telemetry disabled",
            exc,
        )
        return None  # server runs exactly as before
    mw = TelemetryMiddleware(
        logger,
        require_goal=config.COLLECTION_REQUIRE_GOAL,
        transport=transport,
        log_results=config.COLLECTION_LOG_RESULTS,
    )
    mcp.add_middleware(mw)
    return mw
