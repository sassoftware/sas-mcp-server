# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 7 — Decisioning tools (SAS Business Rules & Intelligent Decisioning).

Rule sets and decision flows are always stored in a SAS Content folder: the
one the call names, else the deployment's ``MCP_DEFAULT_FOLDER``. With neither,
the create tools refuse and ask for one. See
:mod:`sas_mcp_server.helpers.folder_helpers`.

The tier's other content — segmentation trees, code files, value lists, custom
functions, validation, and life-cycle tools for every kind — is registered from
:mod:`sas_mcp_server.tools.decisioning_content`, and lookup tables from
:mod:`sas_mcp_server.tools.decisioning_lookups`. Decision flows take every step
kind through :mod:`sas_mcp_server.helpers.decision_flow_helpers`.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from .. import config
from ..config import VIYA_ENDPOINT
from ..helpers import decisioning_assets as assets
from ..helpers import lookup_helpers as lookups
from ..helpers.decision_flow_helpers import build_steps
from ..helpers.folder_helpers import keep_in_folder, resolve_target_folder
from ..viya_client import (
    contains_filter,
    delete_resource,
    get_json,
    get_paged_items,
    post_json,
    put_json,
    raise_for_viya_status,
    return_items,
)
from ._common import coerce_json_list, make_session_helpers
from .decisioning_lifecycle import register_lifecycle_tools
from .decisioning_lookups import register_lookup_tools

# Tolerant alias for list params some MCP clients deliver as JSON-encoded
# strings (see _common.coerce_json_list). The published schema is unchanged.
DictListParam = Annotated[list[dict[str, Any]], BeforeValidator(coerce_json_list)]

RULE_SET_CONTENT_TYPE = "ruleSet"
DECISION_CONTENT_TYPE = "decision"


def _build_decision_flow_body(
    name: str, signature: list[dict[str, Any]], rule_set_steps: list[dict[str, Any]], description: str | None
) -> dict[str, Any]:
    """Build a SAS Decisions flow body from ruleSetSteps shorthand.

    Each entry in *rule_set_steps* is ``{"ruleSetId", "versionId", "mappings"}``
    (mappings: ``[{"stepTermName", "direction", "targetDecisionTermName"}, ...]``)
    and is expanded into the ``application/vnd.sas.decision.step.ruleset`` step
    shape the API expects — including the lowercase ``ruleset`` key, which does
    not match the OpenAPI schema's documented ``ruleSet``.
    """
    steps = []
    for i, step in enumerate(rule_set_steps):
        missing = [k for k in ("ruleSetId", "versionId", "mappings") if k not in step]
        if missing:
            raise ValueError(
                f"rule_set_steps[{i}] is missing required key(s): {', '.join(missing)}. "
                "Each step needs ruleSetId, versionId (a locked rule set revision), and mappings."
            )
        steps.append({
            "type": "application/vnd.sas.decision.step.ruleset",
            "ruleset": {"id": step["ruleSetId"], "versionId": step["versionId"]},
            "mappings": step["mappings"],
        })
    return {
        "name": name,
        "description": description or "",
        "signature": signature,
        "flow": {"steps": steps},
    }


# One parameter of the outer execute method: optional in_out, a type such as
# double, varchar(100) or package datagrid, and the quoted name.
_EXECUTE_PARAM = re.compile(r'\s*(in_out\s+)?(?:package\s+)?[A-Za-z]\w*(?:\(\s*\d+\s*\))?\s+"([^"]+)"\s*')


def plain_input_names(code: str) -> tuple[str, list[str]]:
    """MAS decision code with the decision's input variables under their own names.

    Code generated for MAS ends with a wrapper ``method execute(...)`` that
    declares every input-only variable as ``"name_"`` and hands it on to
    ``execute_internal``, which uses the real names; MAS publishes those wrapper
    names as the module's inputs. Renaming them back inside the wrapper keeps
    the code's behaviour and gives the module the decision's variable names.
    Code whose wrapper does not have the expected shape is returned unchanged.
    Returns the code and the names restored.
    """
    start = code.rfind("method execute(")
    end = code.find("endpackage", start)
    if start < 0 or end < 0 or "execute_internal(" not in code[start:end]:
        return code, []
    block = code[start:end]
    params = []
    for part in block[len("method execute("):block.index(");")].split(","):
        match = _EXECUTE_PARAM.fullmatch(part)
        if match is None:
            return code, []
        params.append((bool(match.group(1)), match.group(2)))
    taken = {name for _, name in params}
    renamed = [name for in_out, name in params if not in_out and name.endswith("_") and name[:-1] not in taken]
    for name in renamed:
        block = block.replace(f'"{name}"', f'"{name[:-1]}"')
    return code[:start] + block + code[end:], [name[:-1] for name in renamed]


