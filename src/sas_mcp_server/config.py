# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
import os
import ssl

from dotenv import load_dotenv
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.provider import AccessToken

load_dotenv()

SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")
ALLOW_RAW_BEARER = os.getenv("ALLOW_RAW_BEARER", "false").lower() in ("true", "1", "yes")

_logger = logging.getLogger(__name__)


class PermissiveOAuthProxy(OAuthProxy):
    """OAuthProxy that optionally accepts raw upstream JWTs.

    When ``ALLOW_RAW_BEARER`` is set, a bearer token that fails the standard
    MCP JWT swap (because it isn't a proxy-issued JWT) falls through to the
    configured ``token_verifier``. If the verifier accepts it (i.e. the
    token is a valid Viya JWT signed by the upstream JWKS), the request
    proceeds with the raw token used directly as the upstream credential.

    This lets PKCE clients and pre-authenticated programmatic clients hit
    the same MCP endpoint without conflict — the additive path only kicks
    in after the standard swap has already failed.
    """

    async def load_access_token(self, token: str) -> AccessToken | None:
        validated = await super().load_access_token(token)
        if validated is not None:
            return validated
        if not ALLOW_RAW_BEARER:
            return None
        raw = await self._token_validator.verify_token(token)
        if raw is not None:
            _logger.info(
                "Accepted raw bearer token (ALLOW_RAW_BEARER=true); "
                "bypassing MCP JWT swap"
            )
        return raw

if not SSL_VERIFY:
    # Disable SSL verification for self-signed Viya certificates
    import httpx
    # Guard against re-patching when this module is reloaded (e.g. by tests
    # that del sys.modules['sas_mcp_server.config'] and re-import). Without
    # this, each reload stacks another wrapper around the existing one,
    # eventually breaking outbound httpx connections in the same process.
    if not getattr(httpx.AsyncClient.__init__, "_sas_mcp_ssl_patched", False):
        _ssl_context = ssl.create_default_context()
        _ssl_context.check_hostname = False
        _ssl_context.verify_mode = ssl.CERT_NONE
        # Monkey-patch httpx to use our permissive SSL context by default
        _original_async_client_init = httpx.AsyncClient.__init__

        def _patched_async_client_init(self, *args, **kwargs):
            kwargs.setdefault("verify", _ssl_context)
            _original_async_client_init(self, *args, **kwargs)

        _patched_async_client_init._sas_mcp_ssl_patched = True
        httpx.AsyncClient.__init__ = _patched_async_client_init

        _original_client_init = httpx.Client.__init__

        def _patched_client_init(self, *args, **kwargs):
            kwargs.setdefault("verify", _ssl_context)
            _original_client_init(self, *args, **kwargs)

        _patched_client_init._sas_mcp_ssl_patched = True
        httpx.Client.__init__ = _patched_client_init

VIYA_ENDPOINT = os.getenv("VIYA_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.getenv("CLIENT_ID", "sas-mcp")
HOST_PORT = int(os.getenv("HOST_PORT", "8134"))
MCP_SIGNING_KEY = os.getenv("MCP_SIGNING_KEY", "default")
CONTEXT_NAME = os.getenv("COMPUTE_CONTEXT_NAME", "SAS Job Execution compute context")
MCP_BASE_URL = os.getenv("MCP_BASE_URL", f"http://localhost:{HOST_PORT}")

if not VIYA_ENDPOINT:
    raise Exception(
        "VIYA_ENDPOINT is not set. Please set it in the environment variables."
    )

AUTHORIZATION_ENDPOINT = f"{VIYA_ENDPOINT}/SASLogon/oauth/authorize"
TOKEN_ENDPOINT = f"{VIYA_ENDPOINT}/SASLogon/oauth/token"
JWKS_URI = f"{VIYA_ENDPOINT}/SASLogon/token_keys"


token_verifier = JWTVerifier(jwks_uri=JWKS_URI, audience=[])

viya_auth = PermissiveOAuthProxy(
    upstream_authorization_endpoint=AUTHORIZATION_ENDPOINT,
    upstream_token_endpoint=TOKEN_ENDPOINT,
    upstream_client_id=CLIENT_ID,
    upstream_client_secret=None,
    jwt_signing_key=MCP_SIGNING_KEY,
    base_url=MCP_BASE_URL,
    forward_pkce=True,
    token_verifier=token_verifier,
    valid_scopes=["openid"],
)