# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared per-request session helpers for the tiered tool modules.

Every tier reuses the same two context managers to open an authenticated Viya
client. They depend on the server's ``get_token`` callable, so
:func:`make_session_helpers` binds it once and returns the pair; each tier's
``register(mcp, get_token)`` unpacks them at the top of its body, leaving the
individual tool bodies calling ``viya_session(...)`` /
``compute_tool_session(...)`` exactly as they did in the monolithic module. This
is the single shared dependency of the tiers — no tier imports another, so any
subset of tiers can be registered on its own.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import Context

from ..viya_client import logger, make_client
from ..viya_utils import get_cached_session

# --- tolerant input coercion -------------------------------------------------
# Some MCP clients (observed live: Claude Cowork) serialize optional list/dict
# parameters as JSON-ENCODED STRINGS ('[{"addData": ...}]' instead of the
# array), and the model cannot fix that from its side — pydantic rejects the
# call before the tool body runs. These BeforeValidator coercions absorb the
# whole failure class server-side without changing the published JSON schema
# (BeforeValidator does not alter the annotated type's schema).


def coerce_json_list(value: Any) -> Any:
    """Parse a JSON-encoded string into the list it encodes."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "expected a JSON array (got an unparseable string). Pass the value as a "
                "real array, not a quoted JSON string."
            ) from exc
        if not isinstance(parsed, list):
            # ValueError, not TypeError: pydantic only converts ValueError into
            # a clean validation error inside a BeforeValidator.
            raise ValueError("expected a JSON array; the string decodes to a non-array value.")  # noqa: TRY004
        return parsed
    return value


def coerce_str_or_json_list(value: Any) -> Any:
    """Like :func:`coerce_json_list`, but a bare string becomes a 1-element list.

    ``report_objects='["Overview"]'`` and ``report_objects='Overview'`` were both
    observed live; each has exactly one sensible reading. A string that merely
    LOOKS like JSON but isn't (a label such as ``'[Draft] Overview'``) is a
    label, and a double-encoded scalar (``'"Overview"'``) unwraps once.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return [value]  # a label that happens to start with '['
            if isinstance(parsed, list):
                return parsed
            return [value]
        if text.startswith('"'):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return [value]
            if isinstance(parsed, str):
                return [parsed]
        return [value]
    return value


def coerce_json_dict(value: Any) -> Any:
    """Parse a JSON-encoded string into the object it encodes."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "expected a JSON object (got an unparseable string). Pass the value as a "
                "real object, not a quoted JSON string."
            ) from exc
        if not isinstance(parsed, dict):
            # ValueError, not TypeError: see coerce_json_list.
            raise ValueError("expected a JSON object; the string decodes to a non-object value.")  # noqa: TRY004
        return parsed
    return value


def make_session_helpers(get_token: Callable[[Context], Awaitable[str]]):
    """Build the ``(viya_session, compute_tool_session)`` context managers.

    Both close over *get_token* — the ``async def get_token(ctx) -> str`` the
    server passes to ``register_tools`` — so tool bodies need no token plumbing.
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

    return viya_session, compute_tool_session
