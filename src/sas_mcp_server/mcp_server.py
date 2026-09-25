#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Starter MCP Server for SAS Viya, utilizing the SAS Viya OAuth flow for authentication.
Handles session management, job submission, and result retrieval using httpx.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fastmcp
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import (
    ALLOW_RAW_BEARER,
    AUTH_ENABLED,
    MCP_BASE_URL,
    MCP_LANDING_PAGE,
    MCP_READ_ONLY,
    MCP_STATELESS_HTTP,
    SERVER_NAME,
    VIYA_ENDPOINT,
    viya_auth,
)
from .exceptions import AuthenticationError
from .helpers.telemetry_helpers import server_version
from .http_debug import install_http_debug
from .landing import LandingPageMiddleware, ServerFacts, collect_facts
from .prompts import register_prompts
from .telemetry import install_telemetry
from .tools import register_tools
from .viya_client import announce_startup, logger
from .viya_utils import shutdown_session_cache

# Load environment variables before accessing them
load_dotenv()


class AuthMiddleware(Middleware):
    async def on_call_tool(self, ctx: MiddlewareContext, call_next: Any) -> Any:
        request = get_http_request()
        bearer_token = request.headers.get("Authorization")
        if not bearer_token:
            logger.error("No auth header found. Cannot proceed")
            raise AuthenticationError("No auth header found. Cannot proceed")

        parts = bearer_token.split()
        jwt = (
            parts[1]
            if len(parts) > 1 and parts[0].lower() == "bearer"
            else bearer_token
        )
        logger.info("Client auth header found, Swapping for upstream token")
        viya_access_info = await viya_auth.load_access_token(jwt)
        if viya_access_info:
            logger.info("Viya access info retrieved successfully!")
            fastmcp_ctx = ctx.fastmcp_context
            if fastmcp_ctx is not None:
                await fastmcp_ctx.set_state("access_token", viya_access_info.token)
        else:
            logger.error("Could not retrieve upstream access token!")
        return await call_next(ctx)


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Tear down warm compute sessions when the server stops."""
    try:
        yield {}
    finally:
        await shutdown_session_cache()


# Initialize the FastMCP server
SERVER_VERSION = server_version()
announce_startup("http", SERVER_VERSION)
_mcp_kwargs: dict[str, Any] = {"lifespan": _lifespan, "version": SERVER_VERSION}
if AUTH_ENABLED:
    _mcp_kwargs["auth"] = viya_auth
else:
    logger.warning(
        "VIYA_AUTH=false: SASLogon authentication is disabled; Viya API calls are sent without Authorization headers"
    )
mcp = FastMCP(SERVER_NAME, **_mcp_kwargs)
# Opt-in telemetry (no-op unless COLLECTION_MODE is enabled). Added FIRST so it
# is the OUTERMOST middleware — it wraps AuthMiddleware and the tool, so an auth
# failure is recorded as status="error" and re-raised unchanged.
install_telemetry(mcp, "http")
install_http_debug()
if AUTH_ENABLED:
    mcp.add_middleware(AuthMiddleware())


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    logger.info("Performing health check . . .")
    return JSONResponse({"status": "healthy", "service": "sas-viya-execution-mcp"})


# Token getter for HTTP mode: reads from context state set by AuthMiddleware
async def _http_get_token(ctx: Context) -> str:
    if not AUTH_ENABLED:
        return ""
    token = await ctx.get_state("access_token")
    if not token:
        raise AuthenticationError("No auth header found. Cannot authenticate to Viya")
    return token


# Register all tools and prompts
register_tools(mcp, _http_get_token)
register_prompts(mcp)

# The path FastMCP mounts the MCP transport on ("/mcp" unless overridden via
# FASTMCP_STREAMABLE_HTTP_PATH) — the landing page must answer on exactly it.
MCP_PATH: str = fastmcp.settings.streamable_http_path


async def _landing_facts() -> ServerFacts:
    """Snapshot rendered by the browser landing page (called once, then cached
    by the middleware — the catalogue is fixed for the process lifetime)."""
    return await collect_facts(
        mcp,
        server_name=SERVER_NAME,
        mcp_url=MCP_BASE_URL.rstrip("/") + MCP_PATH,
        viya_endpoint=VIYA_ENDPOINT,
        auth_enabled=AUTH_ENABLED,
        allow_raw_bearer=ALLOW_RAW_BEARER,
        read_only=MCP_READ_ONLY,
    )


# Starlette middleware wraps the router, so this runs BEFORE the
# RequireAuthMiddleware FastMCP puts on the MCP route — the only place a
# browser GET can be answered with a page instead of the 401. Everything that
# is not `GET /mcp` + `Accept: text/html` is passed through untouched.
_http_middleware: list[StarletteMiddleware] = []
if MCP_LANDING_PAGE:
    _http_middleware.append(
        StarletteMiddleware(LandingPageMiddleware, path=MCP_PATH, facts=_landing_facts)
    )

app = mcp.http_app(middleware=_http_middleware or None, stateless_http=MCP_STATELESS_HTTP)
