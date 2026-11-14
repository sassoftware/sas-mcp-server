# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the zero-prereq OAuth login helper (auth_login.py).

The helper is intentionally decoupled from config (it imports only env_bool),
so it can be exercised without VIYA_ENDPOINT or any server configuration.
"""
import base64
import hashlib
import json
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from sas_mcp_server import auth_login

# ---------------------------------------------------------------------------
# _generate_pkce
# ---------------------------------------------------------------------------


def test_generate_pkce_verifier_and_challenge():
    verifier, challenge = auth_login._generate_pkce()
    assert len(verifier) == 128
    allowed = set(__import__("string").ascii_letters + __import__("string").digits + "-._~")
    assert set(verifier) <= allowed
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge


def test_generate_pkce_is_random():
    assert auth_login._generate_pkce()[0] != auth_login._generate_pkce()[0]


# ---------------------------------------------------------------------------
# _authorize_url
# ---------------------------------------------------------------------------


def test_authorize_url_without_redirect():
    url = auth_login._authorize_url("https://viya.test/", "vscode", "CHAL", None)
    assert url.startswith("https://viya.test/SASLogon/oauth/authorize?")
    assert "client_id=vscode" in url
    assert "response_type=code" in url
    assert "code_challenge=CHAL" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri" not in url


def test_authorize_url_with_redirect():
    url = auth_login._authorize_url("https://viya.test", "vscode", "CHAL",
                                    "http://localhost/cb")
    assert "redirect_uri=http" in url


# ---------------------------------------------------------------------------
# _exchange
# ---------------------------------------------------------------------------


def test_exchange_posts_expected_body():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"access_token": "tok"})
    with patch("sas_mcp_server.auth_login.httpx.post", return_value=resp) as mock_post:
        out = auth_login._exchange("https://v/", "vscode", "code123", "ver", None, True)

    assert out == {"access_token": "tok"}
    args, kwargs = mock_post.call_args
    assert args[0] == "https://v/SASLogon/oauth/token"
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["code"] == "code123"
    assert kwargs["data"]["code_verifier"] == "ver"
    assert kwargs["data"]["client_id"] == "vscode"
    assert "redirect_uri" not in kwargs["data"]
    assert kwargs["verify"] is True
    assert kwargs["auth"] is None
    assert mock_post.call_count == 1


def _token_response(status: int, body) -> httpx.Response:
    request = httpx.Request("POST", "https://v/SASLogon/oauth/token")
    if isinstance(body, str):
        return httpx.Response(status, text=body, request=request)
    return httpx.Response(status, json=body, request=request)


def test_exchange_retries_with_basic_auth_on_invalid_client():
    # #72: a deployment whose vscode client is not public refuses the body-only
    # request; HTTP Basic with the empty secret is accepted and gets the token.
    rejected = _token_response(
        401, {"error": "invalid_client", "error_description": "Bad credentials"}
    )
    ok = _token_response(200, {"access_token": "tok"})
    with patch("sas_mcp_server.auth_login.httpx.post",
               side_effect=[rejected, ok]) as mock_post:
        out = auth_login._exchange("https://v", "vscode", "code123", "ver", None, True)

    assert out == {"access_token": "tok"}
    assert mock_post.call_count == 2
    first, second = mock_post.call_args_list
    assert first.kwargs["auth"] is None
    assert second.kwargs["auth"] == ("vscode", "")
    assert second.kwargs["data"] == first.kwargs["data"]


def test_exchange_raises_when_basic_auth_retry_also_fails():
    rejected = _token_response(401, {"error": "invalid_client"})
    with patch("sas_mcp_server.auth_login.httpx.post",
               side_effect=[rejected, rejected]) as mock_post, \
            pytest.raises(httpx.HTTPStatusError):
        auth_login._exchange("https://v", "vscode", "c", "v", None, True)
    assert mock_post.call_count == 2


@pytest.mark.parametrize("status, body", [
    (400, {"error": "invalid_grant", "error_description": "Invalid authorization code"}),
    (401, {"error": "unauthorized"}),
    (401, "Bad credentials"),
    (401, ["invalid_client"]),
])
def test_exchange_does_not_retry_other_errors(status, body):
    # Only a refused client is retried: a bad code, another 401 or a body that
    # is not an OAuth error object fails at once.
    with patch("sas_mcp_server.auth_login.httpx.post",
               return_value=_token_response(status, body)) as mock_post, \
            pytest.raises(httpx.HTTPStatusError):
        auth_login._exchange("https://v", "vscode", "c", "v", None, True)
    assert mock_post.call_count == 1


def test_exchange_includes_redirect_uri():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={})
    with patch("sas_mcp_server.auth_login.httpx.post", return_value=resp) as mock_post:
        auth_login._exchange("https://v", "vscode", "c", "v", "http://cb", False)
    assert mock_post.call_args[1]["data"]["redirect_uri"] == "http://cb"


# ---------------------------------------------------------------------------
# state persistence
# ---------------------------------------------------------------------------


def test_state_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "login-state.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", p)
    assert auth_login._read_state() is None

    auth_login._write_state({"verifier": "v", "endpoint": "e"})
    assert p.exists()
    assert auth_login._read_state() == {"verifier": "v", "endpoint": "e"}

    auth_login._clear_state()
    assert not p.exists()
    assert auth_login._read_state() is None


def test_read_state_malformed_returns_none(tmp_path, monkeypatch):
    p = tmp_path / "login-state.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(auth_login, "STATE_PATH", p)
    assert auth_login._read_state() is None


def test_clear_state_missing_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_login, "STATE_PATH", tmp_path / "absent.json")
    auth_login._clear_state()  # must not raise


# ---------------------------------------------------------------------------
# _write_cache
# ---------------------------------------------------------------------------


def test_write_cache_shape(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_login, "CACHE_PATH", p)
    out = auth_login._write_cache(
        {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}, "https://v"
    )
    assert out == p
    data = json.loads(p.read_text())
    assert data["https://v"]["access-token"] == "AT"
    assert data["https://v"]["refresh-token"] == "RT"
    assert data["https://v"]["expiry"]  # ISO timestamp present


def test_write_cache_defaults_missing_refresh(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_login, "CACHE_PATH", p)
    auth_login._write_cache({"access_token": "AT"}, "https://v")
    data = json.loads(p.read_text())
    assert data["https://v"]["refresh-token"] == ""


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_no_endpoint_errors(monkeypatch):
    monkeypatch.delenv("VIYA_ENDPOINT", raising=False)
    monkeypatch.setattr(sys, "argv", ["sas-mcp-login"])
    with pytest.raises(SystemExit):
        auth_login.main()


def test_main_code_without_state_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_login, "STATE_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(
        sys, "argv", ["sas-mcp-login", "--endpoint", "https://v", "--code", "X"]
    )
    assert auth_login.main() == 1


def test_main_code_success(tmp_path, monkeypatch):
    state_p = tmp_path / "state.json"
    cache_p = tmp_path / "creds.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", state_p)
    monkeypatch.setattr(auth_login, "CACHE_PATH", cache_p)
    auth_login._write_state({
        "endpoint": "https://v", "client_id": "vscode",
        "redirect_uri": "", "verifier": "ver",
    })
    monkeypatch.setattr(
        sys, "argv", ["sas-mcp-login", "--endpoint", "https://v", "--code", "CODE"]
    )
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"access_token": "AT", "expires_in": 3600})
    with patch("sas_mcp_server.auth_login.httpx.post", return_value=resp):
        rc = auth_login.main()

    assert rc == 0
    assert cache_p.exists()
    assert not state_p.exists()  # state cleared after success
    assert json.loads(cache_p.read_text())["https://v"]["access-token"] == "AT"


def test_main_code_exchange_failure_returns_1(tmp_path, monkeypatch):
    state_p = tmp_path / "state.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", state_p)
    monkeypatch.setattr(auth_login, "CACHE_PATH", tmp_path / "creds.json")
    auth_login._write_state({
        "endpoint": "https://v", "client_id": "vscode",
        "redirect_uri": "", "verifier": "ver",
    })
    monkeypatch.setattr(
        sys, "argv", ["sas-mcp-login", "--endpoint", "https://v", "--code", "CODE"]
    )
    err = httpx.HTTPStatusError("bad", request=MagicMock(), response=MagicMock(text="nope"))
    with patch("sas_mcp_server.auth_login.httpx.post", side_effect=err):
        assert auth_login.main() == 1


def test_main_phase1_non_tty_saves_state(tmp_path, monkeypatch):
    state_p = tmp_path / "state.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", state_p)
    monkeypatch.setattr(
        sys, "argv", ["sas-mcp-login", "--endpoint", "https://v", "--no-browser"]
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = auth_login.main()
    assert rc == 0
    saved = auth_login._read_state()
    assert saved["endpoint"] == "https://v"
    assert "verifier" in saved


def test_main_phase1_interactive_exchanges(tmp_path, monkeypatch):
    state_p = tmp_path / "state.json"
    cache_p = tmp_path / "creds.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", state_p)
    monkeypatch.setattr(auth_login, "CACHE_PATH", cache_p)
    monkeypatch.setattr(
        sys, "argv", ["sas-mcp-login", "--endpoint", "https://v", "--no-browser"]
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "PASTED_CODE")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"access_token": "AT", "expires_in": 60})
    with patch("sas_mcp_server.auth_login.httpx.post", return_value=resp) as mock_post:
        rc = auth_login.main()

    assert rc == 0
    assert mock_post.call_args[1]["data"]["code"] == "PASTED_CODE"
    assert json.loads(cache_p.read_text())["https://v"]["access-token"] == "AT"


# ---------------------------------------------------------------------------
# several endpoints in one credentials file
# ---------------------------------------------------------------------------


def _login_main(monkeypatch, endpoint, *extra):
    monkeypatch.setattr(sys, "argv", ["sas-mcp-login", "--endpoint", endpoint, *extra])


def test_write_cache_with_endpoint_keeps_other_blocks(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_login, "CACHE_PATH", p)
    p.write_text(json.dumps({"Default": {"access-token": "OLD"}}))
    auth_login._write_cache({"access_token": "A"}, "https://a.example.com/")
    auth_login._write_cache({"access_token": "B"}, "https://B.example.com")
    data = json.loads(p.read_text())
    assert data["Default"] == {"access-token": "OLD"}  # legacy block untouched
    assert data["https://a.example.com"]["access-token"] == "A"
    assert data["https://b.example.com"]["access-token"] == "B"


def test_main_code_rejects_state_for_another_endpoint(tmp_path, monkeypatch, capsys):
    state_p = tmp_path / "state.json"
    cache_p = tmp_path / "creds.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", state_p)
    monkeypatch.setattr(auth_login, "CACHE_PATH", cache_p)
    auth_login._write_state({
        "endpoint": "https://a", "client_id": "vscode", "redirect_uri": "", "verifier": "ver",
    })
    _login_main(monkeypatch, "https://b", "--code", "CODE")
    with patch("sas_mcp_server.auth_login.httpx.post") as post:
        assert auth_login.main() == 1
        post.assert_not_called()
    assert not cache_p.exists()
    assert state_p.exists()  # the pending login for https://a is kept
    assert "https://a" in capsys.readouterr().err


def test_main_code_accepts_equivalent_endpoint_spelling(tmp_path, monkeypatch):
    state_p = tmp_path / "state.json"
    cache_p = tmp_path / "creds.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", state_p)
    monkeypatch.setattr(auth_login, "CACHE_PATH", cache_p)
    auth_login._write_state({
        "endpoint": "https://V.example.com/", "client_id": "vscode", "redirect_uri": "", "verifier": "ver",
    })
    _login_main(monkeypatch, "https://v.example.com:443", "--code", "CODE")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"access_token": "AT", "expires_in": 60})
    with patch("sas_mcp_server.auth_login.httpx.post", return_value=resp):
        assert auth_login.main() == 0
    assert "https://v.example.com" in json.loads(cache_p.read_text())


def test_logout_removes_only_that_endpoint(tmp_path, monkeypatch, capsys):
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_login, "CACHE_PATH", p)
    p.write_text(json.dumps({
        "Default": {"access-token": "D"},
        "https://a": {"access-token": "A"},
        "https://b": {"access-token": "B"},
    }))
    _login_main(monkeypatch, "https://a/", "--logout")
    assert auth_login.main() == 0
    assert sorted(json.loads(p.read_text())) == ["Default", "https://b"]
    assert "Removed" in capsys.readouterr().out


def test_logout_unknown_endpoint_is_harmless(tmp_path, monkeypatch, capsys):
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_login, "CACHE_PATH", p)
    _login_main(monkeypatch, "https://nope", "--logout")
    assert auth_login.main() == 0
    assert not p.exists()
    assert "No stored login" in capsys.readouterr().out


def test_login_reports_an_unwritable_credentials_file(tmp_path, monkeypatch, capsys):
    state_p = tmp_path / "state.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", state_p)
    monkeypatch.setattr(auth_login, "CACHE_PATH", tmp_path / "creds.json")
    auth_login._write_state({
        "endpoint": "https://v", "client_id": "vscode", "redirect_uri": "", "verifier": "ver",
    })
    _login_main(monkeypatch, "https://v", "--code", "CODE")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"access_token": "AT", "expires_in": 60})
    failing_store = patch("sas_mcp_server.auth_login.update_block", side_effect=PermissionError("locked"))
    with patch("sas_mcp_server.auth_login.httpx.post", return_value=resp), failing_store:
        assert auth_login.main() == 1  # a message, not a traceback
    assert "Could not store the token" in capsys.readouterr().err


def test_logout_reports_a_corrupt_credentials_file(tmp_path, monkeypatch, capsys):
    p = tmp_path / "credentials.json"
    p.write_text("{nope")
    monkeypatch.setattr(auth_login, "CACHE_PATH", p)
    _login_main(monkeypatch, "https://a", "--logout")
    assert auth_login.main() == 1
    assert p.read_text() == "{nope"  # untouched
    assert "Could not update" in capsys.readouterr().err


def test_logout_removes_default_bound_to_that_endpoint(tmp_path, monkeypatch, capsys):
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_login, "CACHE_PATH", p)
    p.write_text(json.dumps({
        "Default": {"access-token": "D", "endpoint": "https://a"},
        "https://b": {"access-token": "B"},
    }))
    _login_main(monkeypatch, "https://a", "--logout")
    assert auth_login.main() == 0
    assert list(json.loads(p.read_text())) == ["https://b"]
    assert "Default" in capsys.readouterr().out


def test_logout_mentions_an_unbound_default_it_cannot_remove(tmp_path, monkeypatch, capsys):
    p = tmp_path / "credentials.json"
    monkeypatch.setattr(auth_login, "CACHE_PATH", p)
    p.write_text(json.dumps({"Default": {"access-token": "D"}}))
    _login_main(monkeypatch, "https://a", "--logout")
    assert auth_login.main() == 0
    assert json.loads(p.read_text()) == {"Default": {"access-token": "D"}}
    out = capsys.readouterr().out
    assert "No stored login" in out
    assert "not bound to an endpoint" in out


def test_main_code_defaults_to_pending_login_endpoint(tmp_path, monkeypatch):
    # The printed two-step command has no --endpoint; a .env pointing at another
    # deployment must not make it fail.
    state_p = tmp_path / "state.json"
    cache_p = tmp_path / "creds.json"
    monkeypatch.setattr(auth_login, "STATE_PATH", state_p)
    monkeypatch.setattr(auth_login, "CACHE_PATH", cache_p)
    monkeypatch.setenv("VIYA_ENDPOINT", "https://other")
    auth_login._write_state({
        "endpoint": "https://a", "client_id": "vscode", "redirect_uri": "", "verifier": "ver",
    })
    monkeypatch.setattr(sys, "argv", ["sas-mcp-login", "--code", "CODE"])
    resp = MagicMock()
    resp.json = MagicMock(return_value={"access_token": "AT", "expires_in": 60})
    with patch("sas_mcp_server.auth_login.httpx.post", return_value=resp):
        assert auth_login.main() == 0
    assert json.loads(cache_p.read_text())["https://a"]["access-token"] == "AT"


def test_logout_with_code_is_rejected(monkeypatch):
    _login_main(monkeypatch, "https://a", "--logout", "--code", "CODE")
    with pytest.raises(SystemExit):
        auth_login.main()
