# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 7 — Decisioning tools (SAS Business Rules & Intelligent Decisioning)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from ..config import VIYA_ENDPOINT
from ..viya_client import contains_filter, delete_resource, get_json, get_paged_items, post_json, put_json, return_items
from ._common import coerce_json_list, make_session_helpers

# Tolerant alias for list params some MCP clients deliver as JSON-encoded
# strings (see _common.coerce_json_list). The published schema is unchanged.
DictListParam = Annotated[list[dict[str, Any]], BeforeValidator(coerce_json_list)]


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


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 7 (Decisioning) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def create_business_ruleset(
        name: str, signature: DictListParam, ctx: Context, description: str | None = None
    ) -> dict[str, Any]:
        """Create a new SAS Business Rules rule set.

        A rule set with no rules cannot be used in a decision flow — follow up
        with ``create_business_rule`` to populate it.

        Args:
            name: Rule set name (max 30 chars).
            signature: Input/output/inOut variables the rules operate on, each
                ``{"name", "dataType", "direction"}`` — dataType one of string,
                decimal, integer, date, datetime, dataGrid, boolean, any;
                direction one of input, output, inOut.
            description: Optional description.
        """
        body = {"name": name, "description": description, "signature": signature}
        async with viya_session("create_business_ruleset", ctx) as client:
            return await post_json("/businessRules/ruleSets", client, body=body)

    @mcp.tool()
    async def update_business_ruleset(
        ruleset_id: str, name: str, signature: DictListParam, ctx: Context, description: str | None = None
    ) -> dict[str, Any]:
        """Update an existing SAS Business Rules rule set's name/description/signature.

        Changing the signature can invalidate existing rules that reference
        removed variables — check with ``get_business_ruleset`` first if unsure.

        Args:
            ruleset_id: The existing rule set UUID.
            name: Rule set name (max 30 chars).
            signature: Input/output/inOut variables the rules operate on.
            description: Optional description.
        """
        body = {"name": name, "description": description, "signature": signature}
        async with viya_session("update_business_ruleset", ctx) as client:
            return await put_json(f"/businessRules/ruleSets/{ruleset_id}", client, body)

    @mcp.tool()
    async def get_business_ruleset(ruleset_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch a single SAS Business Rules rule set by ID.

        Args:
            ruleset_id: The rule set UUID.
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
            ruleset_id: The rule set UUID to delete.
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

        The revision-creation request replaces the rule set's full content
        from the body sent, so this fetches the rule set with its rules
        included (``application/vnd.sas.business.rule.set.integral+json``)
        and resends them — omitting them would wipe the live rule set's rules,
        not just the new revision.

        Args:
            ruleset_id: The rule set UUID.
            revision_type: "minor" for iterative changes, "major" for a
                significant/approved milestone (default "minor").
        """
        integral_type = "application/vnd.sas.business.rule.set.integral+json"
        async with viya_session("lock_business_ruleset_revision", ctx) as client:
            get_resp = await client.get(
                f"{VIYA_ENDPOINT}/businessRules/ruleSets/{ruleset_id}",
                headers={"Accept": integral_type},
            )
            get_resp.raise_for_status()
            current = get_resp.json()
            body = {
                "name": current["name"],
                "description": current.get("description"),
                "signature": current.get("signature"),
                "rules": current.get("rules"),
            }
            resp = await client.post(
                f"{VIYA_ENDPOINT}/businessRules/ruleSets/{ruleset_id}/revisions",
                params={"revisionType": revision_type},
                content=json.dumps(body).encode(),
                headers={"Content-Type": integral_type},
            )
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def list_business_ruleset_revisions(
        ruleset_id: str, ctx: Context, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List all locked revisions of a rule set.

        Args:
            ruleset_id: The rule set UUID.
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
            ruleset_id: The rule set UUID to add the rule to.
            name: Rule name (max 30 chars).
            conditional: "if" starts a new independent rule chain, "elseif"
                continues the previous rule's chain, "or" ORs into it.
            rule_fired_tracking_enabled: Whether to record when this rule fires.
            conditions: List of conditions (multiple conditions AND together),
                each ``{"type": "complex", "expression", "term": {"name",
                "dataType", "direction"}}``.
            actions: List of actions, each ``{"type": "assignment"|"return",
                "term": {"name", "dataType", "direction"}, "expression"}``.
        """
        body = {
            "name": name,
            "conditional": conditional,
            "ruleFiredTrackingEnabled": rule_fired_tracking_enabled,
            "conditions": conditions,
            "actions": actions,
        }
        async with viya_session("create_business_rule", ctx) as client:
            return await post_json(
                f"/businessRules/ruleSets/{ruleset_id}/rules",
                client,
                body=body,
                params={"createVariables": 1},
            )

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
            ruleset_id: The parent rule set UUID.
            rule_id: The specific rule UUID to update.
            name: Rule name (max 30 chars).
            conditional: "if" starts a new independent rule chain, "elseif"
                continues the previous rule's chain, "or" ORs into it.
            rule_fired_tracking_enabled: Whether to record when this rule fires.
            conditions: List of conditions (multiple conditions AND together).
            actions: List of assignment/return actions to perform when matched.
        """
        body = {
            "name": name,
            "conditional": conditional,
            "ruleFiredTrackingEnabled": rule_fired_tracking_enabled,
            "conditions": conditions,
            "actions": actions,
        }
        async with viya_session("update_business_rule", ctx) as client:
            return await put_json(
                f"/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}", client, body
            )

    @mcp.tool()
    async def get_business_rule(ruleset_id: str, rule_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch a single rule's definition from a SAS Business Rules rule set.

        Args:
            ruleset_id: The parent rule set UUID.
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
            ruleset_id: The rule set UUID.
            limit: Maximum number of results to return (default 100).
        """
        async with viya_session("list_business_rules", ctx) as client:
            items, _ = await get_paged_items(f"/businessRules/ruleSets/{ruleset_id}/rules", client, limit=limit)
            return return_items(items, ["id", "name", "status"])

    @mcp.tool()
    async def delete_business_rule(ruleset_id: str, rule_id: str, ctx: Context) -> str:
        """Permanently delete a rule from a SAS Business Rules rule set.

        Args:
            ruleset_id: The parent rule set UUID.
            rule_id: The rule UUID to delete.
        """
        async with viya_session("delete_business_rule", ctx) as client:
            await delete_resource(f"/businessRules/ruleSets/{ruleset_id}/rules/{rule_id}", client)
            return f"Rule {rule_id} deleted from rule set {ruleset_id}."

    @mcp.tool()
    async def create_decision_flow(
        name: str,
        signature: DictListParam,
        rule_set_steps: DictListParam,
        ctx: Context,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a new SAS Intelligent Decisioning flow chaining rule set steps.

        Args:
            name: Decision name (max 60 chars).
            signature: Flow-level input/output variables, each ``{"name",
                "direction", "dataType"}`` — direction input or output;
                dataType string, decimal, integer, date, datetime, boolean.
            rule_set_steps: Ordered list of rule set steps to execute in
                sequence, each ``{"ruleSetId", "versionId", "mappings"}`` —
                versionId is a locked rule set revision (see
                ``lock_business_ruleset_revision``); mappings is a list of
                ``{"stepTermName", "direction", "targetDecisionTermName"}``
                connecting the rule set's terms to this decision's signature.
                A term produced as output by an earlier step can be consumed
                as input by a later step via a shared signature entry.
            description: Optional description.
        """
        body = _build_decision_flow_body(name, signature, rule_set_steps, description)
        async with viya_session("create_decision_flow", ctx) as client:
            return await post_json("/decisions/flows", client, body=body)

    @mcp.tool()
    async def update_decision_flow(
        decision_id: str,
        name: str,
        signature: DictListParam,
        rule_set_steps: DictListParam,
        ctx: Context,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing SAS Intelligent Decisioning flow.

        Pass ALL rule set steps (existing + new) — the full flow is replaced
        on update, it is not a partial patch.

        Args:
            decision_id: The existing decision flow UUID.
            name: Decision name (max 60 chars).
            signature: Flow-level input/output variables.
            rule_set_steps: Ordered list of rule set steps (see
                ``create_decision_flow`` for the shape).
            description: Optional description.
        """
        body = _build_decision_flow_body(name, signature, rule_set_steps, description)
        async with viya_session("update_decision_flow", ctx) as client:
            return await put_json(f"/decisions/flows/{decision_id}", client, body)

    @mcp.tool()
    async def get_decision_flow(decision_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch the current state of a SAS Intelligent Decisioning flow.

        Args:
            decision_id: The decision flow UUID.
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
            decision_id: The decision flow UUID to delete.
        """
        async with viya_session("delete_decision_flow", ctx) as client:
            await delete_resource(f"/decisions/flows/{decision_id}", client)
            return f"Decision {decision_id} deleted."

    @mcp.tool()
    async def get_decision_flow_code(decision_id: str, ctx: Context) -> str:
        """Retrieve the generated DS2 execution code for a decision flow.

        Args:
            decision_id: The decision flow UUID.
        """
        async with viya_session("get_decision_flow_code", ctx) as client:
            resp = await client.get(
                f"{VIYA_ENDPOINT}/decisions/flows/{decision_id}/code",
                headers={"Accept": "text/vnd.sas.source.ds2"},
            )
            resp.raise_for_status()
            return resp.text

    @mcp.tool()
    async def lock_decision_flow_revision(decision_id: str, ctx: Context) -> dict[str, Any]:
        """Lock the current state of a decision flow as an immutable revision.

        Call after a successful create/update to freeze the approved state as
        a point-in-time snapshot referenceable by ``publish_decision_flow``.

        Args:
            decision_id: The decision flow UUID.
        """
        async with viya_session("lock_decision_flow_revision", ctx) as client:
            current = await get_json(f"/decisions/flows/{decision_id}", client)
            body = {
                "name": current["name"],
                "description": current.get("description"),
                "signature": current.get("signature"),
                "flow": current.get("flow"),
            }
            return await post_json(f"/decisions/flows/{decision_id}/revisions", client, body=body)

    @mcp.tool()
    async def list_decision_flow_revisions(
        decision_id: str, ctx: Context, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List all locked revisions of a decision flow.

        Args:
            decision_id: The decision flow UUID.
            limit: Maximum number of results to return (default 20).
        """
        async with viya_session("list_decision_flow_revisions", ctx) as client:
            items, _ = await get_paged_items(f"/decisions/flows/{decision_id}/revisions", client, limit=limit)
            return return_items(items, ["id", "creationTimeStamp"])

    @mcp.tool()
    async def get_decision_flow_revision(decision_id: str, revision_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch the content of a specific locked decision revision.

        Args:
            decision_id: The decision flow UUID.
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
        destination_name: str = "maslocal",
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

        Args:
            decision_id: The decision flow UUID.
            revision_id: The locked revision UUID (see
                ``lock_decision_flow_revision``).
            publish_name: The published name shown in Model Publish (not the
                MAS module ID — see above).
            destination_name: The configured MAS publishing destination
                (default "maslocal").
            poll_timeout: Max seconds to wait for the publish job to reach a
                terminal state before giving up (default 60.0).
        """
        async with viya_session("publish_decision_flow", ctx) as client:
            code_resp = await client.get(
                f"{VIYA_ENDPOINT}/decisions/flows/{decision_id}/revisions/{revision_id}/code",
                headers={"Accept": "text/vnd.sas.source.ds2"},
            )
            code_resp.raise_for_status()
            body = {
                "name": publish_name,
                "destinationName": destination_name,
                "modelContents": [
                    {
                        "modelName": publish_name,
                        "code": code_resp.text,
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
            resp.raise_for_status()
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
                model_resp.raise_for_status()
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
                job_resp.raise_for_status()
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
