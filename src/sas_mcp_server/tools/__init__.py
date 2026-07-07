# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tiered SAS Viya MCP tools.

Tools are grouped into numbered tiers (see :data:`TIER_TITLES`). Each tier lives
in its own module exposing ``register(mcp, get_token)`` and depends only on the
shared lower layers (``viya_client``, ``viya_utils``, ``config``) plus
``tools._common`` — never on another tier — so any subset can be registered on
its own.

:func:`register_tools` registers every *enabled* tier. Operators choose the set
with the ``MCP_TIERS`` env var (e.g. ``"0-4"`` or ``"0,1,7"``); unset means all
tiers. Callers may also pass ``tiers=`` explicitly (a spec string or an iterable
of tier numbers), which overrides the env var.
"""

from collections.abc import Awaitable, Callable, Iterable

from fastmcp import Context, FastMCP

from ..config import MCP_TIERS
from ..exceptions import ConfigError
from ..viya_client import logger
from . import (
    automl,
    compute,
    data_ops,
    decisioning,
    discovery,
    jobs,
    model_scoring,
    reports,
)

Registrar = Callable[[FastMCP, Callable[[Context], Awaitable[str]]], None]

_TIER_REGISTRARS: dict[int, Registrar] = {
    0: compute.register,
    1: discovery.register,
    2: data_ops.register,
    3: reports.register,
    4: jobs.register,
    5: automl.register,
    6: model_scoring.register,
    7: decisioning.register,
}

TIER_TITLES: dict[int, str] = {
    0: "Compute Contexts & Code Execution",
    1: "Data Discovery",
    2: "Data Operations & Files",
    3: "Reports & Visualization",
    4: "Batch Jobs & Async Execution",
    5: "Automated Machine Learning",
    6: "Model Management & Scoring",
    7: "Decisioning (Business Rules & Intelligent Decisioning)",
}

ALL_TIERS: frozenset[int] = frozenset(_TIER_REGISTRARS)


def _parse_tier_spec(spec: str) -> set[int]:
    """Parse a tier spec like ``"0-4,7"`` into a set of tier numbers."""
    tiers: set[int] = set()
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                raise ConfigError(f"Invalid tier range '{part}' in MCP_TIERS.") from None
            tiers.update(range(lo, hi + 1))
        else:
            try:
                tiers.add(int(part))
            except ValueError:
                raise ConfigError(f"Invalid tier '{part}' in MCP_TIERS.") from None
    unknown = tiers - ALL_TIERS
    if unknown:
        raise ConfigError(
            f"Unknown tier(s) {sorted(unknown)} in MCP_TIERS; valid tiers are {sorted(ALL_TIERS)}."
        )
    return tiers


def resolve_enabled_tiers(tiers: str | Iterable[int] | None = None) -> set[int]:
    """Resolve which tiers to register.

    Precedence: an explicit *tiers* argument wins (a spec string like ``"0-4"``
    or an iterable of tier ints); otherwise the ``MCP_TIERS`` env var is used; if
    neither selects anything, all tiers are enabled.
    """
    if tiers is None:
        tiers = MCP_TIERS
    if isinstance(tiers, str):
        return _parse_tier_spec(tiers) or set(ALL_TIERS)
    selected = {int(t) for t in tiers}
    unknown = selected - ALL_TIERS
    if unknown:
        raise ConfigError(f"Unknown tier(s) {sorted(unknown)}; valid tiers are {sorted(ALL_TIERS)}.")
    return selected or set(ALL_TIERS)


def register_tools(
    mcp: FastMCP,
    get_token: Callable[[Context], Awaitable[str]],
    tiers: str | Iterable[int] | None = None,
) -> None:
    """Register the enabled tiers' tools on *mcp*.

    Args:
        mcp: The FastMCP server instance to register tools on.
        get_token: ``async def get_token(ctx: Context) -> str`` returning a Viya
            access token. HTTP mode pulls it from context state; stdio mode reads
            a cached token or runs a device-code flow.
        tiers: Optional tier selection — a spec string (``"0-4,7"``), an iterable
            of tier numbers, or ``None`` to use the ``MCP_TIERS`` env var (all
            tiers when unset).
    """
    enabled = resolve_enabled_tiers(tiers)
    logger.info("Registering tool tiers: %s", sorted(enabled))
    for tier in sorted(enabled):
        _TIER_REGISTRARS[tier](mcp, get_token)


__all__ = [
    "ALL_TIERS",
    "TIER_TITLES",
    "register_tools",
    "resolve_enabled_tiers",
]
