# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pure record-shaping helpers for collection-mode telemetry.

Everything here is a free function with no middleware state, so it can be
tested without building a server: classification, redaction, fingerprinting and
identity extraction. :mod:`sas_mcp_server.telemetry` keeps only the middleware
itself — the request lifecycle, the sequence counter and the header cadence.

Nothing in this module imports ``config``; the middleware stays importable
without ``VIYA_ENDPOINT`` set.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..usage_logger import bounded_redact
from .telemetry_registry import (
    CLIENT_FIELD_MAX_BYTES,
    ERROR_TYPE_RULES,
    GOAL_IN_RAW_RE,
    HTTP_STATUS_RE,
    TOOL_FAILURE_PREFIXES,
    TOOL_FAILURE_STATUSES,
    TOOL_FAILURE_SUFFIXES,
    TOOL_MESSAGE_MAX_BYTES,
)

_HOST_MASK = "[viya-host]"


def classify_error(text: Any) -> tuple[str | None, int | None]:
    """Map an error string to ``(error_type, http_status)``.

    Errors are logged as free text, so counting "how many 404s" or "how many
    client-side validation failures" otherwise means re-deriving a regex per
    analysis. Returns ``(None, None)`` when there is no error text.
    """
    if not isinstance(text, str) or not text:
        return None, None
    status: int | None = None
    match = HTTP_STATUS_RE.search(text)
    if match:
        try:
            status = int(match.group(1))
        except ValueError:
            status = None
    for pattern, label in ERROR_TYPE_RULES:
        if pattern.search(text):
            return label, status
    return "unknown", status


def is_tool_failure_status(status: Any) -> bool:
    """True when a tool-declared ``status`` string means failure."""
    return isinstance(status, str) and (
        status.startswith(TOOL_FAILURE_PREFIXES)
        or status.endswith(TOOL_FAILURE_SUFFIXES)
        or status in TOOL_FAILURE_STATUSES
    )


def rescue_unparsed_input(raw: dict[str, Any]) -> tuple[dict[str, Any], bool, Any]:
    """Unwrap a ``{"__unparsedToolInput": {"raw": "<json>"}}`` envelope.

    Returns (arguments, rescued?, salvaged_goal). When the blob parses to a
    dict, the call proceeds with the real arguments; when it does not, the
    goal is still salvaged from the raw text so trace coverage stays whole.
    """
    envelope = raw.get("__unparsedToolInput")
    if set(raw) != {"__unparsedToolInput"} or not isinstance(envelope, dict):
        return raw, False, None
    blob = envelope.get("raw")
    if not isinstance(blob, str):
        return raw, False, None
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed, True, None
    match = GOAL_IN_RAW_RE.search(blob)
    salvaged = None
    if match:
        try:
            salvaged = json.loads(f'"{match.group(1)}"')
        except (json.JSONDecodeError, ValueError):
            salvaged = match.group(1)
    return raw, False, salvaged


def result_shape(result_obj: Any) -> Any:
    """Content-free description of a result (used when results aren't logged).

    Key NAMES are schema, not data — and unlike a bare key count they
    distinguish an ``applied`` result from an ``apply_failed`` one.
    """
    if result_obj is None:
        return None
    try:
        if isinstance(result_obj, dict):
            keys = [k if isinstance(k, str) else str(k) for k in list(result_obj)[:24]]
            return {"_type": "object", "_keys": keys}
        if isinstance(result_obj, (list, tuple)):
            return {"_type": "array", "_items": len(result_obj)}
        if isinstance(result_obj, str):
            return {"_type": "string", "_bytes": len(result_obj.encode("utf-8"))}
        return {"_type": type(result_obj).__name__}
    except Exception:  # noqa: BLE001
        return {"_type": "unknown"}


def args_hash(arguments: dict[str, Any]) -> str:
    """Stable 12-hex fingerprint of the (goal-stripped) arguments.

    Exact-retry runs — the loops a trace analysis needs to count — become
    a one-line groupby instead of ad-hoc rehashing.
    """
    try:
        canonical = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        canonical = str(arguments)
    return hashlib.sha256(canonical.encode("utf-8", errors="ignore")).hexdigest()[:12]


def scrub_host(text: Any, viya_host: str | None) -> Any:
    """Mask the Viya host in free-text fields (raw HTTP errors embed it)."""
    if not isinstance(text, str) or not viya_host:
        return text
    return text.replace(viya_host, _HOST_MASK)


