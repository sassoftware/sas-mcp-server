# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for stdio-mode token resolution and the native device-code flow.
"""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import fastmcp
import pytest
from fastmcp import Client

from sas_mcp_server import stdio_server
from sas_mcp_server.exceptions import AuthenticationError
from sas_mcp_server.helpers.telemetry_helpers import server_version


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



@pytest.mark.asyncio
async def test_handshake_reports_our_version_not_fastmcps():
    """stdio passes ``version=`` too; it is the transport most people run."""
    assert server_version() == stdio_server.SERVER_VERSION
    async with Client(stdio_server.mcp) as client:
        info = client.server_info
    assert info is not None
    assert info.version == stdio_server.SERVER_VERSION
    assert info.version != fastmcp.__version__



# ---------------------------------------------------------------------------
# several deployments in one credentials file
# ---------------------------------------------------------------------------

EP_A = "https://a.example.com"
EP_B = "https://b.example.com"


@pytest.fixture
def helper_file(tmp_path, monkeypatch):
    """Point the server at EP_A with a helper cache in tmp and no SAS CLI cache."""
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(stdio_server, "VIYA_ENDPOINT", EP_A)
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: tmp_path / "no-cli.json")
    monkeypatch.setattr(stdio_server, "_helper_credentials_path", lambda: p)
    stdio_server._warned.clear()
    return p


def _block(access, *, expiry=None, refresh=None, endpoint=None):
    block = {"access-token": access}
    if expiry is not None:
        block["expiry"] = expiry
    if refresh is not None:
        block["refresh-token"] = refresh
    if endpoint is not None:
        block["endpoint"] = endpoint
    return block


def test_endpoint_block_beats_default(helper_file):
    helper_file.write_text(json.dumps({"Default": _block("LEGACY"), EP_A: _block("MINE")}))
    assert stdio_server._get_viya_token() == "MINE"


def test_each_server_uses_its_own_endpoint_block(helper_file, monkeypatch):
    helper_file.write_text(json.dumps({EP_A: _block("TOK-A"), EP_B: _block("TOK-B")}))
    assert stdio_server._get_viya_token() == "TOK-A"
    monkeypatch.setattr(stdio_server, "VIYA_ENDPOINT", EP_B)
    assert stdio_server._get_viya_token() == "TOK-B"


def test_endpoint_lookup_ignores_spelling(helper_file, monkeypatch):
    helper_file.write_text(json.dumps({EP_A: _block("MINE")}))
    monkeypatch.setattr(stdio_server, "VIYA_ENDPOINT", "HTTPS://A.example.com:443/")
    assert stdio_server._get_viya_token() == "MINE"


def test_file_without_default_works(helper_file):
    """A fresh install has endpoint blocks only, no legacy Default."""
    helper_file.write_text(json.dumps({EP_A: _block("MINE")}))
    assert stdio_server._get_viya_token() == "MINE"


def test_untagged_default_still_used_with_one_warning(helper_file, caplog):
    helper_file.write_text(json.dumps({"Default": _block("LEGACY")}))
    with caplog.at_level("WARNING"):
        assert stdio_server._get_viya_token() == "LEGACY"
        assert stdio_server._get_viya_token() == "LEGACY"
    warnings = [r.getMessage() for r in caplog.records if "not bound to an endpoint" in r.getMessage()]
    assert len(warnings) == 1  # logged once, not on every tool call
    assert f"sas-mcp-login --endpoint {EP_A}" in warnings[0]
    assert "LEGACY" not in caplog.text


def test_default_tagged_for_this_endpoint_is_used_quietly(helper_file, caplog):
    helper_file.write_text(json.dumps({"Default": _block("LEGACY", endpoint=EP_A)}))
    with caplog.at_level("WARNING"):
        assert stdio_server._get_viya_token() == "LEGACY"
    assert "not bound" not in caplog.text


def test_default_tagged_for_another_endpoint_is_never_used(helper_file, monkeypatch, caplog):
    helper_file.write_text(json.dumps({"Default": _block("TOK-OF-B", endpoint=EP_B)}))
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    with patch("sas_mcp_server.stdio_server.httpx.post") as post, caplog.at_level("WARNING"):
        assert stdio_server._get_viya_token() == "DEVTOK"
        post.assert_not_called()
    assert "TOK-OF-B" not in caplog.text
    assert f"sas-mcp-login --endpoint {EP_A}" in caplog.text


def test_default_tagged_for_other_endpoint_does_not_beat_own_block(helper_file):
    helper_file.write_text(json.dumps({
        "Default": _block("TOK-OF-B", endpoint=EP_B),
        EP_A: _block("MINE"),
    }))
    assert stdio_server._get_viya_token() == "MINE"


def test_no_credentials_warns_with_login_command(helper_file, monkeypatch, caplog):
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    with caplog.at_level("WARNING"):
        assert stdio_server._get_viya_token() == "DEVTOK"
    assert f"sas-mcp-login --endpoint {EP_A}" in caplog.text


def test_refreshing_default_tags_it_and_keeps_other_blocks(helper_file):
    helper_file.write_text(json.dumps({
        "Default": _block("EXPIRED", expiry=_iso(-30), refresh="RT"),
        EP_B: _block("TOK-B"),
        "extra": 1,
    }))
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=_refresh_ok()):
        assert stdio_server._get_viya_token() == "NEWTOK"
    data = json.loads(helper_file.read_text())
    assert data["Default"]["access-token"] == "NEWTOK"
    assert data["Default"]["refresh-token"] == "NEWREFRESH"
    assert data["Default"]["endpoint"] == EP_A  # refresh against this server proves ownership
    assert data[EP_B] == _block("TOK-B")
    assert data["extra"] == 1


def test_tagged_default_is_not_offered_to_another_server(helper_file, monkeypatch):
    """After server A refreshes the legacy block, server B must not pick it up."""
    helper_file.write_text(json.dumps({"Default": _block("EXPIRED", expiry=_iso(-30), refresh="RT")}))
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=_refresh_ok()):
        assert stdio_server._get_viya_token() == "NEWTOK"
    monkeypatch.setattr(stdio_server, "VIYA_ENDPOINT", EP_B)
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    assert stdio_server._get_viya_token() == "DEVTOK"


def test_refreshing_endpoint_block_writes_only_that_block(helper_file):
    legacy = _block("LEGACY")
    helper_file.write_text(json.dumps({
        "Default": legacy,
        EP_A: _block("EXPIRED", expiry=_iso(-30), refresh="RT"),
        EP_B: _block("TOK-B"),
    }))
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=_refresh_ok()):
        assert stdio_server._get_viya_token() == "NEWTOK"
    data = json.loads(helper_file.read_text())
    assert data[EP_A]["access-token"] == "NEWTOK"
    assert "endpoint" not in data[EP_A]  # the key already binds it
    assert data["Default"] == legacy
    assert data[EP_B] == _block("TOK-B")


def test_failed_endpoint_refresh_never_falls_back_to_default(helper_file, monkeypatch):
    """A lapsed login for this endpoint must not be replaced by another deployment's legacy token."""
    helper_file.write_text(json.dumps({
        "Default": _block("LEGACY-OF-OTHER", refresh="OTHER-RT"),
        EP_A: _block("EXPIRED", expiry=_iso(-30), refresh="BADRT"),
    }))
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    bad = MagicMock(status_code=401)
    bad.text = "invalid_grant"
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=bad) as post:
        assert stdio_server._get_viya_token() == "DEVTOK"
    # only this endpoint's own refresh token was ever sent; Default's was not
    assert [c[1]["data"]["refresh_token"] for c in post.call_args_list] == ["BADRT"]


