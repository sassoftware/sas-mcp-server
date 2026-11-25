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
import re
import secrets
from contextlib import nullcontext
from typing import Any

import httpx

from .config import COMPUTE_SESSION_ID, CONTEXT_NAME, VIYA_ENDPOINT
from .viya_client import logger, make_client, raise_for_viya_status


async def get_context_id(client: httpx.AsyncClient, context_name: str) -> str:
    """Return the id of the named compute context, raising if it is absent.

    The Compute API ignores a ``name`` query parameter and answers with every
    context the caller can see, so the lookup has to use ``filter``. The name is
    also matched exactly on the items: taking the first one would run the tool
    in whichever context happens to sort first.
    """
    url = f"{VIYA_ENDPOINT}/compute/contexts"
    quoted = context_name.replace("\\", "\\\\").replace('"', '\\"')
    resp = await client.get(url, params={"filter": f'eq(name,"{quoted}")'})
    raise_for_viya_status(resp)
    coll = resp.json()
    # an item without a name cannot be checked, so it is never taken for ours
    items = [i for i in coll.get("items", []) if i.get("name") == context_name]
    if not items:
        raise RuntimeError(f"Compute context not found: {context_name}")
    return items[0]["id"]


async def create_session(
    client: httpx.AsyncClient, context_id: str, name: str = "py-parallel"
) -> str:
    """Create a compute session in *context_id* and return its id."""
    url = f"{VIYA_ENDPOINT}/compute/contexts/{context_id}/sessions"
    resp = await client.post(url, json={"name": name})
    raise_for_viya_status(resp)
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

    def session_ids(self, user_key: str | None = None) -> set[str]:
        """Ids of the sessions this server keeps warm for *user_key*, or for every user without one."""
        return {sid for key, (sid, _) in self._sessions.items() if user_key is None or key[0] == user_key}

    def forget(self, session_id: str, user_key: str) -> bool:
        """Drop *session_id* from the cache without deleting it; ``True`` if it was cached.

        For a session deleted some other way, so the next call does not try
        to reuse it.
        """
        for key, (sid, _) in list(self._sessions.items()):
            if key[0] == user_key and sid == session_id:
                self._sessions.pop(key, None)
                return True
        return False

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


def cached_session_ids(token: str | None = None) -> set[str]:
    """Ids of the compute sessions this server keeps warm for *token*'s user, or for every user without one."""
    return _SESSION_CACHE.session_ids(None if token is None else _token_user_key(token))


def forget_cached_session(session_id: str, token: str) -> bool:
    """Stop reusing *session_id*; ``True`` if this server had it cached."""
    return _SESSION_CACHE.forget(session_id, _token_user_key(token))


async def shutdown_session_cache() -> None:
    """Delete all cached compute sessions server-side (call from server lifespan)."""
    await _SESSION_CACHE.shutdown()


def clear_session_cache() -> None:
    """Forget all cached compute sessions (test isolation helper)."""
    _SESSION_CACHE.clear()


def _session_summary(item: dict, cached: set[str], contexts: dict[str, dict]) -> dict:
    """The fields of one compute session that say what it is and whether it is busy."""
    context = contexts.get(str(item.get("serverId")), {})
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "state": item.get("state"),
        "owner": item.get("owner"),
        "created": item.get("creationTimeStamp"),
        "context_id": context.get("contextId"),
        "context_name": context.get("contextName"),
        "cached_by_this_server": item.get("id") in cached,
    }


async def _server_contexts(client: httpx.AsyncClient, server_ids: set[str]) -> dict[str, dict]:
    """``contextId`` and ``contextName`` of each compute server, by server id.

    A session item does not name its compute context; the server that runs it
    does. A server that cannot be read is left out, so its sessions show no
    context rather than failing the list.
    """

    async def one(server_id: str) -> tuple[str, dict]:
        try:
            resp = await client.get(f"{VIYA_ENDPOINT}/compute/servers/{server_id}")
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict):
                    return server_id, body
        except httpx.HTTPError:
            logger.debug("Could not read compute server %s", server_id, exc_info=True)
        return server_id, {}

    return dict(await asyncio.gather(*(one(sid) for sid in sorted(server_ids))))


