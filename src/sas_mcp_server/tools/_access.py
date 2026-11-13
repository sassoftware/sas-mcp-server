# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read/write classification of every tool, and the read-only registration gate.

Read-only mode is a *filter*, not a tier: the read/write split cuts across every
tier (Tier 3 holds both ``get_report`` and ``delete_report``), so it composes
with ``MCP_TIERS`` rather than sitting beside it. ``MCP_TIERS=3,7`` plus
``MCP_READ_ONLY=true`` exposes the read tools of Tiers 3 and 7.

The classification is deliberately central rather than per-tier: one table is
what a reviewer has to trust, so it can be audited in a single read. The tier
modules are unaware of it — :class:`ReadOnlyGate` intercepts their ``@mcp.tool()``
calls — which keeps each tier's "depends only on ``_common``" property intact.

**Strict definition.** A tool is read-only only if it can neither change
server-side state nor cause server-side work. That excludes the whole
execute/run family even where it looks like a query: ``execute_sas_code`` and
``submit_batch_job`` run arbitrary code (any verb, including DELETE);
``score_data``, ``catalog_run_agent`` and ``catalog_run_adhoc_analysis`` spawn
jobs and leave run records; ``promote_table_to_memory`` mutates CAS state;
``cancel_job`` and ``reset_compute_session`` destroy something the caller owns.

**Fail-closed.** :data:`READ_ONLY_TOOLS` is an allowlist, so a tool missing from
both sets — a newly added one, say — is withheld in read-only mode rather than
silently exposed. ``test_read_only.py`` asserts the two sets exactly partition
the registered surface, so adding a tool without classifying it fails CI.

