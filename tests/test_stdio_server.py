# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for stdio-mode token resolution and the native device-code flow.
"""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sas_mcp_server import stdio_server
from sas_mcp_server.exceptions import AuthenticationError


@pytest.fixture(autouse=True)
def _force_auth_enabled():
    with patch.object(stdio_server, "AUTH_ENABLED", True):
        yield


def _iso(minutes_from_now: float) -> str:
    """ISO-8601 timestamp offset from now, for building credential expiries."""
    return (datetime.now(UTC) + timedelta(minutes=minutes_from_now)).isoformat()


def _write_creds(
    path: Path,
    *,
    access: str | None = "TOK",
    expiry: str | None = None,
    refresh: str | None = None,
) -> Path:
    """Write a credential file with only the keys provided."""
    default: dict = {}
    if access is not None:
        default["access-token"] = access
    if expiry is not None:
        default["expiry"] = expiry
    if refresh is not None:
        default["refresh-token"] = refresh
    path.write_text(json.dumps({"Default": default}))
    return path


@pytest.mark.asyncio
async def test_lifespan_cleans_up_sessions_on_shutdown():
    """The stdio server lifespan tears down warm compute sessions on exit."""
    with patch(
        "sas_mcp_server.stdio_server.shutdown_session_cache", new=AsyncMock()
    ) as mock_shutdown:
        async with stdio_server._lifespan(stdio_server.mcp):
            mock_shutdown.assert_not_awaited()
        mock_shutdown.assert_awaited_once()

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


def test_get_viya_token_no_auth_mode_skips_all_auth(tmp_path, monkeypatch):
    with patch.object(stdio_server, "AUTH_ENABLED", False), patch(
        "sas_mcp_server.stdio_server.httpx.post"
    ) as post:
        assert stdio_server._get_viya_token() == ""
        post.assert_not_called()


@pytest.mark.asyncio
async def test_stdio_get_token_delegates(monkeypatch):
    monkeypatch.setattr(stdio_server, "_get_viya_token", lambda: "TOK")
    assert await stdio_server._stdio_get_token(None) == "TOK"


# ---------------------------------------------------------------------------
# _token_expired
# ---------------------------------------------------------------------------


def test_token_expired_future():
    assert stdio_server._token_expired({"expiry": _iso(30)}) is False


def test_token_expired_past():
    assert stdio_server._token_expired({"expiry": _iso(-30)}) is True


def test_token_expired_within_skew():
    # 30 seconds left, but the 60s skew means we treat it as already expired.
    assert stdio_server._token_expired({"expiry": _iso(0.5)}) is True


def test_token_expired_missing_expiry_is_usable():
    # No expiry recorded → treat as not expired (preserves legacy behavior).
    assert stdio_server._token_expired({}) is False


def test_token_expired_unparseable_is_usable():
    assert stdio_server._token_expired({"expiry": "not-a-date"}) is False


def test_token_expired_handles_z_suffix():
    past_z = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert stdio_server._token_expired({"expiry": past_z}) is True


# ---------------------------------------------------------------------------
# _read_cached_token — expiry awareness
# ---------------------------------------------------------------------------


def test_read_cached_token_valid_with_future_expiry(tmp_path):
    p = _write_creds(tmp_path / "c.json", access="GOOD", expiry=_iso(30))
    assert stdio_server._read_cached_token(p) == "GOOD"


def test_read_cached_token_expired_returns_none(tmp_path):
    p = _write_creds(tmp_path / "c.json", access="STALE", expiry=_iso(-30))
    assert stdio_server._read_cached_token(p) is None


# ---------------------------------------------------------------------------
# _get_viya_token — expired CLI cache must not shadow a valid helper token
# ---------------------------------------------------------------------------


def test_expired_cli_cache_falls_through_to_valid_helper(tmp_path, monkeypatch):
    cli = _write_creds(tmp_path / "cli.json", access="EXPIRED", expiry=_iso(-30))
    helper = _write_creds(tmp_path / "helper.json", access="VALID", expiry=_iso(30))
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: cli)
    monkeypatch.setattr(stdio_server, "_helper_credentials_path", lambda: helper)
    # No refresh tokens, so no network calls should be attempted.
    with patch("sas_mcp_server.stdio_server.httpx.post") as post:
        assert stdio_server._get_viya_token() == "VALID"
        post.assert_not_called()


# ---------------------------------------------------------------------------
# refresh-token support
# ---------------------------------------------------------------------------


def _refresh_ok(access="NEWTOK", refresh="NEWREFRESH", expires_in=3600):
    resp = MagicMock(status_code=200)
    resp.json = MagicMock(
        return_value={
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": expires_in,
        }
    )
    return resp


def test_refresh_access_token_non_200_returns_none():
    resp = MagicMock(status_code=400)
    resp.text = "invalid_grant"
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=resp):
        assert stdio_server._refresh_access_token("RT", "vscode") is None


def test_expired_token_is_refreshed_and_cache_updated(tmp_path, monkeypatch):
    cli = _write_creds(
        tmp_path / "cli.json", access="EXPIRED", expiry=_iso(-30), refresh="RT"
    )
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: cli)
    monkeypatch.setattr(
        stdio_server, "_helper_credentials_path", lambda: tmp_path / "absent.json"
    )
    with patch(
        "sas_mcp_server.stdio_server.httpx.post", return_value=_refresh_ok()
    ) as post:
        assert stdio_server._get_viya_token() == "NEWTOK"
        # Refresh used the SAS-CLI client id and the cached refresh token.
        sent = post.call_args[1]["data"]
        assert sent["grant_type"] == "refresh_token"
        assert sent["refresh_token"] == "RT"
        assert post.call_args[1]["auth"] == (stdio_server.SAS_CLI_CLIENT_ID, "")
    # The cache was rewritten with the new token, rotated refresh, future expiry.
    updated = json.loads(cli.read_text())["Default"]
    assert updated["access-token"] == "NEWTOK"
    assert updated["refresh-token"] == "NEWREFRESH"
    assert stdio_server._token_expired(updated) is False


def test_refresh_keeps_old_refresh_token_when_not_rotated(tmp_path, monkeypatch):
    cli = _write_creds(
        tmp_path / "cli.json", access="EXPIRED", expiry=_iso(-30), refresh="KEEPME"
    )
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: cli)
    monkeypatch.setattr(
        stdio_server, "_helper_credentials_path", lambda: tmp_path / "absent.json"
    )
    resp = _refresh_ok(refresh=None)  # response omits a new refresh token
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=resp):
        assert stdio_server._get_viya_token() == "NEWTOK"
    assert json.loads(cli.read_text())["Default"]["refresh-token"] == "KEEPME"


def test_failed_refresh_falls_through_to_next_source(tmp_path, monkeypatch):
    cli = _write_creds(
        tmp_path / "cli.json", access="EXPIRED", expiry=_iso(-30), refresh="BADRT"
    )
    helper = _write_creds(tmp_path / "helper.json", access="VALID", expiry=_iso(30))
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: cli)
    monkeypatch.setattr(stdio_server, "_helper_credentials_path", lambda: helper)
    bad = MagicMock(status_code=401)
    bad.text = "invalid_grant"
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=bad):
        assert stdio_server._get_viya_token() == "VALID"


def test_all_expired_no_refresh_falls_back_to_device(tmp_path, monkeypatch):
    cli = _write_creds(tmp_path / "cli.json", access="EXPIRED", expiry=_iso(-30))
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: cli)
    monkeypatch.setattr(
        stdio_server, "_helper_credentials_path", lambda: tmp_path / "absent.json"
    )
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    assert stdio_server._get_viya_token() == "DEVTOK"


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