async def list_compute_sessions(
    client: httpx.AsyncClient,
    limit: int = 50,
    start: int = 0,
    *,
    owner: str | None = None,
    token: str | None = None,
) -> tuple[list[dict], int]:
    """Summaries of compute sessions, and the total count Viya reports.

    With *owner*, only that user's sessions, filtered by Viya; without, every
    session the caller may see, which for an administrator includes other
    users'. The sessions include the one ``execute_sas_code`` keeps warm and
    those that Job Execution started for batch jobs. The collection's items
    carry the owner and state only when the full session type is requested in
    ``Accept-Item``. ``cached_by_this_server`` covers *token*'s user.
    """
    params: dict[str, Any] = {"start": start, "limit": limit}
    if owner:
        quoted = owner.replace("\\", "\\\\").replace('"', '\\"')
        params["filter"] = f'eq(owner,"{quoted}")'
    resp = await client.get(
        f"{VIYA_ENDPOINT}/compute/sessions",
        params=params,
        headers={
            "Accept": "application/vnd.sas.collection+json",
            "Accept-Item": "application/vnd.sas.compute.session+json",
        },
    )
    raise_for_viya_status(resp)
    data = resp.json()
    items = [item for item in data.get("items", []) if isinstance(item, dict)]
    cached = cached_session_ids(token)
    servers = {str(item["serverId"]) for item in items if item.get("serverId")}
    contexts = await _server_contexts(client, servers) if servers else {}
    return [_session_summary(item, cached, contexts) for item in items], int(data.get("count", len(items)) or 0)


async def submit_job(client: httpx.AsyncClient, session_id: str, code: str) -> str:
    """Submit *code* as a job in *session_id* and return the job id."""
    body = {"code": code.splitlines()}
    url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs"
    resp = await client.post(url, json=body)
    # Unchecked, a refused submit is parsed as though it were a job: the error
    # body has no ``id``, so ``job["id"]`` raises ``KeyError`` and the whole
    # failure reaches the caller as the single word ``'id'`` while Viya's own
    # explanation — "The session is not available.", an authorization message —
    # is thrown away (#55).
    raise_for_viya_status(resp)
    job = resp.json()
    return job["id"]


# Page size for compute log/listing collection fetches, and a loop backstop far
# above any real log (10k pages x 1000 lines). Hitting the backstop appends an
# explicit truncation marker rather than cutting silently.
_LINES_PAGE_LIMIT = 1000
_LINES_MAX_PAGES = 10_000


async def _fetch_all_lines(client: httpx.AsyncClient, url: str) -> list[str]:
    """Collect every ``line`` of a compute log/listing collection.

    The log and listing endpoints return *paged* collections (default page ~10
    lines), so a single unpaginated GET silently truncates anything longer —
    for audit-style work the log IS the deliverable, so every page is fetched.
    """
    lines: list[str] = []
    start = 0
    for _ in range(_LINES_MAX_PAGES):
        resp = await client.get(url, params={"start": start, "limit": _LINES_PAGE_LIMIT})
        # A failed page must not read as "no more lines": ``.get("items", [])``
        # on an error body yields ``[]``, the short-page test below ends the
        # loop, and the caller gets an empty log back with nothing to say the
        # fetch failed at all — for audit work the log IS the deliverable (#55).
        raise_for_viya_status(resp)
        items = resp.json().get("items", [])
        lines.extend(item.get("line", "") for item in items)
        if len(items) < _LINES_PAGE_LIMIT:
            return lines
        start += _LINES_PAGE_LIMIT
    lines.append(f"[output truncated after {len(lines)} lines]")
    return lines


