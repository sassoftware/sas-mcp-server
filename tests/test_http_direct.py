# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the direct-auth HTTP server (http_direct_server) — token caching,
API key middleware, and tool registration.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from fastmcp import Client

from sas_mcp_server import http_direct_server as hds


@pytest.fixture(autouse=True)
def reset_token_cache():
    hds._token_cache["token"] = ""
    hds._token_cache["expires_at"] = 0.0
    yield
    hds._token_cache["token"] = ""
    hds._token_cache["expires_at"] = 0.0


def _mock_token_response(token="tok-1", expires_in=3600):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"access_token": token,
                                        "expires_in": expires_in})
    return resp


def _mock_async_client(post_mock):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = post_mock
    return client


# ---------------------------------------------------------------------------
# Token caching
# ---------------------------------------------------------------------------


async def test_get_viya_token_fetches_via_password_grant():
    post = AsyncMock(return_value=_mock_token_response("tok-abc"))
    with patch.object(hds, "VIYA_USERNAME", "user"), \
         patch.object(hds, "VIYA_PASSWORD", "pass"), \
         patch.object(httpx, "AsyncClient",
                      return_value=_mock_async_client(post)):
        token = await hds.get_viya_token()

    assert token == "tok-abc"
    call = post.call_args
    assert call[0][0].endswith("/SASLogon/oauth/token")
    assert call[1]["data"]["grant_type"] == "password"
    assert call[1]["data"]["username"] == "user"


async def test_get_viya_token_is_cached():
    post = AsyncMock(return_value=_mock_token_response("tok-cached"))
    with patch.object(hds, "VIYA_USERNAME", "user"), \
         patch.object(hds, "VIYA_PASSWORD", "pass"), \
         patch.object(httpx, "AsyncClient",
                      return_value=_mock_async_client(post)):
        first = await hds.get_viya_token()
        second = await hds.get_viya_token()

    assert first == second == "tok-cached"
    assert post.call_count == 1


async def test_get_viya_token_refreshes_after_expiry():
    post = AsyncMock(side_effect=[
        _mock_token_response("tok-1", expires_in=30),  # below the margin
        _mock_token_response("tok-2"),
    ])
    with patch.object(hds, "VIYA_USERNAME", "user"), \
         patch.object(hds, "VIYA_PASSWORD", "pass"), \
         patch.object(httpx, "AsyncClient",
                      return_value=_mock_async_client(post)):
        first = await hds.get_viya_token()
        second = await hds.get_viya_token()

    assert first == "tok-1"
    assert second == "tok-2"
    assert post.call_count == 2


async def test_get_viya_token_requires_credentials():
    with patch.object(hds, "VIYA_USERNAME", ""), \
         patch.object(hds, "VIYA_PASSWORD", ""):
        with pytest.raises(hds.AuthenticationError):
            await hds.get_viya_token()


# ---------------------------------------------------------------------------
# API key middleware
# ---------------------------------------------------------------------------


async def _inner_ok_app(scope, receive, send):
    from starlette.responses import JSONResponse
    await JSONResponse({"ok": True})(scope, receive, send)


def _asgi_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_api_key_middleware_rejects_missing_key():
    app = hds.ApiKeyMiddleware(_inner_ok_app, "secret-key")
    async with _asgi_client(app) as client:
        resp = await client.get("/mcp")
    assert resp.status_code == 401


async def test_api_key_middleware_accepts_x_api_key_header():
    app = hds.ApiKeyMiddleware(_inner_ok_app, "secret-key")
    async with _asgi_client(app) as client:
        resp = await client.get("/mcp", headers={"X-API-Key": "secret-key"})
    assert resp.status_code == 200


async def test_api_key_middleware_accepts_bearer_token():
    app = hds.ApiKeyMiddleware(_inner_ok_app, "secret-key")
    async with _asgi_client(app) as client:
        resp = await client.get(
            "/mcp", headers={"Authorization": "Bearer secret-key"})
    assert resp.status_code == 200


async def test_api_key_middleware_rejects_wrong_key():
    app = hds.ApiKeyMiddleware(_inner_ok_app, "secret-key")
    async with _asgi_client(app) as client:
        resp = await client.get("/mcp", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


async def test_api_key_middleware_leaves_health_open():
    app = hds.ApiKeyMiddleware(_inner_ok_app, "secret-key")
    async with _asgi_client(app) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Server registration
# ---------------------------------------------------------------------------


async def test_all_tools_registered_on_direct_server():
    async with Client(hds.mcp) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
    assert "execute_sas_code" in names
    assert "list_reports" in names
    assert "create_report_from_template" in names
    assert "score_data" in names
