# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for stdio-mode token resolution and the native device-code flow.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sas_mcp_server import stdio_server
from sas_mcp_server.exceptions import AuthenticationError

# ---------------------------------------------------------------------------
# credentials-path resolution
# ---------------------------------------------------------------------------


def test_sas_cli_credentials_path_default(monkeypatch):
    monkeypatch.setattr(stdio_server, "SAS_CLI_CONFIG", "")
    assert stdio_server._sas_cli_credentials_path() == Path.home() / ".sas" / "credentials.json"


def test_sas_cli_credentials_path_override(tmp_path, monkeypatch):
    monkeypatch.setattr(stdio_server, "SAS_CLI_CONFIG", str(tmp_path))
    assert stdio_server._sas_cli_credentials_path() == tmp_path / ".sas" / "credentials.json"


def test_helper_credentials_path():
    expected = Path.home() / ".sas-mcp-server" / "credentials.json"
    assert stdio_server._helper_credentials_path() == expected


# ---------------------------------------------------------------------------
# _read_cached_token
# ---------------------------------------------------------------------------


def test_read_cached_token_valid(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"Default": {"access-token": "TOK"}}))
    assert stdio_server._read_cached_token(p) == "TOK"


def test_read_cached_token_missing_file(tmp_path):
    assert stdio_server._read_cached_token(tmp_path / "nope.json") is None


def test_read_cached_token_missing_key(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"something": "else"}))
    assert stdio_server._read_cached_token(p) is None


def test_read_cached_token_malformed(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text("{not json")
    assert stdio_server._read_cached_token(p) is None


# ---------------------------------------------------------------------------
# _get_viya_token fallback chain
# ---------------------------------------------------------------------------


def test_get_viya_token_from_cli_cache(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"Default": {"access-token": "CLITOK"}}))
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: p)
    monkeypatch.setattr(stdio_server, "_helper_credentials_path", lambda: tmp_path / "absent.json")
    assert stdio_server._get_viya_token() == "CLITOK"


def test_get_viya_token_from_helper_cache(tmp_path, monkeypatch):
    helper = tmp_path / "helper.json"
    helper.write_text(json.dumps({"Default": {"access-token": "HELPERTOK"}}))
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(stdio_server, "_helper_credentials_path", lambda: helper)
    assert stdio_server._get_viya_token() == "HELPERTOK"


def test_get_viya_token_falls_back_to_device(tmp_path, monkeypatch):
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: tmp_path / "a.json")
    monkeypatch.setattr(stdio_server, "_helper_credentials_path", lambda: tmp_path / "b.json")
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    assert stdio_server._get_viya_token() == "DEVTOK"


@pytest.mark.asyncio
async def test_stdio_get_token_delegates(monkeypatch):
    monkeypatch.setattr(stdio_server, "_get_viya_token", lambda: "TOK")
    assert await stdio_server._stdio_get_token(None) == "TOK"


# ---------------------------------------------------------------------------
# _native_device_code_token (RFC 8628)
# ---------------------------------------------------------------------------


def _device_init(**overrides):
    flow = {
        "verification_uri": "https://v/device",
        "user_code": "ABCD",
        "device_code": "DEV",
        "expires_in": 1800,
        "interval": 5,
    }
    flow.update(overrides)
    resp = MagicMock(status_code=200)
    resp.json = MagicMock(return_value=flow)
    return resp


def test_native_device_code_success():
    poll = MagicMock(status_code=200)
    poll.json = MagicMock(return_value={"access_token": "DEVTOK"})
    with patch("sas_mcp_server.stdio_server.httpx.post", side_effect=[_device_init(), poll]), \
         patch("sas_mcp_server.stdio_server.time.sleep"), \
         patch("sas_mcp_server.stdio_server.webbrowser.open"):
        assert stdio_server._native_device_code_token() == "DEVTOK"


def test_native_device_code_csrf_rejected():
    init = MagicMock(status_code=403)
    init.text = "CSRF protection is enabled"
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=init), \
         patch("sas_mcp_server.stdio_server.webbrowser.open"), pytest.raises(AuthenticationError, match="CSRF"):
        stdio_server._native_device_code_token()


def test_native_device_code_pending_then_success():
    pending = MagicMock(status_code=400)
    pending.json = MagicMock(return_value={"error": "authorization_pending"})
    success = MagicMock(status_code=200)
    success.json = MagicMock(return_value={"access_token": "TOK"})
    with patch("sas_mcp_server.stdio_server.httpx.post",
               side_effect=[_device_init(), pending, success]), \
         patch("sas_mcp_server.stdio_server.time.sleep"), \
         patch("sas_mcp_server.stdio_server.webbrowser.open"):
        assert stdio_server._native_device_code_token() == "TOK"


def test_native_device_code_timeout():
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=_device_init(expires_in=0)), \
         patch("sas_mcp_server.stdio_server.time.sleep"), \
         patch("sas_mcp_server.stdio_server.webbrowser.open"), pytest.raises(AuthenticationError, match="timed out"):
        stdio_server._native_device_code_token()
