# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SAS Viya compute session and job orchestration.

These helpers drive the Compute service end to end: resolve a compute context,
open a session, submit code, and poll for completion. ``run_one_snippet`` is
the public entry point used by the ``execute_sas_code`` MCP tool.

To avoid paying the (slow) session spin-up cost on every call, compute sessions
are pooled by :class:`_ComputeSessionCache`: one reusable session per
authenticated user and compute context. Because compute sessions are stateful,
SAS WORK tables, macro variables, and assigned librefs persist across calls
until the session is reset (``reset_cached_session``) or reaped by Viya for
inactivity. Generic REST helpers and the shared client/logger live in
:mod:`sas_mcp_server.viya_client`.
"""

import asyncio
import base64
import binascii
import hashlib
import json
from contextlib import nullcontext

import httpx

from .config import COMPUTE_SESSION_ID, CONTEXT_NAME, VIYA_ENDPOINT
from .viya_client import logger, make_client


async def get_context_id(client: httpx.AsyncClient, context_name: str) -> str:
    """Return the id of the named compute context, raising if it is absent."""
    url = f"{VIYA_ENDPOINT}/compute/contexts"
    resp = await client.get(url, params={"name": context_name})
    resp.raise_for_status()
    coll = resp.json()
    items = coll.get("items", [])
    if not items:
        raise RuntimeError(f"Compute context not found: {context_name}")
    return items[0]["id"]


async def create_session(
    client: httpx.AsyncClient, context_id: str, name: str = "py-parallel"
) -> str:
    """Create a compute session in *context_id* and return its id."""
    url = f"{VIYA_ENDPOINT}/compute/contexts/{context_id}/sessions"
    resp = await client.post(url, json={"name": name})
    resp.raise_for_status()
    return resp.json()["id"]


async def delete_session(client: httpx.AsyncClient, sid: str) -> None:
    try:
        delete_url = f"{VIYA_ENDPOINT}/compute/sessions/{sid}"
        await client.delete(delete_url)
        logger.info("Session %s deleted successfully", sid)
    except Exception:
        logger.exception("Failed to delete session %s", sid)
        raise


def _token_user_key(token: str) -> str:
    """Derive a stable per-user cache key from a Viya access token.

    Viya access tokens are JWTs; we read the ``sub`` claim (falling back to
    other identity claims) *without* verifying the signature — the auth layer
    has already validated the token. If the token is not a decodable JWT we
    fall back to a hash of the token string.
    """
    raw = token[7:] if token.startswith("Bearer ") else token
    parts = raw.split(".")
    if len(parts) >= 2:
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            for claim in ("sub", "uid", "user_name", "user_id"):
                value = payload.get(claim)
                if value:
                    return f"{claim}:{value}"
        except (binascii.Error, ValueError, json.JSONDecodeError):
            pass
    return "token:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


async def _session_is_alive(client: httpx.AsyncClient, session_id: str) -> bool:
    """Return ``True`` if *session_id* still exists server-side."""
    try:
        resp = await client.get(f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/state")
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


class _ComputeSessionCache:
    """Process-wide pool of reusable compute sessions, keyed by (user, context).

    One session is kept per authenticated user and compute context so repeat
    tool calls skip the costly session spin-up. A per-key lock serialises the
    create/reset of a given session without blocking unrelated keys.
    """

    SESSION_NAME = "sas-mcp-shared"

    def __init__(self) -> None:
        # key -> (session_id, most recent token seen for that session). The
        # token is kept so :meth:`shutdown` can authenticate the delete calls.
        self._sessions: dict[tuple[str, str], tuple[str, str]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get_or_create(
        self, client: httpx.AsyncClient, context_name: str, user_key: str, token: str
    ) -> str:
        """Return a live session id for (user_key, context_name).

        Creates a session when none is cached or the cached one has been reaped
        server-side (detected via :func:`_session_is_alive`). *token* is stored
        alongside the session for shutdown cleanup.
        """
        key = (user_key, context_name)
        lock = await self._lock_for(key)
        async with lock:
            cached = self._sessions.get(key)
            if cached is not None:
                sid, _ = cached
                if await _session_is_alive(client, sid):
                    logger.info("Reusing cached compute session %s", sid)
                    # Keep the freshest token so shutdown cleanup can authenticate.
                    self._sessions[key] = (sid, token)
                    return sid
                logger.info("Cached compute session %s is gone; recreating", sid)
                self._sessions.pop(key, None)
            context_id = await get_context_id(client, context_name)
            sid = await create_session(client, context_id, name=self.SESSION_NAME)
            self._sessions[key] = (sid, token)
            logger.info("Created and cached compute session %s", sid)
            return sid

    async def reset(
        self, client: httpx.AsyncClient, context_name: str, user_key: str
    ) -> str | None:
        """Drop the cached session for (user_key, context_name) and delete it.

        Returns the deleted session id, or ``None`` if nothing was cached.
        """
        key = (user_key, context_name)
        lock = await self._lock_for(key)
        async with lock:
            cached = self._sessions.pop(key, None)
        if cached is None:
            return None
        sid, _ = cached
        await delete_session(client, sid)
        return sid

    async def shutdown(self) -> None:
        """Delete every cached compute session server-side (best effort).

        Called from the server lifespan on shutdown so warm sessions do not
        linger until Viya reaps them. Each session is deleted with the most
        recent token seen for it; failures (e.g. an expired token) are logged
        and ignored, since Viya reaps an orphaned session on idle timeout.
        """
        async with self._guard:
            entries = list(self._sessions.values())
            self._sessions.clear()
            self._locks.clear()
        if not entries:
            return
        logger.info("Deleting %d cached compute session(s) on shutdown", len(entries))
        for sid, token in entries:
            try:
                async with make_client(token) as client:
                    await delete_session(client, sid)
            except Exception:
                logger.warning(
                    "Could not delete compute session %s on shutdown", sid, exc_info=True
                )

    def clear(self) -> None:
        """Forget all cached sessions without deleting them server-side.

        Used to isolate unit tests; not part of normal operation.
        """
        self._sessions.clear()
        self._locks.clear()


_SESSION_CACHE = _ComputeSessionCache()
_FIXED_SESSION_JOB_LOCK = asyncio.Lock()


async def get_cached_session(
    client: httpx.AsyncClient, context_name: str, token: str
) -> str:
    """Return the reusable compute session id for the token's user + context."""
    if COMPUTE_SESSION_ID:
        return COMPUTE_SESSION_ID
    return await _SESSION_CACHE.get_or_create(
        client, context_name, _token_user_key(token), token
    )