def test_expired_endpoint_block_without_refresh_token_does_not_use_default(helper_file, monkeypatch):
    helper_file.write_text(json.dumps({
        "Default": _block("LEGACY-OF-OTHER"),
        EP_A: _block("EXPIRED", expiry=_iso(-30)),
    }))
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    assert stdio_server._get_viya_token() == "DEVTOK"


def test_default_block_of_multi_block_file_keeps_the_old_shape(helper_file):
    """Older versions read only the Default block; endpoint blocks next to it must not change it."""
    legacy = _block("LEGACY", expiry=_iso(30), refresh="RT")
    helper_file.write_text(json.dumps({"Default": legacy}))
    stdio_server._write_credentials(helper_file, "T", "R", 60, EP_A)
    data = json.loads(helper_file.read_text())
    assert data["Default"] == legacy
    assert set(data[EP_A]) == {"access-token", "refresh-token", "expiry"}


def test_unwritable_cache_still_returns_refreshed_token(helper_file, caplog):
    helper_file.write_text(json.dumps({EP_A: _block("EXPIRED", expiry=_iso(-30), refresh="RT")}))
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=_refresh_ok()), \
         patch("sas_mcp_server.stdio_server.update_block", side_effect=OSError("read-only")), \
         caplog.at_level("WARNING"):
        assert stdio_server._get_viya_token() == "NEWTOK"
    assert "Could not write refreshed credentials" in caplog.text


