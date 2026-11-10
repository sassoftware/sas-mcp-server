# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for Posit Connect mode (POSIT_CONNECT_OAUTH): the session-token to
Viya-token exchange, the config guards, and a server built in that mode."""

import importlib
import json
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest

from sas_mcp_server import posit_connect
from sas_mcp_server.exceptions import AuthenticationError
from sas_mcp_server.landing import ServerFacts, render_page

CONNECT = "https://connect.example.com"


@pytest.fixture
def connect_env(monkeypatch):
    monkeypatch.setenv("CONNECT_SERVER", CONNECT + "/")
    monkeypatch.setenv("CONNECT_API_KEY", "content-key")
    monkeypatch.setenv("CONNECT_CONTENT_GUID", "abc-123")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- exchange_session_token ---------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_posts_rfc8693_request_with_content_key(connect_env):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "VIYA", "token_type": "Bearer"})

    async with _client(handler) as client:
        token = await posit_connect.exchange_session_token("SESSION", client=client)

    assert token == "VIYA"
    (req,) = seen
    assert str(req.url) == f"{CONNECT}/__api__/v1/oauth/integrations/credentials"
    assert req.headers["Authorization"] == "Key content-key"
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert form == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token_type": "urn:posit:connect:user-session-token",
        "subject_token": "SESSION",
    }


@pytest.mark.asyncio
async def test_exchange_sends_audience_when_given(connect_env):
    forms: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        forms.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "VIYA"})

    async with _client(handler) as client:
        await posit_connect.exchange_session_token("S", audience="integ-guid", client=client)
    assert forms[0]["audience"] == ["integ-guid"]


@pytest.mark.asyncio
async def test_exchange_refusal_quotes_connects_reason_and_public_url(connect_env):
    body = {"code": 212, "error": "No OAuth session found. Log in to the Integration before requesting credentials."}
    async with _client(lambda r: httpx.Response(403, json=body)) as client:
        with pytest.raises(AuthenticationError) as exc:
            await posit_connect.exchange_session_token(
                "S", content_url="https://posit.example.com/connect/sas-mcp/", client=client
            )
    message = str(exc.value)
    assert "HTTP 403" in message
    assert "Connect API error 212: No OAuth session found" in message
    assert "open https://posit.example.com/connect/sas-mcp/ in a browser" in message
    # CONNECT_SERVER is Connect's internal address; it must never be offered as a link
    assert "connect.example.com" not in message


@pytest.mark.asyncio
async def test_exchange_refusal_without_json_body_still_reports(connect_env):
    async with _client(lambda r: httpx.Response(403, text="Forbidden")) as client:
        with pytest.raises(AuthenticationError, match=r"HTTP 403\): Forbidden") as exc:
            await posit_connect.exchange_session_token("S", client=client)
    assert "content URL on Posit Connect" in str(exc.value)


@pytest.mark.parametrize(
    ("header", "path", "expected"),
    [
        ("https://posit.example.com/connect/sas-mcp/mcp", "/mcp", "https://posit.example.com/connect/sas-mcp/"),
        ("https://posit.example.com/connect/content/abc/mcp/?x=1", "/mcp/", "https://posit.example.com/connect/content/abc/"),
        ("https://connect.example.org/mcp", "/mcp", "https://connect.example.org/"),
        (None, "/mcp", None),
        ("not a url", "/mcp", None),
    ],
)
def test_public_content_url(header, path, expected):
    assert posit_connect.public_content_url(header, path) == expected


@pytest.mark.asyncio
async def test_exchange_without_access_token_is_an_error(connect_env):
    async with _client(lambda r: httpx.Response(200, json={"token_type": "Bearer"})) as client:
        with pytest.raises(AuthenticationError, match="no access_token"):
            await posit_connect.exchange_session_token("S", client=client)


@pytest.mark.asyncio
async def test_exchange_network_failure_is_an_auth_error(connect_env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(AuthenticationError, match="Could not reach Posit Connect"):
            await posit_connect.exchange_session_token("S", client=client)


@pytest.mark.asyncio
async def test_exchange_outside_connect_says_so(monkeypatch):
    monkeypatch.delenv("CONNECT_SERVER", raising=False)
    monkeypatch.delenv("CONNECT_API_KEY", raising=False)
    with pytest.raises(AuthenticationError, match="CONNECT_SERVER"):
        await posit_connect.exchange_session_token("S")


# --- token getter -------------------------------------------------------------


def _request_with(headers: dict[str, str], path: str = "/mcp"):
    return SimpleNamespace(headers=httpx.Headers(headers), url=SimpleNamespace(path=path))


@pytest.mark.asyncio
async def test_getter_exchanges_the_forwarded_session_token():
    getter = posit_connect.make_token_getter("integ-guid")
    exchange = AsyncMock(return_value="VIYA")
    headers = {
        "posit-connect-user-session-token": "SESSION",
        "x-rsc-request": "https://posit.example.com/connect/sas-mcp/mcp",
    }
    with patch.object(posit_connect, "get_http_request", return_value=_request_with(headers)), \
         patch.object(posit_connect, "exchange_session_token", exchange):
        assert await getter(AsyncMock()) == "VIYA"
    exchange.assert_awaited_once_with(
        "SESSION", audience="integ-guid", content_url="https://posit.example.com/connect/sas-mcp/"
    )


@pytest.mark.asyncio
async def test_getter_without_session_header_raises():
    getter = posit_connect.make_token_getter()
    with patch.object(posit_connect, "get_http_request", return_value=_request_with({})), \
         pytest.raises(AuthenticationError, match="Posit-Connect-User-Session-Token"):
        await getter(AsyncMock())


# --- config guards ------------------------------------------------------------


def _reload_config():
    return importlib.reload(sys.modules["sas_mcp_server.config"])


@pytest.fixture
def restore_config(monkeypatch):
    """Reload config back to the default (proxy) mode after the test."""
    yield
    for name in ("POSIT_CONNECT_OAUTH", "ALLOW_RAW_BEARER", "VIYA_AUTH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VIYA_ENDPOINT", os.environ.get("VIYA_ENDPOINT") or "https://test.viya.com")
    with patch("dotenv.load_dotenv"):
        _reload_config()


def test_connect_mode_builds_no_oauth_proxy(monkeypatch, restore_config):
    monkeypatch.setenv("POSIT_CONNECT_OAUTH", "true")
    monkeypatch.setenv("ALLOW_RAW_BEARER", "false")
    monkeypatch.setenv("VIYA_AUTH", "true")
    with patch("dotenv.load_dotenv"):
        cfg = _reload_config()
    assert cfg.POSIT_CONNECT_OAUTH is True
    assert cfg.viya_auth is None


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [("ALLOW_RAW_BEARER", "true", "ALLOW_RAW_BEARER"), ("VIYA_AUTH", "false", "VIYA_AUTH")],
)
def test_connect_mode_rejects_contradictory_settings(monkeypatch, restore_config, name, value, match):
    monkeypatch.setenv("POSIT_CONNECT_OAUTH", "true")
    monkeypatch.setenv("ALLOW_RAW_BEARER", "false")
    monkeypatch.setenv("VIYA_AUTH", "true")
    monkeypatch.setenv(name, value)
    with patch("dotenv.load_dotenv"), pytest.raises(Exception, match=match):
        _reload_config()


# --- landing page -------------------------------------------------------------


def test_landing_page_names_connect_sign_in():
    facts = ServerFacts(
        server_name="SAS", version="1.0", mcp_url="https://c/mcp", viya_endpoint="https://v",
        auth_enabled=True, allow_raw_bearer=False, enabled_tiers=frozenset(), read_only=False,
        tiers=(), prompts=(), posit_connect=True,
    )
    page = render_page(facts, nonce="n")
    assert "Sign-in: Posit Connect + SAS Viya" in page
    assert "hosted on Posit Connect" in page


# --- whole server in Connect mode ---------------------------------------------

_SERVER_PROBE = textwrap.dedent(
    """
    import asyncio, json, httpx
    from sas_mcp_server import mcp_server

    async def main():
        app = mcp_server.app
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://connect-rewritten-host"
        ) as client:
            resp = await client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream",
                         "Content-Type": "application/json",
                         "Authorization": "Key some-connect-api-key"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "0"}}},
            )
        print(json.dumps({"auth": mcp_server.mcp.auth is None, "status": resp.status_code,
                          "getter": mcp_server._get_token.__name__}))

    asyncio.run(main())
    """
)


def test_server_in_connect_mode_has_no_sas_logon_gate():
    """Built fresh in a subprocess (module-level app): no OAuth provider, and a
    request carrying Connect's credential, not a Viya one, is served."""
    env = {**os.environ, "POSIT_CONNECT_OAUTH": "true", "ALLOW_RAW_BEARER": "false", "VIYA_AUTH": "true",
           "VIYA_ENDPOINT": os.environ.get("VIYA_ENDPOINT") or "https://test.viya.com"}
    out = subprocess.run([sys.executable, "-c", _SERVER_PROBE], env=env, capture_output=True, text=True,
                         timeout=120, check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["auth"] is True
    assert result["status"] == 200
    # the getter handed to install_user_log -> register_tools
    assert result["getter"] == "connect_viya_token"


def test_server_version_falls_back_to_env_for_a_source_bundle(monkeypatch):
    """A Connect bundle has neither a pyproject.toml above the package nor
    installed metadata; the deploy script passes the version in SAS_MCP_VERSION."""
    import importlib.metadata
    import pathlib

    from sas_mcp_server.helpers.telemetry_helpers import server_version

    monkeypatch.setattr(pathlib.Path, "is_file", lambda self: False)

    def no_metadata(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", no_metadata)
    monkeypatch.setenv("SAS_MCP_VERSION", "9.9.9")
    assert server_version() == "9.9.9"
    monkeypatch.delenv("SAS_MCP_VERSION")
    assert server_version() is None
