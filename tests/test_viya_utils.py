# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for viya_utils module (compute session/job orchestration).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sas_mcp_server.viya_utils import (
    _token_user_key,
    clear_session_cache,
    create_session,
    delete_session,
    get_cached_session,
    get_context_id,
    reset_cached_session,
    run_one_snippet,
    shutdown_session_cache,
    submit_job,
    wait_job,
)


@pytest.fixture(autouse=True)
def _default_dynamic_compute_sessions():
    """Keep tests on the context-backed session path unless overridden."""
    with patch("sas_mcp_server.viya_utils.COMPUTE_SESSION_ID", ""):
        yield


def _resp(json_data=None, status_code=200):
    """Build a minimal sync mock HTTP response for compute-cache tests."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_data or {})
    return resp


def _route_context(url, *args, **kwargs):
    """Default ``client.get`` router: context lookup returns one ctx, else 200."""
    if url.endswith("/contexts"):
        return _resp({"items": [{"id": "ctx-1"}]})
    return _resp()


@pytest.mark.asyncio
async def test_get_context_id_success(
    mock_httpx_client, mock_context_response, mock_env_vars
):
    """Test successful context ID retrieval."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=mock_context_response)
    mock_httpx_client.get.return_value = mock_response

    context_id = await get_context_id(mock_httpx_client, "Test Context")

    assert context_id == "test-context-id"
    mock_httpx_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_context_id_not_found(mock_httpx_client, mock_env_vars):
    """Test context ID retrieval when context is not found."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"items": []})
    mock_httpx_client.get.return_value = mock_response

    with pytest.raises(RuntimeError, match="Compute context not found"):
        await get_context_id(mock_httpx_client, "NonExistent Context")


@pytest.mark.asyncio
async def test_create_session(mock_httpx_client, mock_session_response, mock_env_vars):
    """Test session creation."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=mock_session_response)
    mock_httpx_client.post.return_value = mock_response

    session_id = await create_session(
        mock_httpx_client, "test-context-id", "test-session"
    )

    assert session_id == "test-session-id"
    mock_httpx_client.post.assert_called_once()
    call_args = mock_httpx_client.post.call_args
    assert call_args[1]["json"]["name"] == "test-session"


@pytest.mark.asyncio
async def test_delete_session(mock_httpx_client, mock_env_vars):
    """Test session deletion."""
    mock_httpx_client.delete.return_value = AsyncMock()

    await delete_session(mock_httpx_client, "test-session-id")

    mock_httpx_client.delete.assert_called_once()
    call_args = mock_httpx_client.delete.call_args
    assert call_args[0][0].endswith("/compute/sessions/test-session-id")


@pytest.mark.asyncio
async def test_submit_job(
    mock_httpx_client, mock_job_response, sample_sas_code, mock_env_vars
):
    """Test job submission."""
    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value=mock_job_response)
    mock_httpx_client.post.return_value = mock_response

    job_id = await submit_job(mock_httpx_client, "test-session-id", sample_sas_code)

    assert job_id == "test-job-id"
    mock_httpx_client.post.assert_called_once()
    call_args = mock_httpx_client.post.call_args
    assert "code" in call_args[1]["json"]
    assert isinstance(call_args[1]["json"]["code"], list)


@pytest.mark.asyncio
async def test_wait_job_completed(
    mock_httpx_client, mock_job_log, mock_job_listing, mock_env_vars
):
    """Test waiting for job completion."""
    # Mock state response
    mock_state_response = AsyncMock()
    mock_state_response.text = "completed"

    # Mock log response
    mock_log_response = AsyncMock()
    mock_log_response.json = MagicMock(return_value=mock_job_log)

    # Mock listing response
    mock_listing_response = AsyncMock()
    mock_listing_response.json = MagicMock(return_value=mock_job_listing)

    # Set up the client to return different responses
    mock_httpx_client.get.side_effect = [
        mock_state_response,
        mock_log_response,
        mock_listing_response,
    ]

    state, log, listing = await wait_job(
        mock_httpx_client, "test-session-id", "test-job-id", poll=0.01
    )

    assert state == "completed"
    assert "NOTE: DATA statement used" in log
    assert "Obs    x    y" in listing


