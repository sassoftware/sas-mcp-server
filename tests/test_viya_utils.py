# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for viya_utils module (compute session/job orchestration).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sas_mcp_server.viya_utils import (
    create_session,
    get_context_id,
    run_one_snippet,
    submit_job,
    wait_job,
)


@pytest.mark.asyncio
async def test_get_context_id_success(mock_httpx_client, mock_context_response, mock_env_vars):
    """Test successful context ID retrieval."""
    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value=mock_context_response)
    mock_httpx_client.get.return_value = mock_response

    context_id = await get_context_id(mock_httpx_client, "Test Context")

    assert context_id == "test-context-id"
    mock_httpx_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_context_id_not_found(mock_httpx_client, mock_env_vars):
    """Test context ID retrieval when context is not found."""
    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value={"items": []})
    mock_httpx_client.get.return_value = mock_response

    with pytest.raises(RuntimeError, match="Compute context not found"):
        await get_context_id(mock_httpx_client, "NonExistent Context")


@pytest.mark.asyncio
async def test_create_session(mock_httpx_client, mock_session_response, mock_env_vars):
    """Test session creation."""
    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value=mock_session_response)
    mock_httpx_client.post.return_value = mock_response

    session_id = await create_session(mock_httpx_client, "test-context-id", "test-session")

    assert session_id == "test-session-id"
    mock_httpx_client.post.assert_called_once()
    call_args = mock_httpx_client.post.call_args
    assert call_args[1]["json"]["name"] == "test-session"


@pytest.mark.asyncio
async def test_submit_job(mock_httpx_client, mock_job_response, sample_sas_code, mock_env_vars):
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
async def test_wait_job_completed(mock_httpx_client, mock_job_log, mock_job_listing, mock_env_vars):
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
        mock_listing_response
    ]

    state, log, listing = await wait_job(mock_httpx_client, "test-session-id", "test-job-id", poll=0.01)

    assert state == "completed"
    assert "NOTE: DATA statement used" in log
    assert "Obs    x    y" in listing


@pytest.mark.asyncio
async def test_wait_job_error_state(mock_httpx_client, mock_job_log, mock_job_listing, mock_env_vars):
    """Test waiting for job that ends in error state."""
    mock_state_response = AsyncMock()
    mock_state_response.text = "error"

    mock_log_response = AsyncMock()
    mock_log_response.json = MagicMock(return_value={
        "items": [{"line": "ERROR: Something went wrong"}]
    })

    mock_listing_response = AsyncMock()
    mock_listing_response.json = MagicMock(return_value={"items": []})

    mock_httpx_client.get.side_effect = [
        mock_state_response,
        mock_log_response,
        mock_listing_response
    ]

    state, log, listing = await wait_job(mock_httpx_client, "test-session-id", "test-job-id", poll=0.01)

    assert state == "error"
    assert "ERROR: Something went wrong" in log


@pytest.mark.asyncio
async def test_run_one_snippet_success(sample_sas_code, mock_access_token, mock_env_vars):
    """Test successful execution of a SAS code snippet returns a structured dict."""
    with patch('sas_mcp_server.viya_client.httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Mock all the API calls
        mock_context_response = AsyncMock()
        mock_context_response.json = MagicMock(return_value={"items": [{"id": "ctx-id"}]})

        mock_session_response = AsyncMock()
        mock_session_response.json = MagicMock(return_value={"id": "sess-id"})

        mock_job_response = AsyncMock()
        mock_job_response.json = MagicMock(return_value={"id": "job-id"})

        mock_state_response = AsyncMock()
        mock_state_response.text = "completed"

        mock_log_response = AsyncMock()
        mock_log_response.json = MagicMock(return_value={"items": [{"line": "Log output"}]})

        mock_listing_response = AsyncMock()
        mock_listing_response.json = MagicMock(return_value={"items": [{"line": "Listing output"}]})

        mock_delete_response = AsyncMock()

        mock_client.get.side_effect = [
            mock_context_response,
            mock_state_response,
            mock_log_response,
            mock_listing_response
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

    with patch('sas_mcp_server.viya_client.httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Mock minimal responses for the test
        mock_context_response = AsyncMock()
        mock_context_response.json = MagicMock(return_value={"items": [{"id": "ctx-id"}]})

        mock_session_response = AsyncMock()
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
            mock_listing_response
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
async def test_run_one_snippet_propagates_error_and_cleans_up(
    sample_sas_code, mock_access_token, mock_env_vars
):
    """On failure the error propagates and the compute session is still deleted."""
    with patch('sas_mcp_server.viya_client.httpx.AsyncClient') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        ctx_resp = AsyncMock()
        ctx_resp.json = MagicMock(return_value={"items": [{"id": "ctx-id"}]})
        sess_resp = AsyncMock()
        sess_resp.json = MagicMock(return_value={"id": "sess-id"})

        mock_client.get.side_effect = [ctx_resp]
        mock_client.post.side_effect = [sess_resp, Exception("submit boom")]
        mock_client.delete.return_value = AsyncMock()

        with pytest.raises(Exception, match="submit boom"):
            await run_one_snippet(sample_sas_code, "1", mock_access_token)

        mock_client.delete.assert_awaited_once()
