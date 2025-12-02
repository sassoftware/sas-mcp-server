# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import httpx
from fastmcp import utilities
from .config import VIYA_ENDPOINT, CONTEXT_NAME

logger = utilities.logging.get_logger(__name__)


async def _get_text(url, client, verify=True, extra_params=None):
    # Try text/plain in one shot
    full_url = f"{VIYA_ENDPOINT}{url}"
    r = await client.get(
        full_url, headers={"Accept": "text/plain"}, params=extra_params or {}
    )
    if r.status_code == 200 and r.headers.get("Content-Type", "").startswith(
        "text/plain"
    ):
        return r.text
    # Some deployments need an explicit query hint
    r = await client.get(
        full_url,
        headers={"Accept": "text/plain"},
        params={**(extra_params or {}), "type": "text"},
    )
    if r.status_code == 200 and r.headers.get("Content-Type", "").startswith(
        "text/plain"
    ):
        return r.text
    return None  # caller will fallback to paged JSON


async def _get_paged_lines(url, client, page_limit=10000):
    start = 0
    lines = []
    headers = {"Accept": "application/vnd.sas.collection+json"}
    full_url = f"{VIYA_ENDPOINT}{url}"
    while True:
        resp = await client.get(
            full_url, headers=headers, params={"start": start, "limit": page_limit}
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        # items can be dicts like {"line": "..."} or {"text": "..."} depending on endpoint
        for it in items:
            lines.append(it.get("line") or it.get("text") or "")
        if len(items) < page_limit:
            break
        start += page_limit
    return "\n".join(lines)


async def fetch_full_job_log(client, session_id, job_id):
    base = f"/compute/sessions/{session_id}/jobs/{job_id}"
    # 1) Try whole job log as text
    text = await _get_text(f"{base}/log", client)
    if text is not None:
        return text
    # 2) Fallback to paged JSON
    return await _get_paged_lines(f"{base}/log", client)


async def fetch_full_job_listing(client, session_id, job_id):
    base = f"/compute/sessions/{session_id}/jobs/{job_id}"
    text = await _get_text(f"{base}/listing", client)
    if text is not None:
        return text
    return await _get_paged_lines(f"{base}/listing", client)


async def fetch_full_session_log(client, session_id):
    # Entire session log (useful if you want everything the session produced)
    text = await _get_text(f"/compute/sessions/{session_id}/log", client)
    if text is not None:
        return text
    return await _get_paged_lines(f"/compute/sessions/{session_id}/log", client)


async def get_context_id(client, context_name):
    url = f"{VIYA_ENDPOINT}/compute/contexts?name={context_name}"
    resp = await client.get(url)
    coll = resp.json()
    items = coll.get("items", [])
    if not items:
        raise RuntimeError(f"Compute context not found: {context_name}")
    return items[0]["id"]


async def create_session(client, context_id, name="py-parallel"):
    url = f"{VIYA_ENDPOINT}/compute/contexts/{context_id}/sessions"
    resp = await client.post(url, json={"name": name})
    return resp.json()["id"]


async def submit_job(client, session_id, code):
    body = {"code": code.splitlines()}
    url = f"{VIYA_ENDPOINT}/compute/sessions/{session_id}/jobs"
    resp = await client.post(url, json=body)
    job = resp.json()
    return job["id"]


async def wait_job(client, session_id, job_id, poll=2):
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


async def run_one_snippet(snippet_data, snippet_id, token):
    code = snippet_data

    # Prepare authorization header
    # Token should already be in the correct format
    if not token.startswith("Bearer "):
        token = f"Bearer {token}"

    logger.info(f"Creating session with token (length: {len(token)})")

    # Create httpx client with authorization headers
    headers = {"Authorization": token, "Content-Type": "application/json"}

    # Increase timeout for long-running SAS jobs (5 minutes)
    async with httpx.AsyncClient(headers=headers, verify=True, timeout=300.0) as client:
        ctx_id = await get_context_id(client, CONTEXT_NAME)
        sid = await create_session(client, ctx_id, name="py-parallel")
        logger.info(f"Session created: {sid}")
        try:
            jid = await submit_job(client, sid, code)
            logger.info(f"Job submitted: {jid}")
            result = await wait_job(client, sid, jid)
            logger.info(f"Job completed: {result[0]}")
            return (snippet_id, *result)
        except Exception as e:
            logger.error(f"Error executing SAS job: {str(e)}")
            raise e
        finally:
            try:
                delete_url = f"{VIYA_ENDPOINT}/compute/sessions/{sid}"
                await client.delete(delete_url)
                logger.info(f"Session {sid} deleted successfully")
            except Exception as e:
                logger.error(f"Failed to delete session {sid}: {str(e)}")
                raise e