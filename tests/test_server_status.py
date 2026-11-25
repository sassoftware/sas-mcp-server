# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""server_status, the expected-host guard, and token lifetimes."""

import base64
import json
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client, FastMCP

from sas_mcp_server import token_info, tools
from sas_mcp_server.tools import compute
from sas_mcp_server.tools._access import host_check
from sas_mcp_server.user_log import clear_user_cache


def make_jwt(claims: dict) -> str:
    """An unsigned JWT carrying *claims* (the tools never check the signature)."""

    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


@pytest.fixture(autouse=True)
def _isolate():
    clear_user_cache()
    token_info.set_token_renewer(None)
    yield
    clear_user_cache()
    token_info.set_token_renewer(None)


def _identities(user_id: str = "alice"):
    """A make_client stand-in whose GET answers like /identities/users/@currentUser."""
    resp = MagicMock(status_code=200)
    resp.json = MagicMock(return_value={"id": user_id})
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get.return_value = resp
    return patch("sas_mcp_server.user_log.make_client", return_value=client)


async def _server(token: str, *, expect_host: str = "", tiers="0,4"):
    calls: list[str] = []

    async def get_token(ctx):
        calls.append("token")
        return token

    mcp = FastMCP("status-test")
    tools.register_tools(mcp, get_token, tiers=tiers, expect_host=expect_host)
    return mcp, calls


# --- host_check -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "expected", "matches"),
    [
        ("https://hc-build.example.com", "hc-build.example.com", True),
        ("https://HC-Build.example.com/", "https://hc-build.example.com", True),
        ("https://hc-build.example.com", "hc-build.example.com:443", True),
        ("https://hc-build.example.com:8443", "hc-build.example.com", True),
        ("https://hc-build.example.com:8443", "hc-build.example.com:443", False),
        ("https://hc-build.example.com:8443", "hc-build.example.com:typo", False),
        ("https://other.example.com", "hc-build.example.com", False),
        ("https://hc-build.example.com.evil.test", "hc-build.example.com", False),
    ],
)
def test_host_check(endpoint, expected, matches):
    assert host_check(endpoint, expected)["matches"] is matches


def test_host_check_unset_expects_nothing():
    check = host_check("https://hc-build.example.com", "")
    assert check == {"connected_host": "hc-build.example.com", "expected_host": None, "matches": None}


def test_host_check_names_a_non_default_port():
    assert host_check("https://h.example.com:8443", None)["connected_host"] == "h.example.com:8443"


# --- server_status --------------------------------------------------------------


async def test_server_status_reports_endpoint_user_and_token_expiry(monkeypatch):
    monkeypatch.setattr(tools, "VIYA_ENDPOINT", "https://hc-build.example.com")
    monkeypatch.setattr(compute, "VIYA_ENDPOINT", "https://hc-build.example.com")
    monkeypatch.setattr(compute, "MCP_PROFILE", "hc-build")
    exp = int(time.time()) + 3600
    token = make_jwt({"sub": "u1", "exp": exp})
    mcp, _ = await _server(token, expect_host="hc-build.example.com")
    with _identities("alice"):
        async with Client(mcp) as client:
            status = (await client.call_tool("server_status", {})).data

    assert status["viya_endpoint"] == "https://hc-build.example.com"
    assert status["host"] == "hc-build.example.com"
    assert status["profile"] == "hc-build"
    assert status["host_matches"] is True
    assert status["writes_refused"] is False
    assert status["user"] == "alice"
    assert status["token"]["expires_at"] == datetime.fromtimestamp(exp, tz=UTC).isoformat()
    assert 3500 < status["token"]["seconds_left"] <= 3600
    assert status["token"]["renewal_available"] is False
    # No secret leaves the server: neither the token nor any part of it.
    assert token not in json.dumps(status)
    assert token.split(".")[1] not in json.dumps(status)


async def test_server_status_without_a_token_still_names_the_endpoint(monkeypatch):
    monkeypatch.setattr(compute, "MCP_PROFILE", "")
    mcp = FastMCP("status-test")

    async def get_token(ctx):
        raise RuntimeError("no cached login")

    tools.register_tools(mcp, get_token, tiers="0")
    async with Client(mcp) as client:
        status = (await client.call_tool("server_status", {})).data
    assert status["viya_endpoint"] == compute.VIYA_ENDPOINT
    assert status["profile"] is None
    assert status["user"] is None
    assert "no cached login" in status["token"]["error"]