async def wait_job(
    client: httpx.AsyncClient, session_id: str, job_id: str, poll: float = 2
) -> tuple[str, str, str]:
    """Poll *job_id* until it reaches a terminal state; return (state, log, listing)."""
    while True:
        state_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/state"
        resp = await client.get(state_url)
        # The state is read as plain text, so an error body would be treated as
        # a state name — never a terminal one, leaving this loop polling a
        # session that will never answer, forever. Deliberately no wall-clock
        # cap alongside it: a legitimate job can run for hours (the usage log
        # holds multi-hour durations), and this check already ends the only case
        # where the poll cannot terminate on its own (#55).
        raise_for_viya_status(resp)
        state = resp.text.strip()
        if state in ("completed", "error", "warning", "canceled"):
            log_url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/log"
            log_text = "\n".join(await _fetch_all_lines(client, log_url))

            listing_url = (
                f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/listing"
            )
            listing_lines = await _fetch_all_lines(client, listing_url)
            listing_text = (
                "\n".join(listing_lines) if listing_lines else "(no listing output)"
            )

            return state, log_text, listing_text
        await asyncio.sleep(poll)


# --- HTML results -------------------------------------------------------------
# The compute context opens no HTML destination of its own — SAS Data and AI
# Studio's HTML comes from ODS statements it adds to every submit — so a job
# submitted here yields a listing and nothing else. To get the page SAS Data
# and AI Studio would show, the code is wrapped: ODS HTML5 into a body file named for this one call (so two
# calls sharing a session never read each other's page), then the magic string
# that ends whatever the code left open — an unterminated statement, quote or
# comment would otherwise swallow the close — and the close. HTML5 embeds
# graphs as inline SVG by default, so the page needs nothing else to render.
#
# The guard ends with run;quit;, not Enterprise Guide's quit;run;: probed live,
# quit; inside an unfinished DATA step is ERROR 180-322 — on a line the caller
# never wrote — and once even left the session unable to run the next job,
# while run; first ends that step and quit; then closes an open PROC. Both
# wrapper lines stay well under the smallest LINESIZE (64), so the log's
# source echo never wraps them.
_HTML_ODS_ID = "sasmcp"
_HTML_GUARD = ";*';*\";*/;run;quit;"
_GUARD_START = ";*';*\";*/;"
# ODS writes a complete page even when nothing was printed; its body is empty.
_EMPTY_HTML_BODY = re.compile(rb"<body[^>]*>\s*</body>", re.IGNORECASE)
# A line of code as the log echoes it: its number, "!" when the echo resumes
# after output that the line's own statements produced, then the code.
_SOURCE_ECHO = re.compile(r"^\s*(\d+)(\s+!)?\s+(.*?)\s*$")


def new_html_body_file() -> str:
    """A body-file name unique to one call."""
    return f"sasmcp-{secrets.token_hex(4)}.htm"


def _html_wrapper_lines(body_file: str) -> tuple[str, str]:
    return (
        f'ods html5 (id={_HTML_ODS_ID}) file="{body_file}";',
        f"{_HTML_GUARD}ods html5 (id={_HTML_ODS_ID}) close;",
    )


def wrap_for_html(code: str, body_file: str) -> str:
    """Wrap *code* so that its ODS output is also written as HTML to *body_file*."""
    opening, closing = _html_wrapper_lines(body_file)
    return f"{opening}\n{code}\n{closing}"


def strip_html_wrapper(log_text: str, body_file: str) -> str:
    """Remove the wrapper's own lines from *log_text*.

    Those are the echoes of the two wrapper lines and the note ODS writes when
    it opens the body file. What is left is the log of the code as it was
    given, so the model reads no statement it did not write.

    The closing line is found by its number, not its whole text: when its
    run; ends a step the code left open, the log echoes the line up to that
    statement, then the step's own notes, then the rest as a continuation
    (``3  !   quit;ods html5 (id=sasmcp) close;``). The step's notes stay.
    """
    opening, closing = _html_wrapper_lines(body_file)
    note = f"NOTE: Writing HTML5({_HTML_ODS_ID.upper()}) Body file: {body_file}"
    lines = log_text.split("\n")
    echoes = [(i, m.groups()) for i, line in enumerate(lines) if (m := _SOURCE_ECHO.match(line))]
    # The closing line is the last thing submitted, so its first echo is the
    # last one that starts like it.
    close_no = next(
        (num for _, (num, cont, text) in reversed(echoes)
         if not cont and text.startswith(_GUARD_START) and closing.startswith(text)),
        None,
    )
    drop = {
        i for i, (num, cont, text) in echoes
        if (not cont and text == opening)
        or (num == close_no and (closing.startswith(text) if not cont else text in closing))
    }
    return "\n".join(line for i, line in enumerate(lines) if i not in drop and line.strip() != note)


