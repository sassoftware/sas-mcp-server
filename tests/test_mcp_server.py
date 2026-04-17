# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the MCP server and tools.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastmcp import Context
from sas_mcp_server.mcp_server import AuthenticationError


@pytest.mark.asyncio
async def test_execute_sas_code_success(sample_sas_code, mock_access_token):
    """Test successful SAS code execution through the tool."""
    mock_context = MagicMock(spec=Context)
    mock_context.get_state.return_value = mock_access_token

    with patch('sas_mcp_server.tools.run_one_snippet') as mock_run:
        mock_run.return_value = ("1", "completed", "Log output", "Listing output")

        token = mock_context.get_state("access_token")
        output = await mock_run(sample_sas_code, "1", token)

        mock_run.assert_called_once_with(sample_sas_code, "1", mock_access_token)
        assert output == ("1", "completed", "Log output", "Listing output")


@pytest.mark.asyncio
async def test_execute_sas_code_no_token():
    """Test that execute_sas_code raises error when no token is available."""
    mock_context = MagicMock(spec=Context)
    mock_context.get_state.return_value = None

    token = mock_context.get_state("access_token")
    assert token is None


@pytest.mark.asyncio
async def test_execute_sas_code_propagates_errors(sample_sas_code, mock_access_token):
    """Test that errors from run_one_snippet are propagated."""
    mock_context = MagicMock(spec=Context)
    mock_context.get_state.return_value = mock_access_token

    with patch('sas_mcp_server.tools.run_one_snippet') as mock_run:
        mock_run.side_effect = Exception("API Error")

        token = mock_context.get_state("access_token")
        with pytest.raises(Exception, match="API Error"):
            await mock_run(sample_sas_code, "1", token)


def test_authentication_error():
    """Test AuthenticationError exception."""
    error = AuthenticationError("Test error message")

    assert error.message == "Test error message"
    assert str(error) == "AuthenticationError: Test error message"


@pytest.mark.asyncio
async def test_execute_sas_code_with_multiline_code(mock_access_token):
    """Test execute_sas_code with multiline SAS code."""
    multiline_code = """
    data work.sample;
        do i = 1 to 10;
            x = i * 2;
            output;
        end;
    run;

    proc means data=work.sample;
        var x;
    run;
    """

    mock_context = MagicMock(spec=Context)
    mock_context.get_state.return_value = mock_access_token

    with patch('sas_mcp_server.tools.run_one_snippet') as mock_run:
        mock_run.return_value = ("1", "completed", "PROC MEANS output", "Statistics")

        token = mock_context.get_state("access_token")
        result = await mock_run(multiline_code, "1", token)

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0]
        assert call_args[0] == multiline_code
        assert call_args[1] == "1"
        assert call_args[2] == mock_access_token
