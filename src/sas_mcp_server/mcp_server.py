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
from fastmcp.tools.tool import ToolResult
from starlette.responses import JSONResponse
from .viya_utils import run_one_snippet, logger

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
mcp = FastMCP("SAS Viya Execution MCP Server", auth=viya_auth)
mcp.add_middleware(AuthMiddleware())


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    logger.info("Performing health check . . .")
    return JSONResponse({"status": "healthy", "service": "sas-viya-execution-mcp"})


@mcp.tool()
async def execute_sas_code(sas_code: str, ctx: Context) -> ToolResult:
    """
    Executes the provided SAS code in the Viya environment and returns information about the completed Job.
    This will create a job definition for the SAS code, execute it, and then retrieve the results.

    Args:
        sas_code (str): the SAS code snippet to be executed using the Viya Job Execution API Service

    Returns:
        Structured output data containing detailed information about the executed sas code.
        This includes a listing field and a log field. The listing output represents the intended output
        of the SAS code when executed, if the code ran successfully. The log output represents information
        about the execution of the sas code, such as if it ran successfully or not and whether or not there are
        errors or issues with the execution.

    """
    logger.info("--- TOOL USED: execute_sas_code ---")
    token = ctx.get_state("access_token")
    if not token:
        raise AuthenticationError("No auth header found. Cannot authenticate to Viya")

    # Run the async function directly
    output = await run_one_snippet(sas_code, "1", token)
    return output


app = mcp.http_app()