# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the HTTP-mode MCP server: auth middleware, health route, the
token getter, and the AuthenticationError type.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sas_mcp_server import mcp_server
from sas_mcp_server.mcp_server import AuthenticationError


def test_authentication_error():
    """AuthenticationError carries its message and renders a prefixed string."""
    error = AuthenticationError("Test error message")
    assert error.message == "Test error message"
    assert str(error) == "AuthenticationError: Test error message"


@pytest.mark.asyncio
async def test_lifespan_cleans_up_sessions_on_shutdown():
    """The HTTP server lifespan tears down warm compute sessions on exit."""
    with patch(
        "sas_mcp_server.mcp_server.shutdown_session_cache", new=AsyncMock()
    ) as mock_shutdown:
        async with mcp_server._lifespan(mcp_server.mcp):
            mock_shutdown.assert_not_awaited()
        mock_shutdown.assert_awaited_once()


# ---------------------------------------------------------------------------
# _http_get_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_get_token_present():
    ctx = MagicMock()
    ctx.get_state = AsyncMock(return_value="VIYATOK")
    assert await mcp_server._http_get_token(ctx) == "VIYATOK"


@pytest.mark.asyncio
async def test_http_get_token_missing_raises():
    ctx = MagicMock()
    ctx.get_state = AsyncMock(return_value=None)
    with pytest.raises(AuthenticationError):
        await mcp_server._http_get_token(ctx)


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_ok():
    resp = await mcp_server.health_check(MagicMock())
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["status"] == "healthy"
    assert body["service"] == "sas-viya-execution-mcp"


# ---------------------------------------------------------------------------
# AuthMiddleware.on_call_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_middleware_valid_bearer_sets_state():
    mw = mcp_server.AuthMiddleware()
    req = MagicMock()
    req.headers.get.return_value = "Bearer CLIENTJWT"
    info = MagicMock()
    info.token = "VIYATOK"
    ctx = MagicMock()
    ctx.fastmcp_context.set_state = AsyncMock()
    call_next = AsyncMock(return_value="RESULT")

    with patch("sas_mcp_server.mcp_server.get_http_request", return_value=req), \
         patch.object(mcp_server.viya_auth, "load_access_token",
                      AsyncMock(return_value=info)) as mock_load:
        result = await mw.on_call_tool(ctx, call_next)

    assert result == "RESULT"
    mock_load.assert_awaited_once_with("CLIENTJWT")
    ctx.fastmcp_context.set_state.assert_awaited_once_with("access_token", "VIYATOK")
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_auth_middleware_no_header_raises():
    mw = mcp_server.AuthMiddleware()
    req = MagicMock()
    req.headers.get.return_value = None
    call_next = AsyncMock()

    with patch("sas_mcp_server.mcp_server.get_http_request", return_value=req), pytest.raises(AuthenticationError):
        await mw.on_call_tool(MagicMock(), call_next)

    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_middleware_swap_failure_does_not_set_state():
    mw = mcp_server.AuthMiddleware()
    req = MagicMock()
    req.headers.get.return_value = "Bearer X"
    ctx = MagicMock()
    ctx.fastmcp_context.set_state = AsyncMock()
    call_next = AsyncMock(return_value="R")

    with patch("sas_mcp_server.mcp_server.get_http_request", return_value=req), \
         patch.object(mcp_server.viya_auth, "load_access_token",
                      AsyncMock(return_value=None)):
        result = await mw.on_call_tool(ctx, call_next)

    assert result == "R"
    ctx.fastmcp_context.set_state.assert_not_awaited()
    call_next.assert_awaited_once_with(ctx)