async def reset_cached_session(
    client: httpx.AsyncClient, context_name: str, token: str
) -> str | None:
    """Delete and forget the cached compute session for the token's user.

    Returns the deleted session id, or ``None`` if there was no cached session.
    """
    if COMPUTE_SESSION_ID:
        return None
    return await _SESSION_CACHE.reset(client, context_name, _token_user_key(token))


async def shutdown_session_cache() -> None:
    """Delete all cached compute sessions server-side (call from server lifespan)."""
    await _SESSION_CACHE.shutdown()


def clear_session_cache() -> None:
    """Forget all cached compute sessions (test isolation helper)."""
    _SESSION_CACHE.clear()


async def submit_job(client: httpx.AsyncClient, session_id: str, code: str) -> str:
    """Submit *code* as a job in *session_id* and return the job id."""
    body = {"code": code.splitlines()}
    url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs"
    resp = await client.post(url, json=body)
    job = resp.json()
    return job["id"]


async def wait_job(
    client: httpx.AsyncClient, session_id: str, job_id: str, poll: float = 2
) -> tuple[str, str, str]:
    """Poll *job_id* until it reaches a terminal state; return (state, log, listing)."""
    while True:
        state_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/state"
        resp = await client.get(state_url)
        state = resp.text.strip()
        if state in ("completed", "error", "warning", "canceled"):
            # Fetch log
            log_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/log"
            log_resp = await client.get(log_url)
            log = log_resp.json()
            lines = [item["line"] for item in log.get("items", [])]
            log_text = "\n".join(lines)

            # Fetch listing (plain text output)
            listing_url = (
                f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/listing"
            )
            listing_resp = await client.get(listing_url)
            listing_json = listing_resp.json()
            listing_lines = [item["line"] for item in listing_json.get("items", [])]
            listing_text = (
                "\n".join(listing_lines) if listing_lines else "(no listing output)"
            )

            return state, log_text, listing_text
        await asyncio.sleep(poll)


async def run_one_snippet(
    snippet_data: str, snippet_id: str, token: str
) -> dict[str, str]:
    """Execute one SAS snippet end to end and return its structured result.

    Returns a dict with keys ``snippet_id``, ``state``, ``log`` and ``listing``.
    The snippet runs in the caller's cached compute session, so SAS state (WORK
    tables, macro variables, assigned librefs) persists across calls until the
    session is reset via ``reset_cached_session`` or reaped by Viya. The session
    is intentionally *not* torn down here so the next call can reuse it.
    """
    code = snippet_data

    logger.info("Running snippet (token length: %d)", len(token))

    async with make_client(token) as client:
        sid = await get_cached_session(client, CONTEXT_NAME, token)
        try:
            # A fixed externally managed session (e.g. "0001") can be shared
            # across callers; serialize job runs to avoid session-state races.
            job_lock = _FIXED_SESSION_JOB_LOCK if COMPUTE_SESSION_ID else nullcontext()
            async with job_lock:
                jid = await submit_job(client, sid, code)
                logger.info("Job submitted: %s", jid)
                state, log_text, listing_text = await wait_job(client, sid, jid)
            logger.info("Job completed: %s", state)
            return {
                "snippet_id": snippet_id,
                "state": state,
                "log": log_text,
                "listing": listing_text,
            }
        except Exception:
            logger.exception("Error executing SAS job")
            raise