async def fetch_html_result(
    client: httpx.AsyncClient, session_id: str, job_id: str, body_file: str
) -> bytes | None:
    """Return the page the job wrote to *body_file*, or ``None`` if it printed nothing.

    A job's results collection lists only what that job wrote, even in a
    session reused across many calls.
    """
    url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs/{job_id}/results"
    resp = await client.get(url, params={"limit": 1000})
    raise_for_viya_status(resp)
    for item in resp.json().get("items", []):
        if item.get("name") != body_file:
            continue
        href = next((link.get("href") for link in item.get("links", []) if link.get("rel") == "self"), None)
        if not href:
            return None
        page = await client.get(f"{VIYA_ENDPOINT}{href}", headers={"Accept": "text/html"})
        raise_for_viya_status(page)
        return None if _EMPTY_HTML_BODY.search(page.content) else page.content
    return None


async def store_html_result(client: httpx.AsyncClient, page: bytes, job_id: str) -> str:
    """Save *page* to the Viya Files service and return the new file's id.

    The file has no parent folder, so Viya's default rules let only the person
    who created it (and administrators) read it. It is saved without an
    expiration, the Files service's default, so it stays until deleted.
    """
    resp = await client.post(
        f"{VIYA_ENDPOINT}/files/files",
        content=page,
        headers={
            "Content-Type": "text/html",
            # inline: a browser shows the page rather than downloading it.
            "Content-Disposition": f'inline; filename="sas-results-{job_id}.html"',
        },
    )
    raise_for_viya_status(resp)
    return resp.json()["id"]


async def _publish_html(
    client: httpx.AsyncClient, session_id: str, job_id: str, body_file: str
) -> dict[str, str]:
    """Fetch the job's HTML page and save it where a browser can open it.

    Returns the fields to add to the result: the page's URL and file id, nothing
    when the code printed nothing, or ``html_results_error`` when either step
    failed. The log and listing are the result the caller asked for, so a
    failure here is reported alongside them rather than raised.
    """
    try:
        page = await fetch_html_result(client, session_id, job_id, body_file)
        if page is None:
            return {}
        file_id = await store_html_result(client, page, job_id)
    except Exception as exc:
        logger.warning("Could not publish the HTML results of job %s", job_id, exc_info=True)
        return {"html_results_error": f"The HTML results could not be saved: {exc}"}
    return {
        "html_results_url": f"{VIYA_ENDPOINT}/files/files/{file_id}/content",
        "html_results_file_id": file_id,
    }


async def run_one_snippet(
    snippet_data: str, snippet_id: str, token: str, html_results: bool = False
) -> dict[str, str]:
    """Execute one SAS snippet end to end and return its structured result.

    Returns a dict with keys ``snippet_id``, ``state``, ``log`` and ``listing``.
    With *html_results*, the code's ODS output is also written as HTML and saved
    to the Files service, adding ``html_results_url`` and
    ``html_results_file_id`` (or ``html_results_error``) when it printed
    anything. The snippet runs in the caller's cached compute session, so SAS
    state (WORK tables, macro variables, assigned librefs) persists across
    calls until the session is reset via ``reset_cached_session`` or reaped by
    Viya. The session is intentionally *not* torn down here so the next call
    can reuse it.
    """
    body_file = new_html_body_file() if html_results else None
    code = wrap_for_html(snippet_data, body_file) if body_file else snippet_data

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
            result = {
                "snippet_id": snippet_id,
                "state": state,
                "log": log_text,
                "listing": listing_text,
            }
            if body_file:
                result["log"] = strip_html_wrapper(log_text, body_file)
                result.update(await _publish_html(client, sid, jid, body_file))
            return result
        except Exception:
            logger.exception("Error executing SAS job")
            raise
