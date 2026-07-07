# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Redaction, bounded serialization, path resolution, and a synchronous JSONL
writer for opt-in collection-mode telemetry. Deliberately free of any fastmcp
imports so its logic can be exercised in a bare unit environment.

``bounded_redact`` is the load-bearing addition: it redacts AND caps size/work
in a single pass, so an arbitrarily large tool result (e.g. a 25 MiB base64
export or a million-row table) is never fully materialized, deep-scanned, or
double-serialized on the event loop, and object type is preserved on
truncation (keeping JSONL columns object-typed for DuckDB/pandas)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

module_logger = logging.getLogger(__name__)

GOAL_KEY = "goal"

REDACT_KEY_RE = re.compile(
    r"(?i)(token|password|passwd|pwd|secret|authorization|bearer|credential"
    r"|api[_-]?key|refresh[_-]?token|access[_-]?token)"
)

_BEARER_RE = re.compile(r"Bearer\s+\S+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9._-]{20,}")  # JWT-like

_REDACTED = "[REDACTED]"
_TRUNC_MARK = "…<truncated>"


def _scrub_str(s: str) -> str:
    return _JWT_RE.sub(_REDACTED, _BEARER_RE.sub(_REDACTED, s))


def redact(obj: Any) -> Any:
    """Recursively mask secret-looking dict keys and inline-scrub JWT/Bearer
    strings. Leaves other scalars alone. Never raises.

    UNBOUNDED convenience/back-compat helper (unit-tested directly). The hot
    path uses ``bounded_redact`` instead, which also caps work and size.
    """
    try:
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and REDACT_KEY_RE.search(k):
                    out[k] = _REDACTED
                else:
                    out[k] = redact(v)
            return out
        if isinstance(obj, (list, tuple)):
            return [redact(v) for v in obj]
        if isinstance(obj, str):
            return _scrub_str(obj)
        return obj
    except Exception:  # noqa: BLE001 - redaction must never break logging
        return _REDACTED


def truncate(value: Any, max_bytes: int) -> tuple[Any, bool]:
    """Cap a field's serialized form at ``max_bytes`` utf-8 bytes.

    Returns (value_or_clipped_string, truncated_flag). Kept for back-compat and
    direct unit tests; ``bounded_redact`` is the size-and-work bounded
    replacement used on the hot path.
    """
    try:
        if value is None:
            return None, False
        if isinstance(value, (dict, list)):
            encoded = json.dumps(value, default=str, ensure_ascii=False)
        elif isinstance(value, str):
            encoded = value
        else:
            encoded = str(value)
        raw = encoded.encode("utf-8")
        if len(raw) <= max_bytes:
            return value, False
        clipped = raw[:max_bytes].decode("utf-8", errors="ignore")
        return f"{clipped}…<truncated {len(raw)} bytes>", True
    except Exception:  # noqa: BLE001
        return _REDACTED, True


class _Budget:
    """Mutable byte budget shared across a single bounded_redact traversal."""

    __slots__ = ("remaining", "exceeded")

    def __init__(self, total: int) -> None:
        self.remaining = max(0, int(total))
        self.exceeded = False

    def take(self, n: int) -> None:
        self.remaining -= n
        if self.remaining < 0:
            self.exceeded = True


def _br(obj: Any, b: _Budget) -> Any:
    if obj is None:
        return None
    if isinstance(obj, str):
        # Clip cheaply (by chars; len() is O(1)) BEFORE the scrub regex runs,
        # so a 25 MiB blob is never scanned — only a bounded prefix is.
        n = len(obj)
        if n > b.remaining:
            clip = obj[: b.remaining] if b.remaining > 0 else ""
            b.take(n)  # marks exceeded
            return (_scrub_str(clip) + _TRUNC_MARK) if clip else _TRUNC_MARK
        b.take(n)
        return _scrub_str(obj)
    if isinstance(obj, bool):  # bool before int
        b.take(5)
        return obj
    if isinstance(obj, (int, float)):
        b.take(8)
        return obj
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if b.remaining <= 0:
                b.exceeded = True
                out["_truncated"] = True
                break
            ks = k if isinstance(k, str) else str(k)
            b.take(len(ks) + 3)
            if isinstance(k, str) and REDACT_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = _br(v, b)
        return out
    if isinstance(obj, (list, tuple)):
        out_l: list[Any] = []
        for v in obj:
            if b.remaining <= 0:
                b.exceeded = True
                out_l.append(_TRUNC_MARK)
                break
            b.take(1)
            out_l.append(_br(v, b))
        return out_l
    # Other scalars: stringify (bounded) and scrub.
    return _br(str(obj), b)


