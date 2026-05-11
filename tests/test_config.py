# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the config module.

These tests use ``importlib.reload`` (which preserves the module object's
identity and just re-runs its top-level code) rather than
``del sys.modules[...]`` + import, which would create a NEW module instance
and leave the old one orphaned. Orphaned config modules carry their own
``viya_auth = OAuthProxy(...)`` instance — each holding internal httpx
state. Accumulating orphans across multiple tests in the same pytest session
corrupts the event-loop-bound httpx state used by later integration tests,
producing empty-message ``httpcore.ConnectError``s on real Viya calls.
"""
import importlib
import sys
import pytest
from unittest.mock import patch


def _reload_config():
    """Reload sas_mcp_server.config in place. Imports first if not yet loaded."""
    if 'sas_mcp_server.config' in sys.modules:
        return importlib.reload(sys.modules['sas_mcp_server.config'])
    import sas_mcp_server.config as cfg
    return cfg


def test_config_loading_with_env_vars(mock_env_vars):
    """Test that configuration values are loaded from environment variables."""
    cfg = _reload_config()
    assert cfg.VIYA_ENDPOINT == "https://test.viya.com"
    assert cfg.CLIENT_ID == "test-client"
    assert cfg.HOST_PORT == 8134
    assert cfg.MCP_SIGNING_KEY == "test-key"
    assert cfg.CONTEXT_NAME == "Test Context"

    # Derived endpoints
    assert cfg.AUTHORIZATION_ENDPOINT == "https://test.viya.com/SASLogon/oauth/authorize"
    assert cfg.TOKEN_ENDPOINT == "https://test.viya.com/SASLogon/oauth/token"
    assert cfg.JWKS_URI == "https://test.viya.com/SASLogon/token_keys"


def test_config_viya_endpoint_trailing_slash(monkeypatch):
    """Test that trailing slashes are removed from VIYA_ENDPOINT."""
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com/")
    monkeypatch.setenv("CLIENT_ID", "test-client")
    monkeypatch.setenv("HOST_PORT", "8134")
    monkeypatch.setenv("MCP_SIGNING_KEY", "test-key")
    cfg = _reload_config()
    assert cfg.VIYA_ENDPOINT == "https://test.viya.com"


def test_config_missing_viya_endpoint(monkeypatch):
    """Test that missing VIYA_ENDPOINT raises an exception."""
    monkeypatch.delenv("VIYA_ENDPOINT", raising=False)
    monkeypatch.setenv("CLIENT_ID", "test-client")
    # Block module-level load_dotenv from reloading VIYA_ENDPOINT from .env.
    with patch('dotenv.load_dotenv'):
        with pytest.raises(Exception, match="VIYA_ENDPOINT is not set"):
            _reload_config()
    # Restore a valid module state for subsequent tests in the session, since
    # the failed reload leaves the module in a partially-initialised state.
    _reload_config()


def test_config_default_values(monkeypatch):
    """Test default values when optional env vars are not set."""
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    monkeypatch.delenv("CLIENT_ID", raising=False)
    monkeypatch.delenv("HOST_PORT", raising=False)
    monkeypatch.delenv("MCP_SIGNING_KEY", raising=False)
    monkeypatch.delenv("COMPUTE_CONTEXT_NAME", raising=False)
    # Block module-level load_dotenv from repopulating from .env.
    with patch('dotenv.load_dotenv'):
        cfg = _reload_config()
    assert cfg.CLIENT_ID == "sas-mcp"
    assert cfg.HOST_PORT == 8134
    assert cfg.MCP_SIGNING_KEY == "default"
    assert cfg.CONTEXT_NAME == "SAS Job Execution compute context"
