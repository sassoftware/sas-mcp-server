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

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastmcp import Context

from ..viya_client import logger, make_client
from ..viya_utils import get_cached_session


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
