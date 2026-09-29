# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AML detection-FLOW authoring (Financial Crimes Visual Scenario Designer).

This is the REAL detection-authoring layer — the ``svi-vsd-service`` that backs
the Visual Investigator **Flows** admin page. Unlike ``SCENARIO_PARAM`` (a CAS
config copy that does not surface in the product UI), a flow created here:

  * appears in the Flows admin UI (``admin.html#/admin-scenario-alerts/flows``),
  * is editable by investigators in that UI (native product object), and
  * can be **deployed to production** via ``POST /svi-vsd-service/deployments/``.

A flow = ``{name, description, alertDomainId, solutionName, analyticServerType,
dataSources[], scenarios[]}``. Each scenario carries its detection LOGIC as a
SAS DATA step (``dataStepCode``) plus ``scenarioParameters[]`` (tunable
thresholds). Brand-new logic is therefore authored by editing ``dataStepCode`` /
parameters on a scenario.

Contract validated by live writes on a SAS Viya environment:
  * ``POST /svi-vsd-service/flows/`` — both Content-Type AND Accept must be
    ``application/vnd.sas.fcs.vsd.flow+json`` (else 406). A minimal body 500s;
    the reliable path is clone-an-existing-flow with server-assigned identifiers
    stripped (``id``/``guid``/``flowId``/``*TimeStamp``/``version``/… removed →
    the service regenerates them). Verified: a cloned flow appears in the UI.
  * Deployment object = ``{flowGuid, flowRep, status:PUBLISHED, type, notes}``.

All writes default to ``dry_run=True`` and are approval-gated. Do not deploy on
the shared demo without approval.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import get_json, logger, make_client
from ._vi_links import approval_item_url, flow_url

_FLOW_CT = "application/vnd.sas.fcs.vsd.flow+json"
_DEPLOY_CT = "application/vnd.sas.fcs.vsd.deployment+json"
# VI approval items are datahub "documents" — POST /svi-datahub/documents
# (base path, application/json). Validated live via the VI "New Inquiry" UI.
_INQUIRY_TYPE = "tm_inquiry"
# VI rejects Inquiry.Description over 300 chars (DH5108) — truncate before POST.
_INQUIRY_DESC_MAX = 300

# Server-assigned fields to strip when cloning a flow into a new one, so the
# service regenerates them instead of colliding with the source.
_STRIP = {"id", "guid", "flowId", "dataSourceId", "scenarioId",
          "createdBy", "modifiedBy", "creationTimeStamp", "modifiedTimeStamp",
          "version", "lastUpdateNumber", "links", "deployed", "publishCode"}


def _strip_ids(obj: Any) -> Any:
    """Recursively drop server-assigned identifiers/timestamps for a clone."""
    if isinstance(obj, dict):
        return {k: _strip_ids(v) for k, v in obj.items() if k not in _STRIP}
    if isinstance(obj, list):
        return [_strip_ids(x) for x in obj]
    return obj


def _parse_ts(s: str | None) -> datetime | None:
    """Parse a Viya ISO-8601 UTC timestamp (``...Z``); None if absent/invalid."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _flow_param_map(flow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{scenario_name: {param_name: value}} from a flow (or a deployed flowRep)."""
    out: dict[str, dict[str, Any]] = {}
    for s in flow.get("scenarios", []):
        params: dict[str, Any] = {}
        for p in s.get("scenarioParameters", []):
            vals = p.get("values")
            params[p.get("name")] = vals[0].get("value") if vals else p.get("value")
        out[s.get("name")] = params
    return out


# Cap on per-deployment detail fetches when resolving a flow's latest deploy.
_DEP_SCAN_MAX = 50

# Guardrail: numeric parameter changes beyond this % (vs the PRODUCTION value)
# require explicit acknowledgement. Catches scale-mixing incidents like citing a
# classic-layer 31,500 while actually raising a VSD param 10000→40000 (+300%).
_LARGE_CHANGE_PCT = 50.0


def _pct_change(old: Any, new: Any) -> float | None:
    """Signed % change old→new; None when either side is non-numeric or old==0."""
    try:
        o = float(str(old).replace(",", ""))
        n = float(str(new).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    return round((n - o) / abs(o) * 100.0, 1)


def _diff_vs_production(draft_flow: dict[str, Any],
                        dep: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Param-level diff: draft flow definition vs deployed flowRep snapshot.

    ``dep`` is a deployment DETAIL (carries ``flowRep``); None → everything in
    the draft counts as undeployed (never-published flow).
    """
    prod = _flow_param_map((dep or {}).get("flowRep") or {})
    out: list[dict[str, Any]] = []
    for scen, params in _flow_param_map(draft_flow).items():
        for pname, dval in params.items():
            pval = prod.get(scen, {}).get(pname)
            if str(dval) != str(pval):
                out.append({"scenario": scen, "parameter": pname,
                            "production_value": pval, "draft_value": dval,
                            "change_pct": _pct_change(pval, dval)})
    return out


async def _latest_deployment(client: httpx.AsyncClient,
                             flow_guid: str) -> dict[str, Any] | None:
    """Latest deployment DETAIL for a flow guid, or None if never deployed.

    Production truth on this box: the flow definition actually RUNNING is the
    ``flowRep`` snapshot inside the latest PUBLISHED deployment — the Flows UI
    shows the current (possibly draft-edited) definition, and the flow's
    ``deployed`` flag only means "has been deployed at least once".

    Contract quirks (live-validated 2026-07-07): the deployments collection
    IGNORES ``filter=eq(flowGuid,…)`` (returns everything), its summary items
    do not carry ``flowGuid``, ``Accept-Item`` → 404, and ``sortBy`` is
    unreliable once the ``:`` gets URL-encoded (falls back to ascending). Only
    reliable mapping = fetch the summaries, sort client-side newest-first, then
    fetch details one by one until ``flowGuid`` matches. Recent deploys resolve
    in 1-2 detail calls.
    """
    data = await get_json(f"/svi-vsd-service/deployments/?limit={_DEP_SCAN_MAX}",
                          client)
    items = sorted(data.get("items", []),
                   key=lambda d: d.get("publishedDate") or "", reverse=True)
    for item in items:
        det = await get_json(f"/svi-vsd-service/deployments/{item.get('id')}",
                             client, accept=_DEPLOY_CT)
        if det.get("flowGuid") == flow_guid:
            return det
    return None


def register_aml_flows(mcp: FastMCP, get_token) -> None:
    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    @mcp.tool()
    async def list_vsd_flows(ctx: Context, limit: int = 50) -> dict[str, Any]:
        """List detection FLOWS from the Visual Scenario Designer (svi-vsd-service). Read-only.

        These are the flows shown in the VI Flows admin UI. Each groups the
        detection scenarios that generate alerts for a domain.

        NOTE: the ``deployed`` flag only means "deployed at least once" — it does
        NOT guarantee the current definition is what production runs (draft edits
        stay UI-visible but inactive until deploy_flow). For the accurate
        production state of one flow use ``get_vsd_flow`` (production_status /
        diff_vs_production) or ``list_flow_deployments``.

        Args:
            limit: Max flows (default 50).
        """
        async with viya_session("list_vsd_flows", ctx) as client:
            data = await get_json(f"/svi-vsd-service/flows/?start=0&limit={int(limit)}", client)
            out = [{"id": f.get("id"), "name": f.get("name"),
                    "alertDomainId": f.get("alertDomainId"),
                    "solutionName": f.get("solutionName"),
                    "deployed": f.get("deployed"),
                    "description": f.get("description")}
                   for f in data.get("items", [])]
            return {"count": data.get("count"), "flows": out}

    @mcp.tool()
    async def get_vsd_flow(flow_id: str, ctx: Context,
                           include_code: bool = False) -> dict[str, Any]:
        """Get a detection flow's full structure AND its production-deploy state. Read-only.

        Returns each scenario's name, type, parameters and (optionally) its SAS
        ``dataStepCode`` detection logic — the basis for tuning or authoring.

        Also resolves the ambiguity the Flows UI hides: the UI shows the CURRENT
        definition (which may be a draft edit), while production runs the latest
        PUBLISHED deployment snapshot. Output includes ``production_status``,
        ``last_deployment``, and — when the draft differs — ``diff_vs_production``
        per parameter.

        Args:
            flow_id: Flow id (from ``list_vsd_flows``).
            include_code: True → include each scenario's ``dataStepCode`` (can be large).
        """
        async with viya_session("get_vsd_flow", ctx) as client:
            f = await get_json(f"/svi-vsd-service/flows/{flow_id}", client)
            scenarios = []
            for s in f.get("scenarios", []):
                item = {"name": s.get("name"), "description": s.get("description"),
                        "scenarioType": s.get("scenarioType"), "pathType": s.get("pathType"),
                        "activeFlg": s.get("activeFlg"),
                        # Value lives at values[0].value (same path tune_flow_scenario
                        # writes); top-level "value" is always null on this box.
                        "parameters": [{"name": p.get("name"),
                                        "value": ((p.get("values") or [{}])[0].get("value")
                                                  if p.get("values") else p.get("value"))}
                                       for p in s.get("scenarioParameters", [])]}
                if include_code:
                    item["dataStepCode"] = s.get("dataStepCode")
                scenarios.append(item)

            # --- Production-deploy state (the "is what I see actually live?" answer) ---
            last_dep: dict[str, Any] | None = None
            undeployed_changes: bool | None = None
            diff: list[dict[str, Any]] = []
            prod_status = "Unknown (could not retrieve deployment state)"
            try:
                dep = await _latest_deployment(client, flow_id)
                if dep is None:
                    undeployed_changes = True
                    prod_status = "Not deployed (not live in production — this definition has never run in production)"
                else:
                    last_dep = {"deployment_id": dep.get("id"),
                                "publishedDate": dep.get("publishedDate"),
                                "status": dep.get("status")}
                    mod = _parse_ts(f.get("modifiedTimeStamp"))
                    pub = _parse_ts(dep.get("publishedDate"))
                    undeployed_changes = bool(mod and pub and mod > pub)
                    if undeployed_changes:
                        prod_status = ("Live in production but [undeployed edits exist] — "
                                       "the visible definition differs from what production runs (use deploy_flow to "
                                       "apply)")
                        diff = _diff_vs_production(f, dep)
                    else:
                        prod_status = "In sync — the visible definition matches what production runs"
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("deployment-state check failed for %s: %s", flow_id, exc)

            return {"id": f.get("id"), "name": f.get("name"),
                    "alertDomainId": f.get("alertDomainId"),
                    "solutionName": f.get("solutionName"),
                    "deployed": f.get("deployed"),
                    "production_status": prod_status,
                    "undeployed_changes": undeployed_changes,
                    "last_deployment": last_dep,
                    "diff_vs_production": diff or None,
                    "flow_modifiedTimeStamp": f.get("modifiedTimeStamp"),
                    "dataSources": [{"name": d.get("name"), "tableName": d.get("tableName")}
                                    for d in f.get("dataSources", [])],
                    "scenarios": scenarios}

    @mcp.tool()
    async def list_flow_deployments(ctx: Context, flow_id: str | None = None,
                                    limit: int = 10) -> dict[str, Any]:
        """List production DEPLOYMENTS (svi-vsd-service). Read-only — the production truth.

        Each PUBLISHED deployment is a point-in-time snapshot (``flowRep``) of a
        flow that went live. The latest one per flow IS what production runs —
        regardless of any draft edits visible on the Flows admin page.

        Args:
            flow_id: Optional flow guid (= ``list_vsd_flows`` id) to filter to one flow.
            limit: Max records (default 10, newest first).
        """
        async with viya_session("list_flow_deployments", ctx) as client:
            # Server-side filter/sortBy are unreliable here (see _latest_deployment)
            # — fetch summaries and sort client-side, newest first.
            data = await get_json(
                f"/svi-vsd-service/deployments/?limit={_DEP_SCAN_MAX}", client)
            summaries = sorted(data.get("items", []),
                               key=lambda d: d.get("publishedDate") or "",
                               reverse=True)
            items: list[dict[str, Any]] = []
            for d in summaries:
                rec = {"deployment_id": d.get("id"),
                       "publishedDate": d.get("publishedDate"),
                       "status": d.get("status"),
                       "createdBy": d.get("createdBy")}
                if flow_id:
                    # flowGuid only lives in the detail — fetch to filter.
                    det = await get_json(
                        f"/svi-vsd-service/deployments/{d.get('id')}",
                        client, accept=_DEPLOY_CT)
                    if det.get("flowGuid") != flow_id:
                        continue
                    rec["flowGuid"] = det.get("flowGuid")
                    rec["flow_name"] = (det.get("flowRep") or {}).get("name")
                items.append(rec)
                if len(items) >= int(limit):
                    break
            return {"total_deployments": data.get("count"), "flow_id": flow_id,
                    "deployments": items,
                    "note": "The latest PUBLISHED deployment is what production runs. Check diff against the flow "
                            "definition via get_vsd_flow's diff_vs_production."}

    @mcp.tool()
    async def create_flow_from_template(base_flow_id: str, new_name: str, ctx: Context,
                                        description: str = "",
                                        alert_domain_id: str | None = None,
                                        dry_run: bool = True) -> dict[str, Any]:
        """Create a NEW detection flow by cloning an existing one (VSD). GATED WRITE.

        Clones ``base_flow_id`` (its data sources + scenarios + detection logic)
        into a new flow with fresh identifiers, ``origin=NEW`` and NOT deployed.
        The new flow appears in the Flows admin UI immediately and is editable by
        investigators there; tune its scenarios/parameters, then ``deploy_flow``
        to promote it to production (behind approval).

        A brand-new flow body is rejected (500) by the service, so cloning a close
        template is the supported authoring path; edit scenario logic afterward.

        Args:
            base_flow_id: Existing flow to clone (from ``list_vsd_flows``).
            new_name: Name for the new flow.
            description: Optional description.
            alert_domain_id: Optional domain override (default: same as base).
            dry_run: Keep True to preview. False creates it (requires approval).
        """
        async with viya_session("create_flow_from_template", ctx) as client:
            base = await get_json(f"/svi-vsd-service/flows/{base_flow_id}", client)
            body = _strip_ids(base)
            body["name"] = new_name
            body["description"] = description or f"Cloned from {base.get('name')}"
            body["origin"] = "NEW"
            body["activeFlag"] = True
            if alert_domain_id:
                body["alertDomainId"] = alert_domain_id
            for s in body.get("scenarios", []):
                s["origin"] = "NEW"
            if dry_run:
                return {"dry_run": True, "new_name": new_name,
                        "base_flow": base.get("name"),
                        "would_clone": {"scenarios": len(body.get("scenarios", [])),
                                        "dataSources": len(body.get("dataSources", [])),
                                        "alertDomainId": body.get("alertDomainId")},
                        "note": "After approval, rerun with dry_run=False. The new flow is undeployed and editable in "
                                "the UI. Promote to production with deploy_flow. Be careful writing to the shared demo "
                                "environment.",
                        **flow_url(new_name)}
            resp = await client.post(f"{VIYA_ENDPOINT}/svi-vsd-service/flows/",
                                     content=_dumps(body),
                                     headers={"Content-Type": _FLOW_CT, "Accept": _FLOW_CT})
            resp.raise_for_status()
            created = resp.json()
            result = {"created": True, "flow_id": created.get("id"),
                      "flow_guid": created.get("guid"), "name": created.get("name"),
                      "alertDomainId": created.get("alertDomainId"),
                      "deployed": created.get("deployed", False),
                      "scenarios": len(created.get("scenarios", [])),
                      **flow_url(new_name, created.get("id"))}
            if created.get("id"):
                # Lineage: the new flow depends on the template it was cloned from.
                # Wrapped in try/except — lineage.py is wired separately in mcp_server.py.
                try:
                    from ..lineage import flow_uri as _flow_uri
                    from ..lineage import record_failsoft
                    await record_failsoft(
                        client, result,
                        [(_flow_uri(created["id"]), _flow_uri(base_flow_id))],
                    )
                except ImportError:
                    pass
            return result

    @mcp.tool()
    async def deploy_flow(flow_id: str, ctx: Context, notes: str = "",
                          dry_run: bool = True,
                          approval_id: str | None = None) -> dict[str, Any]:
        """Deploy a detection flow to PRODUCTION (publish via svi-vsd-service). GATED WRITE.

        Publishes the flow so its scenarios run in the live detection process
        (``POST /svi-vsd-service/deployments/`` → status PUBLISHED). This is the
        production switch — gate behind explicit approval (ask in the UI before
        dry_run=False). Live firing still depends on the detection engine running.

        GOVERNANCE: pass the ``approval_id`` from
        ``submit_flow_change_for_approval`` — the deploy is REFUSED while that
        inquiry is still open (status OPN), and the id is stamped into the
        deployment notes for traceability. After publishing, the deployed
        snapshot is read back and verified against the submitted definition
        (closed loop) — see ``post_deploy_verification`` in the result.

        Args:
            flow_id: Flow to deploy (from ``list_vsd_flows``).
            notes: Optional deployment note.
            dry_run: Keep True to preview the request. False deploys (requires approval).
            approval_id: Inquiry id of the approved change request. Strongly
                recommended — enforces approved-before-deploy and audit linkage.
        """
        async with viya_session("deploy_flow", ctx) as client:
            flow = await get_json(f"/svi-vsd-service/flows/{flow_id}", client)
            # --- Guardrail: refuse while the linked approval is still open ---
            approval_status: str | None = None
            if approval_id:
                data: Any = await get_json(
                    f"/svi-datahub/documents/{_INQUIRY_TYPE}?limit=200", client)
                items = data if isinstance(data, list) else data.get("items", [])
                doc = next((d for d in items
                            if str(d.get("id")) == str(approval_id)), None)
                if doc is None:
                    return {"error": "approval item not found",
                            "approval_id": approval_id, "flow_id": flow_id}
                approval_status = doc.get("fieldValues", {}).get("status")
                if not dry_run and approval_status == "OPN":
                    return {"error": "approval_pending",
                            "approval_id": approval_id,
                            "approval_status": approval_status,
                            "note": "The approval inquiry is still OPN (pending). Wait for an approver to approve it "
                                    "in VI before deploying.",
                            **approval_item_url(_INQUIRY_TYPE, str(approval_id))}
            dep_notes = notes or f"Deploy {flow.get('name')}"
            if approval_id:
                dep_notes = f"{dep_notes} [approval:{approval_id}]"
            # Contract validated live: type="FLOW" (not "PUBLISH"); flowRep = the
            # full flow object; media type = ...fcs.vsd.deployment+json. Server
            # sets status=PUBLISHED and flips the flow's deployed flag to true.
            body = {"flowGuid": flow.get("guid"), "flowRep": flow,
                    "type": "FLOW", "notes": dep_notes}
            path = "/svi-vsd-service/deployments/"
            if dry_run:
                return {"dry_run": True, "flow_id": flow_id, "flow_name": flow.get("name"),
                        "approval_id": approval_id, "approval_status": approval_status,
                        "would_post": {"method": "POST", "url": f"{VIYA_ENDPOINT}{path}",
                                       "flowGuid": flow.get("guid"), "type": "FLOW",
                                       "notes": dep_notes},
                        "note": "Production deploy requires approval. After approval, rerun with dry_run=False. Live "
                                "firing depends on the detection engine running.",
                        **flow_url(flow.get("name"), flow_id)}
            resp = await client.post(f"{VIYA_ENDPOINT}{path}", content=_dumps(body),
                                     headers={"Content-Type": _DEPLOY_CT,
                                              "Accept": _DEPLOY_CT})
            resp.raise_for_status()
            dep = resp.json()
            # --- Closed loop: read back what production now runs and verify it
            # matches the definition we just submitted.
            verification: dict[str, Any]
            try:
                det = await get_json(f"/svi-vsd-service/deployments/{dep.get('id')}",
                                     client, accept=_DEPLOY_CT)
                mismatches = _diff_vs_production(flow, det)
                verification = {"verified": not mismatches,
                                "publishedDate": det.get("publishedDate"),
                                "mismatches": mismatches or None}
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("post-deploy verification failed: %s", exc)
                verification = {"verified": None,
                                "note": "Post-deploy read-back verification failed — check with get_vsd_flow"}
            result = {"deployed": True, "flow_id": flow_id, "flow_name": flow.get("name"),
                      "deployment_id": dep.get("id"), "status": dep.get("status"),
                      "approval_id": approval_id,
                      "post_deploy_verification": verification,
                      **flow_url(flow.get("name"), flow_id)}
            if dep.get("id"):
                # Lineage: the production deployment depends on its flow.
                # Wrapped in try/except — lineage.py is wired separately in mcp_server.py.
                try:
                    from ..lineage import deployment_uri, record_failsoft
                    from ..lineage import flow_uri as _flow_uri
                    await record_failsoft(
                        client, result,
                        [(deployment_uri(dep["id"]), _flow_uri(flow_id))],
                    )
                except ImportError:
                    pass
            return result

    @mcp.tool()
    async def tune_flow_scenario(flow_id: str, scenario_name: str, param_name: str,
                                 new_value: str, ctx: Context,
                                 dry_run: bool = True,
                                 acknowledge_large_change: bool = False) -> dict[str, Any]:
        """Tune a parameter of a scenario inside a VSD detection flow. GATED WRITE.

        Edits the flow's live definition (``svi-vsd-service``), so the change is
        visible in the Flows admin UI and editable by investigators — unlike the
        legacy ``set_scenario_parameter`` which writes the SCENARIO_PARAM sashdat
        (a config copy that never surfaces in the product UI). After tuning, run
        ``deploy_flow`` to push it to production.

        LARGE-CHANGE GUARD: the change % is computed against the PRODUCTION value
        (latest deployed snapshot — the number that actually matters), not just
        the draft. A numeric change beyond ±50% is REFUSED unless
        ``acknowledge_large_change=True``, which you may only set after the user
        explicitly confirmed the magnitude in chat. This catches scale-mixing
        (e.g. citing a classic-layer 31,500 while raising a VSD param
        10000→40000 = +300%).

        Contract validated live: the value lives at
        ``scenarioParameters[name].values[0].value`` (a string); the whole flow is
        PUT back to ``/svi-vsd-service/flows/{id}``.

        Args:
            flow_id: Flow id (from ``list_vsd_flows``).
            scenario_name: Exact scenario name inside the flow (from ``get_vsd_flow``).
            param_name: Exact parameter name (e.g. 'scenario_score', 'look_back_days').
            new_value: New value (stored as text).
            dry_run: Keep True to preview current→proposed. False applies (requires approval).
            acknowledge_large_change: Set True ONLY after the user explicitly
                confirmed a >±50% change (state the production value and % in chat).
        """
        async with viya_session("tune_flow_scenario", ctx) as client:
            flow = await get_json(f"/svi-vsd-service/flows/{flow_id}", client)
            scenarios = flow.get("scenarios", [])
            scen = next((s for s in scenarios if s.get("name") == scenario_name), None)
            if scen is None:
                return {"error": "scenario not found in flow", "flow_id": flow_id,
                        "requested": scenario_name,
                        "available_scenarios": [s.get("name") for s in scenarios]}
            params = scen.get("scenarioParameters", [])
            param = next((p for p in params if p.get("name") == param_name), None)
            if param is None:
                return {"error": "parameter not found in scenario",
                        "scenario_name": scenario_name, "requested": param_name,
                        "available_parameters": [p.get("name") for p in params]}
            values = param.get("values") or []
            current = values[0].get("value") if values else None

            # --- Large-change guard: baseline = production value when available ---
            prod_value: Any = None
            baseline_src = "draft"
            try:
                dep = await _latest_deployment(client, flow_id)
                if dep is not None:
                    prod_value = _flow_param_map(dep.get("flowRep") or {}) \
                        .get(scenario_name, {}).get(param_name)
                    if prod_value is not None:
                        baseline_src = "production"
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("production lookup failed for %s: %s", flow_id, exc)
            baseline = prod_value if prod_value is not None else current
            change_pct = _pct_change(baseline, new_value)
            large = change_pct is not None and abs(change_pct) > _LARGE_CHANGE_PCT
            guard = {"production_value": prod_value, "baseline": baseline,
                     "baseline_source": baseline_src, "change_pct": change_pct,
                     "large_change": large}

            if dry_run:
                out = {"dry_run": True, "flow_id": flow_id, "flow_name": flow.get("name"),
                       "scenario_name": scenario_name, "param_name": param_name,
                       "current_value": current, "proposed_value": str(new_value),
                       **guard,
                       "note": "After approval, rerun with dry_run=False. Change is visible in the Flows UI. Promote "
                               "to production with deploy_flow.",
                       **flow_url(flow.get("name"), flow_id)}
                if large:
                    out["warning"] = (f"Large change: baseline ({baseline_src})={baseline} → "
                                      f"{new_value} ({change_pct:+.1f}%). "
                                      "Explicit user confirmation + acknowledge_large_change=True required to apply.")
                return out
            if large and not acknowledge_large_change:
                return {"error": "large_change_guard",
                        "flow_id": flow_id, "scenario_name": scenario_name,
                        "param_name": param_name, "proposed_value": str(new_value),
                        **guard,
                        "note": (f"Change magnitude exceeds ±{_LARGE_CHANGE_PCT:.0f}% "
                                 f"({baseline_src} baseline: {baseline}→{new_value}, "
                                 f"{change_pct:+.1f}%). This was refused because a change of "
                                 "this scale may be an error (layer/scale mix-up). Present the "
                                 "production value and % to the user, get explicit confirmation, "
                                 "then rerun with acknowledge_large_change=True.")}
            if not values:
                return {"error": "parameter has no value slot to set",
                        "scenario_name": scenario_name, "param_name": param_name}
            values[0]["value"] = str(new_value)
            resp = await client.put(f"{VIYA_ENDPOINT}/svi-vsd-service/flows/{flow_id}",
                                    content=_dumps(flow),
                                    headers={"Content-Type": _FLOW_CT, "Accept": _FLOW_CT})
            resp.raise_for_status()
            return {"applied": True, "flow_id": flow_id, "flow_name": flow.get("name"),
                    "scenario_name": scenario_name, "param_name": param_name,
                    "old_value": current, "new_value": str(new_value),
                    **guard,
                    "note": "Promote to production with deploy_flow (approval required).",
                    **flow_url(flow.get("name"), flow_id)}

    @mcp.tool()
    async def submit_flow_change_for_approval(change_summary: str, ctx: Context,
                                              flow_id: str | None = None,
                                              dry_run: bool = True,
                                              validation_summary: str | None = None,
                                              skip_validation: bool = False) -> dict[str, Any]:
        """Submit ANY detection change to Visual Investigator's approval queue. GATED WRITE.

        Instead of the agent applying/deploying directly, this creates a VI
        **inquiry** (a datahub document in the tm_inquiry workflow) describing the
        proposed change. It lands in the reviewer's queue in VI; a human reviews
        and approves there. Use for BOTH:
          * VSD-flow changes (pass ``flow_id``) → after approval, ``deploy_flow``.
          * SCENARIO_PARAM parameter tuning (no ``flow_id``) → after approval, pass
            the returned inquiry id as ``approval_id`` to ``set_scenario_parameter``.
        Never self-approve; leave the change unapplied until a human approves.

        VALIDATION GATE: run ``backtest_scenario`` FIRST and pass its key metrics
        as ``validation_summary`` — without it (and without an explicit
        ``skip_validation=True`` the user asked for) this tool REFUSES, so
        unvalidated changes cannot reach the approval queue.

        Args:
            change_summary: Human-readable description of the change (what/why, old→new).
                Keep it SHORT — the VI Inquiry.Description field caps at 300 chars
                (DH5108); header/footer take ~110, so aim for <=150 chars here.
                Longer text is truncated with "…".
            flow_id: Optional VSD flow id if the change is a flow edit (from ``list_vsd_flows``).
            dry_run: Keep True to preview the inquiry. False creates it (requires approval).
            validation_summary: REQUIRED (unless skip_validation): one line of
                backtest evidence from ``backtest_scenario``
                (e.g. 'Validation: detection rate 20→100%, productivity 8→21%, est. 8 alerts/365d').
                Included in the inquiry so the approver sees the evidence.
            skip_validation: True bypasses the validation requirement — only when
                the USER explicitly said to skip backtesting (state that in chat).
        """
        if not skip_validation and not (validation_summary or "").strip():
            return {"error": "validation_required",
                    "note": ("Approval submission requires validation evidence. "
                             "Run backtest_scenario first and pass its key metrics "
                             "(estimated detection rate / productivity current→proposed etc.) "
                             "as validation_summary. Only set skip_validation=True when the "
                             "user has explicitly asked to skip backtesting.")}
        async with viya_session("submit_flow_change_for_approval", ctx) as client:
            flow = None
            diff: list[dict[str, Any]] = []
            prod_dep: dict[str, Any] | None = None
            auto_diff_line = ""
            if flow_id:
                flow = await get_json(f"/svi-vsd-service/flows/{flow_id}", client)
                # Guardrail: read the CURRENT PRODUCTION values mechanically from
                # the deployed snapshot and embed production→proposed (% change) in
                # the inquiry, so the approver never depends on hand-copied numbers.
                try:
                    prod_dep = await _latest_deployment(client, flow_id)
                    diff = _diff_vs_production(flow, prod_dep)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("production diff failed for %s: %s", flow_id, exc)
                    auto_diff_line = "Production value retrieval failed (manual check required)"
                if not auto_diff_line:
                    if prod_dep is None:
                        auto_diff_line = "New flow (never deployed to production)"
                    elif not diff:
                        auto_diff_line = "No diff vs production"
                    else:
                        parts = []
                        for d in diff[:3]:
                            pct = d.get("change_pct")
                            pct_s = f"({pct:+.0f}%)" if pct is not None else ""
                            parts.append(f"{d['parameter']} {d['production_value']}"
                                         f"→{d['draft_value']}{pct_s}")
                        if len(diff) > 3:
                            parts.append(f"and {len(diff) - 3} more")
                        auto_diff_line = "Production→proposed: " + "; ".join(parts)
            head = (f"[Detection Flow Change Approval] flow_id={flow_id} / flow={flow.get('name')}"
                    if flow else "[Detection Scenario Change Approval]")
            tail = ("After approval, apply to production with deploy_flow." if flow
                    else "After approval, pass this approval_id to set_scenario_parameter(approval_id=...) to apply.")
            mid = change_summary
            if auto_diff_line:
                mid = f"{mid}\n{auto_diff_line}"
            if validation_summary and validation_summary.strip():
                mid = f"{mid}\n{validation_summary.strip()}"
            desc = f"{head}\n{mid}\n{tail} (this inquiry is the approval review)."
            if len(desc) > _INQUIRY_DESC_MAX:  # VI caps Inquiry.Description at 300 (DH5108)
                keep = _INQUIRY_DESC_MAX - len(head) - len(tail) - 4  # \n×2 + "…" + margin
                desc = f"{head}\n{mid[:max(keep, 0)]}…\n{tail}"
            body = {"objectTypeName": _INQUIRY_TYPE,
                    "fieldValues": {"description_type": "res", "employee_ind": "N",
                                    "Description": desc}}
            prod_dep_info = ({"deployment_id": prod_dep.get("id"),
                              "publishedDate": prod_dep.get("publishedDate")}
                             if prod_dep else None)
            if dry_run:
                out = {"dry_run": True, "flow_id": flow_id,
                       "would_create_inquiry": body["fieldValues"],
                       "diff_vs_production": diff or None,
                       "production_deployment": prod_dep_info,
                       "note": "After approval, rerun with dry_run=False to create the inquiry in VI's approval queue. "
                               "The change is not applied (no auto-apply or auto-deploy)."}
                if flow:
                    out.update(flow_url(flow.get("name"), flow_id))
                return out
            resp = await client.post(f"{VIYA_ENDPOINT}/svi-datahub/documents",
                                     content=_dumps(body),
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json"})
            resp.raise_for_status()
            doc = resp.json()
            did = doc.get("id") or doc.get("fieldValues", {}).get("inquiry_id")
            apply_hint = ("Check status with check_flow_approval → apply with deploy_flow." if flow
                          else "After approval, pass this approval_id to set_scenario_parameter(approval_id=...) to "
                               "apply.")
            return {"submitted": True, "flow_id": flow_id,
                    "flow_name": flow.get("name") if flow else None,
                    "approval_id": did, "approval_type": _INQUIRY_TYPE,
                    "approval_status": doc.get("fieldValues", {}).get("status"),
                    "diff_vs_production": diff or None,
                    "production_deployment": prod_dep_info,
                    "note": f"Inquiry created in VI's approval queue. After a reviewer approves it in VI, {apply_hint}",
                    **approval_item_url(_INQUIRY_TYPE, str(did))}

    @mcp.tool()
    async def check_flow_approval(approval_id: str, ctx: Context) -> dict[str, Any]:
        """Check the status of a flow-change approval item (VI inquiry). Read-only.

        Reads the inquiry's workflow status. 'OPN' = still open (pending review);
        a closed/resolved status means the reviewer has actioned it. Use before
        deploying: only ``deploy_flow`` once the approver has approved.

        Args:
            approval_id: The inquiry id from ``submit_flow_change_for_approval``.
        """
        async with viya_session("check_flow_approval", ctx) as client:
            data: Any = await get_json(
                f"/svi-datahub/documents/{_INQUIRY_TYPE}?limit=200", client)
            items = data if isinstance(data, list) else data.get("items", [])
            doc = next((d for d in items if str(d.get("id")) == str(approval_id)), None)
            if doc is None:
                return {"error": "approval item not found", "approval_id": approval_id}
            fv = doc.get("fieldValues", {})
            status = fv.get("status")
            return {"approval_id": approval_id, "status": status,
                    "pending": status == "OPN",
                    "description": fv.get("Description"),
                    "last_updated": doc.get("lastUpdatedAt"),
                    **approval_item_url(_INQUIRY_TYPE, str(approval_id))}


def _dumps(obj: Any) -> bytes:
    """UTF-8 JSON bytes (ensure_ascii=False preserves non-ASCII names)."""
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
