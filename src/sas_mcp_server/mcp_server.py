#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Starter MCP Server for SAS Viya, utilizing the SAS Viya OAuth flow for authentication.
Handles session management, job submission, and result retrieval using httpx.
"""

# Auth handling and API access with Viya
from .config import viya_auth
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.exceptions import FastMCPError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.responses import JSONResponse
from .viya_utils import logger
from .tools import register_tools
from .prompts import register_prompts

# Load environment variables before accessing them
load_dotenv()


class AuthenticationError(FastMCPError):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return f"AuthenticationError: {self.message}"


class AuthMiddleware(Middleware):
    async def on_call_tool(self, ctx: MiddlewareContext, call_next):
        request = get_http_request()
        bearer_token = request.headers.get("Authorization")
        if bearer_token:
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
                viya_access_token = viya_access_info.token
                ctx.fastmcp_context.set_state("access_token", viya_access_token)
            else:
                logger.error("Could not retrieve upstream access token!")
        else:
            logger.error("No auth header found. Cannot proceed")
            raise AuthenticationError("No auth header found. Cannot proceed")
        return await call_next(ctx)


# Initialize the FastMCP server
from .config import VIYA_ENDPOINT
logger.info(f"Connecting to SAS Viya at {VIYA_ENDPOINT}")
mcp = FastMCP("SAS Viya Execution MCP Server", auth=viya_auth)
mcp.add_middleware(AuthMiddleware())


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    logger.info("Performing health check . . .")
    return JSONResponse({"status": "healthy", "service": "sas-viya-execution-mcp"})


# Token getter for HTTP mode: reads from context state set by AuthMiddleware
async def _http_get_token(ctx: Context) -> str:
    token = ctx.get_state("access_token")
    if not token:
        raise AuthenticationError("No auth header found. Cannot authenticate to Viya")
    return token


# Register all tools and prompts
register_tools(mcp, _http_get_token)
register_prompts(mcp)

app = mcp.http_app()
