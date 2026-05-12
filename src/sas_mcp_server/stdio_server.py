#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Stdio MCP Server for SAS Viya.

Authenticates via OAuth 2.0 Device Authorization Grant (RFC 8628). Two paths
are supported, tried in order:

  1. Token cached by the SAS Viya CLI's ``sas-viya auth loginCode`` command.
     Default location: ``~/.sas/credentials.json`` (override the parent dir
     with the ``SAS_CLI_CONFIG`` env var). This path is the recommended one
     because SAS Logon Manager typically CSRF-protects the device endpoint,
     so the CLI's browser-driven flow is the path of least resistance.

  2. Native device-code flow against ``/SASLogon/oauth/device_authorization``.
     Used only when no cached credentials exist. Works on Viya instances
     whose admins have not enabled CSRF protection on the device endpoint
     and whose OAuth client is registered with the device-code grant type.

Password grant has been removed: it requires a password in plaintext on disk,
OAuth 2.1 deprecates it, and it does not work for confidential OAuth clients
(SAS Logon rejects empty client secrets with ``invalid_client``).
"""

import json
import os
import sys
import time
import webbrowser
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.exceptions import FastMCPError

from .config import CLIENT_ID, SSL_VERIFY, VIYA_ENDPOINT
from .prompts import register_prompts
from .tools import register_tools
from .viya_utils import logger

load_dotenv()

SAS_CLI_CONFIG = os.getenv("SAS_CLI_CONFIG", "")
DEVICE_AUTH_URL = f"{VIYA_ENDPOINT}/SASLogon/oauth/device_authorization"
TOKEN_URL = f"{VIYA_ENDPOINT}/SASLogon/oauth/token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class AuthenticationError(FastMCPError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return f"AuthenticationError: {self.message}"


def _credentials_path() -> Path:
    """Location of the access token cached by ``sas-viya auth loginCode``."""
    base = Path(SAS_CLI_CONFIG) if SAS_CLI_CONFIG else Path.home()
    return base / ".sas" / "credentials.json"


def _read_sas_cli_token() -> str | None:
    """Return the access token cached by the SAS Viya CLI, or ``None``."""
    path = _credentials_path()
    if not path.exists():
        return None
    try:
        creds = json.loads(path.read_text())
        token = creds["Default"]["access-token"]
    except (KeyError, json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not read sas-viya credentials at {path}: {exc}")
        return None
    logger.info(f"Loaded access token from {path}")
    return token


def _native_device_code_token() -> str:
    """Run RFC 8628 device flow directly against SAS Logon."""
    init = httpx.post(
        DEVICE_AUTH_URL,
        auth=(CLIENT_ID, ""),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={"client_id": CLIENT_ID, "scope": "openid"},
        verify=SSL_VERIFY,
        timeout=15.0,
    )
    if init.status_code == 403 and "CSRF" in init.text:
        raise AuthenticationError(
            "Viya rejected the device-authorization request (CSRF protection "
            "on /SASLogon/oauth/device_authorization). Install the sas-viya "
            "CLI, run `sas-viya auth loginCode`, and re-launch this server; "
            f"the cached token at {_credentials_path()} will be used."
        )
    init.raise_for_status()
    flow = init.json()

    verification_uri = flow.get("verification_uri_complete") or flow["verification_uri"]
    user_code = flow["user_code"]
    expires_in = int(flow.get("expires_in", 1800))
    interval = max(int(flow.get("interval", 5)), 5)

    msg = (
        f"\n[sas-mcp-server] To authenticate, open:\n  {verification_uri}\n"
        f"and enter code: {user_code}\n"
    )
    logger.info(msg)
    print(msg, file=sys.stderr, flush=True)
    try:
        webbrowser.open(verification_uri, new=2)
    except Exception:
        pass

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        poll = httpx.post(
            TOKEN_URL,
            auth=(CLIENT_ID, ""),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": flow["device_code"],
                "client_id": CLIENT_ID,
            },
            verify=SSL_VERIFY,
            timeout=15.0,
        )
        if poll.status_code == 200:
            return poll.json()["access_token"]
        try:
            err = poll.json().get("error")
        except Exception:
            err = None
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise AuthenticationError(
            f"Device-code authentication failed: {err or poll.text[:200]}"
        )
    raise AuthenticationError("Device-code authentication timed out")


def _get_viya_token() -> str:
    token = _read_sas_cli_token()
    if token:
        return token
    logger.info(
        "No cached sas-viya CLI credentials; attempting native device-code flow"
    )
    return _native_device_code_token()


async def _stdio_get_token(ctx: Context) -> str:
    return _get_viya_token()


logger.info(f"Connecting to SAS Viya at {VIYA_ENDPOINT}")
mcp = FastMCP("SAS Viya Execution MCP Server")
register_tools(mcp, _stdio_get_token)
register_prompts(mcp)


def main() -> None:
    """Run the MCP server in stdio mode."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