# ---------------------------------------------------------------------------
# malformed credentials files must fall through, never crash a tool call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        [1, 2],  # top level is not an object
        "just a string",
        {"Default": "x"},  # block is not an object
        {"Default": ["x"]},
        {EP_A: "x", "Default": 5},
        {EP_A: {"access-token": 123}},  # token is not a string
        {EP_A: {"access-token": {"nested": "x"}}},
        {EP_A: {"access-token": "OLD", "expiry": _iso(-30), "refresh-token": ["x"]}},
        {"Default": {"access-token": "T", "endpoint": 5}},  # unreadable tag
        {"Default": {"access-token": "T", "endpoint": ["x"]}},
    ],
)
def test_malformed_file_falls_through_to_device_flow(helper_file, monkeypatch, content):
    helper_file.write_text(json.dumps(content))
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    with patch("sas_mcp_server.stdio_server.httpx.post") as post:
        assert stdio_server._get_viya_token() == "DEVTOK"
        post.assert_not_called()


def test_malformed_default_does_not_hide_a_valid_endpoint_block(helper_file):
    helper_file.write_text(json.dumps({"Default": "x", EP_A: _block("MINE")}))
    assert stdio_server._get_viya_token() == "MINE"


def test_unparseable_file_falls_through(helper_file, monkeypatch):
    helper_file.write_text("{not json")
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    assert stdio_server._get_viya_token() == "DEVTOK"


def test_token_lookup_parses_each_file_once(helper_file, tmp_path, monkeypatch):
    cli = tmp_path / "cli.json"
    cli.write_text(json.dumps({"Default": _block("EXPIRED", expiry=_iso(-30))}))
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: cli)
    helper_file.write_text(json.dumps({"Default": _block("LEGACY")}))
    reads = []
    real = stdio_server._read_credentials_file
    monkeypatch.setattr(stdio_server, "_read_credentials_file", lambda p: reads.append(p) or real(p))
    assert stdio_server._get_viya_token() == "LEGACY"
    assert sorted(map(str, reads)) == sorted(map(str, [cli, helper_file]))  # helper file read once, not three times


def test_refreshing_the_cli_cache_does_not_touch_its_windows_acl(helper_file, tmp_path, monkeypatch):
    cli = tmp_path / "cli.json"
    cli.write_text(json.dumps({"Default": _block("EXPIRED", expiry=_iso(-30), refresh="RT")}))
    monkeypatch.setattr(stdio_server, "_sas_cli_credentials_path", lambda: cli)
    seen = []
    from sas_mcp_server import credentials_store
    monkeypatch.setattr(credentials_store, "win_harden", seen.append)
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=_refresh_ok()):
        assert stdio_server._get_viya_token() == "NEWTOK"
    assert seen == []  # another tool's file is not ours to re-permission
    # while our own cache still is
    helper_file.write_text(json.dumps({EP_A: _block("EXPIRED", expiry=_iso(-30), refresh="RT")}))
    cli.unlink()
    with patch("sas_mcp_server.stdio_server.httpx.post", return_value=_refresh_ok()):
        assert stdio_server._get_viya_token() == "NEWTOK"
    assert seen == [helper_file]


# bytes that are not valid text in UTF-8 or in Windows-1252
CP_BAD_1 = bytes([0x7b, 0x22, 0x44, 0x22, 0x3a, 0x22, 0x81, 0x8d, 0x8f, 0x22, 0x7d])
CP_BAD_2 = bytes([0xff, 0xfe, 0x7b, 0x00, 0x7d, 0x00, 0x81])
CP_BAD_3 = bytes([0x81, 0x8d])


@pytest.mark.parametrize("content", [CP_BAD_1, CP_BAD_2, CP_BAD_3])
def test_credentials_file_in_another_encoding_falls_through(helper_file, monkeypatch, caplog, content):
    helper_file.write_bytes(content)
    monkeypatch.setattr(stdio_server, "_native_device_code_token", lambda: "DEVTOK")
    with caplog.at_level("WARNING"):
        assert stdio_server._get_viya_token() == "DEVTOK"
    assert "Could not read credentials" in caplog.text
