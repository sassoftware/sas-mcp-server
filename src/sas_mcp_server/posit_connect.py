# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SAS Viya tokens from a Posit Connect viewer OAuth integration.

On Posit Connect (``POSIT_CONNECT_OAUTH=true``) the MCP client signs in to
Connect, not to SAS Logon. Connect forwards a per-request user session token in
the ``Posit-Connect-User-Session-Token`` header, and a viewer OAuth integration
whose provider is SAS Logon turns that into the user's own Viya access token.
Connect keeps the user's SAS Logon grant and refreshes it, so every call asks
afresh rather than caching.

The exchange is RFC 8693 token exchange against Connect's
``/__api__/v1/oauth/integrations/credentials`` endpoint, authenticated with the
content's own API key — the same request ``posit-sdk``'s
``Client().oauth.get_credentials()`` makes, done here with httpx to avoid the
extra dependency. Connect sets ``CONNECT_SERVER`` and ``CONNECT_API_KEY`` in the
content's environment.
"""

import logging
import os

import httpx
from fastmcp import Context
from fastmcp.server.dependencies import get_http_request

from .exceptions import AuthenticationError

logger = logging.getLogger(__name__)

SESSION_TOKEN_HEADER = "Posit-Connect-User-Session-Token"
CREDENTIALS_PATH = "/__api__/v1/oauth/integrations/credentials"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
USER_SESSION_TOKEN_TYPE = "urn:posit:connect:user-session-token"
_TIMEOUT = httpx.Timeout(30.0)


def _content_hint() -> str:
    """Where the user can authorize the SAS Logon integration, when known."""
    server = os.getenv("CONNECT_SERVER", "").rstrip("/")
    guid = os.getenv("CONNECT_CONTENT_GUID", "").strip()
    if server and guid:
        return f" Open {server}/connect/#/apps/{guid} in a browser and authorize SAS Logon there, then retry."
    return " Open this MCP server's page on Posit Connect in a browser and authorize SAS Logon there, then retry."


async def exchange_session_token(
    session_token: str,
    *,
    audience: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Trade a Connect user session token for the user's SAS Viya access token."""
    server = os.getenv("CONNECT_SERVER", "").rstrip("/")
    api_key = os.getenv("CONNECT_API_KEY", "")
    if not server or not api_key:
        raise AuthenticationError(
            "POSIT_CONNECT_OAUTH is on but CONNECT_SERVER / CONNECT_API_KEY are not set. "
            "Posit Connect sets both for deployed content; this mode only works there."
        )
    data = {
        "grant_type": GRANT_TYPE,
        "subject_token_type": USER_SESSION_TOKEN_TYPE,
        "subject_token": session_token,
    }
    if audience:
        data["audience"] = audience

    url = server + CREDENTIALS_PATH
    headers = {"Authorization": f"Key {api_key}"}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as owned:
                resp = await owned.post(url, data=data, headers=headers)
        else:
            resp = await client.post(url, data=data, headers=headers)
    except httpx.HTTPError as exc:
        raise AuthenticationError(f"Could not reach Posit Connect at {server} to get a SAS Viya token: {exc}") from exc

    if resp.status_code >= 400:
        # Connect answers 4xx when the viewer has not authorized the
        # integration yet, or when no (or more than one) integration matches.
        logger.warning("Posit Connect credential exchange failed: HTTP %s %s", resp.status_code, resp.text[:500])
        raise AuthenticationError(
            f"Posit Connect could not provide a SAS Viya token (HTTP {resp.status_code}). "
            "Either you have not yet authorized the SAS Logon integration for this content, or "
            "the content has no SAS Logon integration attached (or several, and "
            "POSIT_CONNECT_INTEGRATION_GUID is not set)." + _content_hint()
        )
    try:
        token = resp.json().get("access_token")
    except ValueError:
        token = None
    if not isinstance(token, str) or not token:
        raise AuthenticationError("Posit Connect returned no access_token for the SAS Logon integration.")
    return token


def make_token_getter(audience: str | None = None):
    """The token getter tools are registered with in Posit Connect mode."""

    async def connect_viya_token(ctx: Context) -> str:
        session_token = get_http_request().headers.get(SESSION_TOKEN_HEADER)
        if not session_token:
            raise AuthenticationError(
                f"No {SESSION_TOKEN_HEADER} header on this request. POSIT_CONNECT_OAUTH=true only works "
                "for content deployed on Posit Connect, which adds that header for signed-in users."
            )
        return await exchange_session_token(session_token, audience=audience)

    return connect_viya_token
