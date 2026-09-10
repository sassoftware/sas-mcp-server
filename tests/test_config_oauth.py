# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the configured PermissiveOAuthProxy: the additive raw-bearer path
gated by ALLOW_RAW_BEARER, and the credential the proxy presents to SAS Logon
when it exchanges a code upstream.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sas_mcp_server.auth as auth_mod
import sas_mcp_server.config as config


@pytest.mark.asyncio
async def test_standard_swap_succeeds_returns_validated():
    """When the standard MCP JWT swap succeeds, its result is returned as-is."""
    auth = config.viya_auth
    validated = MagicMock()
    with patch.object(auth_mod.OAuthProxy, "load_access_token",
                      AsyncMock(return_value=validated)):
        result = await auth.load_access_token("client-jwt")
    assert result is validated


@pytest.mark.asyncio
async def test_raw_bearer_disabled_returns_none():
    """Swap fails and ALLOW_RAW_BEARER is off -> None (no raw fallthrough)."""
    auth = config.viya_auth
    with patch.object(auth_mod.OAuthProxy, "load_access_token", AsyncMock(return_value=None)), \
         patch.object(auth, "_allow_raw_bearer", False):
        result = await auth.load_access_token("raw-token")
    assert result is None


@pytest.mark.asyncio
async def test_raw_bearer_enabled_accepts_valid_upstream_jwt():
    """Swap fails, ALLOW_RAW_BEARER on, verifier accepts -> raw token returned."""
    auth = config.viya_auth
    raw = MagicMock()
    with patch.object(auth_mod.OAuthProxy, "load_access_token", AsyncMock(return_value=None)), \
         patch.object(auth, "_allow_raw_bearer", True), \
         patch.object(auth, "_token_validator") as mock_validator:
        mock_validator.verify_token = AsyncMock(return_value=raw)
        result = await auth.load_access_token("raw-token")
    assert result is raw


@pytest.mark.asyncio
async def test_raw_bearer_enabled_rejects_invalid_token():
    """Swap fails, ALLOW_RAW_BEARER on, verifier rejects -> None."""
    auth = config.viya_auth
    with patch.object(auth_mod.OAuthProxy, "load_access_token", AsyncMock(return_value=None)), \
         patch.object(auth, "_allow_raw_bearer", True), \
         patch.object(auth, "_token_validator") as mock_validator:
        mock_validator.verify_token = AsyncMock(return_value=None)
        result = await auth.load_access_token("bad-token")
    assert result is None


def test_upstream_token_request_presents_no_client_password():
    """sas-mcp is a public client, so the upstream exchange must send no secret.

    This asserts the bytes rather than the setting, because the setting is not
    what broke. FastMCP 4.0 replaced authlib's OAuth2 client with its own, and
    the replacement defaults to ``client_secret_basic`` whether or not a secret
    exists — where authlib had chosen ``none`` when there was none. Our
    ``upstream_client_secret=None`` was therefore f-string interpolated into
    ``Basic base64("sas-mcp:None")``, the literal word "None" as the password,
    and SAS Logon rejected every browser sign-in with "invalid_client: Missing
    credentials" (#54). Nothing else in the suite constructs the upstream
    client, so the break shipped in three releases unnoticed.

    It reaches into two FastMCP internals on purpose: they are what carries the
    credential onto the wire, so if a future release renames them this test
    should fail loudly rather than keep passing while the contract moves.
    """
    client = config.viya_auth._create_upstream_oauth_client()
    data: dict = {}
    headers: dict = {}
    client._apply_client_auth(data, headers)

    assert "Authorization" not in headers, (
        f"public client sent a password: {headers.get('Authorization')!r}"
    )
    # RFC 6749 §2.3.1: a client without a secret identifies itself in the body.
    assert data.get("client_id") == config.CLIENT_ID

def test_cimd_is_disabled_for_dynamic_localhost_callbacks():
    """Local MCP callbacks must use normal dynamic client registration.
    Works with enable_cimd=False to the PermissiveOAuthProxy(...) call in src/sas_mcp_server/config.py
    """
    assert config.viya_auth._cimd_manager is None
