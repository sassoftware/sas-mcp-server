# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import httpx
from fastmcp import utilities
from .config import VIYA_ENDPOINT, CONTEXT_NAME, SSL_VERIFY

logger = utilities.logging.get_logger(__name__)


# ---------------------------------------------------------------------------
# Generic API helpers (used by new tools)
# ---------------------------------------------------------------------------

async def _get_json(url, client, params=None, accept="application/json"):
    """GET a JSON response from a Viya REST endpoint."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.get(full_url, headers={"Accept": accept}, params=params or {})
    resp.raise_for_status()
    return resp.json()


async def _get_paged_items(url, client, limit=20, start=0, filters=None, extra_params=None):
    """GET a paginated collection and return the items list plus total count."""
    params = {"start": start, "limit": limit}
    if filters:
        params["filter"] = filters
    if extra_params:
        params.update(extra_params)
    data = await _get_json(url, client, params=params,
                           accept="application/vnd.sas.collection+json")
    return data.get("items", []), data.get("count", 0)


async def _post_json(url, client, body=None, params=None, accept="application/json"):
    """POST JSON to a Viya REST endpoint and return the response JSON."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.post(full_url, json=body,
                             headers={"Content-Type": "application/json",
                                      "Accept": accept},
                             params=params or {})
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


async def _put_data(url, client, data, content_type="text/csv", params=None):
    """PUT raw data (e.g. CSV upload) to a Viya REST endpoint."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.put(full_url, content=data,
                            headers={"Content-Type": content_type},
                            params=params or {})
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


async def _delete_resource(url, client):
    """DELETE a Viya REST resource."""
    full_url = f"{VIYA_ENDPOINT}{url}"
    resp = await client.delete(full_url)
    resp.raise_for_status()


def _make_client(token):
    """Create an httpx.AsyncClient with auth headers for Viya API calls."""
    if not token.startswith("Bearer "):
        token = f"Bearer {token}"
    headers = {"Authorization": token}
    return httpx.AsyncClient(headers=headers, verify=SSL_VERIFY, timeout=300.0)


# ---------------------------------------------------------------------------
# Original helpers (log/listing fetching)
# ---------------------------------------------------------------------------

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

    logger.info(f"Creating session with token (length: {len(token)})")

    async with _make_client(token) as client:
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