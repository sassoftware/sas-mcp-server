# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration and shared fixtures for SAS MCP Server tests.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from fastmcp import FastMCP, Client


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables for testing."""
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    monkeypatch.setenv("CLIENT_ID", "test-client")
    monkeypatch.setenv("HOST_PORT", "8134")
    monkeypatch.setenv("MCP_SIGNING_KEY", "test-key")
    monkeypatch.setenv("COMPUTE_CONTEXT_NAME", "Test Context")


@pytest.fixture
def sample_sas_code():
    """Sample SAS code for testing."""
    return """
    data test;
        x = 1;
        y = 2;
    run;
    
    proc print data=test;
    run;
    """


@pytest.fixture
def mock_httpx_client():
    """Mock httpx AsyncClient for testing API calls."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    return mock_client


@pytest.fixture
def mock_context_response():
    """Mock response for compute context API call."""
    return {
        "items": [
            {
                "id": "test-context-id",
                "name": "Test Context",
                "version": 2
            }
        ]
    }


@pytest.fixture
def mock_session_response():
    """Mock response for session creation."""
    return {
        "id": "test-session-id",
        "name": "py-parallel",
        "state": "idle"
    }


@pytest.fixture
def mock_job_response():
    """Mock response for job submission."""
    return {
        "id": "test-job-id",
        "sessionId": "test-session-id",
        "state": "running"
    }


@pytest.fixture
def mock_job_log():
    """Mock job log output."""
    return {
        "items": [
            {"line": "NOTE: DATA statement used (Total process time):"},
            {"line": "      real time           0.01 seconds"},
            {"line": "      cpu time            0.01 seconds"}
        ]
    }


@pytest.fixture
def mock_job_listing():
    """Mock job listing output."""
    return {
        "items": [
            {"line": "Obs    x    y"},
            {"line": "  1    1    2"}
        ]
    }


@pytest.fixture
def mock_access_token():
    """Mock Viya access token."""
    return "mock-access-token-12345"


@pytest.fixture
def mock_bearer_token():
    """Mock Bearer token from client."""
    return "Bearer client-jwt-token-12345"


@pytest.fixture
def mock_viya_access_info():
    """Mock ViyaAccessInfo object."""
    mock_info = MagicMock()
    mock_info.token = "mock-viya-access-token"
    return mock_info


# ---------------------------------------------------------------------------
# Payload test fixtures
# ---------------------------------------------------------------------------


def _make_mock_response(json_data=None, status_code=200, text=None):
    """Create a mock httpx response."""
    resp = AsyncMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_data or {})
    resp.content = b'{}' if json_data is not None or status_code != 204 else b''
    resp.text = text or ""
    resp.headers = {"Content-Type": "application/json"}
    return resp


@pytest.fixture
def mock_json_response():
    """Factory fixture to create mock HTTP responses."""
    return _make_mock_response


@pytest.fixture
def mcp_server_with_mock_client():
    """Create an MCP server with a mock HTTP client for payload testing.

    Returns (mcp, mock_client) — the mock_client captures all HTTP calls
    made by tools so tests can inspect URLs, bodies, params, and headers.
    """
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    paged_resp = _make_mock_response({"items": [], "count": 0})
    json_resp = _make_mock_response({"id": "test-id"})
    post_resp = _make_mock_response({"id": "test-id"}, status_code=201)
    post_resp.content = b'{"id": "test-id"}'
    put_resp = _make_mock_response({"tableName": "test"}, status_code=201)
    put_resp.content = b'{"tableName": "test"}'
    delete_resp = _make_mock_response(status_code=204)

    mock_client.get.return_value = paged_resp
    mock_client.post.return_value = post_resp
    mock_client.put.return_value = put_resp
    mock_client.delete.return_value = delete_resp

    with patch("sas_mcp_server.tools._make_client", return_value=mock_client):
        mcp = FastMCP("Payload Test Server")

        async def mock_get_token(ctx):
            return "test-token"

        from sas_mcp_server.tools import register_tools
        register_tools(mcp, mock_get_token)
        yield mcp, mock_client


# ---------------------------------------------------------------------------
# Integration test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def viya_credentials():
    """Load Viya credentials from environment. Skip if not available."""
    endpoint = os.getenv("VIYA_ENDPOINT", "")
    username = os.getenv("VIYA_USERNAME", "")
    password = os.getenv("VIYA_PASSWORD", "")
    if not all([endpoint, username, password]):
        pytest.skip("VIYA_ENDPOINT, VIYA_USERNAME, and VIYA_PASSWORD required")
    return {"endpoint": endpoint, "username": username, "password": password}


@pytest.fixture(scope="session")
def viya_token(viya_credentials):
    """Get a real Viya access token via password grant."""
    from sas_mcp_server.config import CLIENT_ID, SSL_VERIFY
    resp = httpx.post(
        f"{viya_credentials['endpoint']}/SASLogon/oauth/token",
        auth=(CLIENT_ID, ""),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "password",
            "username": viya_credentials["username"],
            "password": viya_credentials["password"],
        },
        verify=SSL_VERIFY,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="session")
def integration_mcp_server(viya_token):
    """Create an MCP server with real Viya auth for integration tests."""
    mcp = FastMCP("Integration Test Server")

    _token = viya_token

    async def real_get_token(ctx):
        return _token

    from sas_mcp_server.tools import register_tools
    register_tools(mcp, real_get_token)
    return mcp
