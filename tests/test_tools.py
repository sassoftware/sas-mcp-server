# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the generic Viya REST helper functions in viya_client.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp import Client

from sas_mcp_server.helpers import auto_ml_helpers
from sas_mcp_server.viya_client import (
    delete_resource,
    get_json,
    get_paged_items,
    make_client,
    post_json,
    return_items,
)

# ---------------------------------------------------------------------------
# get_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_json_success(mock_httpx_client, mock_env_vars):
    """Test get_json returns parsed JSON."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"name": "cas-shared-default"})
    mock_httpx_client.get.return_value = mock_response

    result = await get_json("/casManagement/servers/cas1", mock_httpx_client)

    assert result == {"name": "cas-shared-default"}
    mock_httpx_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_json_with_params(mock_httpx_client, mock_env_vars):
    """Test get_json passes query params."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"items": []})
    mock_httpx_client.get.return_value = mock_response

    await get_json("/test", mock_httpx_client, params={"limit": 10})

    call_kwargs = mock_httpx_client.get.call_args
    assert call_kwargs[1]["params"] == {"limit": 10}


@pytest.mark.asyncio
async def test_get_json_raises_on_error(mock_httpx_client, mock_env_vars):
    """Test get_json propagates HTTP errors."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )
    )
    mock_httpx_client.get.return_value = mock_response

    with pytest.raises(httpx.HTTPStatusError):
        await get_json("/bad/path", mock_httpx_client)


# ---------------------------------------------------------------------------
# get_paged_items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paged_items_success(mock_httpx_client, mock_env_vars):
    """Test get_paged_items returns items and count."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(
        return_value={
            "items": [{"name": "Public"}, {"name": "Formats"}],
            "count": 2,
        }
    )
    mock_httpx_client.get.return_value = mock_response

    items, count = await get_paged_items(
        "/casManagement/servers/cas1/caslibs", mock_httpx_client, limit=50
    )

    assert len(items) == 2
    assert count == 2
    assert items[0]["name"] == "Public"


@pytest.mark.asyncio
async def test_get_paged_items_with_filter(mock_httpx_client, mock_env_vars):
    """Test get_paged_items passes filter parameter."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"items": [], "count": 0})
    mock_httpx_client.get.return_value = mock_response

    await get_paged_items(
        "/files/files", mock_httpx_client, filters="contains(name,'test')"
    )

    call_kwargs = mock_httpx_client.get.call_args[1]
    assert call_kwargs["params"]["filter"] == "contains(name,'test')"


# ---------------------------------------------------------------------------
# post_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_json_success(mock_httpx_client, mock_env_vars):
    """Test post_json sends body and returns response."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 201
    mock_response.content = b'{"id": "new-project"}'
    mock_response.json = MagicMock(return_value={"id": "new-project"})
    mock_httpx_client.post.return_value = mock_response

    result = await post_json(
        "/mlPipelineAutomation/projects", mock_httpx_client, body={"name": "test"}
    )

    assert result == {"id": "new-project"}
    mock_httpx_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_post_json_no_content(mock_httpx_client, mock_env_vars):
    """Test post_json handles 204 No Content."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 204
    mock_response.content = b""
    mock_httpx_client.post.return_value = mock_response

    result = await post_json("/some/action", mock_httpx_client)

    assert result == {}


# ---------------------------------------------------------------------------
# delete_resource
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_resource_success(mock_httpx_client, mock_env_vars):
    """Test delete_resource sends DELETE request."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_httpx_client.delete.return_value = mock_response

    await delete_resource("/jobExecution/jobs/job123", mock_httpx_client)

    mock_httpx_client.delete.assert_called_once()


# ---------------------------------------------------------------------------
# make_client
# ---------------------------------------------------------------------------


