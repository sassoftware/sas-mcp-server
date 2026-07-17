# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for read-only mode (MCP_READ_ONLY / register_tools read_only=)."""

import pytest
from fastmcp import Client, FastMCP

from sas_mcp_server import tools
from sas_mcp_server.tools._access import READ_ONLY_TOOLS, WRITE_TOOLS, ReadOnlyGate


async def _register(tiers=None, read_only=None):
    mcp = FastMCP("read-only-test")

    async def get_token(ctx):
        return "test-token"

    tools.register_tools(mcp, get_token, tiers=tiers, read_only=read_only)
    async with Client(mcp) as client:
        return {t.name for t in await client.list_tools()}


# --- the classification covers the whole surface ------------------------------


async def test_classification_partitions_every_registered_tool():
    """Every tool is classified exactly once — the drift guard.

    A tool added to a tier without being classified fails here, and is withheld
    in read-only mode until someone decides which side it belongs on.
    """
    registered = await _register()
    classified = READ_ONLY_TOOLS | WRITE_TOOLS
    assert registered - classified == set(), "unclassified tool(s) — add to _access.py"
    assert classified - registered == set(), "classified tool(s) that no tier registers"


def test_read_and_write_sets_are_disjoint():
    assert not (READ_ONLY_TOOLS & WRITE_TOOLS)


# --- filtering behaviour ------------------------------------------------------


async def test_read_only_registers_only_read_tools():
    names = await _register(read_only=True)
    assert names == set(READ_ONLY_TOOLS)
    assert len(names) == 43


async def test_read_only_withholds_every_mutating_tool():
    names = await _register(read_only=True)
    assert names & WRITE_TOOLS == set()


@pytest.mark.parametrize(
    "tool_name",
    [
        "execute_sas_code",  # arbitrary code
        "submit_batch_job",  # arbitrary code
        "delete_report",
        "delete_decision_flow",
        "create_report",
        "apply_report_operations",
        "update_business_rule",
        "upload_data",
        "score_data",  # strict: causes server-side work
        "catalog_run_adhoc_analysis",
        "promote_table_to_memory",
        "cancel_job",
        "reset_compute_session",
    ],
)
async def test_named_dangerous_tools_are_absent(tool_name):
    assert tool_name not in await _register(read_only=True)


@pytest.mark.parametrize(
    "tool_name",
    ["get_report", "list_reports", "export_report", "catalog_search", "get_castable_data"],
)
async def test_named_read_tools_survive(tool_name):
    assert tool_name in await _register(read_only=True)


async def test_default_is_unfiltered():
    assert len(await _register()) == 74


# --- composition with tier selection ------------------------------------------


async def test_composes_with_tier_selection():
    names = await _register(tiers="3", read_only=True)
    assert names == {
        "list_reports",
        "get_report",
        "get_report_outline",
        "describe_report_objects",
        "export_report",
    }


async def test_composes_with_tier_range():
    names = await _register(tiers="0-4", read_only=True)
    assert len(names) == 28
    assert "list_compute_contexts" in names  # tier 0, read
    assert "execute_sas_code" not in names  # tier 0, write
    assert "list_mas_modules" not in names  # tier 6, not selected


# --- env var wiring -----------------------------------------------------------


async def test_env_var_drives_default(monkeypatch):
    monkeypatch.setattr(tools, "MCP_READ_ONLY", True)
    assert await _register() == set(READ_ONLY_TOOLS)
    monkeypatch.setattr(tools, "MCP_READ_ONLY", False)
    assert len(await _register()) == 74


async def test_explicit_argument_overrides_env_var(monkeypatch):
    monkeypatch.setattr(tools, "MCP_READ_ONLY", True)
    assert len(await _register(read_only=False)) == 74
    monkeypatch.setattr(tools, "MCP_READ_ONLY", False)
    assert await _register(read_only=True) == set(READ_ONLY_TOOLS)


# --- the gate itself ----------------------------------------------------------
# The tiers all use @mcp.tool(), but the gate accepts every form FastMCP does so
# a tier written in another style cannot slip a mutating tool past it.


async def _gated_names(register_with_gate) -> set[str]:
    mcp = FastMCP("gate-test")
    gate = ReadOnlyGate(mcp, allowed=frozenset({"allowed_tool"}))
    register_with_gate(gate)
    async with Client(mcp) as client:
        return {t.name for t in await client.list_tools()}


async def test_gate_handles_bare_decorator():
    def register(gate):
        @gate.tool
        async def allowed_tool() -> str:
            """Allowed."""
            return "ok"

        @gate.tool
        async def blocked_tool() -> str:
            """Blocked."""
            return "no"

    assert await _gated_names(register) == {"allowed_tool"}


async def test_gate_handles_explicit_name_forms():
    def register(gate):
        @gate.tool("allowed_tool")
        async def positional_name() -> str:
            """Renamed onto the allowlist."""
            return "ok"

        @gate.tool(name="blocked_tool")
        async def keyword_name() -> str:
            """Renamed off the allowlist."""
            return "no"

    # Classification follows the REGISTERED name, not the Python function name.
    assert await _gated_names(register) == {"allowed_tool"}


async def test_gate_records_withheld_names():
    mcp = FastMCP("gate-test")
    gate = ReadOnlyGate(mcp, allowed=frozenset({"allowed_tool"}))

    @gate.tool()
    async def blocked_tool() -> str:
        """Blocked."""
        return "no"

    assert gate.withheld == ["blocked_tool"]


def test_gate_passes_other_attributes_through():
    mcp = FastMCP("gate-test")
    gate = ReadOnlyGate(mcp)
    assert gate.name == mcp.name
    # Bound methods are recreated per access, so compare equality, not identity.
    assert gate.prompt == mcp.prompt
