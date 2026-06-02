# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for PermissiveOAuthProxy.load_access_token — the additive raw-bearer
path gated by ALLOW_RAW_BEARER.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sas_mcp_server.config as config


@pytest.mark.asyncio
async def test_standard_swap_succeeds_returns_validated():
    """When the standard MCP JWT swap succeeds, its result is returned as-is."""
    auth = config.viya_auth
    validated = MagicMock()
    with patch.object(config.OAuthProxy, "load_access_token",
                      AsyncMock(return_value=validated)):
        result = await auth.load_access_token("client-jwt")
    assert result is validated


@pytest.mark.asyncio
async def test_raw_bearer_disabled_returns_none():
    """Swap fails and ALLOW_RAW_BEARER is off -> None (no raw fallthrough)."""
    auth = config.viya_auth
    with patch.object(config.OAuthProxy, "load_access_token", AsyncMock(return_value=None)), \
         patch.object(config, "ALLOW_RAW_BEARER", False):
        result = await auth.load_access_token("raw-token")
    assert result is None


@pytest.mark.asyncio
async def test_raw_bearer_enabled_accepts_valid_upstream_jwt():
    """Swap fails, ALLOW_RAW_BEARER on, verifier accepts -> raw token returned."""
    auth = config.viya_auth
    raw = MagicMock()
    with patch.object(config.OAuthProxy, "load_access_token", AsyncMock(return_value=None)), \
         patch.object(config, "ALLOW_RAW_BEARER", True), \
         patch.object(auth, "_token_validator") as mock_validator:
        mock_validator.verify_token = AsyncMock(return_value=raw)
        result = await auth.load_access_token("raw-token")
    assert result is raw


@pytest.mark.asyncio
async def test_raw_bearer_enabled_rejects_invalid_token():
    """Swap fails, ALLOW_RAW_BEARER on, verifier rejects -> None."""
    auth = config.viya_auth
    with patch.object(config.OAuthProxy, "load_access_token", AsyncMock(return_value=None)), \
         patch.object(config, "ALLOW_RAW_BEARER", True), \
         patch.object(auth, "_token_validator") as mock_validator:
        mock_validator.verify_token = AsyncMock(return_value=None)
        result = await auth.load_access_token("bad-token")
    assert result is None
