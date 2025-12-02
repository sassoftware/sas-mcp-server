# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Pytest configuration and shared fixtures for SAS MCP Server tests.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx


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