@pytest.mark.asyncio
async def test_wait_job_error_state(
    mock_httpx_client, mock_job_log, mock_job_listing, mock_env_vars
):
    """Test waiting for job that ends in error state."""
    mock_state_response = AsyncMock()
    mock_state_response.text = "error"

    mock_log_response = AsyncMock()
    mock_log_response.json = MagicMock(
        return_value={"items": [{"line": "ERROR: Something went wrong"}]}
    )

    mock_listing_response = AsyncMock()
    mock_listing_response.json = MagicMock(return_value={"items": []})

    mock_httpx_client.get.side_effect = [
        mock_state_response,
        mock_log_response,
        mock_listing_response,
    ]

    state, log, listing = await wait_job(
        mock_httpx_client, "test-session-id", "test-job-id", poll=0.01
    )

    assert state == "error"
    assert "ERROR: Something went wrong" in log


@pytest.mark.asyncio
async def test_run_one_snippet_success(
    sample_sas_code, mock_access_token, mock_env_vars
):
    """Test successful execution of a SAS code snippet returns a structured dict."""
    with patch("sas_mcp_server.viya_client.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Mock all the API calls
        mock_context_response = AsyncMock()
        mock_context_response.raise_for_status = MagicMock()
        mock_context_response.json = MagicMock(
            return_value={"items": [{"id": "ctx-id"}]}
        )

        mock_session_response = AsyncMock()
        mock_session_response.raise_for_status = MagicMock()
        mock_session_response.json = MagicMock(return_value={"id": "sess-id"})

        mock_job_response = AsyncMock()
        mock_job_response.json = MagicMock(return_value={"id": "job-id"})

        mock_state_response = AsyncMock()
        mock_state_response.text = "completed"

        mock_log_response = AsyncMock()
        mock_log_response.json = MagicMock(
            return_value={"items": [{"line": "Log output"}]}
        )

        mock_listing_response = AsyncMock()
        mock_listing_response.json = MagicMock(
            return_value={"items": [{"line": "Listing output"}]}
        )

        mock_delete_response = AsyncMock()

        mock_client.get.side_effect = [
            mock_context_response,
            mock_state_response,
            mock_log_response,
            mock_listing_response,
        ]
        mock_client.post.side_effect = [mock_session_response, mock_job_response]
        mock_client.delete.return_value = mock_delete_response

        result = await run_one_snippet(sample_sas_code, "1", mock_access_token)

        assert result["snippet_id"] == "1"
        assert result["state"] == "completed"
        assert "Log output" in result["log"]
        assert "Listing output" in result["listing"]


@pytest.mark.asyncio
async def test_run_one_snippet_with_bearer_prefix(sample_sas_code, mock_env_vars):
    """Test that Bearer prefix is handled correctly."""
    token_with_bearer = "Bearer test-token"

    with patch("sas_mcp_server.viya_client.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Mock minimal responses for the test
        mock_context_response = AsyncMock()
        mock_context_response.raise_for_status = MagicMock()
        mock_context_response.json = MagicMock(
            return_value={"items": [{"id": "ctx-id"}]}
        )

        mock_session_response = AsyncMock()
        mock_session_response.raise_for_status = MagicMock()
        mock_session_response.json = MagicMock(return_value={"id": "sess-id"})

        mock_job_response = AsyncMock()
        mock_job_response.json = MagicMock(return_value={"id": "job-id"})

        mock_state_response = AsyncMock()
        mock_state_response.text = "completed"

        mock_log_response = AsyncMock()
        mock_log_response.json = MagicMock(return_value={"items": []})

        mock_listing_response = AsyncMock()
        mock_listing_response.json = MagicMock(return_value={"items": []})

        mock_delete_response = AsyncMock()

        mock_client.get.side_effect = [
            mock_context_response,
            mock_state_response,
            mock_log_response,
            mock_listing_response,
        ]
        mock_client.post.side_effect = [mock_session_response, mock_job_response]
        mock_client.delete.return_value = mock_delete_response

        result = await run_one_snippet(sample_sas_code, "1", token_with_bearer)
        assert result["state"] == "completed"

        # Verify the client was created with Bearer token
        call_kwargs = mock_client_class.call_args[1]
        assert "Authorization" in call_kwargs["headers"]
        assert call_kwargs["headers"]["Authorization"] == token_with_bearer


@pytest.mark.asyncio
async def test_wait_job_polls_until_terminal(mock_httpx_client, mock_env_vars):
    """wait_job keeps polling while the state is non-terminal."""
    running = AsyncMock()
    running.text = "running"
    completed = AsyncMock()
    completed.text = "completed"
    log_resp = AsyncMock()
    log_resp.json = MagicMock(return_value={"items": [{"line": "L"}]})
    listing_resp = AsyncMock()
    listing_resp.json = MagicMock(return_value={"items": [{"line": "O"}]})
    mock_httpx_client.get.side_effect = [running, completed, log_resp, listing_resp]

    state, log, listing = await wait_job(mock_httpx_client, "s", "j", poll=0.001)

    assert state == "completed"
    assert "L" in log
    assert "O" in listing


@pytest.mark.asyncio
async def test_run_one_snippet_propagates_error_and_keeps_session(
    sample_sas_code, mock_access_token, mock_env_vars
):
    """On failure the error propagates; the session is kept (not deleted) for reuse."""
    with patch("sas_mcp_server.viya_client.httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        ctx_resp = AsyncMock()
        ctx_resp.raise_for_status = MagicMock()
        ctx_resp.json = MagicMock(return_value={"items": [{"id": "ctx-id"}]})
        sess_resp = AsyncMock()
        sess_resp.raise_for_status = MagicMock()
        sess_resp.json = MagicMock(return_value={"id": "sess-id"})

        mock_client.get.side_effect = [ctx_resp]
        mock_client.post.side_effect = [sess_resp, Exception("submit boom")]
        mock_client.delete.return_value = AsyncMock()

        with pytest.raises(Exception, match="submit boom"):
            await run_one_snippet(sample_sas_code, "1", mock_access_token)

        # Reusable sessions are intentionally not torn down on error.
        mock_client.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# Compute session cache (reuse / reset / per-user keying)
# ---------------------------------------------------------------------------


def test_token_user_key_uses_jwt_sub():
    """A JWT token is keyed by its ``sub`` claim (signature not verified)."""
    import base64
    import json

    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": "user-123"}).encode())
        .rstrip(b"=")
        .decode()
    )
    assert _token_user_key(f"header.{payload}.sig") == "sub:user-123"
    # The Bearer prefix is stripped before decoding.
    assert _token_user_key(f"Bearer header.{payload}.sig") == "sub:user-123"


def test_token_user_key_distinguishes_users_and_is_stable():
    """Non-JWT tokens fall back to a stable hash that separates users."""
    assert _token_user_key("opaque-aaa") == _token_user_key("opaque-aaa")
    assert _token_user_key("opaque-aaa") != _token_user_key("opaque-bbb")


def test_token_user_key_falls_back_on_undecodable_jwt():
    """A dotted token whose payload is not valid base64/JSON hashes instead."""
    assert _token_user_key("aaa.notbase64!!.ccc").startswith("token:")


@pytest.mark.asyncio
async def test_get_cached_session_creates_then_reuses(mock_env_vars):
    """A second call for the same user+context reuses the session (one create)."""
    clear_session_cache()
    client = AsyncMock()
    client.get.side_effect = _route_context
    client.post.return_value = _resp({"id": "sess-1"})

    sid1 = await get_cached_session(client, "Ctx", "tok-aaa")
    sid2 = await get_cached_session(client, "Ctx", "tok-aaa")

    assert sid1 == sid2 == "sess-1"
    assert client.post.call_count == 1  # session created once, then reused


@pytest.mark.asyncio
async def test_get_cached_session_recreates_when_reaped(mock_env_vars):
    """If the cached session has been reaped (state 404), a new one is created."""
    clear_session_cache()
    client = AsyncMock()

    def route_get(url, *args, **kwargs):
        if url.endswith("/contexts"):
            return _resp({"items": [{"id": "ctx-1"}]})
        if "/state" in url:
            return _resp(status_code=404)  # session no longer exists
        return _resp()

    client.get.side_effect = route_get
    client.post.side_effect = [_resp({"id": "sess-1"}), _resp({"id": "sess-2"})]

    sid1 = await get_cached_session(client, "Ctx", "tok-bbb")
    sid2 = await get_cached_session(client, "Ctx", "tok-bbb")

    assert sid1 == "sess-1"
    assert sid2 == "sess-2"
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_get_cached_session_recreates_on_validation_error(mock_env_vars):
    """A network error while validating the cached session forces a new one."""
    clear_session_cache()
    client = AsyncMock()

    def route_get(url, *args, **kwargs):
        if url.endswith("/contexts"):
            return _resp({"items": [{"id": "ctx-1"}]})
        if "/state" in url:
            raise httpx.ConnectError("network down")
        return _resp()

    client.get.side_effect = route_get
    client.post.side_effect = [_resp({"id": "sess-1"}), _resp({"id": "sess-2"})]

    sid1 = await get_cached_session(client, "Ctx", "tok-eee")
    sid2 = await get_cached_session(client, "Ctx", "tok-eee")

    assert sid1 == "sess-1"
    assert sid2 == "sess-2"
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_get_cached_session_is_per_user(mock_env_vars):
    """Different users never share a session, even for the same context."""
    clear_session_cache()
    client = AsyncMock()
    client.get.side_effect = _route_context
    client.post.side_effect = [_resp({"id": "sess-A"}), _resp({"id": "sess-B"})]

    sid_a = await get_cached_session(client, "Ctx", "token-user-a")
    sid_b = await get_cached_session(client, "Ctx", "token-user-b")

    assert sid_a == "sess-A"
    assert sid_b == "sess-B"
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_get_cached_session_fixed_session_mode_skips_context_lookup(mock_env_vars):
    """When COMPUTE_SESSION_ID is set, no context/session APIs are called."""
    clear_session_cache()
    client = AsyncMock()
    with patch("sas_mcp_server.viya_utils.COMPUTE_SESSION_ID", "0001"):
        sid = await get_cached_session(client, "Ctx", "tok-fixed")
    assert sid == "0001"
    client.get.assert_not_called()
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_reset_cached_session_deletes_and_forgets(mock_env_vars):
    """Reset deletes the server-side session and clears the cache entry."""
    clear_session_cache()
    client = AsyncMock()
    client.get.side_effect = _route_context
    client.post.return_value = _resp({"id": "sess-1"})
    client.delete.return_value = _resp(status_code=204)

    await get_cached_session(client, "Ctx", "tok-ccc")
    deleted = await reset_cached_session(client, "Ctx", "tok-ccc")

    assert deleted == "sess-1"
    client.delete.assert_awaited_once()
    assert "/compute/sessions/sess-1" in client.delete.call_args[0][0]

    # Nothing cached anymore.
    assert await reset_cached_session(client, "Ctx", "tok-ccc") is None


@pytest.mark.asyncio
async def test_reset_cached_session_noop_when_empty(mock_env_vars):
    """Reset with nothing cached returns None and never calls delete."""
    clear_session_cache()
    client = AsyncMock()

    assert await reset_cached_session(client, "Ctx", "tok-ddd") is None
    client.delete.assert_not_called()


@pytest.mark.asyncio
async def test_reset_cached_session_fixed_session_mode_is_noop(mock_env_vars):
    """Fixed-session mode leaves externally managed sessions untouched."""
    client = AsyncMock()
    with patch("sas_mcp_server.viya_utils.COMPUTE_SESSION_ID", "0001"):
        assert await reset_cached_session(client, "Ctx", "tok-any") is None
    client.delete.assert_not_called()


def _delete_client():
    """An AsyncMock usable as ``async with make_client(...) as client``."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_shutdown_session_cache_deletes_all(mock_env_vars):
    """Shutdown deletes every cached session using its stored token."""
    clear_session_cache()
    client = AsyncMock()
    client.get.side_effect = _route_context
    client.post.side_effect = [_resp({"id": "sess-1"}), _resp({"id": "sess-2"})]
    await get_cached_session(client, "CtxA", "tok-a")
    await get_cached_session(client, "CtxB", "tok-a")

    del_client = _delete_client()
    with patch("sas_mcp_server.viya_utils.make_client", return_value=del_client):
        await shutdown_session_cache()

    assert del_client.delete.await_count == 2
    # Cache is emptied; a follow-up reset finds nothing.
    assert await reset_cached_session(client, "CtxA", "tok-a") is None


@pytest.mark.asyncio
async def test_shutdown_session_cache_empty_is_noop(mock_env_vars):
    """Shutdown with an empty cache builds no client and deletes nothing."""
    clear_session_cache()
    with patch("sas_mcp_server.viya_utils.make_client") as make_client_mock:
        await shutdown_session_cache()
        make_client_mock.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_session_cache_swallows_delete_errors(mock_env_vars):
    """A failing delete on shutdown is logged, not raised."""
    clear_session_cache()
    client = AsyncMock()
    client.get.side_effect = _route_context
    client.post.return_value = _resp({"id": "sess-1"})
    await get_cached_session(client, "Ctx", "tok-a")

    del_client = _delete_client()
    del_client.delete.side_effect = httpx.HTTPError("boom")
    with patch("sas_mcp_server.viya_utils.make_client", return_value=del_client):
        await shutdown_session_cache()  # must not raise
