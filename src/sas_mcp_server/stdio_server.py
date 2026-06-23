#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Stdio MCP Server for SAS Viya.

Authenticates via OAuth 2.0. Three paths are tried in order; the first one
that yields a *usable* token wins. A cached token counts as usable only if it
is present and not expired — an expired token is skipped rather than served, so
a stale cache never shadows a valid one. When a cache's access token is expired
but it still holds a refresh token, the refresh token is exchanged for a fresh
access token, which is written back to the same cache before use.

  1. Token cached by the SAS Viya CLI's ``sas-viya auth loginCode`` command.
     Default location: ``~/.sas/credentials.json`` (override the parent dir
     with the ``SAS_CLI_CONFIG`` env var).

  2. Token cached by this project's own zero-prereq login helper
     (``uv run sas-mcp-login``). Default location:
     ``~/.sas-mcp-server/credentials.json``. The helper runs Authorization
     Code + PKCE against the built-in ``vscode`` OAuth client and writes
     the token in the same shape as the SAS Viya CLI cache.

  3. Native device-code flow against ``/SASLogon/oauth/device_authorization``.
     Used only when no cached credentials exist. Works on Viya instances
     whose admins have not enabled CSRF protection on the device endpoint
     and whose OAuth client is registered with the device-code grant type.

