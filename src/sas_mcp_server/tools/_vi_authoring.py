# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AML scenario authoring/tuning tools.

Read and (gated) update AML detection-scenario parameters in ``SCENARIO_PARAM``
(the scenario configuration: scenario_nm x scenario_param_nm x scenario_param_value,
e.g. ``look_back_days``, ``alert_score``, ``agg_sec_amt``, ``num_branches``).

Tuning = change a scenario parameter value. Writes go to the Public caslib source
(sashdat) deterministically via fixed SAS. ``set_scenario_parameter`` defaults to
dry_run (preview) and is approval-gated.

NOTE: taking effect in the live detection engine depends on the AML Alert
Generation Process / MAS reloading the config (on this demo the MAS scenario
service was unavailable). The config edit itself is the deterministic tuning action.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import bound_text, get_json, logger, make_client
from ..viya_utils import run_one_snippet
from ._vi_links import (
    routing_rule_url,
    scenario_note,
    scenario_table_url,
    strategy_url,
)

# svi-alert triage content types (validated against the live API).
_RULE_CT = "application/vnd.sas.investigation.triage.routing.rule+json"
_EXPR_CT = "application/vnd.sas.investigation.triage.logic.expression+json"

_LOAD = ('cas s; caslib _all_ assign;\n'
         'proc casutil; load casdata="SCENARIO_PARAM.sashdat" incaslib="Public" '
         'casout="_sp" outcaslib="CASUSER" replace; quit;\n')


def _sq(v: str) -> str:
    """SAS single-quoted literal (suppresses macro resolution; escapes quotes)."""
    return "'" + str(v).replace("'", "''") + "'"


def _approval_redirect(action: str, target: str) -> dict[str, Any]:
    """Refuse a direct legacy SCENARIO_PARAM write and route to the approval flow.

    All parameter/scenario changes must go through the VSD-flow approval workflow
    so a human approves in Visual Investigator before anything reaches production.
    These legacy tools write the SCENARIO_PARAM sashdat (no UI, no approval gate),
    so direct application (dry_run=False) is disabled.
    """
    return {
        "blocked": True,
        "reason": (f"Direct application is disabled ({action}: {target}). "
                   "All parameter/scenario changes must go through the approval workflow."),
        "use_instead": [
            "tune_flow_scenario(flow_id, scenario_name, param_name, new_value)  # Adjust within a VSD flow (visible in "
            "the Flows UI)",
            "submit_flow_change_for_approval(flow_id, change_summary)  # Put the change on VI's approval queue",
            "(Approver reviews and approves in VI) → check_flow_approval → deploy_flow to push to production",
        ],
        "note": "This legacy SCENARIO_PARAM write does not appear in the product UI and has no approval gate — do not "
                "use directly.",
    }


