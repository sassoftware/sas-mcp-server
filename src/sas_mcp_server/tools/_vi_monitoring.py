# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AML monitoring/analysis tools: VI strategy/scenario API + CAS analytical by-products."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import bound_text, get_json, logger, make_client
from ..viya_utils import run_one_snippet


def register_aml_monitoring(mcp: FastMCP, get_token) -> None:
    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    # ------------------------------------------------------------------
    # Layer B: VI Strategy/Scenario API (READ-ONLY)
    # ------------------------------------------------------------------
    # Read the productised strategy/scenario definitions and domains via the
    # svi-alert API. A strategy groups the scenarios that generate alerts; a
    # domain groups strategies.

    @mcp.tool()
    async def list_domains(ctx: Context, limit: int = 50, start: int = 0) -> dict[str, Any]:
        """List VI alert domains (top-level grouping of strategies). Read-only.

        Args:
            limit: Max domains (default 50).
            start: Pagination offset.
        """
        async with viya_session("list_domains", ctx) as client:
            data = await get_json("/svi-alert/domains", client,
                                  params={"start": start, "limit": limit})
        items = data.get("items", [])
        return {"count": data.get("count", len(items)),
                "domains": [{"domainId": d.get("domainId"),
                             "domainName": d.get("domainName"),
                             "description": d.get("domainDescription"),
                             "defaultAlertScore": d.get("defaultAlertScore")} for d in items]}

    @mcp.tool()
    async def list_strategies(ctx: Context, domain_id: str | None = None,
                              limit: int = 50, start: int = 0) -> dict[str, Any]:
        """List VI strategies (each groups the scenarios that generate alerts). Read-only.

        Args:
            domain_id: Optional — restrict to strategies in one domain.
            limit: Max strategies (default 50).
            start: Pagination offset.
        """
        url = (f"/svi-alert/domains/{domain_id}/strategies" if domain_id
               else "/svi-alert/strategies")
        async with viya_session("list_strategies", ctx) as client:
            data = await get_json(url, client, params={"start": start, "limit": limit})
        items = data.get("items", [])
        return {"count": data.get("count", len(items)),
                "strategies": [{"strategyId": s.get("strategyId"),
                                "strategyName": s.get("strategyName"),
                                "status": s.get("strategyStatus"),
                                "priority": s.get("strategyPriority"),
                                "domainId": s.get("domainId"),
                                "scoreRangeLow": s.get("scoreRangeLow"),
                                "scoreRangeHigh": s.get("scoreRangeHigh")} for s in items]}

    @mcp.tool()
    async def get_strategy(strategy_id: str, ctx: Context) -> dict[str, Any]:
        """Get one VI strategy by id (full definition). Read-only.

        Args:
            strategy_id: The strategyId (from ``list_strategies``).
        """
        async with viya_session("get_strategy", ctx) as client:
            return await get_json(f"/svi-alert/strategies/{strategy_id}", client)

    @mcp.tool()
    async def list_scenarios(strategy_id: str, ctx: Context,
                             limit: int = 100, start: int = 0) -> dict[str, Any]:
        """List the scenarios that belong to a VI strategy. Read-only.

        Args:
            strategy_id: The strategyId.
            limit: Max scenarios (default 100).
            start: Pagination offset.
        """
        async with viya_session("list_scenarios", ctx) as client:
            data = await get_json(f"/svi-alert/strategies/{strategy_id}/scenarios",
                                  client, params={"start": start, "limit": limit})
        items = data.get("items", [])
        return {"count": data.get("count", len(items)),
                "scenarios": [{"scenarioId": s.get("scenarioId"),
                               "scenarioName": s.get("scenarioName"),
                               "strategyId": s.get("strategyId")} for s in items]}

    @mcp.tool()
    async def get_strategy_queue_metrics(strategy_id: str, ctx: Context) -> dict[str, Any]:
        """Get today's operational queue metrics for a VI strategy (alert backlog). Read-only.

        Args:
            strategy_id: The strategyId.
        """
        from datetime import datetime
        today = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z")
        url = f"{VIYA_ENDPOINT}/svi-alert/strategies/{strategy_id}/queues/metrics"
        async with viya_session("get_strategy_queue_metrics", ctx) as client:
            resp = await client.get(url, headers={"Accept": "application/json"},
                                    params={"today": today})
            resp.raise_for_status()
            return {"metrics": resp.json()}

    # ------------------------------------------------------------------
    # Layer C: CAS analytical by-products (WRITE — spawns compute sessions)
    # ------------------------------------------------------------------
    # Scenario tuning/effectiveness, thresholds and transaction trends are
    # NOT exposed by the VI API — they are analytical outputs in the Public
    # caslib. Each tool runs a FIXED SAS aggregation (logic baked in, bounded
    # output) so results are deterministic and auditable. Classified as WRITE
    # because they spawn a Viya compute session (server-side work), even though
    # the SAS code itself is read-only.

    async def _run_fixed_sas(sas: str, ctx: Context) -> dict[str, Any]:
        token = await get_token(ctx)
        res = await run_one_snippet(sas, "1", token)
        listing = (res.get("listing") or "").strip()
        # Return the listing whenever there is one; a benign WARNING (e.g. OUTOBS=
        # truncation) sets state="warning" but the results are still valid. Only
        # fall back to the raw log when there is genuinely no listing output.
        if listing and listing != "(no listing output)":
            return {"state": res.get("state"), "output": bound_text(listing)}
        return {"state": res.get("state"), "output": bound_text(res.get("log", ""))}

    def _seg_clause(col: str, segment: str | None) -> str:
        """Build a safe WHERE for a segment code (alnum/_/- only)."""
        if not segment:
            return ""
        if not segment.replace("_", "").replace("-", "").isalnum():
            raise ValueError("segment must be alphanumeric")
        return f'where upcase({col})=upcase("{segment}");'

    @mcp.tool()
    async def get_scenario_effectiveness(ctx: Context,
                                         segment: str | None = None) -> dict[str, Any]:
        """Scenario tuning effectiveness (productivity / false-negative / recall) by segment.

        Fixed aggregation over SAS10021_TUNING_METRICS (ATL/BTL tuning output).
        Use this for "which scenarios/segments detect poorly" — do NOT re-derive.

        Args:
            segment: Optional segment code to filter (e.g. a risk segment).
        """
        where = _seg_clause("SEGMENT", segment)
        sas = f"""options validvarname=any;
cas s; caslib _all_ assign;
proc casutil; load casdata="SAS10021_TUNING_METRICS.sashdat" incaslib="Public"
  casout="_t" outcaslib="CASUSER" replace; quit;
title "Scenario effectiveness (SAS10021_TUNING_METRICS)";
proc print data=CASUSER._t noobs; {where} run;
title; cas s terminate;"""
        return await _run_fixed_sas(sas, ctx)

    @mcp.tool()
    async def get_scenario_thresholds(ctx: Context,
                                      segment: str | None = None) -> dict[str, Any]:
        """Current scenario threshold values by segment (defined, not guessed).

        Fixed read of AMLA_CURR_THLD_VAL (current + first threshold, alert volume,
        productive alerts). Use these values instead of estimating thresholds.

        Args:
            segment: Optional segment_name to filter.
        """
        where = _seg_clause("segment_name", segment)
        sas = f"""options validvarname=any;
cas s; caslib _all_ assign;
proc casutil; load casdata="AMLA_CURR_THLD_VAL.sashdat" incaslib="Public"
  casout="_t" outcaslib="CASUSER" replace; quit;
title "Current scenario thresholds (AMLA_CURR_THLD_VAL)";
proc print data=CASUSER._t noobs; {where} run;
title; cas s terminate;"""
        return await _run_fixed_sas(sas, ctx)

    @mcp.tool()
    async def get_alert_disposition_stats(ctx: Context,
                                          segment: str | None = None) -> dict[str, Any]:
        """Alert disposition stats by segment (ATL/BTL dispositioned dataset).

        Fixed aggregation over SAS10021_ALERT_DISPOSITIONED_DATASET: alert count
        and average current threshold value per segment. Use for false-positive /
        disposition context.

        Args:
            segment: Optional segment_name to filter.
        """
        where = _seg_clause("segment_name", segment)
        sas = f"""options validvarname=any;
cas s; caslib _all_ assign;
proc casutil; load casdata="SAS10021_ALERT_DISPOSITIONED_DATASET.sashdat" incaslib="Public"
  casout="_t" outcaslib="CASUSER" replace; quit;
title "Alert disposition stats by segment";
proc sql; create table _o as
  select segment_name, count(*) as alert_count,
         mean('Current Threshold Value'n) as avg_threshold format=comma16.2
  from CASUSER._t {('' if not where else where.replace(';',''))}
  group by segment_name order by alert_count desc; quit;
proc print data=_o noobs; run;
title; cas s terminate;"""
        return await _run_fixed_sas(sas, ctx)

    @mcp.tool()
    async def get_transaction_trends(dimension: str, ctx: Context) -> dict[str, Any]:
        """Overall transaction trends aggregated by a party dimension.

        Fixed aggregation over PARTY_SUMMARY2: total transaction amount/count and
        party count grouped by the chosen dimension. For recent-overall trend
        analysis.

        Args:
            dimension: One of 'party_type', 'country', 'geo_risk', 'industry'.
        """
        cols = {"party_type": "Party Type", "country": "Country Name",
                "geo_risk": "Geo Risk", "industry": "Industry Desc"}
        if dimension not in cols:
            raise ValueError(f"dimension must be one of {sorted(cols)}")
        col = cols[dimension]
        sas = f"""options validvarname=any;
cas s; caslib _all_ assign;
proc casutil; load casdata="PARTY_SUMMARY2.sashdat" incaslib="Public"
  casout="_t" outcaslib="CASUSER" replace; quit;
title "Transaction trends by {col}";
proc sql outobs=200; create table _o as
  select '{col}'n as dimension,
         count(*) as party_count,
         sum('Total Transaction Amount'n) as total_amount format=comma20.2,
         sum('Total Transaction Count'n) as total_txn_count
  from CASUSER._t group by '{col}'n order by total_amount desc; quit;
proc print data=_o noobs; run;
title; cas s terminate;"""
        return await _run_fixed_sas(sas, ctx)

    @mcp.tool()
    async def get_party_summary(party_number: int, ctx: Context) -> dict[str, Any]:
        """Get one party's transaction summary from PARTY_SUMMARY2 (fixed lookup).

        Args:
            party_number: Numeric Party Number.
        """
        pn = int(party_number)
        sas = f"""options validvarname=any;
cas s; caslib _all_ assign;
proc casutil; load casdata="PARTY_SUMMARY2.sashdat" incaslib="Public"
  casout="_t" outcaslib="CASUSER" replace; quit;
title "Party summary for {pn}";
proc print data=CASUSER._t noobs; where 'Party Number'n = {pn}; run;
title; cas s terminate;"""
        return await _run_fixed_sas(sas, ctx)

    @mcp.tool()
    async def get_customer_risk(customer_id: str, ctx: Context) -> dict[str, Any]:
        """Get a customer's risk rating from CRR_DATA (computed CRR). Read-only.

        Fixed lookup of the customer's branch/group risk scores and risk class —
        use these instead of re-deriving customer risk from raw tables.

        Args:
            customer_id: Customer ID (char, e.g. from an alert's customer_id).
        """
        cid = str(customer_id).replace("'", "''")
        sas = f"""options validvarname=any;
cas s; caslib _all_ assign;
proc casutil; load casdata="CRR_DATA.sashdat" incaslib="Public"
  casout="_t" outcaslib="CASUSER" replace; quit;
title "Customer risk rating (CRR_DATA) for {customer_id}";
proc print data=CASUSER._t noobs; where customer_id='{cid}';
  var customer_id customer_name customer_type branch_risk_class group_risk_class
      branch_overall_score group_overall_score flag_current_crr; run;
title; cas s terminate;"""
        return await _run_fixed_sas(sas, ctx)

    @mcp.tool()
    async def detect_transaction_anomalies(ctx: Context, metric: str = "amount",
                                           window_days: int = 90,
                                           top_n: int = 20) -> dict[str, Any]:
        """Scan recent transactions for peer-relative outlier parties — scenario-independent. Read-only.

        Fixed, deterministic unsupervised outlier detection over the recent
        transaction window (AML_TRANS_MODEL_ABT): aggregates each party over the
        last ``window_days`` (relative to the latest transaction date), then ranks
        by z-score of the chosen metric vs the population. High z-score = anomaly
        (a "seed" not necessarily caught by existing scenarios). Not tied to any
        scenario — complements coverage-gap analysis.

        Args:
            metric: ``amount`` (recent total) | ``velocity`` (txn count) |
                ``max_txn`` (largest single transaction).
            window_days: Recent look-back window in days (default 90).
            top_n: Number of top outlier parties to return (default 20, max 200).
        """
        aggs = {"amount": "sum(amt)", "velocity": "count(*)", "max_txn": "max(amt)"}
        if metric not in aggs:
            raise ValueError(f"metric must be one of {sorted(aggs)}")
        wd = int(window_days)
        tn = max(1, min(int(top_n), 200))
        agg = aggs[metric]
        sas = f"""options validvarname=any;
%let wd={wd}; %let tn={tn};
cas s; caslib _all_ assign;
proc casutil; load casdata="AML_TRANS_MODEL_ABT.sashdat" incaslib="Public"
  casout="_t" outcaslib="CASUSER" replace; quit;
data _tx; set CASUSER._t;
  _dt = input(transaction_dttm, anydtdtm40.); amt = currency_amount;
  keep party_number_cat _dt amt party_type_desc risk_classification
       residence_country_code politically_exposed_person_ind;
run;
proc sql noprint; select max(_dt) into :maxn from _tx; quit;
%let thr=%sysevalf(&maxn - &wd*86400);
proc sql; create table _p as
  select party_number_cat, {agg} as val, count(*) as txn_cnt,
         sum(amt) as amt_sum, max(amt) as amt_max,
         max(party_type_desc) as party_type, max(risk_classification) as risk,
         max(residence_country_code) as country,
         max(politically_exposed_person_ind) as pep
  from _tx where _dt >= &thr group by party_number_cat; quit;
proc sql noprint; select mean(val), std(val) into :mu, :sd from _p; quit;
data _z; set _p; if &sd>0 then z=round((val-&mu)/&sd,0.01); else z=0; run;
proc sort data=_z; by descending val; run;
title "Recent-transaction anomalies (metric={metric}, window &wd days, top &tn by z-score)";
proc print data=_z(obs=&tn) noobs;
  var party_number_cat val z txn_cnt amt_sum amt_max party_type risk country pep;
  format val amt_sum amt_max comma18.; run;
title; cas s terminate;"""
        return await _run_fixed_sas(sas, ctx)
