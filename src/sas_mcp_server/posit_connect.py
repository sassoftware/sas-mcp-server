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
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastmcp import Context
from fastmcp.server.dependencies import get_http_request

from .exceptions import AuthenticationError

logger = logging.getLogger(__name__)

SESSION_TOKEN_HEADER = "Posit-Connect-User-Session-Token"
# The URL the client asked for, as Connect received it (public scheme, host and
# path). CONNECT_SERVER is Connect's *internal* address (e.g.
# http://localhost:3939), fine for API calls but useless as a link for a user.
ORIGINAL_URL_HEADER = "X-RSC-Request"
CREDENTIALS_PATH = "/__api__/v1/oauth/integrations/credentials"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
USER_SESSION_TOKEN_TYPE = "urn:posit:connect:user-session-token"
_TIMEOUT = httpx.Timeout(30.0)


def public_content_url(original_url: str | None, request_path: str) -> str | None:
    """The content's public root URL, from Connect's ``X-RSC-Request`` header.

    The header holds the full public URL of the request (``…/sas-mcp/mcp``);
    dropping the query and the app-relative *request_path* (``/mcp``) leaves
    the content's own URL, which is where a viewer authorizes integrations.
    """
    if not original_url:
        return None
    parts = urlsplit(original_url)
    if not parts.scheme or not parts.netloc:
        return None
    path = parts.path
    if request_path and request_path != "/" and path.endswith(request_path):
        path = path[: -len(request_path)]
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/") + "/", "", ""))


def _app_path(request) -> str:
    """The request path relative to the app's mount point.

    Behind Connect the app runs with a ``root_path`` (e.g. ``/connect/sas-mcp``)
    and Starlette's ``request.url.path`` includes it. Stripping that full path
    from ``X-RSC-Request`` would take the content's own path with it, leaving
    only the host, so strip just the part below the mount (``/mcp``).
    """
    scope = getattr(request, "scope", None) or {}
    path: str = scope.get("path") or request.url.path
    root: str = scope.get("root_path", "") or ""
    if root and path.startswith(root):
        return path[len(root) :] or "/"
    return path


def _content_hint(content_url: str | None) -> str:
    """Where the user can authorize the SAS Logon integration."""
    where = content_url or "this MCP server's content URL on Posit Connect"
    return f" To authorize SAS Logon, open {where} in a browser while signed in to Connect, then retry the tool."


def _connect_reason(resp: httpx.Response) -> str:
    """Connect's own explanation of a refused exchange.

    Connect API errors are ``{"code": <n>, "error": "<message>", ...}``. The
    message names the actual problem, e.g. "No OAuth session found. Log in to
    the Integration before requesting credentials." or "No OAuth integrations
    have been associated with this content item."
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = body.get("error") or body.get("message") or body.get("error_description")
        if isinstance(message, str) and message.strip():
            code = body.get("code")
            return f"Connect API error {code}: {message.strip()}" if code is not None else message.strip()
    text = resp.text.strip()
    return text[:300] if text else ""


async def exchange_session_token(
    session_token: str,
    *,
    audience: str | None = None,
    content_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Trade a Connect user session token for the user's SAS Viya access token."""
    # POSIT_CONNECT_API_URL overrides the CONNECT_SERVER Connect injects (from
    # Server.EnvAddress). With off-host execution, an EnvAddress of
    # http://localhost:3939 is the content's own pod, not Connect.
    server = (os.getenv("POSIT_CONNECT_API_URL") or os.getenv("CONNECT_SERVER", "")).rstrip("/")
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
        # str() of httpx timeouts is often empty, so always name the type.
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        raise AuthenticationError(
            f"Could not reach Posit Connect at {server} to get a SAS Viya token: {detail}"
        ) from exc

    if resp.status_code >= 400:
        # Connect answers 4xx when the viewer has not authorized the
        # integration yet, when no (or more than one) integration matches, or
        # when the viewer's stored grant can no longer be refreshed.
        reason = _connect_reason(resp)
        logger.warning("Posit Connect credential exchange failed: HTTP %s %s", resp.status_code, reason)
        raise AuthenticationError(
            f"Posit Connect could not provide a SAS Viya token (HTTP {resp.status_code})"
            + (f": {reason}" if reason else ".")
            + " Usual causes: you have not authorized the SAS Logon integration for this content yet; "
            "the integration is not attached to this content item (or several are, and "
            "POSIT_CONNECT_INTEGRATION_GUID is not set); or your SAS Logon grant expired." + _content_hint(content_url)
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
        request = get_http_request()
        session_token = request.headers.get(SESSION_TOKEN_HEADER)
        if not session_token:
            raise AuthenticationError(
                f"No {SESSION_TOKEN_HEADER} header on this request. POSIT_CONNECT_OAUTH=true only works "
                "for content deployed on Posit Connect, which adds that header for signed-in users."
            )
        content_url = public_content_url(request.headers.get(ORIGINAL_URL_HEADER), _app_path(request))
        return await exchange_session_token(session_token, audience=audience, content_url=content_url)

    return connect_viya_token
