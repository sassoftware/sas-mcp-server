# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Agent action audit trail.

Every MCP tool invocation is recorded by :class:`AuditMiddleware` (a FastMCP
middleware, so no per-tool code) into:

1. a local JSONL file (``AUDIT_JSONL_DIR/agent_audit_{date}.jsonl``) — the
   durable, append-only record and the event bus the demo presenter daemon
   tails; and
2. a global-scope CAS table (``AUDIT_CASLIB.AUDIT_CAS_TABLE``) — the mirror a
   viewer opens in SAS Studio / Data Explorer during the demo. At demo scale
   (tens of rows) the mirror is refreshed by re-uploading the full CSV,
   debounced a few seconds after the last tool call; a lock serializes flushes.

Platform-level auditing is separate and automatic: every Viya REST call the
server makes is already recorded by the Viya audit service and visible to
admins in SAS Environment Manager → Audit. This module only adds the
tool-level trail (which tool, which params, what it returned, where to look).

All sinks are fail-soft: an audit failure logs a warning and never fails the
tool call being audited.
"""

import asyncio
import csv
import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .config import (
    AUDIT_CAS_MIRROR,
    AUDIT_CAS_TABLE,
    AUDIT_CASLIB,
    AUDIT_JSONL_DIR,
    VIYA_ENDPOINT,
)
from .viya_client import logger, make_client

# Argument values under these key substrings are masked in the audit record.
_SECRET_KEY_PARTS = ("token", "secret", "password", "authorization")
# Long free-text arguments (SAS code bodies, CSV payloads) are truncated.
_MAX_VALUE_CHARS = 2000

# Session-lifetime state for the CAS mirror.
_rows: list[dict[str, str]] = []
_flush_task: asyncio.Task | None = None
_flush_lock = asyncio.Lock()
_cas_server: str | None = None
_FLUSH_DELAY_S = 5.0

_CSV_COLUMNS = ["ts", "tool", "status", "duration_ms", "dry_run", "params", "urls"]


def _sanitize(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _sanitize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, key) for v in value[:50]]
    if isinstance(value, str):
        if any(part in key.lower() for part in _SECRET_KEY_PARTS):
            return "***"
        if len(value) > _MAX_VALUE_CHARS:
            return value[:_MAX_VALUE_CHARS] + f"... [{len(value)} chars total]"
    return value


def _harvest_urls(obj: Any, found: list[str]) -> None:
    """Collect ref_url / api_url values from a tool's structured result."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("ref_url", "api_url") and isinstance(v, str):
                found.append(v)
            else:
                _harvest_urls(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _harvest_urls(item, found)


def jsonl_path(when: datetime | None = None) -> Path:
    d = (when or datetime.now(UTC)).strftime("%Y%m%d")
    return Path(AUDIT_JSONL_DIR) / f"agent_audit_{d}.jsonl"


def _append_jsonl(record: dict[str, Any]) -> None:
    path = jsonl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _rows_as_csv() -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(_rows)
    return buf.getvalue()


async def _resolve_cas_server(client) -> str:
    global _cas_server
    if _cas_server:
        return _cas_server
    resp = await client.get(
        f"{VIYA_ENDPOINT}/casManagement/servers", params={"limit": 1}
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        raise RuntimeError("No CAS server visible to this identity.")
    server = str(items[0].get("name") or items[0].get("id"))
    _cas_server = server
    return server


async def _flush_to_cas(token: str | None) -> None:
    """Replace the CAS mirror table with the current in-memory rows.

    *token* is the caller's already-resolved Viya access token — there is no
    module-level auth client to fall back on, since access tokens live only in
    per-request FastMCP context state (set by ``AuthMiddleware``).
    """
    async with _flush_lock:
        if not _rows:
            return
        async with make_client(token) as client:
            server = await _resolve_cas_server(client)
            base = (f"{VIYA_ENDPOINT}/casManagement/servers/{server}"
                    f"/caslibs/{AUDIT_CASLIB}/tables")
            # Unload the previous mirror. DELETE does not remove a loaded
            # global table (400 on this deployment) — the state endpoint does.
            resp = await client.put(
                f"{base}/{AUDIT_CAS_TABLE}/state",
                params={"value": "unloaded"}, headers={"Accept": "*/*"},
            )
            if resp.status_code not in (200, 201, 404):
                logger.warning("Audit mirror: unexpected %s unloading %s",
                               resp.status_code, AUDIT_CAS_TABLE)
            resp = await client.post(
                base,
                data={"tableName": AUDIT_CAS_TABLE, "format": "csv",
                      "containsHeaderRow": "true", "scope": "global"},
                files={"file": ("audit.csv", _rows_as_csv().encode("utf-8"),
                                "text/csv")},
            )
            resp.raise_for_status()
            body = resp.json()
            # The upload may land session-scoped depending on the deployment;
            # promote so any SAS Studio session can open it.
            if body.get("scope") != "global":
                promote = await client.put(
                    f"{base}/{AUDIT_CAS_TABLE}/state",
                    params={"value": "loaded", "scope": "global"},
                    headers={"Accept": "*/*"},
                )
                if promote.status_code >= 400:
                    logger.warning("Audit mirror: promote failed (%s)",
                                   promote.status_code)
            logger.info("Audit mirror refreshed: %s.%s (%d rows)",
                        AUDIT_CASLIB, AUDIT_CAS_TABLE, len(_rows))


def _schedule_flush(token: str | None) -> None:
    """Debounce: (re)start a timer that flushes after the tools go quiet."""
    global _flush_task

    async def _later() -> None:
        try:
            await asyncio.sleep(_FLUSH_DELAY_S)
            await _flush_to_cas(token)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — audit must never break tools
            logger.warning("Audit CAS mirror flush failed", exc_info=True)

    if _flush_task and not _flush_task.done():
        _flush_task.cancel()
    _flush_task = asyncio.create_task(_later())


def record_action(
    tool: str,
    arguments: dict[str, Any] | None,
    status: str,
    duration_ms: int,
    urls: list[str],
    token: str | None = None,
) -> dict[str, Any]:
    """Build, persist (JSONL), and enqueue (CAS mirror) one audit record."""
    args = _sanitize(arguments or {})
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "tool": tool,
        "status": status,
        "duration_ms": duration_ms,
        "dry_run": str((arguments or {}).get("dry_run", "")),
        "params": args,
        "urls": urls,
    }
    try:
        _append_jsonl(record)
    except OSError:
        logger.warning("Audit JSONL append failed", exc_info=True)
    if AUDIT_CAS_MIRROR:
        _rows.append({
            **{k: str(record[k]) for k in ("ts", "tool", "status",
                                           "duration_ms", "dry_run")},
            "params": json.dumps(args, ensure_ascii=False, default=str)[:2000],
            "urls": " ".join(urls)[:1000],
        })
        _schedule_flush(token)
    return record


class AuditMiddleware(Middleware):
    """Record every tool invocation; one choke point for all ~80 tools."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        tool = getattr(context.message, "name", "?")
        arguments = getattr(context.message, "arguments", None) or {}
        start = time.monotonic()
        status = "ok"
        urls: list[str] = []
        try:
            result = await call_next(context)
            structured = getattr(result, "structured_content", None)
            _harvest_urls(structured, urls)
            return result
        except Exception as exc:
            status = f"error: {type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            try:
                fastmcp_ctx = context.fastmcp_context
                token = (
                    await fastmcp_ctx.get_state("access_token")
                    if fastmcp_ctx is not None else None
                )
                record_action(
                    tool, arguments, status,
                    int((time.monotonic() - start) * 1000), urls,
                    token=token,
                )
            except Exception:  # noqa: BLE001 — audit must never break tools
                logger.warning("Audit record failed for %s", tool, exc_info=True)


def register_audit_tools(mcp: FastMCP) -> None:
    """Register audit utility tools on *mcp*."""

    @mcp.tool()
    async def flush_audit_log(
        ctx: Context, folder_path: str | None = None
    ) -> dict[str, Any]:
        """Flush the audit trail: push pending rows to the CAS mirror table and
        export today's JSONL audit log into the SAS Content demo folder.

        Call at the end of a demo session so the complete trail is visible both
        as a CAS table (SAS Studio / Data Explorer) and as a .log file in the
        SAS Content tree.

        Args:
            folder_path: SAS Content folder for the export (defaults to the
                configured DEMO_FOLDER_PATH).
        """
        from .config import DEMO_FOLDER_PATH
        from .content import save_text_file

        logger.info("--- TOOL USED: flush_audit_log ---")
        token = await ctx.get_state("access_token")
        result: dict[str, Any] = {"rows_in_mirror": len(_rows)}
        try:
            await _flush_to_cas(token)
            result["cas_table"] = f"{AUDIT_CASLIB}.{AUDIT_CAS_TABLE}"
        except Exception as exc:  # noqa: BLE001 — report, don't fail
            result["cas_mirror_error"] = str(exc)[:300]

        path = jsonl_path()
        if path.exists():
            target = folder_path or DEMO_FOLDER_PATH
            async with make_client(token) as client:
                saved = await save_text_file(
                    client, target, "agent_audit.log",
                    path.read_text(encoding="utf-8"),
                )
            result["exported_to"] = f"{target}/{saved['name']}"
        else:
            result["exported_to"] = None
        return {"status": "ok", **result}
