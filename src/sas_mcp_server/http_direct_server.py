#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
HTTP MCP Server for SAS Viya with direct (service-account) authentication.

Unlike the standard HTTP server (``app``), which requires each MCP client to
complete a browser-based OAuth flow, this server authenticates to Viya itself
using the password grant with the credentials from ``.env`` — the same model
as stdio mode — and serves the MCP protocol over streamable HTTP.

This is intended for MCP clients that cannot perform interactive OAuth, such
as SAS Retrieval Agent Manager (RAM) or other server-to-server integrations.
The endpoint can optionally be protected with a static API key by setting
``MCP_API_KEY``; clients must then send it as an ``X-API-Key`` header or as
an ``Authorization: Bearer`` token.
"""

import os
import time

import httpx
import uvicorn
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.exceptions import FastMCPError
from starlette.responses import JSONResponse

from .config import VIYA_ENDPOINT, CLIENT_ID, SSL_VERIFY, HOST_PORT
from .viya_utils import logger
from .tools import register_tools
from .prompts import register_prompts

load_dotenv()

VIYA_USERNAME = os.getenv("VIYA_USERNAME", "")
VIYA_PASSWORD = os.getenv("VIYA_PASSWORD", "")
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

# Refresh the cached token this many seconds before it actually expires.
_TOKEN_EXPIRY_MARGIN = 60.0

_token_cache = {"token": "", "expires_at": 0.0}


class AuthenticationError(FastMCPError):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return f"AuthenticationError: {self.message}"


async def get_viya_token() -> str:
    """Return a Viya access token, using the password grant with caching.

    The token is cached and reused until shortly before its expiry, so a
    busy agent does not hit /SASLogon on every tool call.
    """
    if _token_cache["token"] and time.monotonic() < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not VIYA_USERNAME or not VIYA_PASSWORD:
        raise AuthenticationError(
            "VIYA_USERNAME and VIYA_PASSWORD must be set in .env for "
            "direct HTTP mode"
        )
    async with httpx.AsyncClient(verify=SSL_VERIFY) as client:
        resp = await client.post(
            f"{VIYA_ENDPOINT}/SASLogon/oauth/token",
            auth=(CLIENT_ID, ""),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "password",
                "username": VIYA_USERNAME,
                "password": VIYA_PASSWORD,
            },
        )
    resp.raise_for_status()
    body = resp.json()
    _token_cache["token"] = body["access_token"]
    expires_in = float(body.get("expires_in", 0))
    _token_cache["expires_at"] = (
        time.monotonic() + max(expires_in - _TOKEN_EXPIRY_MARGIN, 0.0)
    )
    return _token_cache["token"]


async def _direct_get_token(ctx: Context) -> str:
    return await get_viya_token()


logger.info(f"Connecting to SAS Viya at {VIYA_ENDPOINT}")
mcp = FastMCP("SAS Viya Execution MCP Server")

register_tools(mcp, _direct_get_token)
register_prompts(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "service": "sas-viya-execution-mcp"})


class ApiKeyMiddleware:
    """ASGI middleware that rejects HTTP requests lacking the API key.

    Accepts the key via ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``.
    ``/health`` stays open so liveness probes work without credentials.
    """

    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        provided = headers.get("x-api-key", "")
        if not provided:
            auth = headers.get("authorization", "")
            parts = auth.split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                provided = parts[1]
        if provided != self.api_key:
            response = JSONResponse({"error": "invalid or missing API key"},
                                    status_code=401)
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def build_app():
    """Build the ASGI app, wrapping with API key auth when configured."""
    app = mcp.http_app()
    if MCP_API_KEY:
        logger.info("API key protection enabled (MCP_API_KEY is set)")
        return ApiKeyMiddleware(app, MCP_API_KEY)
    logger.warning("MCP_API_KEY is not set — the MCP endpoint is unauthenticated. "
                   "Anyone who can reach it can act on Viya as the .env user.")
    return app


def main():
    """Run the MCP server over streamable HTTP with direct Viya auth."""
    uvicorn.run(build_app(), host="0.0.0.0", port=HOST_PORT)


if __name__ == "__main__":
    main()
