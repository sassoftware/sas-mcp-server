# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 10 — Visual Investigator & AML.

Entry point that wires all VI/AML sub-modules into the MCP server.
Each sub-module exposes a ``register_*`` function; this module calls them
all so ``tools/__init__.py`` only needs one import.
"""

from collections.abc import Awaitable, Callable

from fastmcp import Context, FastMCP

from ..audit import register_audit_tools
from ..lineage import register_lineage_tools
from ._vi_alerts import register_aml_alerts
from ._vi_authoring import register_aml_authoring
from ._vi_backtest import register_aml_backtest
from ._vi_deploy import register_aml_deploy
from ._vi_documents import register_aml_documents
from ._vi_entities import register_aml_entities
from ._vi_external import register_aml_external
from ._vi_flows import register_aml_flows
from ._vi_flw import register_flw_tools
from ._vi_models import register_aml_models
from ._vi_monitoring import register_aml_monitoring
from ._vi_ui_apps import register_apps


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register all Tier 10 (Visual Investigator / AML) tools."""
    register_apps(mcp)  # UI providers — no get_token needed
    register_aml_alerts(mcp, get_token)
    register_aml_authoring(mcp, get_token)
    register_aml_backtest(mcp, get_token)
    register_aml_deploy(mcp, get_token)
    register_aml_documents(mcp, get_token)
    register_aml_entities(mcp, get_token)
    register_aml_external(mcp, get_token)
    register_flw_tools(mcp, get_token)
    register_aml_flows(mcp, get_token)
    register_aml_models(mcp, get_token)
    register_aml_monitoring(mcp, get_token)
    register_lineage_tools(mcp, get_token)
    register_audit_tools(mcp)