**Advertised as well as enforced.** The same partition is published to clients
as MCP *tool annotations* (spec revision 2025-03-26, "Tool annotations"):
``read_only_hint`` is derived from :data:`READ_ONLY_TOOLS` — one table, one
truth — and the finer ``destructive_hint`` / ``idempotent_hint`` /
``open_world_hint`` come from the small sets below. Annotations are hints for the
*client's* approval UX (group read-only tools, warn before destructive ones),
not enforcement; ``MCP_READ_ONLY`` remains the enforcement, and the spec tells
clients to treat hints as untrusted unless the server is trusted. Without them
a client must assume the pessimistic defaults — writable, destructive,
open-world — for every tool, ``list_caslibs`` included. See
:func:`annotations_for`.
"""

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

# --- classification ----------------------------------------------------------
# Grouped by tier, matching src/sas_mcp_server/tools/<module>.py.

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        # Tier 0 — Compute Contexts & Code Execution
        "list_compute_contexts",
        # Tier 1 — Data Discovery
        "catalog_search",
        "catalog_search_helper",
        "catalog_find_instance",
        "catalog_list_agents",
        "catalog_get_agent_history",
        "catalog_get_adhoc_analysis",
        "catalog_download_table_profile",
        "list_compute_libraries",
        "list_compute_tables",
        "list_compute_columns",
        "list_cas_servers",
        "list_caslibs",
        "list_castables",
        "list_source_tables",
        "get_castable_info",
        "get_castable_columns",
        "get_castable_data",
        "get_compute_table_data",
        # Tier 2 — Data Operations & Files
        "list_files",
        "download_file",
        "find_folder",
        # Tier 3 — Reports & Visualization
        "list_reports",
        "get_report",
        "get_report_outline",
        "describe_report_objects",
        "export_report",
        # Tier 4 — Batch Jobs & Async Execution
        "list_jobs",
        "get_job_status",
        "get_job_log",
        # Tier 5 — Automated Machine Learning
        "list_ml_projects",
        # Tier 6 — Model Management & Scoring
        "list_registered_models",
        "list_publishing_destinations",
        "list_mas_modules",
        "get_mas_module_step_signature",
        "get_model",
        "list_model_versions",
        "get_score_execution",
        "list_published_models",
        # Tier 7 — Decisioning
        "list_business_rulesets",
        "get_business_ruleset",
        "list_business_ruleset_revisions",
        "list_business_rules",
        "get_business_rule",
        "list_decision_flows",
        "get_decision_flow",
        "get_decision_flow_code",
        "list_decision_flow_revisions",
        "get_decision_flow_revision",
        "list_decisioning_assets",
        "get_decisioning_asset",
        "get_decisioning_asset_code",
        "list_decisioning_asset_revisions",
        "get_lookup_table",
        # Tier 9 — Business Glossary
        "list_glossary_term_types",
        "get_glossary_term_type",
        "search_glossary_terms",
        "list_glossary_terms",
        "get_glossary_term",
        "list_term_assets",
        "list_table_terms",
        # Tier 11 — Model Lifecycle
        "list_model_repositories",
        "list_model_projects",
        "get_project_kpis",
    }
)

# Every tool that is NOT read-only, listed explicitly so the completeness test
# can prove the classification covers the whole surface. Comments mark the
# tools whose exclusion is a judgement call rather than an obvious create /
# update / delete.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        # Tier 0
        "execute_sas_code",  # arbitrary code — can perform any verb
        "reset_compute_session",  # destroys the caller's session state
        # Tier 1
        "catalog_run_agent",  # spawns a run, leaves history
        "catalog_run_adhoc_analysis",  # spawns a profiling job
        # Runs caller-submitted FedSQL in a compute session: it spawns a job and
        # materialises scratch tables, so it "causes server-side work" whatever
        # the statement says. Its SELECT-only screen is a strong guard but the
        # sole one — PROC FEDSQL ignores a libref's access=readonly (verified:
        # a DATA step write is denied, FedSQL's CREATE/UPDATE/DELETE/DROP on the
        # same libref succeed), so there is no library-level backstop behind it.
        "query_data",
        # Tier 2
        "upload_data",
        "upload_inline_data",
        "upload_file",
        "promote_table_to_memory",  # mutates CAS in-memory state
        "create_folder",
        "save_castable",  # writes a file into the caslib's data source
        "drop_castable",  # removes the table from CAS memory
        # Tier 3
        "create_report",
        "apply_report_operations",
        "copy_report",
        "delete_report",
        # Tier 4
        "submit_batch_job",  # runs arbitrary code
        "cancel_job",  # mutates a running job
        # Tier 5
        "create_ml_project",
        "run_ml_project",
        "register_ml_champion_model",
        "publish_ml_champion_model",
        # Tier 6
        "score_data",  # invokes a MAS module; may persist output
        # Runs a SAS session and a score job, writing (then removing) scratch
        # tables and a score definition: server-side work.
        "test_score_model",
        "score_table",  # writes the scored table and leaves a score definition
        # Tier 7
        "create_business_ruleset",
        "update_business_ruleset",
        "delete_business_ruleset",
        "lock_business_ruleset_revision",
        "create_business_rule",
        "update_business_rule",
        "delete_business_rule",
        "create_decision_flow",
        "update_decision_flow",
        "delete_decision_flow",
        "lock_decision_flow_revision",
        "publish_decision_flow",
        "lock_decisioning_asset_revision",
        "copy_decisioning_asset",
        "delete_decisioning_asset",
        "create_lookup_table",
        "update_lookup_table",
        "activate_lookup_table",
        # Compiles DS2 in a SAS session to answer: no state change, but
        # server-side work, which the strict definition excludes.
        "validate_decision_expression",
        # Tier 9
        "create_glossary_term",
        "update_glossary_term",
        "delete_glossary_term",
        # A term type is the template terms are created from, so these change
        # what every term of that type must carry — not just one term.
        "create_glossary_term_type",
        "update_glossary_term_type",
        "delete_glossary_term_type",
        "import_glossary_terms",
        # Creates/removes a catalog relationship between a term and a column.
        "assign_glossary_term",
        "unassign_glossary_term",
        # Tier 10
        "create_segmentation_tree",
        "update_segmentation_tree",
        "create_segmentation_tree_from_table",
        "create_code_file",
        "update_code_file",
        "create_value_list",
        "update_value_list_entries",
        "create_custom_function",
        "update_custom_function",
        "validate_custom_function",  # compiles DS2 in a SAS session, like validate_decision_expression
        # Tier 11
        "create_model_project",
        "set_project_champion",  # also fills the project's target fields and variables
        # Runs PROC ASTORE in a SAS session: no state change, but server-side work.
        "describe_analytic_store",
        "register_model",
        "publish_model",
        "validate_published_model",  # a score job against the published copy, like test_score_model
        "create_performance_definition",
        "run_performance_definition",  # scores and writes results to ModelPerformanceData
        "set_kpi_alert_rule",
        "delete_kpi_alert_rule",
    }
)


# --- behaviour hints (MCP tool annotations) ----------------------------------
# All three sets are subsets of WRITE_TOOLS (asserted in tests); a read-only
# tool is by definition non-destructive, idempotent and — here — closed-world.

# May remove or overwrite state that exists before the call. The spec's
# contrast is "destructive updates" vs "only additive updates", so an update
# that replaces an object's content counts, as does a create with a
# name-conflict policy that can overwrite. Arbitrary code can do anything.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "execute_sas_code",  # arbitrary code
        "submit_batch_job",  # arbitrary code
        "reset_compute_session",  # destroys the caller's session
        "cancel_job",  # kills a running job
        "delete_report",
        "delete_business_ruleset",
        "delete_business_rule",
        "delete_decision_flow",
        "update_business_ruleset",  # PUT replaces the rule set's content
        "update_business_rule",  # PUT replaces the rule's content
        "update_decision_flow",  # PUT replaces the full flow
        "delete_decisioning_asset",
        "update_segmentation_tree",  # node / outcomes replace the tree's structure
        "update_code_file",  # new code replaces the working revision's code
        "update_value_list_entries",  # replaces the entries
        "update_lookup_table",  # remove_keys / replace_entries drop entries
        # Replaces the entries every published decision using the table reads.
        "activate_lookup_table",
        "update_custom_function",  # new code changes every rule set that calls it
        "apply_report_operations",  # operations can remove pages/objects
        "create_report",  # on_conflict="replace" can overwrite a report
        "copy_report",  # result_name_conflict="replace" likewise
        "publish_ml_champion_model",  # re-publish replaces the destination module
        "delete_glossary_term",
        "unassign_glossary_term",  # removes an existing term/column assignment
        # PUT replaces the whole term; the tool merges first, but a caller can
        # still overwrite a definition or an attribute that was already set.
        "update_glossary_term",
        # Takes the attribute definitions of every term of that type with it.
        "delete_glossary_term_type",
        # remove_attributes drops a definition, which stops every existing
        # term's stored value from being readable as that attribute.
        "update_glossary_term_type",
        # update_existing=true overwrites a term already at that path.
        "import_glossary_terms",
        # allow_unsaved=true drops a table that only exists in memory.
        "drop_castable",
        "save_castable",  # replace=true overwrites the file
        "score_table",  # an existing output table of that name is replaced
        # replace=true deletes the model of that name; it comes back with a new ID.
        "register_model",
        "publish_model",  # replace=true replaces the published model
        "set_project_champion",  # replaces the project's current champion
        # replace=true changes the project's one performance definition.
        "create_performance_definition",
        # Updates the rule for the same KPI, variable and direction in place.
        "set_kpi_alert_rule",
        "delete_kpi_alert_rule",
    }
)

# Repeating the call with the same arguments has no additional effect: PUTs
# (the update_* tools), deletes of something already gone, cancelling a
# cancelled job, and promote_table_to_memory's explicit already-loaded guard.
# Everything else on the write side creates, uploads, or starts work anew each
# time — or we could not verify otherwise, and the spec's default is "no".
IDEMPOTENT_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "update_business_ruleset",
        "update_business_rule",
        "update_decision_flow",
        "update_segmentation_tree",
        "update_code_file",
        "update_value_list_entries",
        "update_custom_function",
        "delete_decisioning_asset",
        "validate_custom_function",
        "validate_decision_expression",
        "delete_report",
        "delete_business_ruleset",
        "delete_business_rule",
        "delete_decision_flow",
        "cancel_job",
        "reset_compute_session",
        "promote_table_to_memory",
        "update_glossary_term",
        "delete_glossary_term",
        "update_glossary_term_type",
        "delete_glossary_term_type",
        # Both check for the existing link first and report it rather than
        # creating a duplicate or failing on an absent one.
        "assign_glossary_term",
        "unassign_glossary_term",
        # Each finds what an earlier call made and reports it instead of acting again.
        "create_folder",
        "create_model_project",
        "save_castable",
        "drop_castable",
        "set_project_champion",
        "create_performance_definition",  # returns the project's definition rather than a second one
        # One rule per KPI, variable and direction: a repeat sets the same values again.
        "set_kpi_alert_rule",
        "delete_kpi_alert_rule",
        "describe_analytic_store",  # like validate_custom_function: work, no state change
    }
)

# Can reach beyond the one authenticated Viya deployment: arbitrary SAS code
# (PROC HTTP, FILENAME URL, ...) and the upload tools' `url` source. Every
# other tool talks only to Viya, so its world is closed.
OPEN_WORLD_TOOLS: frozenset[str] = frozenset(
    {
        "execute_sas_code",
        "submit_batch_job",
        "upload_data",
        "upload_file",
        # Git and container-registry destinations are outside Viya.
        "publish_model",
    }
)

# What an unclassified tool advertises: the spec's own pessimistic defaults,
# stated explicitly rather than left implicit. test_read_only.py guarantees no
# registered tool takes this path, so this is belt-and-braces, not a policy.
_PESSIMISTIC = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def annotations_for(name: str) -> ToolAnnotations:
    """MCP tool annotations for *name*, derived from the classification above.

    ``read_only_hint`` mirrors :data:`READ_ONLY_TOOLS` exactly, so what a client
    is told and what ``MCP_READ_ONLY`` enforces cannot drift apart. Unknown
    names get the pessimistic defaults (fail closed).
    """
    if name in READ_ONLY_TOOLS:
        return ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=name in OPEN_WORLD_TOOLS,
        )
    if name in WRITE_TOOLS:
        return ToolAnnotations(
            read_only_hint=False,
            destructive_hint=name in DESTRUCTIVE_TOOLS,
            idempotent_hint=name in IDEMPOTENT_WRITE_TOOLS,
            open_world_hint=name in OPEN_WORLD_TOOLS,
        )
    return _PESSIMISTIC.model_copy()


# --- registration gate -------------------------------------------------------


class ReadOnlyGate:
    """FastMCP stand-in that withholds mutating tools at registration time.

    Wraps the real server and intercepts ``@mcp.tool()`` so the tier modules
    register unmodified: a tool outside :data:`READ_ONLY_TOOLS` is never handed
    to FastMCP at all. It is therefore absent from ``list_tools`` and uncallable
    — the model cannot see it, so it cannot try it and be refused. Any other
    attribute access falls through to the wrapped server.
    """

    def __init__(self, mcp: FastMCP, allowed: frozenset[str] = READ_ONLY_TOOLS) -> None:
        self._mcp = mcp
        self._allowed = allowed
        self.withheld: list[str] = []

    def tool(self, name_or_fn: Any = None, **kwargs: Any) -> Any:
        """Mirror ``FastMCP.tool``, dropping tools that are not read-only.

        Handles every calling form FastMCP accepts (bare ``@mcp.tool``,
        ``@mcp.tool()``, ``@mcp.tool("name")``, ``@mcp.tool(name=...)``) so the
        gate cannot be bypassed by a tier written in a different style. A
        withheld tool's function is returned undecorated; tiers never use the
        return value.
        """
        if callable(name_or_fn):  # bare @mcp.tool — returns the tool, not a decorator
            name = kwargs.get("name") or name_or_fn.__name__
            if name not in self._allowed:
                self.withheld.append(name)
                return name_or_fn
            return self._mcp.tool(name_or_fn, **kwargs)

        def decorator(fn: Callable[..., Any]) -> Any:
            explicit = name_or_fn if isinstance(name_or_fn, str) else kwargs.get("name")
            name = explicit or fn.__name__
            if name not in self._allowed:
                self.withheld.append(name)
                return fn
            return self._mcp.tool(name_or_fn, **kwargs)(fn)

        return decorator

    def __getattr__(self, item: str) -> Any:
        return getattr(self._mcp, item)


__all__ = [
    "DESTRUCTIVE_TOOLS",
    "IDEMPOTENT_WRITE_TOOLS",
    "OPEN_WORLD_TOOLS",
    "READ_ONLY_TOOLS",
    "WRITE_TOOLS",
    "ReadOnlyGate",
    "annotations_for",
]
