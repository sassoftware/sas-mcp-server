# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the Tier 10 Code Assistance & Documentation integration."""

import pytest
from fastmcp import Client, FastMCP

from conftest import _make_mock_response
from sas_mcp_server import tools

_TIER_TEN_TOOLS = {"get_doc_answer", "generate_sas_code"}
VIYA = "https://test.viya.com"


@pytest.fixture(autouse=True)
def _pin_endpoint(monkeypatch):
    """Pin VIYA_ENDPOINT so request URLs are predictable regardless of .env."""
    import sas_mcp_server.viya_client as viya_client

    monkeypatch.setattr(viya_client, "VIYA_ENDPOINT", VIYA)


async def _tool_names(*, read_only: bool = False) -> set[str]:
    mcp = FastMCP("genai-assistant-test")

    async def get_token(ctx):
        return "test-token"

    tools.register_tools(mcp, get_token, tiers="10", read_only=read_only)
    async with Client(mcp) as client:
        return {tool.name for tool in await client.list_tools()}


async def _call(mcp, mock_client, tool_name, arguments):
    """Register Tier 10 onto the fixture's server and call one tool."""

    async def get_token(ctx):
        return "test-token"

    # The fixture's server registers the whole default surface; add Tier 10
    # alone here so a call resolves to the same mock client and token provider.
    tools.register_tools(mcp, get_token, tiers="10")
    async with Client(mcp) as client:
        return await client.call_tool(tool_name, arguments)


async def test_tier_ten_registers_both_tools_in_read_only_mode():
    """Both Tier 10 tools are classified read-only, so read-only mode withholds neither."""
    assert await _tool_names(read_only=True) == _TIER_TEN_TOOLS
    assert await _tool_names() == _TIER_TEN_TOOLS


async def test_generate_sas_code_forwards_the_reference_rest_contract(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"content": "data example;\nrun;"})

    result = await _call(
        mcp,
        mock_client,
        "generate_sas_code",
        {
            "prompt": "Create a DATA step",
            "language": "sas",
            "use_rag_for_sas": False,
            "product": ["SAS Studio"],
        },
    )

    assert result.data == {"generated_code": "data example;\nrun;"}
    url = mock_client.post.call_args.args[0]
    payload = mock_client.post.call_args.kwargs["json"]
    assert url == f"{VIYA}/genAiGateway/v1/copilotRequest"
    assert payload == {
        "applicationName": "SAS MCP Server",
        "copilot": {"id": "codeAssistant", "version": "v1"},
        "message": {
            "type": "userRequest",
            "content": "Generate code from the provided requirements.",
            "context": {
                "type": "code",
                "commandId": "generate",
                "useRAGForSAS": False,
                "languageId": "sas",
                "selectedText": "Create a DATA step",
                "currentFile": {
                    "name": "mcp-request.sas",
                    "language": "sas",
                    "content": "Create a DATA step",
                },
                "product": ["SAS Studio"],
            },
        },
    }


async def test_generate_sas_code_defaults_to_sas_and_omits_absent_product(mcp_server_with_mock_client):
    """language defaults to sas, and no `product` key is sent when none was asked for."""
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"content": "proc print; run;"})

    await _call(mcp, mock_client, "generate_sas_code", {"prompt": "Print a table"})

    context = mock_client.post.call_args.kwargs["json"]["message"]["context"]
    assert context["languageId"] == "sas"
    assert context["useRAGForSAS"] is True
    assert "product" not in context


async def test_get_doc_answer_forwards_the_doc_context(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"content": "PROC MEANS computes summary statistics."})

    result = await _call(mcp, mock_client, "get_doc_answer", {"question": "What does PROC MEANS do?"})

    assert result.data == {"answer": "PROC MEANS computes summary statistics."}
    payload = mock_client.post.call_args.kwargs["json"]
    assert payload["message"]["content"] == "What does PROC MEANS do?"
    assert payload["message"]["context"] == {
        "type": "doc",
        "product": [
            "SAS Studio",
            "SAS Studio with SAS Viya Platform Programming Documentation",
        ],
    }


async def test_get_doc_answer_accepts_the_nested_message_shape(mcp_server_with_mock_client):
    """The reply is read from `message.content` when the gateway nests it."""
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"message": {"content": "Nested answer."}})

    result = await _call(mcp, mock_client, "get_doc_answer", {"question": "Anything?"})

    assert result.data == {"answer": "Nested answer."}


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_doc_answer", {"question": "   "}),
        ("generate_sas_code", {"prompt": "   "}),
    ],
)
async def test_blank_input_is_rejected_before_any_request(mcp_server_with_mock_client, tool_name, arguments):
    mcp, mock_client = mcp_server_with_mock_client

    with pytest.raises(Exception, match="must not be empty"):
        await _call(mcp, mock_client, tool_name, arguments)

    mock_client.post.assert_not_called()


async def test_an_empty_copilot_reply_is_an_error_not_an_empty_answer(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"content": "   "})

    with pytest.raises(Exception, match="empty response"):
        await _call(mcp, mock_client, "get_doc_answer", {"question": "What does PROC MEANS do?"})


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ('["SAS Studio"]', ["SAS Studio"]),  # JSON-encoded array
        ("SAS Studio", ["SAS Studio"]),  # bare string
        (["SAS Studio"], ["SAS Studio"]),  # a real array, unchanged
    ],
)
async def test_product_accepts_the_shapes_clients_actually_send(mcp_server_with_mock_client, sent, expected):
    """Some MCP clients serialize an optional list param as a string; both are accepted.

    Without the BeforeValidator, pydantic rejects the call before the tool body
    runs and the model cannot correct it. See _common.coerce_str_or_json_list.
    """
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"content": "An answer."})

    await _call(mcp, mock_client, "get_doc_answer", {"question": "What is PROC MEANS?", "product": sent})

    assert mock_client.post.call_args.kwargs["json"]["message"]["context"]["product"] == expected