def scrub_host_deep(value: Any, viya_host: str | None) -> Any:
    """Recursively mask the Viya host in a (bounded) logged result.

    Full results in failures/always mode quote raw VA bodies and carry
    open_url — scrubbing only error/tool_message would leak the host in
    the very field the other two scrubbed. Runs on ALREADY-BOUNDED values
    (<= max_result_bytes), so the recursion is cheap.
    """
    if not viya_host:
        return value
    if isinstance(value, str):
        return value.replace(viya_host, _HOST_MASK)
    if isinstance(value, dict):
        return {k: scrub_host_deep(v, viya_host) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_host_deep(v, viya_host) for v in value]
    return value


def tool_outcome(result_obj: Any, viya_host: str | None) -> dict[str, Any]:
    """Tool-DECLARED outcome fields from a structured result.

    This repo's tools return failures as data ({"status": "apply_failed",
    ...}), which the MCP layer records as success — without these fields
    every VA rejection in a trace looks like a win.
    """
    if not isinstance(result_obj, dict):
        return {}
    out: dict[str, Any] = {}
    tool_status = result_obj.get("status")
    if isinstance(tool_status, str):
        out["tool_status"] = tool_status
        out["is_tool_error"] = is_tool_failure_status(tool_status)
        message = result_obj.get("message")
        if isinstance(message, str) and out["is_tool_error"]:
            # Scrub BEFORE bounding, or a host cut at the cap boundary
            # would partially leak.
            out["tool_message"], _ = bounded_redact(
                scrub_host(message, viya_host), TOOL_MESSAGE_MAX_BYTES
            )
        for key in ("failed_operation_index", "error_count"):
            value = result_obj.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                out[key] = value
    return out


def sanitize_client(value: str | None) -> str | None:
    """Redact + bound one client identity string.

    clientInfo comes straight off the handshake, i.e. from the caller, and
    .env.sample promises every field is capped and Bearer/JWT-scrubbed.
    Capped tighter than max_field_bytes: this rides on EVERY record, so a
    client that sends kilobytes must not eat the rotation budget.
    """
    if not value:
        return None
    out, _ = bounded_redact(value, CLIENT_FIELD_MAX_BYTES)
    return out if isinstance(out, str) and out else None


def raw_client_info(context: Any) -> tuple[str | None, str | None]:
    """Best-effort ``(name, version)`` off the MCP initialize handshake.

    Returns the RAW values (the caller sanitizes and caches them). Never
    raises: ``(None, None)`` whenever the handshake is unreachable, which a
    sessionless transport makes routine — absent is expected, not a defect.
    """
    try:
        fc = getattr(context, "fastmcp_context", None)
        session = getattr(getattr(fc, "request_context", None), "session", None)
        info = getattr(getattr(session, "client_params", None), "clientInfo", None)
        if info is None:
            return None, None
        name = getattr(info, "name", None)
        version = getattr(info, "version", None)
        return (
            name if isinstance(name, str) and name else None,
            version if isinstance(version, str) and version else None,
        )
    except Exception:  # noqa: BLE001 - identity is best-effort
        return None, None


def server_version() -> str | None:
    """The running server's version.

    ``importlib.metadata`` reports the *installed distribution* metadata,
    which goes stale under an editable install — real logs carried ``1.2.0``
    while the checkout was several releases newer, making the field worse than
    useless for correlating behaviour to a version. A source checkout's
    ``pyproject.toml`` is authoritative when present, so it wins; installed
    deployments (no pyproject alongside the package) fall through to metadata.
    A source bundle with neither (Posit Connect deploys the package directory
    without installing it) names its version in ``SAS_MCP_VERSION``.
    """
    try:
        import tomllib
        from pathlib import Path

        # helpers/telemetry_helpers.py -> src/sas_mcp_server -> src -> repo root
        pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as fh:
                found = tomllib.load(fh).get("project", {}).get("version")
            if isinstance(found, str) and found:
                return found
    except Exception:  # noqa: BLE001 - fall through to installed metadata
        pass
    try:
        from importlib.metadata import version

        return version("sas-mcp-server")
    except Exception:  # noqa: BLE001
        import os

        return os.getenv("SAS_MCP_VERSION", "").strip() or None
