# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for prompt template rendering.
"""
import pytest
from unittest.mock import MagicMock
from fastmcp import FastMCP
from sas_mcp_server.prompts import register_prompts


@pytest.fixture
def prompt_mcp():
    """Create a minimal FastMCP instance with prompts registered."""
    mcp = FastMCP("test-prompts")
    register_prompts(mcp)
    return mcp


# ---------------------------------------------------------------------------
# Test that all prompts are registered
# ---------------------------------------------------------------------------


def test_all_prompts_registered(prompt_mcp):
    """Verify every expected prompt is registered."""
    expected = {
        "debug_sas_log",
        "explore_dataset",
        "data_quality_check",
        "statistical_analysis",
        "optimize_sas_code",
        "explain_sas_code",
        "sas_macro_builder",
        "generate_report",
    }
    registered = set(prompt_mcp._prompt_manager._prompts.keys())
    assert expected.issubset(registered), f"Missing prompts: {expected - registered}"


# ---------------------------------------------------------------------------
# Individual prompt rendering tests
# ---------------------------------------------------------------------------


def test_debug_sas_log_basic(prompt_mcp):
    """Test debug_sas_log renders with required params."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["debug_sas_log"].fn
    messages = prompt_fn(log_text="ERROR: File not found.")
    assert len(messages) == 1
    assert "ERROR: File not found." in messages[0].content.text


def test_debug_sas_log_with_filter(prompt_mcp):
    """Test debug_sas_log renders with severity filter."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["debug_sas_log"].fn
    messages = prompt_fn(log_text="some log", severity_filter="ERROR")
    assert "ERROR" in messages[0].content.text


def test_explore_dataset(prompt_mcp):
    """Test explore_dataset prompt."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["explore_dataset"].fn
    messages = prompt_fn(library="WORK", dataset="CARS")
    assert "WORK.CARS" in messages[0].content.text
    assert "PROC CONTENTS" in messages[0].content.text


def test_explore_dataset_with_focus_vars(prompt_mcp):
    """Test explore_dataset with focus variables."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["explore_dataset"].fn
    messages = prompt_fn(library="WORK", dataset="CARS", focus_vars="mpg, weight")
    assert "mpg, weight" in messages[0].content.text


def test_data_quality_check(prompt_mcp):
    """Test data_quality_check prompt."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["data_quality_check"].fn
    messages = prompt_fn(library="SASHELP", dataset="CLASS")
    assert "SASHELP.CLASS" in messages[0].content.text
    assert "completeness" in messages[0].content.text


def test_statistical_analysis(prompt_mcp):
    """Test statistical_analysis prompt."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["statistical_analysis"].fn
    messages = prompt_fn(
        analysis_type="linear regression",
        response_variable="price",
        predictors="sqft, bedrooms",
        dataset="WORK.HOUSES",
    )
    content = messages[0].content.text
    assert "linear regression" in content
    assert "price" in content
    assert "sqft, bedrooms" in content


def test_optimize_sas_code(prompt_mcp):
    """Test optimize_sas_code prompt."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["optimize_sas_code"].fn
    messages = prompt_fn(sas_code="data test; set big; run;")
    assert "data test; set big; run;" in messages[0].content.text


def test_explain_sas_code(prompt_mcp):
    """Test explain_sas_code prompt."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["explain_sas_code"].fn
    messages = prompt_fn(sas_code="proc sql; quit;", audience_level="beginner")
    assert "beginner" in messages[0].content.text


def test_sas_macro_builder(prompt_mcp):
    """Test sas_macro_builder prompt."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["sas_macro_builder"].fn
    messages = prompt_fn(macro_name="load_data", purpose="Load CSV into CAS")
    assert "%load_data" in messages[0].content.text
    assert "Load CSV into CAS" in messages[0].content.text


def test_generate_report(prompt_mcp):
    """Test generate_report prompt."""
    prompt_fn = prompt_mcp._prompt_manager._prompts["generate_report"].fn
    messages = prompt_fn(dataset="WORK.SALES", report_type="detailed", output_format="PDF")
    content = messages[0].content.text
    assert "detailed" in content
    assert "PDF" in content
    assert "WORK.SALES" in content