# --- the guard ------------------------------------------------------------------


async def test_wrong_host_refuses_writes_without_requesting_a_token(monkeypatch):
    mcp, calls = await _server("test-token", expect_host="hc-build.example.com")
    async with Client(mcp) as client:
        for tool, args in (
            ("execute_sas_code", {"sas_code": "data _null_; run;"}),
            ("submit_batch_job", {"sas_code": "data _null_; run;"}),
            ("cancel_job", {"job_id": "j1"}),
        ):
            result = await client.call_tool(tool, args, raise_on_error=False)
            assert result.is_error
            refusal = result.structured_content
            assert refusal["status"] == "refused"
            assert refusal["reason"] == "unexpected_host"
            assert refusal["tool"] == tool
            assert refusal["expected_host"] == "hc-build.example.com"
            assert refusal["connected_host"] == host_check(tools.VIYA_ENDPOINT, None)["connected_host"]
            assert refusal["gated_by"] == "MCP_EXPECT_HOST"
    assert calls == []  # nothing was sent to SAS Viya


async def test_wrong_host_keeps_read_tools_working(monkeypatch):
    mcp, _ = await _server("test-token", expect_host="hc-build.example.com")
    with _identities():
        async with Client(mcp) as client:
            status = (await client.call_tool("server_status", {})).data
    assert status["host_matches"] is False
    assert status["writes_refused"] is True
    assert status["expected_host"] == "hc-build.example.com"


async def test_matching_host_lets_writes_through():
    expected = host_check(tools.VIYA_ENDPOINT, None)["connected_host"]
    mcp, calls = await _server("test-token", expect_host=expected)
    with patch("sas_mcp_server.tools._common.make_client") as make_client:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        make_client.return_value = client
        async with Client(mcp) as mcp_client:
            result = await mcp_client.call_tool("cancel_job", {"job_id": "j1"}, raise_on_error=False)
    assert not result.is_error
    assert calls  # the tool ran and asked for its token


async def test_no_expected_host_adds_no_middleware():
    plain = FastMCP("plain")
    guarded = FastMCP("guarded")

    async def get_token(ctx):
        return "t"

    tools.register_tools(plain, get_token, tiers="0", expect_host="")
    tools.register_tools(guarded, get_token, tiers="0", expect_host="somewhere.example.com")
    assert len(guarded.middleware) == len(plain.middleware) + 1


# --- token_info -----------------------------------------------------------------


def test_token_lifetime_reads_exp_only():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    token = make_jwt({"exp": int(now.timestamp()) + 90})
    assert token_info.token_lifetime(token, now=now) == {
        "expires_at": "2026-11-23T12:01:30+00:00",
        "seconds_left": 90,
    }
    assert token_info.token_lifetime(f"Bearer {token}", now=now)["seconds_left"] == 90


@pytest.mark.parametrize("token", [None, "", "not-a-jwt", "a.b.c", make_jwt({"sub": "x"}), make_jwt({"exp": True})])
def test_token_lifetime_without_a_readable_expiry(token):
    assert token_info.token_lifetime(token) is None


# --- submit_batch_job and the token's lifetime ----------------------------------


def _jwt_expiring_in(seconds: int) -> str:
    return make_jwt({"sub": "u1", "exp": int(time.time()) + seconds})


async def test_ensure_token_lifetime_renews_a_token_about_to_expire():
    short, long_ = _jwt_expiring_in(600), _jwt_expiring_in(4 * 3600)
    token_info.set_token_renewer(lambda token: long_)
    token, lifetime = await token_info.ensure_token_lifetime(short)
    assert token == long_
    assert lifetime["renewed"] is True
    assert lifetime["seconds_left"] > 3 * 3600
    assert "warning" not in lifetime


async def test_ensure_token_lifetime_leaves_a_long_token_alone():
    renewer = MagicMock(return_value=_jwt_expiring_in(4 * 3600))
    token_info.set_token_renewer(renewer)
    long_ = _jwt_expiring_in(2 * 3600)
    token, lifetime = await token_info.ensure_token_lifetime(long_, expected_runtime_minutes=60)
    assert token == long_
    assert lifetime["renewed"] is False
    renewer.assert_not_called()


