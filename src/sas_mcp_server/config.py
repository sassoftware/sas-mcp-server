# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import ssl

from dotenv import load_dotenv
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier

load_dotenv()

SSL_VERIFY = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

if not SSL_VERIFY:
    # Disable SSL verification for self-signed Viya certificates
    import httpx
    _ssl_context = ssl.create_default_context()
    _ssl_context.check_hostname = False
    _ssl_context.verify_mode = ssl.CERT_NONE
    # Monkey-patch httpx to use our permissive SSL context by default
    _original_async_client_init = httpx.AsyncClient.__init__

    def _patched_async_client_init(self, *args, **kwargs):
        kwargs.setdefault("verify", _ssl_context)
        _original_async_client_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = _patched_async_client_init

    _original_client_init = httpx.Client.__init__

    def _patched_client_init(self, *args, **kwargs):
        kwargs.setdefault("verify", _ssl_context)
        _original_client_init(self, *args, **kwargs)

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

viya_auth = OAuthProxy(
    upstream_authorization_endpoint=AUTHORIZATION_ENDPOINT,
    upstream_token_endpoint=TOKEN_ENDPOINT,
    upstream_client_id=CLIENT_ID,
    upstream_client_secret="",
    jwt_signing_key=MCP_SIGNING_KEY,
    base_url=MCP_BASE_URL,
    forward_pkce=True,
    token_verifier=token_verifier,
    valid_scopes=[],
)