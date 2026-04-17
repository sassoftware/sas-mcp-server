# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the new API wrapper functions in viya_utils and tool registration.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from sas_mcp_server.viya_utils import (
    _get_json,
    _get_paged_items,
    _post_json,
    _put_data,
    _delete_resource,
    _make_client,
)


# ---------------------------------------------------------------------------
# _get_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_json_success(mock_httpx_client, mock_env_vars):
    """Test _get_json returns parsed JSON."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"name": "cas-shared-default"})
    mock_httpx_client.get.return_value = mock_response

    result = await _get_json("/casManagement/servers/cas1", mock_httpx_client)

    assert result == {"name": "cas-shared-default"}
    mock_httpx_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_json_with_params(mock_httpx_client, mock_env_vars):
    """Test _get_json passes query params."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"items": []})
    mock_httpx_client.get.return_value = mock_response

    await _get_json("/test", mock_httpx_client, params={"limit": 10})

    call_kwargs = mock_httpx_client.get.call_args
    assert call_kwargs[1]["params"] == {"limit": 10}


@pytest.mark.asyncio
async def test_get_json_raises_on_error(mock_httpx_client, mock_env_vars):
    """Test _get_json propagates HTTP errors."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
    )
    mock_httpx_client.get.return_value = mock_response

    with pytest.raises(httpx.HTTPStatusError):
        await _get_json("/bad/path", mock_httpx_client)


# ---------------------------------------------------------------------------
# _get_paged_items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paged_items_success(mock_httpx_client, mock_env_vars):
    """Test _get_paged_items returns items and count."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={
        "items": [{"name": "Public"}, {"name": "Formats"}],
        "count": 2,
    })
    mock_httpx_client.get.return_value = mock_response

    items, count = await _get_paged_items("/casManagement/servers/cas1/caslibs",
                                          mock_httpx_client, limit=50)

    assert len(items) == 2
    assert count == 2
    assert items[0]["name"] == "Public"


@pytest.mark.asyncio
async def test_get_paged_items_with_filter(mock_httpx_client, mock_env_vars):
    """Test _get_paged_items passes filter parameter."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"items": [], "count": 0})
    mock_httpx_client.get.return_value = mock_response

    await _get_paged_items("/files/files", mock_httpx_client,
                           filters="contains(name,'test')")

    call_kwargs = mock_httpx_client.get.call_args[1]
    assert call_kwargs["params"]["filter"] == "contains(name,'test')"


# ---------------------------------------------------------------------------
# _post_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_json_success(mock_httpx_client, mock_env_vars):
    """Test _post_json sends body and returns response."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 201
    mock_response.content = b'{"id": "new-project"}'
    mock_response.json = MagicMock(return_value={"id": "new-project"})
    mock_httpx_client.post.return_value = mock_response

    result = await _post_json("/mlPipelineAutomation/projects",
                              mock_httpx_client, body={"name": "test"})

    assert result == {"id": "new-project"}
    mock_httpx_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_post_json_no_content(mock_httpx_client, mock_env_vars):
    """Test _post_json handles 204 No Content."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 204
    mock_response.content = b""
    mock_httpx_client.post.return_value = mock_response

    result = await _post_json("/some/action", mock_httpx_client)

    assert result == {}


# ---------------------------------------------------------------------------
# _put_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_data_success(mock_httpx_client, mock_env_vars):
    """Test _put_data uploads raw data."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 201
    mock_response.content = b'{"tableName": "test"}'
    mock_response.json = MagicMock(return_value={"tableName": "test"})
    mock_httpx_client.put.return_value = mock_response

    result = await _put_data("/casManagement/servers/cas1/caslibs/Public/tables/test",
                             mock_httpx_client, data=b"a,b\n1,2")

    assert result == {"tableName": "test"}
    mock_httpx_client.put.assert_called_once()


# ---------------------------------------------------------------------------
# _delete_resource
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_resource_success(mock_httpx_client, mock_env_vars):
    """Test _delete_resource sends DELETE request."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_httpx_client.delete.return_value = mock_response

    await _delete_resource("/jobExecution/jobs/job123", mock_httpx_client)

    mock_httpx_client.delete.assert_called_once()


# ---------------------------------------------------------------------------
# _make_client
# ---------------------------------------------------------------------------


def test_make_client_adds_bearer_prefix(mock_env_vars):
    """Test _make_client adds Bearer prefix when missing."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_client("my-token")
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer my-token"


def test_make_client_preserves_bearer_prefix(mock_env_vars):
    """Test _make_client does not double-prefix Bearer."""
    with patch('sas_mcp_server.viya_utils.httpx.AsyncClient') as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_client("Bearer my-token")
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer my-token"
