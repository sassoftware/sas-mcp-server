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

A second, independent axis selects *verbs* rather than domains: ``MCP_READ_ONLY``
(or ``read_only=``) withholds every tool that could change server-side state or
cause server-side work, across whichever tiers are enabled. See
:mod:`sas_mcp_server.tools._access`.
"""

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, cast

from fastmcp import Context, FastMCP

from ..config import MCP_READ_ONLY, MCP_TIERS
from ..exceptions import ConfigError
from ..viya_client import logger
from . import (
    automl,
    code_assistant,
    compute,
    data_ops,
    decisioning,
    discovery,
    glossary,
    jobs,
    model_scoring,
    reports,
    workbench,
)
from ._access import (
    DESTRUCTIVE_TOOLS,
    IDEMPOTENT_WRITE_TOOLS,
    OPEN_WORLD_TOOLS,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    ReadOnlyGate,
    annotations_for,
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
    8: workbench.register,
    9: glossary.register,
    10: code_assistant.register,
}

TIER_TITLES: dict[int, str] = {
    0: "Compute Contexts & Code Execution",
    1: "Data Discovery",
    2: "Data Operations & Files",
    3: "Reports & Visualization",
    4: "Batch Jobs & Async Execution",
    5: "Automated Machine Learning",
    6: "Model Management & Scoring",
    7: "Decisioning (SAS Intelligent Decisioning)",
    8: "Workbench (Execute Code Only)",
    9: "Business Glossary (SAS Data Governance)",
    10: "Code Assistance & Documentation (SAS Code Assistant)",
}

ALL_TIERS: frozenset[int] = frozenset(_TIER_REGISTRARS)

# tool name -> tier, filled in as the tiers register. Telemetry stamps it on
# every record so usage can be rolled up per tier (which tiers earn their place
# in a deployment's MCP_TIERS) without a second, drift-prone lookup table.
TOOL_TIERS: dict[str, int] = {}


class _TierRecorder:
    """FastMCP stand-in that records which tier registered each tool and
    stamps the tool's MCP annotations.

    Mirrors :class:`ReadOnlyGate`'s duck-typing so the tier modules stay
    unmodified, and composes with it (it wraps whichever target is in play).
    Two things happen on the way through, neither visible to the tier:

    * ``TOOL_TIERS[name] = tier`` — a side effect for telemetry and the landing
      page.
    * ``annotations=`` is filled in from :func:`annotations_for` (the central
      read/write classification) unless the tier passed its own, so every tool
      advertises ``readOnlyHint`` & co. to clients without any per-tool code
      (the model field is ``read_only_hint``; camelCase is the wire alias).
    """

    def __init__(self, target: Any, tier: int) -> None:
        self._target = target
        self._tier = tier

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        TOOL_TIERS[name] = self._tier
        kwargs.setdefault("annotations", annotations_for(name))

    def tool(self, name_or_fn: Any = None, **kwargs: Any) -> Any:
        if callable(name_or_fn):  # bare @mcp.tool
            self._record(kwargs.get("name") or name_or_fn.__name__, kwargs)
            return self._target.tool(name_or_fn, **kwargs)

        def decorator(fn: Callable[..., Any]) -> Any:
            explicit = name_or_fn if isinstance(name_or_fn, str) else kwargs.get("name")
            self._record(explicit or fn.__name__, kwargs)
            return self._target.tool(name_or_fn, **kwargs)(fn)

        return decorator

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target, item)


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
    read_only: bool | None = None,
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
        read_only: When true, register only the read-only tools of the enabled
            tiers; mutating tools are never registered, so they do not appear in
            ``list_tools``. ``None`` uses the ``MCP_READ_ONLY`` env var.
    """
    enabled = resolve_enabled_tiers(tiers)
    ro = MCP_READ_ONLY if read_only is None else bool(read_only)
    gate = ReadOnlyGate(mcp) if ro else None
    # The gate stands in for the server by duck-typing ``tool()`` — deliberate,
    # so the tiers register unmodified — which a static type cannot express.
    target = cast(FastMCP, gate) if gate is not None else mcp
    logger.info("Registering tool tiers: %s (read_only=%s)", sorted(enabled), ro)
    for tier in sorted(enabled):
        # Tier 8's sole tool is already included when Tier 0 is enabled.
        if tier == 8 and 0 in enabled:
            continue
        # Wrapped per tier so TOOL_TIERS learns the tier each tool came from.
        _TIER_REGISTRARS[tier](cast(FastMCP, _TierRecorder(target, tier)), get_token)
    if gate is not None:
        logger.info(
            "Read-only mode: withheld %d mutating tool(s): %s",
            len(gate.withheld),
            ", ".join(sorted(gate.withheld)),
        )


__all__ = [
    "ALL_TIERS",
    "DESTRUCTIVE_TOOLS",
    "IDEMPOTENT_WRITE_TOOLS",
    "OPEN_WORLD_TOOLS",
    "READ_ONLY_TOOLS",
    "TIER_TITLES",
    "TOOL_TIERS",
    "WRITE_TOOLS",
    "annotations_for",
    "register_tools",
    "resolve_enabled_tiers",
]
