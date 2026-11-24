# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""How long the caller's SAS Viya access token has left, and renewing it.

A long batch job can outlive the token it was submitted with, and nothing told
the caller how much time the token had. :func:`token_lifetime` reads the
``exp`` claim of the token itself, so it works on both transports and needs no
request. The token is never logged or returned, only its expiry.

Renewal needs a refresh token, which only the stdio server holds (it reads it
from the login cache). The stdio server registers its renewer with
:func:`set_token_renewer`; in HTTP mode none is registered, and the MCP client
renews its own token through the OAuth proxy.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "RENEW_BELOW_SECONDS",
    "can_renew",
    "ensure_token_lifetime",
    "jwt_claims",
    "renew_token",
    "set_token_renewer",
    "token_lifetime",
]

# With no expected run time given, a token with less than this left is renewed
# before a batch job is submitted.
RENEW_BELOW_SECONDS = 30 * 60

_renewer: Callable[[str], str | None] | None = None


def jwt_claims(token: str | None) -> dict[str, Any]:
    """The claims of a JWT access token, read without checking the signature.

    The auth layer has already validated the token; this only reads it.
    Returns ``{}`` for anything that is not a decodable JWT.
    """
    if not token:
        return {}
    raw = token[7:] if token.startswith("Bearer ") else token
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_lifetime(token: str | None, now: datetime | None = None) -> dict[str, Any] | None:
    """``{"expires_at": <ISO 8601 UTC>, "seconds_left": <int>}`` for *token*, or ``None``.

    ``None`` when the token carries no ``exp`` claim (no token at all, as with
    ``VIYA_AUTH=false``, or one that is not a JWT).
    """
    exp = jwt_claims(token).get("exp")
    if not isinstance(exp, int | float) or isinstance(exp, bool):
        return None
    expires = datetime.fromtimestamp(exp, tz=UTC)
    current = now or datetime.now(UTC)
    return {
        "expires_at": expires.isoformat(),
        "seconds_left": max(0, int((expires - current).total_seconds())),
    }


def set_token_renewer(renewer: Callable[[str], str | None] | None) -> None:
    """Register the function that renews the server's token, or ``None`` to clear it.

    *renewer* is synchronous, returns the new access token or ``None`` when it
    cannot renew, and must not start an interactive sign-in.
    """
    global _renewer
    _renewer = renewer


def can_renew() -> bool:
    """Whether this server can renew its token by itself."""
    return _renewer is not None


async def renew_token(token: str) -> str | None:
    """Renew the token through the registered renewer; ``None`` when that is not possible."""
    renewer = _renewer
    if renewer is None:
        return None
    return await asyncio.to_thread(renewer, token)


async def ensure_token_lifetime(
    token: str, expected_runtime_minutes: int | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Renew *token* when it may not last the work about to start.

    The threshold is the expected run time when the caller gives one, and
    never less than :data:`RENEW_BELOW_SECONDS`. Returns the token to use (the
    renewed one, or *token* unchanged) and its lifetime with ``renewed``,
    ``renewal_available`` and, when it is still too short, ``warning``. The
    lifetime is ``None`` when the token has no readable expiry.
    """
    lifetime = token_lifetime(token)
    if lifetime is None:
        return token, None
    needed = max(RENEW_BELOW_SECONDS, (expected_runtime_minutes or 0) * 60)
    renewed = False
    if lifetime["seconds_left"] < needed and can_renew():
        fresh = await renew_token(token)
        fresh_lifetime = token_lifetime(fresh) if fresh else None
        if fresh and fresh_lifetime is not None:
            token, lifetime, renewed = fresh, fresh_lifetime, True
    result: dict[str, Any] = {**lifetime, "renewed": renewed, "renewal_available": can_renew()}
    if expected_runtime_minutes and lifetime["seconds_left"] < expected_runtime_minutes * 60:
        result["warning"] = (
            f"The access token expires in {lifetime['seconds_left'] // 60} minutes, before the "
            f"{expected_runtime_minutes} minutes this job is expected to run. Steps that call CAS or "
            "other SAS Viya services may fail after expiry. Monitor the submitted job with get_job_status."
        )
    elif lifetime["seconds_left"] < RENEW_BELOW_SECONDS:
        result["warning"] = (
            f"The access token expires in {lifetime['seconds_left'] // 60} minutes. "
            "Monitor the submitted job with get_job_status."
        )
    return token, result