def bounded_redact(obj: Any, max_bytes: int) -> tuple[Any, bool]:
    """Redact AND bound size/work in a single pass. Returns (value, truncated).

    * Strings are clipped to the remaining budget BEFORE the JWT/Bearer scrub
      regex runs, so an arbitrarily large field (e.g. a 25 MiB base64 export)
      is never fully materialized or CPU-scanned on the event loop.
    * dict/list recursion carries a shared byte budget and stops as soon as it
      is exhausted (a million-row result is bounded to a prefix).
    * Object type is PRESERVED — a truncated container returns a dict/list
      (marked with ``_truncated``), never a bare string — so JSONL columns stay
      object-typed for DuckDB/pandas analysis.
    """
    try:
        b = _Budget(max_bytes)
        value = _br(obj, b)
        return value, b.exceeded
    except Exception:  # noqa: BLE001 - redaction must never break logging
        return _REDACTED, True


def _win_harden(path: Path) -> None:
    """Best-effort Windows ACL lock-down (POSIX chmod is a no-op on win32).

    Removes inherited ACEs and grants only the current user Full control, so a
    log that may contain SAS code, table rows, and PII is not group/world
    readable on shared or roaming-profile hosts. Silently best-effort.

    Only ever call this on a path we own the lifecycle of — the log FILE itself,
    or a directory this process just created. Never call it on a pre-existing,
    potentially shared directory: ``/inheritance:r`` strips inheritance so that
    NEW sibling subdirectories inherit an empty DACL and become unusable.
    """
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    with contextlib.suppress(Exception):
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            check=False,
            timeout=5,
        )


def resolve_log_path(raw: str) -> Path:
    """Expand ~, create the parent dir, and tighten perms ONLY on a directory
    this call creates itself.

    We must never re-permission a directory that already exists. If a user points
    ``COLLECTION_LOG_PATH`` at a shared or standard location (e.g. ``~`` or the
    system temp dir), chmod-ing it to 0700 (POSIX) or stripping its ACL
    inheritance (Windows, via ``_win_harden``) would damage a directory shared
    with other software. On Windows this is especially harmful: once inheritance
    is stripped, sibling subdirectories created afterwards inherit an empty DACL
    and become unusable. So we lock down the leaf parent only when we just created
    it; a pre-existing parent is left untouched. The log file itself is always
    hardened separately (see ``UsageLogger.__init__``).

    NOTE: os.chmod(0o700/0o600) is effectively a NO-OP on Windows; icacls is
    applied there instead. See .env.sample for the shared-host caveat.
    """
    path = Path(os.path.expanduser(raw))
    parent = path.parent
    created_parent = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if created_parent:
        # We own this directory's lifecycle, so it is safe to lock it to the
        # current user; a directory we did not create is left as-is.
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)
        _win_harden(parent)
    return path


class UsageLogger:
    """Synchronous JSONL writer backed by a RotatingFileHandler.

    The stdlib handler acquires an internal lock per emit, so concurrent tool
    calls — including from multiple worker threads (the middleware offloads
    write() via anyio.to_thread) — cannot interleave partial lines: one record
    == one atomic newline-terminated append. utf-8, NO BOM (avoids the Windows
    / PowerShell UTF-16+BOM trap). ``write`` never raises.
    """

    def __init__(
        self,
        path: str,
        *,
        max_log_bytes: int,
        backup_count: int,
        max_field_bytes: int,
        max_result_bytes: int | None = None,
    ) -> None:
        self.path = resolve_log_path(path)
        self.max_field_bytes = max_field_bytes
        self.max_result_bytes = (
            max_result_bytes if max_result_bytes is not None else max_field_bytes
        )
        self._logger = logging.getLogger(
            f"sas_mcp_server.usage_logger.instance.{id(self)}"
        )
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = RotatingFileHandler(
            filename=str(self.path),
            maxBytes=max_log_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.terminator = "\n"
        handler.setFormatter(logging.Formatter("%(message)s"))  # raw line only
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)  # no-op on Windows; icacls covers win32
        # Lock the log file to the current user on Windows too. Hardening the
        # file (not just the dir) keeps the privacy guarantee even when the
        # parent dir pre-existed and was therefore intentionally left untouched
        # by resolve_log_path. Safe: it only affects this one file.
        _win_harden(self.path)
        self._logger.handlers = [handler]

    @classmethod
    def from_config(
        cls,
        *,
        path: str,
        max_log_bytes: int,
        backup_count: int,
        max_field_bytes: int,
        max_result_bytes: int | None = None,
    ) -> UsageLogger:
        return cls(
            path,
            max_log_bytes=max_log_bytes,
            backup_count=backup_count,
            max_field_bytes=max_field_bytes,
            max_result_bytes=max_result_bytes,
        )

    def write(self, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(record, default=str, ensure_ascii=False)
            self._logger.info(line)
        except Exception as exc:  # noqa: BLE001 - logging must NEVER propagate
            module_logger.debug("usage log write failed: %s", exc)