async def _flow_body(
    client: Any,
    name: str,
    signature: list[dict[str, Any]],
    steps: list[dict[str, Any]] | None,
    rule_set_steps: list[dict[str, Any]] | None,
    description: str | None,
    add_missing_variables: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The request body for a decision flow, and the steps report when ``steps`` was used."""
    if (steps is None) == (rule_set_steps is None):
        raise ValueError(
            "Give the flow as exactly one of steps (any step kind) or rule_set_steps (rule sets only)."
        )
    if rule_set_steps is not None:
        return _build_decision_flow_body(name, signature, rule_set_steps, description), None
    assert steps is not None  # exactly one of the two was given
    built, report = await build_steps(client, steps, signature, add_missing_variables=add_missing_variables)
    body = {"name": name, "description": description or "", "signature": signature, "flow": {"steps": built}}
    return body, report


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 7 (Decisioning) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def create_business_ruleset(
        name: str,
        signature: DictListParam,
        ctx: Context,
        description: str | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Create a new SAS Business Rules rule set in a SAS Content folder.

        A rule set with no rules cannot be used in a decision flow — follow up
        with ``create_business_rule`` to populate it.

        The rule set is always stored in a folder. Without ``folder`` it goes to
        the deployment's default folder; when there is none, the call is refused
        and you should ask the user which folder to use. The result's ``folder``
        says where it was stored.

        Args:
            name: Rule set name (max 30 chars).
            signature: Input/output/inOut variables the rules operate on, each
                ``{"name", "dataType", "direction"}`` — dataType one of string,
                decimal, integer, date, datetime, dataGrid, boolean, any;
                direction one of input, output, inOut.
            description: Optional description.
            folder: Where to store the rule set: a folder path such as
                ``/Public/Content/Decisions``, ``My Folder`` (or a folder below
                it, ``My Folder/Risk``), a folder ID, or a folder URI
                (``/folders/folders/<id>``). Omit to use the default folder.
        """
        body = {"name": name, "description": description, "signature": signature}
        async with viya_session("create_business_ruleset", ctx) as client:
            target = await resolve_target_folder(client, folder, "rule set")
            result = await post_json(
                "/businessRules/ruleSets", client, body=body, params={"parentFolderUri": target["uri"]}
            )
            result["folder"] = target
            return result

    @mcp.tool()
    async def update_business_ruleset(
        ruleset_id: str,
        name: str,
        signature: DictListParam,
        ctx: Context,
        description: str | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing SAS Business Rules rule set's name/description/signature.

        Changing the signature can invalidate existing rules that reference
        removed variables — check with ``get_business_ruleset`` first if unsure.

        The rule set stays in its folder. One that is in no folder (created
        before folders were required) is filed into ``folder``, or the default
        folder. The result's ``folder`` says where the rule set is.

        Args:
            ruleset_id: The existing rule set UUID (not its name — list_business_rulesets returns both).
            name: Rule set name (max 30 chars).
            signature: Input/output/inOut variables the rules operate on.
            description: Optional description.
            folder: Only used when the rule set is in no folder yet: a folder
                path, ``My Folder``, a folder ID or a folder URI to file it in.
        """
        body = {"name": name, "description": description, "signature": signature}
        uri = f"/businessRules/ruleSets/{ruleset_id}"
        async with viya_session("update_business_ruleset", ctx) as client:
            result = await put_json(uri, client, body)
            result["folder"] = await keep_in_folder(
                client, asset_uri=uri, name=name, content_type=RULE_SET_CONTENT_TYPE,
                folder=folder, what="rule set",
            )
            return result

    @mcp.tool()
    async def get_business_ruleset(ruleset_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch a single SAS Business Rules rule set by ID.

        Args:
            ruleset_id: The rule set UUID (not its name — list_business_rulesets returns both).
        """
        async with viya_session("get_business_ruleset", ctx) as client:
            return await get_json(f"/businessRules/ruleSets/{ruleset_id}", client)

    @mcp.tool()
    async def list_business_rulesets(
        ctx: Context, limit: int = 20, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List SAS Business Rules rule sets, optionally filtered by name substring.

        Args:
            limit: Maximum number of results to return (default 20).
            filter_name: Optional substring to match against rule set names.
        """
        filters = contains_filter(filter_name)
        async with viya_session("list_business_rulesets", ctx) as client:
            items, _ = await get_paged_items("/businessRules/ruleSets", client, limit=limit, filters=filters)
            return return_items(items, ["id", "name", "status"])

    @mcp.tool()
    async def delete_business_ruleset(ruleset_id: str, ctx: Context) -> str:
        """Permanently delete a SAS Business Rules rule set.

        Only call this once the rule set is confirmed unused by any decision
        flow — deleting a rule set still referenced by a decision fails.

        Args:
            ruleset_id: The rule set UUID to delete (not its name — list_business_rulesets returns both).
        """
        async with viya_session("delete_business_ruleset", ctx) as client:
            await delete_resource(f"/businessRules/ruleSets/{ruleset_id}", client)
            return f"Rule set {ruleset_id} deleted."

    @mcp.tool()
    async def lock_business_ruleset_revision(
        ruleset_id: str, ctx: Context, revision_type: str = "minor"
    ) -> dict[str, Any]:
        """Lock the current state of a rule set as an immutable revision.

        Decision steps reference a specific rule set revision (versionId), not
        the live working copy, so a revision must exist before wiring a rule
        set into a decision flow — call again after editing rules if a
        decision needs to pick up the changes.

        Viya locks the revision that was current and opens the next one: the
        result describes that new working revision, as before, and
        ``locked_revision`` the frozen one — use its ``id`` as the versionId
        for a reference that later edits cannot change.

        The revision-creation request replaces the rule set's full content
        from the body sent, so this fetches the rule set with its rules
        included (``application/vnd.sas.business.rule.set.integral+json``)
        and resends them — omitting them would wipe the live rule set's rules,
        not just the new revision.

        Args:
            ruleset_id: The rule set UUID (not its name — list_business_rulesets returns both).
            revision_type: "minor" for iterative changes, "major" for a
                significant/approved milestone (default "minor").
        """
        async with viya_session("lock_business_ruleset_revision", ctx) as client:
            locked = await assets.lock_revision(client, "rule_set", ruleset_id, revision_type)
            return {**locked["working"], "locked_revision": locked["locked_revision"]}

    @mcp.tool()
    async def list_business_ruleset_revisions(
        ruleset_id: str, ctx: Context, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List all locked revisions of a rule set.

        Args:
            ruleset_id: The rule set UUID (not its name — list_business_rulesets returns both).
            limit: Maximum number of results to return (default 20).
        """
        async with viya_session("list_business_ruleset_revisions", ctx) as client:
            items, _ = await get_paged_items(f"/businessRules/ruleSets/{ruleset_id}/revisions", client, limit=limit)
            return return_items(items, ["id", "majorRevision", "minorRevision"])

    @mcp.tool()
    async def create_business_rule(
        ruleset_id: str,
        name: str,
        conditional: str,
        rule_fired_tracking_enabled: bool,
        conditions: DictListParam,
        actions: DictListParam,
        ctx: Context,
    ) -> dict[str, Any]:
        """Create a new rule inside an existing SAS Business Rules rule set.

        A rule set can hold multiple rules, each evaluated per its conditional
        type. Condition/action expressions must include the variable name
        directly (e.g. ``"credit_score < 650"``, not just ``"< 650"``) — the
        API accepts the latter as valid but generates DS2 code with a missing
        left-hand operand. Boolean signature variables must be compared with
        ``= 0``/``= 1`` in expressions, not ``= false``/``= true``.

        Args:
            ruleset_id: The rule set UUID to add the rule to (not its name — list_business_rulesets returns both).
            name: Rule name (max 30 chars).
            conditional: "if" starts a new independent rule chain, "elseif"
                continues the previous rule's chain, "or" ORs into it.
            rule_fired_tracking_enabled: Whether to record when this rule fires.
            conditions: List of conditions (multiple conditions AND together),
                each ``{"type": "complex", "expression", "term": {"name",
                "dataType", "direction"}}``, or ``{"type": "lookup", "term":
                {...}, "lookup": "<lookup table name or ID>"}`` — true when the
                term's value is a key of the lookup table.
            actions: List of actions, each ``{"type": "assignment"|"return",
                "term": {"name", "dataType", "direction"}, "expression"}``, or
                ``{"type": "lookupValue", "term": {...}, "expression": "<key>",
                "lookup": "<lookup table name or ID>"}`` — assigns the value the
                lookup table stores for the key the expression evaluates to.

        A lookup named in a condition or action is resolved to its ID. Rules
        use the lookup table's active version. The result's ``problems`` lists
        any condition or action Viya stored as invalid.
        """
        async with viya_session("create_business_rule", ctx) as client:
            conditions, actions = await lookups.resolve_rule_lookups(client, conditions, actions)
            body = {
                "name": name,
                "conditional": conditional,
                "ruleFiredTrackingEnabled": rule_fired_tracking_enabled,
                "conditions": conditions,
                "actions": actions,
            }
            rule = await post_json(
                f"/businessRules/ruleSets/{ruleset_id}/rules",
                client,
                body=body,
                params={"createVariables": 1},
            )
        problems = lookups.rule_problems(rule)
        if problems:
            rule["problems"] = problems
        return rule

    @mcp.tool()
    async def update_business_rule(
        ruleset_id: str,
        rule_id: str,
        name: str,
        conditional: str,
        rule_fired_tracking_enabled: bool,
        conditions: DictListParam,
        actions: DictListParam,
        ctx: Context,
    ) -> dict[str, Any]:
        """Update an existing rule inside a SAS Business Rules rule set.

        Args:
            ruleset_id: The parent rule set UUID (not its name — list_business_rulesets returns both).
            rule_id: The specific rule UUID to update.
            name: Rule name (max 30 chars).
            conditional: "if" starts a new independent rule chain, "elseif"
                continues the previous rule's chain, "or" ORs into it.
            rule_fired_tracking_enabled: Whether to record when this rule fires.
            conditions: List of conditions (multiple conditions AND together),
                including ``lookup`` conditions (see create_business_rule).
            actions: List of assignment/return/lookupValue actions to perform when matched.
        """
        async with viya_session("update_business_rule", ctx) as client:
            conditions, actions = await lookups.resolve_rule_lookups(client, conditions, actions)
            body = {
                "name": name,
                "conditional": conditional,
                "ruleFiredTrackingEnabled": rule_fired_tracking_enabled,
                "conditions": conditions,
                "actions": actions,
            }
            rule = await put_json(
                f"/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}", client, body
            )
        problems = lookups.rule_problems(rule)
        if problems:
            rule["problems"] = problems
        return rule

    @mcp.tool()
    async def get_business_rule(ruleset_id: str, rule_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch a single rule's definition from a SAS Business Rules rule set.

        Args:
            ruleset_id: The parent rule set UUID (not its name — list_business_rulesets returns both).
            rule_id: The rule UUID.
        """
        async with viya_session("get_business_rule", ctx) as client:
            return await get_json(f"/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}", client)

    @mcp.tool()
    async def list_business_rules(
        ruleset_id: str, ctx: Context, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List all rules inside a SAS Business Rules rule set.

        Args:
            ruleset_id: The rule set UUID (not its name — list_business_rulesets returns both).
            limit: Maximum number of results to return (default 100).
        """
        async with viya_session("list_business_rules", ctx) as client:
            items, _ = await get_paged_items(f"/businessRules/ruleSets/{ruleset_id}/rules", client, limit=limit)
            return return_items(items, ["id", "name", "status"])

    @mcp.tool()
    async def delete_business_rule(ruleset_id: str, rule_id: str, ctx: Context) -> str:
        """Permanently delete a rule from a SAS Business Rules rule set.

        Args:
            ruleset_id: The parent rule set UUID (not its name — list_business_rulesets returns both).
            rule_id: The rule UUID to delete.
        """
        async with viya_session("delete_business_rule", ctx) as client:
            await delete_resource(f"/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}", client)
            return f"Rule {rule_id} deleted from rule set {ruleset_id}."

    @mcp.tool()
    async def create_decision_flow(
        name: str,
        signature: DictListParam,
        ctx: Context,
        steps: DictListParam | None = None,
        rule_set_steps: DictListParam | None = None,
        description: str | None = None,
        folder: str | None = None,
        add_missing_variables: bool = True,
    ) -> dict[str, Any]:
        """Create a SAS Intelligent Decisioning flow: rule sets, segmentation trees, code files,
        models, sub-decisions, assignments, conditions and branches.

        Give the flow as ``steps`` (every step kind, wired by name) or, as
        before, ``rule_set_steps`` (rule sets only, wired by hand). A flow that
        sets defaults, scores a model, branches on its score and checks the
        result::

            steps = [
                {"type": "assignment", "name": "Defaults",
                 "assignments": {"offer": "NONE", "credit_limit": 0}},
                {"type": "model", "id": "<model repository id>"},
                {"type": "branch", "branch_type": "range", "variable": "P_BAD1", "cases": [
                    {"label": "Low risk", "max": 0.1999, "steps": [
                        {"type": "assignment", "assignments": {"offer": "GOLD", "credit_limit": 10000}}]},
                    {"label": "Medium risk", "min": 0.2, "max": 0.4999, "steps": [
                        {"type": "rule_set", "id": "<rule set id>"}]}],
                 "default": [{"type": "assignment", "assignments": {"offer": "DECLINE"}}]},
                {"type": "condition", "expression": "offer = 'GOLD'",
                 "on_true": [{"type": "code_file", "id": "<code file id>"}], "on_false": []},
            ]

        Steps nest: a branch case, a branch default and either side of a
        condition hold steps of any kind, branches and conditions included.

        * **Assignment** — ``assignments`` maps a decision variable to a
          constant, written plainly (``"GOLD"``, ``10000``): the service quotes
          strings itself and evaluates nothing, so ``"a + b"`` is stored as
          that text. Compute values in a rule set or code file. The variable
          must be in ``signature``; ``name`` labels the step.
        * **Model** — ``id`` of a model in the model repository
          (``list_registered_models``, Tier 6). Its inputs map to same-named
          decision variables; its outputs (e.g. ``P_BAD1``, ``I_BAD``) are added
          as decision variables unless ``signature`` has them, a class label
          as a string. ``mappings`` renames: ``{"model variable": "decision variable"}``.
        * **Branch** — ``variable`` (on the branch, or per case) and ``cases``,
          checked in order; the first match runs, else ``default``. ``range``
          cases take ``min`` and/or ``max`` (leave one out for an open end) and
          compare inclusively, ``>= min`` and ``<= max``; cases must not share
          a value, so end the lower case just below the next (``max 0.1999``
          next to ``min 0.2``). ``equals`` cases take one ``value``; ``like``
          cases a ``pattern`` with ``%`` and ``_`` wildcards.
        * **Condition** — a DS2 ``expression`` over decision variables, e.g.
          ``score >= 630 and band = 'A'``; check it with
          validate_decision_expression first.
        * **Rule set, segmentation tree, code file, sub-decision** — ``id``,
          optional ``revision`` and ``mappings``. Segmentation trees, code
          files, value lists and custom functions are created with the Tier 10
          (Decisioning Advanced) tools; list_decisioning_assets finds existing ones.

        The decision is always stored in a SAS Content folder. Without
        ``folder`` it goes to the deployment's default folder; when there is
        none, the call is refused and you should ask the user which folder to
        use. The result's ``folder`` says where it was stored.

        Args:
            name: Decision name (max 60 chars).
            signature: Flow-level input/output variables, each ``{"name",
                "direction", "dataType"}`` — direction input or output;
                dataType string, decimal, integer, date, datetime, boolean.
            steps: The flow, in order — any step kind (see above): ``rule_set``,
                ``segmentation_tree``, ``code_file``, ``decision`` (a sub-decision),
                ``model``, ``assignment``, ``condition`` or ``branch``. ``revision``
                defaults to the newest locked revision (one is locked if there is
                none); "working" uses the editable one. Mappings default to
                same-named decision variables; ``{"step term": "decision variable"}``
                renames. The result's ``steps_report`` lists the revisions used,
                the variables added and any step input nothing maps to.
            rule_set_steps: Ordered list of rule set steps to execute in
                sequence, each ``{"ruleSetId", "versionId", "mappings"}`` —
                versionId is a locked rule set revision (see
                ``lock_business_ruleset_revision``); mappings is a list of
                ``{"stepTermName", "direction", "targetDecisionTermName"}``
                connecting the rule set's terms to this decision's signature.
                A term produced as output by an earlier step can be consumed
                as input by a later step via a shared signature entry.
            description: Optional description.
            folder: Where to store the decision: a folder path such as
                ``/Public/Content/Decisions``, ``My Folder`` (or a folder below
                it, ``My Folder/Risk``), a folder ID, or a folder URI
                (``/folders/folders/<id>``). Omit to use the default folder.
            add_missing_variables: With ``steps``, add a decision variable for every
                step term none matches (default true): Intelligent Decisioning
                validates a step only when all its terms are mapped. The result's
                ``steps_report.variables_added`` lists them.
        """
        async with viya_session("create_decision_flow", ctx) as client:
            if steps is None:
                # rule_set_steps are checked locally, so report a bad step before a missing folder
                body, report = await _flow_body(
                    client, name, signature, steps, rule_set_steps, description, add_missing_variables
                )
                target = await resolve_target_folder(client, folder, "decision")
            else:
                # building steps can lock revisions, so only once the folder is known to be usable
                target = await resolve_target_folder(client, folder, "decision")
                body, report = await _flow_body(
                    client, name, signature, steps, rule_set_steps, description, add_missing_variables
                )
            result = await post_json(
                "/decisions/flows", client, body=body, params={"parentFolderUri": target["uri"]}
            )
            result["folder"] = target
            if report is not None:
                result["steps_report"] = report
            return result

    @mcp.tool()
    async def update_decision_flow(
        decision_id: str,
        name: str,
        signature: DictListParam,
        ctx: Context,
        steps: DictListParam | None = None,
        rule_set_steps: DictListParam | None = None,
        description: str | None = None,
        folder: str | None = None,
        add_missing_variables: bool = True,
    ) -> dict[str, Any]:
        """Update an existing SAS Intelligent Decisioning flow.

        Pass ALL steps (existing + new), as ``steps`` or ``rule_set_steps`` —
        the full flow is replaced on update, it is not a partial patch.

        The decision stays in its folder. One that is in no folder (created
        before folders were required) is filed into ``folder``, or the default
        folder. The result's ``folder`` says where the decision is.

        Args:
            decision_id: The existing decision flow UUID (not its name — list_decision_flows returns both).
            name: Decision name (max 60 chars).
            signature: Flow-level input/output variables.
            steps: The whole flow, every step kind — assignments, models,
                branches, conditions, rule sets, trees, code files, sub-decisions;
                ``create_decision_flow`` describes each, with an example.
            rule_set_steps: Ordered list of rule set steps (see
                ``create_decision_flow`` for the shape).
            description: Optional description.
            folder: Only used when the decision is in no folder yet: a folder
                path, ``My Folder``, a folder ID or a folder URI to file it in.
            add_missing_variables: With ``steps``, add a decision variable for every
                step term none matches (default true; see ``create_decision_flow``).
        """
        uri = f"/decisions/flows/{decision_id}"
        async with viya_session("update_decision_flow", ctx) as client:
            body, report = await _flow_body(
                client, name, signature, steps, rule_set_steps, description, add_missing_variables
            )
            result = await put_json(uri, client, body)
            if report is not None:
                result["steps_report"] = report
            result["folder"] = await keep_in_folder(
                client, asset_uri=uri, name=name, content_type=DECISION_CONTENT_TYPE,
                folder=folder, what="decision",
            )
            return result

    @mcp.tool()
    async def get_decision_flow(decision_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch the current state of a SAS Intelligent Decisioning flow.

        Args:
            decision_id: The decision flow UUID (not its name — list_decision_flows returns both).
        """
        async with viya_session("get_decision_flow", ctx) as client:
            return await get_json(f"/decisions/flows/{decision_id}", client)

    @mcp.tool()
    async def list_decision_flows(
        ctx: Context, limit: int = 20, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List SAS Intelligent Decisioning flows, optionally filtered by name substring.

        Args:
            limit: Maximum number of results to return (default 20).
            filter_name: Optional substring to match against decision names.
        """
        filters = contains_filter(filter_name)
        async with viya_session("list_decision_flows", ctx) as client:
            items, _ = await get_paged_items("/decisions/flows", client, limit=limit, filters=filters)
            return return_items(items, ["id", "name", "majorRevision"])

    @mcp.tool()
    async def delete_decision_flow(decision_id: str, ctx: Context) -> str:
        """Permanently delete a SAS Intelligent Decisioning flow.

        Args:
            decision_id: The decision flow UUID to delete (not its name — list_decision_flows returns both).
        """
        async with viya_session("delete_decision_flow", ctx) as client:
            await delete_resource(f"/decisions/flows/{decision_id}", client)
            return f"Decision {decision_id} deleted."

    @mcp.tool()
    async def get_decision_flow_code(decision_id: str, ctx: Context) -> str:
        """Retrieve the generated DS2 execution code for a decision flow.

        Args:
            decision_id: The decision flow UUID (not its name — list_decision_flows returns both).
        """
        async with viya_session("get_decision_flow_code", ctx) as client:
            resp = await client.get(
                f"{VIYA_ENDPOINT}/decisions/flows/{decision_id}/code",
                headers={"Accept": "text/vnd.sas.source.ds2"},
            )
            raise_for_viya_status(resp)
            return resp.text

    @mcp.tool()
    async def lock_decision_flow_revision(decision_id: str, ctx: Context) -> dict[str, Any]:
        """Lock the current state of a decision flow as an immutable revision.

        Call after a successful create/update to freeze the approved state as
        a point-in-time snapshot referenceable by ``publish_decision_flow``.
        Viya locks the revision that was current and opens the next one: the
        result describes that new working revision, as before, and
        ``locked_revision`` the frozen one.

        Args:
            decision_id: The decision flow UUID (not its name — list_decision_flows returns both).
        """
        async with viya_session("lock_decision_flow_revision", ctx) as client:
            locked = await assets.lock_revision(client, "decision", decision_id)
            return {**locked["working"], "locked_revision": locked["locked_revision"]}

    @mcp.tool()
    async def list_decision_flow_revisions(
        decision_id: str, ctx: Context, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List all locked revisions of a decision flow.

        Args:
            decision_id: The decision flow UUID (not its name — list_decision_flows returns both).
            limit: Maximum number of results to return (default 20).
        """
        async with viya_session("list_decision_flow_revisions", ctx) as client:
            items, _ = await get_paged_items(f"/decisions/flows/{decision_id}/revisions", client, limit=limit)
            return return_items(items, ["id", "creationTimeStamp"])

    @mcp.tool()
    async def get_decision_flow_revision(decision_id: str, revision_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch the content of a specific locked decision revision.

        Args:
            decision_id: The decision flow UUID (not its name — list_decision_flows returns both).
            revision_id: The revision UUID.
        """
        async with viya_session("get_decision_flow_revision", ctx) as client:
            return await get_json(f"/decisions/flows/{decision_id}/revisions/{revision_id}", client)

    @mcp.tool()
    async def publish_decision_flow(
        decision_id: str,
        revision_id: str,
        publish_name: str,
        ctx: Context,
        destination_name: str | None = None,
        poll_timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Publish a locked decision revision to a Micro Analytic Score (MAS) destination.

        Required before ``score_data`` can execute the decision — MAS runs
        published modules, not decision flows directly. Requires the DS2 code
        generation service to be healthy for this decision's rule sets; an
        error mentioning rule set code generation is an environment-level
        issue, not a bad payload.

        Publishing is asynchronous and the resulting MAS module ID is
        server-generated — it is NOT ``publish_name``. This polls the
        publish job (``properties.masModules[0].jobUri``) until it reaches a
        terminal state and returns the real ``moduleId`` alongside the
        publish record, so the result is directly usable with
        ``get_mas_module_step_signature``/``score_data`` without a separate
        lookup via ``list_mas_modules``.

        The code is generated for MAS, as SAS Intelligent Decisioning does when
        it publishes, so lookup tables are read from MAS. Unlike a module the
        Intelligent Decisioning UI publishes, whose inputs end in an underscore
        (``credit_score_``), the module's inputs keep the decision's variable
        names.

        Args:
            decision_id: The decision flow UUID (not its name — list_decision_flows returns both).
            revision_id: The locked revision UUID (see
                ``lock_decision_flow_revision``).
            publish_name: The published name shown in Model Publish (not the
                MAS module ID — see above).
            destination_name: A Micro Analytic Score publishing destination.
                Default: the deployment's ``MCP_DEFAULT_PUBLISH_DESTINATION``,
                ``maslocal`` unless an administrator changed it. The code is
                generated for MAS, so the destination must be a MAS one.
            poll_timeout: Max seconds to wait for the publish job to reach a
                terminal state before giving up (default 60.0).
        """
        async with viya_session("publish_decision_flow", ctx) as client:
            # Code for the default target (CAS) reads lookup tables as CAS formats,
            # which MAS refuses ("Unrecognized format").
            code_resp = await client.get(
                f"{VIYA_ENDPOINT}/decisions/flows/{decision_id}/revisions/{revision_id}/code",
                headers={"Accept": "text/vnd.sas.source.ds2"},
                params={"codeTarget": "microAnalyticService"},
            )
            raise_for_viya_status(code_resp)
            code, _ = plain_input_names(code_resp.text)
            body = {
                "name": publish_name,
                "destinationName": destination_name or config.MCP_DEFAULT_PUBLISH_DESTINATION,
                "modelContents": [
                    {
                        "modelName": publish_name,
                        "code": code,
                        "codeType": "ds2",
                        "sourceUri": f"/decisions/flows/{decision_id}/revisions/{revision_id}",
                    }
                ],
            }
            resp = await client.post(
                f"{VIYA_ENDPOINT}/modelPublish/models",
                content=json.dumps(body).encode(),
                headers={"Content-Type": "application/vnd.sas.models.publishing.request+json"},
            )
            raise_for_viya_status(resp)
            data = resp.json()
            items = data.get("items", [])
            published = items[0] if items else data
            model_id = published.get("id")

            poll_interval = 1.0
            elapsed = 0.0

            # Phase 1: resolve the async publish job's URI. It is often absent
            # from the initial POST response (properties is {} for a moment),
            # so re-fetch the model until masModules[0].jobUri appears. Check
            # what's already in hand before sleeping so poll_timeout=0 still
            # returns any immediately-available result. Re-fetching needs the
            # publish record's id; if the POST response carried none, stop here
            # and return a pending record rather than GET .../models/None (404).
            job_uri = ""
            while True:
                mas_modules = (published.get("properties") or {}).get("masModules") or []
                job_uri = mas_modules[0].get("jobUri", "") if mas_modules else ""
                if job_uri or model_id is None or elapsed >= poll_timeout:
                    break
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                model_resp = await client.get(f"{VIYA_ENDPOINT}/modelPublish/models/{model_id}")
                raise_for_viya_status(model_resp)
                published = model_resp.json()

            if not job_uri:
                published["moduleState"] = "pending"
                return published

            # Phase 2: poll the job to a terminal state. Query once before
            # checking the deadline so this phase always runs at least once,
            # even if phase 1 consumed the whole budget.
            terminal_states = ("completed", "failed", "error", "canceled", "cancelled")
            while True:
                job_resp = await client.get(f"{VIYA_ENDPOINT}{job_uri}")
                raise_for_viya_status(job_resp)
                job = job_resp.json()
                state = job.get("state")
                if state in terminal_states:
                    published["moduleId"] = job.get("moduleId")
                    published["moduleState"] = state
                    if job.get("errors"):
                        published["moduleErrors"] = job["errors"]
                    return published
                if elapsed >= poll_timeout:
                    break
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            published["moduleState"] = "pending"
            return published

    @mcp.tool()
    async def validate_decision_expression(
        expression: str, ctx: Context, signature: DictListParam | None = None
    ) -> dict[str, Any]:
        """Check that a DS2 expression compiles: a rule condition or action, a branch or a condition step.

        The expression may use the variables in ``signature``, DS2 functions
        and custom functions. Returns ``valid`` and the compiler's error and
        warning lines. A variable the signature does not declare is a warning
        (DS2 creates it), not an error.

        Args:
            expression: The expression, e.g. ``risk_band(credit_score) = 'LOW'``.
            signature: The variables it uses, ``[{"name", "dataType", "direction"}]``.
        """
        async with viya_session("validate_decision_expression", ctx) as client:
            return await assets.validate_expression(client, expression, signature)

    register_lifecycle_tools(mcp, viya_session)
    register_lookup_tools(mcp, viya_session)