def test_make_client_adds_bearer_prefix(mock_env_vars):
    """Test make_client adds Bearer prefix when missing."""
    with patch("sas_mcp_server.viya_client.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        make_client("my-token")
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer my-token"


def test_make_client_preserves_bearer_prefix(mock_env_vars):
    """Test make_client does not double-prefix Bearer."""
    with patch("sas_mcp_server.viya_client.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        make_client("Bearer my-token")
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer my-token"


# ---------------------------------------------------------------------------
# return_items
# ---------------------------------------------------------------------------


def test_return_items_existing_props():
    """Test return_items extracts specified fields."""
    items = [
        {"name": "item1", "description": "desc1", "extra": "x"},
        {"name": "item2", "description": "desc2", "extra": "y"},
    ]
    result = return_items(items, ["name", "description"])
    assert result == [
        {"name": "item1", "description": "desc1"},
        {"name": "item2", "description": "desc2"},
    ]


def test_return_items_missing_props():
    """Test return_items handles missing fields gracefully."""
    items = [
        {"name": "item1", "extra": "x"},
        {"description": "desc2", "extra": "y"},
    ]
    result = return_items(items, ["name", "description"])
    assert result == [
        {"name": "item1", "description": ""},
        {"name": "", "description": "desc2"},
    ]


def test_return_items_empty_list():
    """Test return_items handles empty input list."""
    result = return_items([], ["name", "description"])
    assert result == []


def test_return_items_no_matching_props():
    """Test return_items raises if no specified fields are present."""
    items = [
        {"extra": "x"},
        {"extra": "y"},
    ]
    with pytest.raises(ValueError):
        return_items(items, ["name", "description"])


# ---------------------------------------------------------------------------
# MCP tool coverage (Tier 5 model management tools)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_publishing_destinations_tool_request(mcp_server_with_mock_client):
    """Tool builds the expected destinations endpoint + query params."""
    mcp, mock_client = mcp_server_with_mock_client

    async with Client(mcp) as client:
        await client.call_tool(
            "list_publishing_destinations",
            {"limit": 25, "start": 10, "filter_name": "mas"},
        )

    url = mock_client.get.call_args[0][0]
    params = mock_client.get.call_args[1]["params"]
    assert "/modelPublish/destinations" in url
    assert params["limit"] == 25
    assert params["start"] == 10
    assert params["filter"] == "contains(name,'mas')"


@pytest.mark.asyncio
async def test_register_ml_champion_model_tool_calls_helper(
    mcp_server_with_mock_client,
):
    """Tool should pass MLRegisterProps + client to ml_register_publish."""
    mcp, mock_client = mcp_server_with_mock_client

    with patch(
        "sas_mcp_server.helpers.auto_ml_helpers.ml_register_publish",
        new_callable=AsyncMock,
    ) as mock_register:
        mock_register.return_value = {"message": "registered"}

        async with Client(mcp) as client:
            await client.call_tool(
                "register_ml_champion_model",
                {"project_id": "proj-123"},
            )

    mock_register.assert_awaited_once()
    args = mock_register.await_args.args
    assert isinstance(args[0], auto_ml_helpers.MLRegisterProps)
    assert args[0].project_id == "proj-123"
    assert args[1] is mock_client


@pytest.mark.asyncio
async def test_publish_ml_champion_model_tool_calls_helper(mcp_server_with_mock_client):
    """Tool should pass MLPublishProps + client to ml_register_publish."""
    mcp, mock_client = mcp_server_with_mock_client

    with patch(
        "sas_mcp_server.helpers.auto_ml_helpers.ml_register_publish",
        new_callable=AsyncMock,
    ) as mock_publish:
        mock_publish.return_value = {"message": "published"}

        async with Client(mcp) as client:
            await client.call_tool(
                "publish_ml_champion_model",
                {"project_id": "proj-123", "destination_name": "MAS"},
            )

    mock_publish.assert_awaited_once()
    args = mock_publish.await_args.args
    assert isinstance(args[0], auto_ml_helpers.MLPublishProps)
    assert args[0].project_id == "proj-123"
    assert args[0].destination_name == "MAS"
    assert args[1] is mock_client
