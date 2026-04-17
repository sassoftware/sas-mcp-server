#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Stdio MCP Server for SAS Viya.
Authenticates directly to Viya using password grant, allowing MCP clients
to start the server on demand without a pre-running HTTP server.
"""

import os
import httpx
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.exceptions import FastMCPError
from .config import VIYA_ENDPOINT, CLIENT_ID, SSL_VERIFY
from .viya_utils import logger
from .tools import register_tools
from .prompts import register_prompts

load_dotenv()

VIYA_USERNAME = os.getenv("VIYA_USERNAME", "")
VIYA_PASSWORD = os.getenv("VIYA_PASSWORD", "")


class AuthenticationError(FastMCPError):
    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return f"AuthenticationError: {self.message}"


def _get_viya_token() -> str:
    """Authenticate to Viya using password grant and return an access token."""
    if not VIYA_USERNAME or not VIYA_PASSWORD:
        raise AuthenticationError(
            "VIYA_USERNAME and VIYA_PASSWORD must be set in .env for stdio mode"
        )
    resp = httpx.post(
        f"{VIYA_ENDPOINT}/SASLogon/oauth/token",
        auth=(CLIENT_ID, ""),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "password",
            "username": VIYA_USERNAME,
            "password": VIYA_PASSWORD,
        },
        verify=SSL_VERIFY,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# Token getter for stdio mode: acquires token via password grant
async def _stdio_get_token(ctx: Context) -> str:
    return _get_viya_token()


# Initialize the FastMCP server (no auth — stdio clients handle auth differently)
logger.info(f"Connecting to SAS Viya at {VIYA_ENDPOINT}")
mcp = FastMCP("SAS Viya Execution MCP Server")

# Register all tools and prompts
register_tools(mcp, _stdio_get_token)
register_prompts(mcp)


def main():
    """Run the MCP server in stdio mode."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