async def test_ensure_token_lifetime_renews_for_the_expected_run_time():
    renewer = MagicMock(return_value=_jwt_expiring_in(4 * 3600))
    token_info.set_token_renewer(renewer)
    token, lifetime = await token_info.ensure_token_lifetime(
        _jwt_expiring_in(2 * 3600), expected_runtime_minutes=180
    )
    renewer.assert_called_once()
    assert lifetime["renewed"] is True
    assert "warning" not in lifetime


async def test_ensure_token_lifetime_warns_when_even_a_renewed_token_is_too_short():
    token_info.set_token_renewer(lambda token: _jwt_expiring_in(4 * 3600))
    _, lifetime = await token_info.ensure_token_lifetime(_jwt_expiring_in(600), expected_runtime_minutes=300)
    assert lifetime["renewed"] is True
    assert "300 minutes" in lifetime["warning"]
    assert "resubmit" not in lifetime["warning"]


async def test_ensure_token_lifetime_without_a_renewer_warns():
    short = _jwt_expiring_in(600)
    token, lifetime = await token_info.ensure_token_lifetime(short)
    assert token == short
    assert lifetime["renewed"] is False
    assert lifetime["renewal_available"] is False
    assert "Monitor the submitted job" in lifetime["warning"]


async def test_ensure_token_lifetime_keeps_the_token_when_renewal_fails():
    short = _jwt_expiring_in(600)
    token_info.set_token_renewer(lambda token: None)
    token, lifetime = await token_info.ensure_token_lifetime(short)
    assert token == short
    assert lifetime["renewed"] is False
    assert lifetime["renewal_available"] is True


async def test_ensure_token_lifetime_without_an_expiry():
    assert await token_info.ensure_token_lifetime("opaque-token") == ("opaque-token", None)


def _jobs_client():
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    resp = MagicMock(status_code=201, content=b"{}")
    resp.json = MagicMock(return_value={"id": "job-1", "state": "pending"})
    client.post.return_value = resp
    return client


async def test_submit_batch_job_submits_with_the_renewed_token():
    short, long_ = _jwt_expiring_in(600), _jwt_expiring_in(4 * 3600)
    token_info.set_token_renewer(lambda token: long_)
    mcp, _ = await _server(short, tiers="4")
    client = _jobs_client()
    with patch("sas_mcp_server.tools.jobs.make_client", return_value=client) as make_client:
        async with Client(mcp) as mcp_client:
            job = (
                await mcp_client.call_tool(
                    "submit_batch_job", {"sas_code": "data _null_; run;", "expected_runtime_minutes": 90}
                )
            ).data
    make_client.assert_called_once_with(long_)
    assert job["id"] == "job-1"
    assert job["token_lifetime"]["renewed"] is True
    assert job["token_lifetime"]["seconds_left"] > 3 * 3600
    assert long_ not in json.dumps(job)


async def test_submit_batch_job_reports_the_lifetime_without_renewing():
    token = _jwt_expiring_in(2 * 3600)
    mcp, _ = await _server(token, tiers="4")
    client = _jobs_client()
    with patch("sas_mcp_server.tools.jobs.make_client", return_value=client) as make_client:
        async with Client(mcp) as mcp_client:
            job = (await mcp_client.call_tool("submit_batch_job", {"sas_code": "data _null_; run;"})).data
    make_client.assert_called_once_with(token)
    assert job["token_lifetime"]["renewed"] is False
    assert 7000 < job["token_lifetime"]["seconds_left"] <= 7200
    assert "warning" not in job["token_lifetime"]


async def test_short_lifetime_warning_identifies_the_already_submitted_job():
    mcp, _ = await _server(_jwt_expiring_in(300), tiers="4")
    client = _jobs_client()
    with patch("sas_mcp_server.tools.jobs.make_client", return_value=client):
        async with Client(mcp) as mcp_client:
            job = (await mcp_client.call_tool("submit_batch_job", {
                "sas_code": "run;", "expected_runtime_minutes": 60,
            })).data
    client.post.assert_awaited_once()
    assert job["id"] == "job-1"
    assert "Monitor the submitted job" in job["token_lifetime"]["warning"]
    assert "resubmit" not in job["token_lifetime"]["warning"]


@pytest.mark.parametrize("tiers", ["0", "3", "9"])
async def test_every_tier_selection_gets_server_status(tiers):
    mcp, _ = await _server("t", tiers=tiers)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "server_status" in names
    assert tools.TOOL_TIERS["server_status"] == 0
