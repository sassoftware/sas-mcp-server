# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the config module.
"""
import pytest
import os
import importlib
import sys
from unittest.mock import patch

def test_config_loading_with_env_vars(mock_env_vars):
    """Test that configuration values are loaded from environment variables."""
    # Remove config module from cache if it exists
    if 'sas_mcp_server.config' in sys.modules:
        del sys.modules['sas_mcp_server.config']
    
    # Import after setting env vars
    from sas_mcp_server.config import (
        VIYA_ENDPOINT,
        CLIENT_ID,
        HOST_PORT,
        MCP_SIGNING_KEY,
        CONTEXT_NAME,
        AUTHORIZATION_ENDPOINT,
        TOKEN_ENDPOINT,
        JWKS_URI
    )
    
    assert VIYA_ENDPOINT == "https://test.viya.com"
    assert CLIENT_ID == "test-client"
    assert HOST_PORT == 8134
    assert MCP_SIGNING_KEY == "test-key"
    assert CONTEXT_NAME == "Test Context"
    
    # Test derived endpoints
    assert AUTHORIZATION_ENDPOINT == "https://test.viya.com/SASLogon/oauth/authorize"
    assert TOKEN_ENDPOINT == "https://test.viya.com/SASLogon/oauth/token"
    assert JWKS_URI == "https://test.viya.com/SASLogon/token_keys"

def test_config_viya_endpoint_trailing_slash(monkeypatch):
    """Test that trailing slashes are removed from VIYA_ENDPOINT."""
    # Remove config module from cache if it exists
    if 'sas_mcp_server.config' in sys.modules:
        del sys.modules['sas_mcp_server.config']
    
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com/")
    monkeypatch.setenv("CLIENT_ID", "test-client")
    monkeypatch.setenv("HOST_PORT", "8134")
    monkeypatch.setenv("MCP_SIGNING_KEY", "test-key")
    
    # Import fresh config
    from sas_mcp_server import config as config_module
    
    assert config_module.VIYA_ENDPOINT == "https://test.viya.com"

def test_config_missing_viya_endpoint(monkeypatch):
    """Test that missing VIYA_ENDPOINT raises an exception."""
    # Remove config module from cache if it exists
    if 'sas_mcp_server.config' in sys.modules:
        del sys.modules['sas_mcp_server.config']
    
    # Also remove dependent modules that might have cached the config
    for mod in list(sys.modules.keys()):
        if mod.startswith('sas_mcp_server'):
            del sys.modules[mod]
    
    # Must unset VIYA_ENDPOINT before importing
    monkeypatch.delenv("VIYA_ENDPOINT", raising=False)
    monkeypatch.setenv("CLIENT_ID", "test-client")
    
    # Patch load_dotenv in dotenv module BEFORE importing config
    with patch('dotenv.load_dotenv'):
        with pytest.raises(Exception, match="VIYA_ENDPOINT is not set"):
            import sas_mcp_server.config as config_module

def test_config_default_values(monkeypatch):
    """Test default values when optional env vars are not set."""
    # Remove config module from cache if it exists
    if 'sas_mcp_server.config' in sys.modules:
        del sys.modules['sas_mcp_server.config']
    
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    monkeypatch.delenv("CLIENT_ID", raising=False)
    monkeypatch.delenv("HOST_PORT", raising=False)
    monkeypatch.delenv("MCP_SIGNING_KEY", raising=False)
    monkeypatch.delenv("COMPUTE_CONTEXT_NAME", raising=False)
    
    import sas_mcp_server.config as config_module
    
    assert config_module.CLIENT_ID == "sas-mcp"
    assert config_module.HOST_PORT == 8134
    assert config_module.MCP_SIGNING_KEY == "default"
    assert config_module.CONTEXT_NAME == "SAS Job Execution compute context"