# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for tool-tier selection and registration (MCP_TIERS / register_tools tiers=)."""

import pytest
from fastmcp import Client, FastMCP

from sas_mcp_server import tools
from sas_mcp_server.exceptions import ConfigError


async def _register(tiers):
    mcp = FastMCP("tier-test")

    async def get_token(ctx):
        return "test-token"

    tools.register_tools(mcp, get_token, tiers=tiers)
    async with Client(mcp) as client:
        return {t.name for t in await client.list_tools()}


def test_resolve_empty_selection_is_all_tiers():
    assert tools.resolve_enabled_tiers("") == set(tools.ALL_TIERS)
    assert tools.resolve_enabled_tiers([]) == set(tools.ALL_TIERS)


def test_resolve_range_list_and_csv():
    assert tools.resolve_enabled_tiers("0-4") == {0, 1, 2, 3, 4}
    assert tools.resolve_enabled_tiers("0,1,7") == {0, 1, 7}
    assert tools.resolve_enabled_tiers("0-2,7") == {0, 1, 2, 7}
    assert tools.resolve_enabled_tiers([2, 3]) == {2, 3}


@pytest.mark.parametrize("bad", ["0-99", "9", "abc", [42]])
def test_resolve_rejects_unknown_tiers(bad):
    with pytest.raises(ConfigError):
        tools.resolve_enabled_tiers(bad)


def test_env_var_drives_default(monkeypatch):
    monkeypatch.setattr(tools, "MCP_TIERS", "0-4")
    assert tools.resolve_enabled_tiers(None) == {0, 1, 2, 3, 4}
    monkeypatch.setattr(tools, "MCP_TIERS", "")
    assert tools.resolve_enabled_tiers(None) == set(tools.ALL_TIERS)


async def test_register_all_tiers_registers_everything():
    names = await _register(None)
    assert len(names) == 68
    assert "execute_sas_code" in names
    assert "publish_decision_flow" in names


async def test_register_subset_excludes_other_tiers():
    names = await _register("0-4")
    assert len(names) == 36
    assert "execute_sas_code" in names  # tier 0
    assert "list_jobs" in names  # tier 4
    assert "list_mas_modules" not in names  # tier 6
    assert "create_business_ruleset" not in names  # tier 7


async def test_register_single_tier():
    names = await _register("7")
    assert len(names) == 22
    assert "create_business_ruleset" in names
    assert "get_mas_module_step_signature" not in names  # tier 6, not 7