Password grant has been removed: it requires a password in plaintext on disk,
OAuth 2.1 deprecates it, and it does not work for confidential OAuth clients
(SAS Logon rejects empty client secrets with ``invalid_client``).
"""

import contextlib
import json
import os
import sys
import time
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastmcp import Context, FastMCP

from .config import CLIENT_ID, SSL_VERIFY, VIYA_ENDPOINT
from .exceptions import AuthenticationError
from .prompts import register_prompts
from .tools import register_tools
from .viya_client import logger
from .viya_utils import shutdown_session_cache

load_dotenv()

SAS_CLI_CONFIG = os.getenv("SAS_CLI_CONFIG", "")
DEVICE_AUTH_URL = f"{VIYA_ENDPOINT}/SASLogon/oauth/device_authorization"
TOKEN_URL = f"{VIYA_ENDPOINT}/SASLogon/oauth/token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# OAuth client that minted each cache's tokens — needed to refresh them. The
# SAS Viya CLI uses ``sas.cli`` and ``sas-mcp-login`` uses ``vscode``; both are
# overridable for sites that registered different public clients.
SAS_CLI_CLIENT_ID = os.getenv("SAS_CLI_CLIENT_ID", "sas.cli")
HELPER_CLIENT_ID = os.getenv("AUTH_HELPER_CLIENT_ID", "vscode")
# Treat a token as expired this many seconds early, so we refresh before a call
# in flight can race the real expiry.
TOKEN_EXPIRY_SKEW = 60


def _sas_cli_credentials_path() -> Path:
    """Location of the access token cached by ``sas-viya auth loginCode``."""
    base = Path(SAS_CLI_CONFIG) if SAS_CLI_CONFIG else Path.home()
    return base / ".sas" / "credentials.json"


def _helper_credentials_path() -> Path:
    """Location of the access token cached by ``sas-mcp-login``."""
    return Path.home() / ".sas-mcp-server" / "credentials.json"


def _load_credentials(path: Path) -> dict | None:
    """Return the ``Default`` credential block from *path*, or ``None``."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())["Default"]
    except (KeyError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read credentials at %s: %s", path, exc)
        return None


def _token_expired(creds: dict, skew_seconds: int = TOKEN_EXPIRY_SKEW) -> bool:
    """Whether the cached token is at or past its expiry (minus a safety skew).

    A missing or unparseable ``expiry`` is treated as *not* expired, so caches
    that never recorded one keep working exactly as before.
    """
    raw = creds.get("expiry")
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return datetime.now(UTC) >= expiry - timedelta(seconds=skew_seconds)


def _read_cached_token(path: Path) -> str | None:
    """Return a *non-expired* ``Default.access-token`` from *path*, or ``None``.

    An expired token returns ``None`` so the caller falls through to a refresh
    or the next credential source instead of serving a token Viya will 401.
    """
    creds = _load_credentials(path)
    if creds is None:
        return None
    token = creds.get("access-token")
    if not token:
        logger.warning("No access-token in credentials at %s", path)
        return None
    if _token_expired(creds):
        logger.info("Cached token at %s is expired; will refresh or fall back", path)
        return None
    logger.info("Loaded access token from %s", path)
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
            "on /SASLogon/oauth/device_authorization). Run either "
            "`sas-viya auth loginCode` (writes ~/.sas/credentials.json) or "
            "`uv run sas-mcp-login` (writes ~/.sas-mcp-server/credentials.json) "
            "and re-launch this server."
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
    with contextlib.suppress(Exception):
        webbrowser.open(verification_uri, new=2)

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


def _refresh_access_token(refresh_token: str, client_id: str) -> dict | None:
    """Exchange a refresh token for a fresh token set, or ``None`` on failure.

    Best-effort: a wrong client, a revoked or expired refresh token, or a
    network error all return ``None`` so the caller falls through cleanly.
    """
    try:
        resp = httpx.post(
            TOKEN_URL,
            auth=(client_id, ""),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            verify=SSL_VERIFY,
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Token refresh request failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.info(
            "Token refresh rejected (HTTP %s) for client %s", resp.status_code, client_id
        )
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _write_credentials(
    path: Path, access_token: str, refresh_token: str, expires_in: int
) -> None:
    """Persist a refreshed token set in the same shape ``sas-mcp-login`` writes."""
    expiry = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    payload = {
        "Default": {
            "access-token": access_token,
            "refresh-token": refresh_token,
            "expiry": expiry,
        }
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("Could not write refreshed credentials to %s: %s", path, exc)


def _refresh_cached_token(path: Path, client_id: str) -> str | None:
    """Refresh the token cached at *path* and return the new access token.

    Returns ``None`` if there is no refresh token or the exchange fails.
    """
    creds = _load_credentials(path)
    if creds is None:
        return None
    refresh_token = creds.get("refresh-token")
    if not refresh_token:
        return None
    tokens = _refresh_access_token(refresh_token, client_id)
    if not tokens or not tokens.get("access_token"):
        return None
    # SAS Logon may or may not rotate the refresh token; keep the old one if not.
    new_refresh = tokens.get("refresh_token") or refresh_token
    _write_credentials(
        path, tokens["access_token"], new_refresh, int(tokens.get("expires_in", 0))
    )
    logger.info("Refreshed access token and updated cache at %s", path)
    return tokens["access_token"]


def _get_viya_token() -> str:
    for path, client_id in (
        (_sas_cli_credentials_path(), SAS_CLI_CLIENT_ID),
        (_helper_credentials_path(), HELPER_CLIENT_ID),
    ):
        token = _read_cached_token(path)
        if token:
            return token
        # Present-but-expired (or absent access token): try a refresh before
        # giving up on this source and moving to the next.
        token = _refresh_cached_token(path, client_id)
        if token:
            return token
    logger.info(
        "No usable cached credentials at ~/.sas or ~/.sas-mcp-server; "
        "attempting native device-code flow"
    )
    return _native_device_code_token()


async def _stdio_get_token(ctx: Context) -> str:
    return _get_viya_token()


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Tear down warm compute sessions when the server stops."""
    try:
        yield {}
    finally:
        await shutdown_session_cache()


logger.info("Connecting to SAS Viya at %s", VIYA_ENDPOINT)
mcp = FastMCP("SAS Viya Execution MCP Server", lifespan=_lifespan)
register_tools(mcp, _stdio_get_token)
register_prompts(mcp)


def main() -> None:
    """Run the MCP server in stdio mode."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