def register_aml_authoring(mcp: FastMCP, get_token) -> None:
    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    async def _approval_status(approval_id: str, ctx: Context) -> tuple[bool, str | None]:
        """Return (approved?, status) for a VI approval inquiry (approved = status != OPN)."""
        token = await get_token(ctx)
        async with make_client(token) as client:
            data: Any = await get_json("/svi-datahub/documents/tm_inquiry?limit=200", client)
            items = data if isinstance(data, list) else data.get("items", [])
            doc = next((d for d in items if str(d.get("id")) == str(approval_id)), None)
            if doc is None:
                return (False, "not_found")
            st = doc.get("fieldValues", {}).get("status")
            return (st is not None and st != "OPN", st)

    async def _run_fixed_sas(sas: str, ctx: Context) -> dict[str, Any]:
        token = await get_token(ctx)
        res = await run_one_snippet(sas, "1", token)
        listing = (res.get("listing") or "").strip()
        # Return the listing whenever there is one. A benign WARNING (e.g.
        # OUTOBS= truncation) sets state="warning", but the query results are
        # still valid — gating on state=="completed" here would hide them behind
        # the raw SAS log. Only fall back to the log when there is no listing.
        if listing and listing != "(no listing output)":
            return {"state": res.get("state"), "output": bound_text(listing)}
        return {"state": res.get("state"), "output": bound_text(res.get("log", ""))}

    @mcp.tool()
    async def list_scenario_parameters(ctx: Context, scenario_name: str | None = None,
                                       limit: int = 60) -> dict[str, Any]:
        """List AML detection-scenario parameters (SCENARIO_PARAM). Read-only.

        Use to see tunable parameters and current values before tuning.

        Args:
            scenario_name: Optional exact scenario name to filter (e.g.
                'Excessive Account Monitoring').
            limit: Max rows (default 60).
        """
        where = "where scenario_param_nm ne '' "
        if scenario_name:
            where += f"and scenario_nm={_sq(scenario_name)} "
        sas = (_LOAD +
               f'title "scenario parameters";\n'
               f'proc sql outobs={int(limit)}; select distinct scenario_nm, scenario_param_nm, '
               f'scenario_param_value, data_type_cd from CASUSER._sp {where} '
               f'order by scenario_nm, scenario_param_nm; quit;\n'
               'title; cas s terminate;')
        return await _run_fixed_sas(sas, ctx)

    @mcp.tool()
    async def set_scenario_parameter(scenario_name: str, param_name: str, new_value: str,
                                     ctx: Context, dry_run: bool = True,
                                     approval_id: str | None = None) -> dict[str, Any]:
        """Tune an AML detection scenario parameter (SCENARIO_PARAM). APPROVAL-GATED WRITE.

        For scenarios NOT in a VSD flow (many classic AML scenarios like 'Cash
        Accumulation', 'CTR Party Large Cash Transaction'). Prefer ``tune_flow_scenario``
        for VSD-flow scenarios (those show in the Flows UI).

        dry_run=True previews the current value. **Applying (dry_run=False)
        REQUIRES an approved ``approval_id``**: first call
        ``submit_flow_change_for_approval`` to put the change on VI's approval queue,
        have a human approve it in VI, then pass that inquiry id here. Without an
        approved id the change is refused — the agent cannot self-approve.

        Args:
            scenario_name: Exact scenario name (from ``list_scenario_parameters``).
            param_name: Parameter name (e.g. 'alert_score', 'look_back_days').
            new_value: New value (string; numeric values are stored as text).
            dry_run: Keep True to preview (read current). False applies (needs approval_id).
            approval_id: Approved VI inquiry id (from ``submit_flow_change_for_approval``
                after a human approved it). Required to actually apply.
        """
        sc, pn, nv = _sq(scenario_name), _sq(param_name), _sq(new_value)
        cond = f"scenario_nm={sc} and scenario_param_nm={pn}"
        if dry_run:
            sas = (_LOAD +
                   f'title "current value (preview)";\n'
                   f'proc print data=CASUSER._sp(where=({cond})) noobs;\n'
                   '  var scenario_nm scenario_param_nm scenario_param_value; run;\n'
                   'title; cas s terminate;')
            res = await _run_fixed_sas(sas, ctx)
            return {"dry_run": True, "scenario_name": scenario_name,
                    "param_name": param_name, "proposed_value": new_value,
                    "current": res.get("output"),
                    "note": "Approval required to apply. Use: submit_flow_change_for_approval → get approval → pass "
                            "the approval_id here.",
                    **scenario_table_url(scenario_name)}
        # Approval gate: applying requires an APPROVED VI inquiry id. The agent
        # cannot self-approve — a human must approve in VI first.
        if not approval_id:
            return _approval_redirect("scenario parameter change", f"{scenario_name}.{param_name}")
        approved, status = await _approval_status(approval_id, ctx)
        if not approved:
            return {"blocked": True, "approval_id": approval_id, "approval_status": status,
                    "reason": (f"This change is not yet approved (status={status}). "
                               "Have an approver approve it in VI, then retry."),
                    "scenario_name": scenario_name, "param_name": param_name}
        sas = (_LOAD +
               f'data CASUSER._sp; set CASUSER._sp;\n'
               f'  if {cond} then scenario_param_value={nv};\nrun;\n'
               'proc casutil; save casdata="_sp" incaslib="CASUSER" '
               'casout="SCENARIO_PARAM" outcaslib="Public" replace; quit;\n'
               f'title "applied value";\n'
               f'proc print data=CASUSER._sp(where=({cond})) noobs;\n'
               '  var scenario_nm scenario_param_nm scenario_param_value; run;\n'
               'title; cas s terminate;')
        res = await _run_fixed_sas(sas, ctx)
        return {"applied": True, "scenario_name": scenario_name, "param_name": param_name,
                "new_value": new_value, "result": res.get("output"),
                **scenario_table_url(scenario_name), "note": scenario_note()}

    @mcp.tool()
    async def create_scenario(new_scenario_name: str, base_scenario_name: str, ctx: Context,
                              description: str = "", dry_run: bool = True) -> dict[str, Any]:
        """Create a NEW detection scenario by cloning an existing one's flow + parameters. GATED WRITE.

        The new scenario reuses the base scenario's flow (detection template) and
        parameter set, with fresh identifiers and ``active_flg=0`` (INACTIVE). Tune
        its parameters afterward with ``set_scenario_parameter``, then promote with
        ``activate_scenario`` (behind production approval). Brand-new detection
        LOGIC (a new flow) is NOT created here — that is the product Scenario
        Administrator's domain.

        Defaults to dry_run (reports how many parameter rows would be cloned).

        Args:
            new_scenario_name: Name for the new scenario (must not already exist).
            base_scenario_name: Existing scenario to clone the flow/params from.
            description: Optional description for the new scenario.
            dry_run: Keep True to preview. False creates it (INACTIVE), requires approval.
        """
        nn, bn, ds = _sq(new_scenario_name), _sq(base_scenario_name), _sq(description)
        if dry_run:
            sas = (_LOAD +
                   f'proc sql; title "create preview";\n'
                   f'select (select count(*) from CASUSER._sp where scenario_nm={bn}) as base_param_rows,\n'
                   f'       (select count(*) from CASUSER._sp where scenario_nm={nn}) as name_already_exists;\n'
                   'quit; title; cas s terminate;')
            res = await _run_fixed_sas(sas, ctx)
            return {"dry_run": True, "new_scenario_name": new_scenario_name,
                    "base_scenario_name": base_scenario_name, "preview": res.get("output"),
                    "note": "Verify base_param_rows>0 and name_already_exists=0, then rerun with dry_run=False after "
                            "approval.",
                    **scenario_table_url(new_scenario_name)}
        # Direct application disabled — create real flows via create_flow_from_template
        # (VSD) and route through the approval workflow.
        return _approval_redirect("scenario creation", new_scenario_name)
        sas = (_LOAD +  # unreachable — legacy path kept for reference
               f'data _new; set CASUSER._sp(where=(scenario_nm={bn}));\n'
               '  length _sk $ 144; retain _sk;\n'
               '  if _n_=1 then _sk=uuidgen();\n'
               f'  scenario_sk=_sk; scenario_nm={nn}; scenario_desc={ds}; active_flg=0;\n'
               '  scenario_param_sk=uuidgen(); drop _sk;\nrun;\n'
               'data CASUSER._sp; set CASUSER._sp _new; run;\n'
               'proc casutil; save casdata="_sp" incaslib="CASUSER" '
               'casout="SCENARIO_PARAM" outcaslib="Public" replace; quit;\n'
               f'title "created (INACTIVE)"; proc sql; select count(*) as param_rows, '
               f'max(active_flg) as active_flg from CASUSER._sp where scenario_nm={nn}; quit; title;\n'
               'cas s terminate;')
        res = await _run_fixed_sas(sas, ctx)
        return {"created": True, "new_scenario_name": new_scenario_name,
                "base_scenario_name": base_scenario_name, "active": 0,
                "result": res.get("output"),
                **scenario_table_url(new_scenario_name), "note": scenario_note()}

    @mcp.tool()
    async def activate_scenario(scenario_name: str, ctx: Context, active: bool = True,
                                dry_run: bool = True) -> dict[str, Any]:
        """Activate (production) or deactivate a detection scenario (active_flg). GATED WRITE.

        This is the production promotion switch — gate it behind an explicit
        production approval (ask the user in the UI before dry_run=False). Live
        effect depends on the detection engine (AGP/MAS) reloading the config.

        Args:
            scenario_name: Scenario to (de)activate.
            active: True → active_flg=1 (production); False → 0 (INACTIVE).
            dry_run: Keep True to preview current state. False applies, requires approval.
        """
        nm = _sq(scenario_name)
        flag = 1 if active else 0
        if dry_run:
            sas = (_LOAD +
                   f'title "current"; proc sql; select distinct scenario_nm, active_flg '
                   f'from CASUSER._sp where scenario_nm={nm}; quit; title; cas s terminate;')
            res = await _run_fixed_sas(sas, ctx)
            return {"dry_run": True, "scenario_name": scenario_name,
                    "proposed_active_flg": flag, "current": res.get("output"),
                    "note": "Production promotion requires approval. After approval, rerun with dry_run=False.",
                    **scenario_table_url(scenario_name)}
        # Direct activation disabled — production go-live goes through deploy_flow
        # after VI approval (submit_flow_change_for_approval → check_flow_approval).
        return _approval_redirect("scenario activation", scenario_name)
        sas = (_LOAD +  # unreachable — legacy path kept for reference
               f'data CASUSER._sp; set CASUSER._sp;\n'
               f'  if scenario_nm={nm} then active_flg={flag};\nrun;\n'
               'proc casutil; save casdata="_sp" incaslib="CASUSER" '
               'casout="SCENARIO_PARAM" outcaslib="Public" replace; quit;\n'
               f'title "result"; proc sql; select distinct scenario_nm, active_flg '
               f'from CASUSER._sp where scenario_nm={nm}; quit; title; cas s terminate;')
        res = await _run_fixed_sas(sas, ctx)
        return {"applied": True, "scenario_name": scenario_name, "active_flg": flag,
                "result": res.get("output"),
                **scenario_table_url(scenario_name), "note": scenario_note()}

    def _dry_run_preview(method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "dry_run": True,
            "would_send": {"method": method, "url": f"{VIYA_ENDPOINT}{path}", "body": body},
            "note": "Dry-run preview before approval. To execute, rerun with dry_run=False after approval. Do not "
                    "write to the shared demo environment.",
        }

    @mcp.tool()
    async def create_strategy(domain_id: str, name: str, ctx: Context,
                              description: str = "", score_range_low: int = 0,
                              score_range_high: int = 100,
                              dry_run: bool = True) -> dict[str, Any]:
        """Author a VI triage strategy (groups scenarios for alert display). GATED WRITE.

        Defaults to dry_run: returns the request it WOULD POST without creating
        anything. New strategies are created INACTIVE (never auto-activated).

        Args:
            domain_id: Target domain (from ``list_domains``).
            name: Strategy name.
            description: Strategy description.
            score_range_low / score_range_high: Alert score range.
            dry_run: Keep True to preview only. False executes the POST (requires approval).
        """
        body = {"strategyName": name, "strategyDescription": description,
                "domainId": domain_id, "scoreRangeLow": score_range_low,
                "scoreRangeHigh": score_range_high, "strategyStatus": "INACTIVE"}
        path = "/svi-alert/strategies"
        if dry_run:
            return _dry_run_preview("POST", path, body)
        async with viya_session("create_strategy", ctx) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}{path}", json=body,
                headers={"Content-Type": "application/vnd.sas.investigation.triage.strategy+json",
                         "Accept": "application/json"})
            resp.raise_for_status()
            created = resp.json()
            sid = created.get("id") or created.get("strategyId")
            return {**created, **strategy_url(sid)}

    # ------------------------------------------------------------------
    # Alert-routing RULES (svi-alert triage layer). This is the product
    # "Rules API" (create → add conditions → transition to production):
    #   - a routing rule (1:1 with a work queue) routes matching alerts to
    #     that queue; its status code ACTIVE/INACTIVE is the lifecycle.
    #   - logic expressions are its conditions (property + operator + value),
    #     combined by AND (disjunctionFlag=false) or OR (true).
    # Contract validated live: POST /svi-alert/routingRules,
    # POST /svi-alert/logicExpressions, PUT (full echo body) to transition.
    # NOTE: at most ONE routing rule per queue — target a queue that has none
    # (see ``list_alert_queues(only_without_rule=True)``).
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_alert_queues(ctx: Context, only_without_rule: bool = False,
                                limit: int = 100) -> dict[str, Any]:
        """List VI alert work queues (and which already have a routing rule). Read-only.

        A routing rule is 1:1 with a queue, so a NEW rule needs a queue that has
        none. Use ``only_without_rule=True`` to list just the assignable queues.

        Args:
            only_without_rule: True → only queues without a routing rule yet.
            limit: Max queues (default 100).
        """
        async with viya_session("list_alert_queues", ctx) as client:
            queues = await get_json(f"/svi-alert/queues?limit={int(limit)}", client)
            rules = await get_json(f"/svi-alert/routingRules?limit={int(limit)}", client)
            used = {r.get("queueId") for r in rules.get("items", [])}
            out = []
            for q in queues.get("items", []):
                qid = q.get("queueId")
                has_rule = qid in used
                if only_without_rule and has_rule:
                    continue
                out.append({"queueId": qid, "queueName": q.get("queueName"),
                            "domainId": q.get("domainId"), "strategyId": q.get("strategyId"),
                            "has_routing_rule": has_rule})
            return {"count": len(out), "queues": out}

    @mcp.tool()
    async def list_routing_rules(ctx: Context, limit: int = 100) -> dict[str, Any]:
        """List alert-routing rules and their conditions (logic expressions). Read-only.

        Args:
            limit: Max rules (default 100).
        """
        async with viya_session("list_routing_rules", ctx) as client:
            rules = await get_json(f"/svi-alert/routingRules?limit={int(limit)}", client)
            exprs = await get_json(f"/svi-alert/logicExpressions?limit={int(limit)}", client)
            by_rule: dict[str, list[dict[str, Any]]] = {}
            for e in exprs.get("items", []):
                by_rule.setdefault(e.get("routingRuleId"), []).append(
                    {"id": e.get("logicExpressionId"), "property": e.get("propertyName"),
                     "operator": e.get("conditionCode"), "value": e.get("valueText")})
            out = []
            for r in rules.get("items", []):
                rid = r.get("routingRuleId")
                out.append({"routingRuleId": rid, "queueId": r.get("queueId"),
                            "status": r.get("routingRuleStatusCode"),
                            "logic": "OR" if r.get("disjunctionFlag") else "AND",
                            "conditions": by_rule.get(rid, [])})
            return {"count": len(out), "rules": out}

    @mcp.tool()
    async def create_routing_rule(queue_id: str, conditions: list[dict[str, Any]],
                                  ctx: Context, match_any: bool = False,
                                  activate: bool = False,
                                  dry_run: bool = True) -> dict[str, Any]:
        """Create an alert-routing RULE (+ its conditions) on the svi-alert triage layer. GATED WRITE.

        This is the product "Create a Rule" action. Creates a routing rule bound to
        ``queue_id`` and one or more conditions (logic expressions). Defaults to
        INACTIVE — promote later with ``transition_routing_rule`` (behind approval).

        A queue can host only ONE routing rule; pick a free queue via
        ``list_alert_queues(only_without_rule=True)``.

        Args:
            queue_id: Target work queue (must have no routing rule yet).
            conditions: List of ``{property_name, operator, value}`` — e.g.
                ``[{"property_name": "currentScore", "operator": "GTE", "value": "950"}]``.
                operator in GT/GTE/LT/LTE (equality/others as supported by the field).
            match_any: False → ALL conditions must hold (AND); True → ANY (OR).
            activate: True creates it ACTIVE (production). Default False = INACTIVE.
            dry_run: Keep True to preview. False creates it (requires approval).
        """
        status = "ACTIVE" if activate else "INACTIVE"
        rule_body = {"queueId": queue_id, "disjunctionFlag": bool(match_any),
                     "routingRuleStatusCode": status, "userCreatedRoutingRuleFlag": True}
        norm = [{"propertyName": c.get("property_name") or c.get("propertyName"),
                 "conditionCode": c.get("operator") or c.get("conditionCode"),
                 "valueText": str(c.get("value") if c.get("value") is not None
                                  else c.get("valueText"))}
                for c in conditions]
        if dry_run:
            return {"dry_run": True, "would_create": {"routingRule": rule_body,
                    "conditions": norm}, "queue_id": queue_id,
                    "note": "After approval, rerun with dry_run=False. Default is INACTIVE (promote with "
                            "transition_routing_rule). Be careful writing to the shared demo environment."}
        async with viya_session("create_routing_rule", ctx) as client:
            resp = await client.post(f"{VIYA_ENDPOINT}/svi-alert/routingRules",
                                     json=rule_body,
                                     headers={"Content-Type": _RULE_CT, "Accept": "application/json"})
            resp.raise_for_status()
            rule = resp.json()
            rid = rule.get("routingRuleId")
            made = []
            for cb in norm:
                cr = await client.post(f"{VIYA_ENDPOINT}/svi-alert/logicExpressions",
                                       json={"routingRuleId": rid, **cb},
                                       headers={"Content-Type": _EXPR_CT, "Accept": "application/json"})
                cr.raise_for_status()
                made.append({"id": cr.json().get("logicExpressionId"), **cb})
            return {"created": True, "routing_rule_id": rid, "queue_id": queue_id,
                    "status": status, "logic": "OR" if match_any else "AND",
                    "conditions": made, **routing_rule_url(rid)}

    @mcp.tool()
    async def transition_routing_rule(routing_rule_id: str, ctx: Context,
                                      active: bool = True,
                                      dry_run: bool = True) -> dict[str, Any]:
        """Transition a routing rule to production (ACTIVE) or back to INACTIVE. GATED WRITE.

        This is the product "Transition a Rule" action — the production switch.
        Gate ACTIVE behind explicit approval (ask in the UI before dry_run=False).

        Args:
            routing_rule_id: Rule to transition (from ``list_routing_rules``).
            active: True → ACTIVE (production); False → INACTIVE.
            dry_run: Keep True to preview current→proposed. False applies (requires approval).
        """
        target = "ACTIVE" if active else "INACTIVE"
        async with viya_session("transition_routing_rule", ctx) as client:
            cur = await get_json(f"/svi-alert/routingRules/{routing_rule_id}", client)
            if dry_run:
                return {"dry_run": True, "routing_rule_id": routing_rule_id,
                        "current_status": cur.get("routingRuleStatusCode"),
                        "proposed_status": target,
                        "note": "Promoting to ACTIVE requires approval. After approval, rerun with dry_run=False.",
                        **routing_rule_url(routing_rule_id)}
            # PUT requires the full object echoed back (incl. modifiedTimeStamp).
            body = {k: v for k, v in cur.items() if k != "links"}
            body["routingRuleStatusCode"] = target
            put = await client.put(f"{VIYA_ENDPOINT}/svi-alert/routingRules/{routing_rule_id}",
                                   json=body,
                                   headers={"Content-Type": _RULE_CT, "Accept": "application/json"})
            put.raise_for_status()
            return {"applied": True, "routing_rule_id": routing_rule_id, "status": target,
                    **routing_rule_url(routing_rule_id)}

    @mcp.tool()
    async def delete_routing_rule(routing_rule_id: str, ctx: Context,
                                  dry_run: bool = True) -> dict[str, Any]:
        """Delete a routing rule and its conditions (logic expressions). GATED WRITE.

        Args:
            routing_rule_id: Rule to delete.
            dry_run: Keep True to preview what would be deleted. False deletes (requires approval).
        """
        async with viya_session("delete_routing_rule", ctx) as client:
            exprs = await get_json("/svi-alert/logicExpressions?limit=200", client)
            mine = [e.get("logicExpressionId") for e in exprs.get("items", [])
                    if e.get("routingRuleId") == routing_rule_id]
            if dry_run:
                return {"dry_run": True, "routing_rule_id": routing_rule_id,
                        "would_delete_conditions": mine,
                        "note": "After approval, rerun with dry_run=False to delete."}
            for eid in mine:
                dr = await client.delete(f"{VIYA_ENDPOINT}/svi-alert/logicExpressions/{eid}")
                dr.raise_for_status()
            rr = await client.delete(f"{VIYA_ENDPOINT}/svi-alert/routingRules/{routing_rule_id}")
            rr.raise_for_status()
            return {"deleted": True, "routing_rule_id": routing_rule_id,
                    "deleted_conditions": mine}
