# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AML scenario backtest: validate a PROPOSED rule/threshold on EXISTING data.

The governance gap this closes: before a new/tuned detection scenario goes to
the approval queue, run it against historical data so the approver sees
evidence, not a promise. Two fixed, deterministic evidence layers:

1. Simulation — apply the proposed aggregate rule (metric >= threshold per
   party over a look-back window) to the recent raw transactions
   (``AML_TRANS_MODEL_ABT``): estimated alerted parties / volume / PEP and
   high-risk hits.
2. Disposition overlap — against historically dispositioned alerts
   (``AML_MONITORING_ALERTS``: fp_flag 0=productive, 1=false positive), how
   many productive vs false-positive alerts an amount-threshold at the
   proposed level would have captured, vs the current threshold.

Results are appended to ``Public/SCENARIO_VALIDATION_RESULTS`` (saved AND
promoted to CAS memory) — the data source of the reusable VA validation
dashboard, so the approver can see the same metrics inside Viya.
"""

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import bound_text, logger, make_client
from ..viya_utils import run_one_snippet
from ._vi_links import cas_table_url, report_url

VALIDATION_CASLIB = "Public"
VALIDATION_TABLE = "SCENARIO_VALIDATION_RESULTS"

# The reusable VA validation dashboard (methodology text + backtest-history
# crosstab + recall/precision bar), fed by Public/SCENARIO_VALIDATION_RESULTS.
# It is a server-side artifact and demo-env resets WIPE it (a hardcoded id died
# exactly this way, returning dead ref_url links) — so the id is resolved at
# runtime: known-name lookup first, then auto-recreation from the packaged BIRD
# template. The resolved id is cached per process.
# TODO: Replace these with your own VA report name(s) once you create the
# validation dashboard in your Viya environment. The lookup tries each name in
# order, falls back to auto-recreating from the BIRD template if none match.
# These are the server-side report names — they must match what's on the server.
VALIDATION_REPORT_NAMES = ("AML シナリオ検証（バックテスト）",
                           "シナリオ変更検証ダッシュボード")
_VALIDATION_BIRD_TEMPLATE = Path(__file__).parent / "resources" / "validation_dashboard_bird.json"
_validation_report_id: str | None = None


async def _find_report_by_name(client: httpx.AsyncClient, name: str) -> str | None:
    resp = await client.get(f"{VIYA_ENDPOINT}/reports/reports",
                            params={"filter": f'eq(name,"{name}")', "limit": 1})
    if resp.status_code != 200:
        return None
    items = resp.json().get("items") or []
    return items[0].get("id") if items else None


async def _create_validation_dashboard(client: httpx.AsyncClient) -> str:
    """Recreate the dashboard in /Public from the packaged BIRD template."""
    bird = json.loads(_VALIDATION_BIRD_TEMPLATE.read_text(encoding="utf-8"))
    folder = await client.get(f"{VIYA_ENDPOINT}/folders/folders/@item",
                              params={"path": "/Public"})
    folder.raise_for_status()
    created = await client.post(
        f"{VIYA_ENDPOINT}/reports/reports",
        params={"parentFolderUri": f"/folders/folders/{folder.json()['id']}"},
        json={"name": bird.get("label", VALIDATION_REPORT_NAMES[0])},
        headers={"Content-Type": "application/vnd.sas.report+json",
                 "Accept": "application/vnd.sas.report+json"})
    created.raise_for_status()
    rid = created.json()["id"]
    put = await client.put(
        f"{VIYA_ENDPOINT}/reports/reports/{rid}/content", json=bird,
        headers={"Content-Type": "application/vnd.sas.report.content+json",
                 "Accept": "application/vnd.sas.report.content+json"})
    put.raise_for_status()
    logger.info("validation dashboard recreated from template: %s", rid)
    return rid


async def _ensure_validation_dashboard(client: httpx.AsyncClient) -> str:
    """Return the id of a validation dashboard that actually exists right now."""
    global _validation_report_id
    if _validation_report_id:
        resp = await client.get(
            f"{VIYA_ENDPOINT}/reports/reports/{_validation_report_id}")
        if resp.status_code == 200:
            return _validation_report_id
        _validation_report_id = None  # wiped (demo reset) — fall through
    for name in VALIDATION_REPORT_NAMES:
        rid = await _find_report_by_name(client, name)
        if rid:
            _validation_report_id = rid
            return rid
    _validation_report_id = await _create_validation_dashboard(client)
    return _validation_report_id

_METRICS = {"amount_sum": "sum(amt)", "txn_count": "count(*)", "max_txn": "max(amt)"}

# Model-as-detection-candidate artifacts (analysis by-products, like the
# validation-results table — NOT detection config):
# - ALERT_MODEL_SCORES: per-alert model scores incl. train/holdout split flag
# - ALERT_SCORING_MODEL: the trained GRADBOOST analytic store (astore)
MODEL_SCORES_TABLE = "ALERT_MODEL_SCORES"
MODEL_ASTORE_TABLE = "ALERT_SCORING_MODEL"


def _persist_validation_sas() -> str:
    """SAS block that appends WORK._row to the persisted validation-results table."""
    return f"""
proc cas;
  table.fileInfo result=r / caslib="{VALIDATION_CASLIB}";
  exists = 0;
  do f over r.FileInfo;
    if upcase(f.Name) = "{VALIDATION_TABLE}.SASHDAT" then exists = 1;
  end;
  if exists = 1 then
    table.loadTable / caslib="{VALIDATION_CASLIB}" path="{VALIDATION_TABLE}.sashdat"
      casOut={{caslib="CASUSER" name="_hist" replace=True}};
run; quit;
%macro _app;
%if %sysfunc(exist(CASUSER._hist)) %then %do;
  data CASUSER._allv; set CASUSER._hist _row; run;
%end; %else %do;
  data CASUSER._allv; set _row; run;
%end;
%mend; %_app;
proc casutil;
  save casdata="_allv" incaslib="CASUSER" outcaslib="{VALIDATION_CASLIB}"
    casout="{VALIDATION_TABLE}" replace;
  droptable casdata="{VALIDATION_TABLE}" incaslib="{VALIDATION_CASLIB}" quiet;
  load casdata="{VALIDATION_TABLE}.sashdat" incaslib="{VALIDATION_CASLIB}"
    casout="{VALIDATION_TABLE}" outcaslib="{VALIDATION_CASLIB}" promote;
quit;"""


def _train_alert_model_sas(lbl: str, ntrees: int, learning_rate: float,
                           max_depth: int, holdout_fraction: float, seed: int,
                           save_scores: bool) -> str:
    """Fixed SAS: train the alert-scoring (FP-suppression) GRADBOOST on dispositions.

    Leakage guard: inputs are limited to what exists BEFORE an alert is worked
    (amount, rule score, customer attrs, scenario) — investigation_result /
    reported_flag / created_case_flag / processing dates are outcomes and are
    deliberately NOT model inputs.
    """
    persist = ""
    if save_scores:
        persist = f"""
data CASUSER._scores;
  set CASUSER._sc;
  length model_label $100;
  model_label='{lbl}'; score=P_productive1;
  trained_dttm=datetime(); format trained_dttm datetime19.;
  keep alert_id score productive part amount scenario model_label trained_dttm;
run;
proc casutil;
  save casdata="_scores" incaslib="CASUSER" outcaslib="{VALIDATION_CASLIB}"
    casout="{MODEL_SCORES_TABLE}" replace;
  droptable casdata="{MODEL_SCORES_TABLE}" incaslib="{VALIDATION_CASLIB}" quiet;
  load casdata="{MODEL_SCORES_TABLE}.sashdat" incaslib="{VALIDATION_CASLIB}"
    casout="{MODEL_SCORES_TABLE}" outcaslib="{VALIDATION_CASLIB}" promote;
  save casdata="_gbt" incaslib="CASUSER" outcaslib="{VALIDATION_CASLIB}"
    casout="{MODEL_ASTORE_TABLE}" replace;
quit;"""
    return f"""options validvarname=any;
cas s; caslib _all_ assign;
proc casutil; load casdata="AML_MONITORING_ALERTS.sashdat" incaslib="Public"
  casout="_al" outcaslib="CASUSER" replace; quit;
data _al2;
  set CASUSER._al;
  if fp_flag in (0,1);
  productive = (fp_flag=0);
  pep_n = (upcase(substr(PEP,1,1))='Y');
  _r = ranuni({seed}); part = (_r < {holdout_fraction});
  drop _r;
run;
data CASUSER._mal; set _al2; run;
data CASUSER._mal_tr; set _al2(where=(part=0)); run;
proc gradboost data=CASUSER._mal_tr seed={seed} noprint
               ntrees={ntrees} learningrate={learning_rate} maxdepth={max_depth};
  target productive / level=nominal;
  input amount alert_score customer_age customer_duration / level=interval;
  input scenario customer_type branch_country pep_n / level=nominal;
  savestate rstore=CASUSER._gbt;
run;
proc astore;
  score data=CASUSER._mal rstore=CASUSER._gbt out=CASUSER._sc
        copyvars=(alert_id productive part amount scenario);
run;
data _hold; set CASUSER._sc(where=(part=1)); run;
proc rank data=_hold out=_ranked; var P_productive1; ranks rnk; run;
proc sql noprint;
  select sum(productive=1), sum(productive=0) into :n1 trimmed, :n0 trimmed
  from _hold;
quit;
title "holdout AUC (n1=&n1 productive / n0=&n0 FP alerts)";
proc sql;
  select (sum(case when productive=1 then rnk else 0 end) - &n1*(&n1+1)/2)
         / (&n1*&n0) as AUC format=6.3
  from _ranked;
quit;
%macro _ops;
%let cuts=0.05 0.1 0.15 0.2 0.3 0.5;
%do i=1 %to 6;
  %let c=%scan(&cuts,&i,%str( ));
  proc sql; create table _op&i as select &c as cutoff,
    sum(productive=1 and P_productive1>=&c) as cap_prod,
    sum(productive=0 and P_productive1>=&c) as cap_fp
  from _hold; quit;
%end;
data _ops;
  set _op1-_op6;
  recall_pct=round(100*cap_prod/max(&n1,1),0.1);
  precision_pct=round(100*cap_prod/max(cap_prod+cap_fp,1),0.1);
run;
%mend; %_ops;
title "operating points (holdout) - pick a cutoff, then run backtest_model";
proc print data=_ops noobs; run;{persist}
title; cas s terminate;"""


def _backtest_model_sas(lbl: str, thr: float, alert_where: str,
                        save_results: bool) -> str:
    """Fixed SAS: evaluate the persisted model scores as a detection candidate.

    HOLDOUT ONLY — the model must not be graded on alerts it trained on, so
    counts here are on the ~holdout split (smaller base than the rule rows,
    which use the full history). Compare rule vs model by the RATE columns.
    """
    persist = _persist_validation_sas() if save_results else ""
    return f"""options validvarname=any;
%let thr={thr};
cas s; caslib _all_ assign;
proc cas;
  table.fileInfo result=r / caslib="{VALIDATION_CASLIB}";
  exists = 0;
  do f over r.FileInfo;
    if upcase(f.Name) = "{MODEL_SCORES_TABLE}.SASHDAT" then exists = 1;
  end;
  if exists = 0 then print "MODEL_BACKTEST_GUARD_NO_SCORES";
  else
    table.loadTable / caslib="{VALIDATION_CASLIB}" path="{MODEL_SCORES_TABLE}.sashdat"
      casOut={{caslib="CASUSER" name="_sc" replace=True}};
run; quit;
%macro _main;
%if not %sysfunc(exist(CASUSER._sc)) %then %do;
  title "MODEL_BACKTEST_GUARD_NO_SCORES: run train_alert_scoring_model first";
  data _null_; put "no {MODEL_SCORES_TABLE} table"; run;
%end;
%else %do;
data _hold; set CASUSER._sc; where part=1 {alert_where}; run;
proc sql; create table _ov as select
  sum(productive=1) as prior_productive, sum(productive=0) as prior_fp,
  sum(productive=1 and score>=&thr) as cap_prod,
  sum(productive=0 and score>=&thr) as cap_fp
from _hold; quit;
data _row;
  length validation_label $100 metric $16;
  validation_dttm=datetime(); format validation_dttm datetime19.;
  validation_label='{lbl}'; metric='model_score';
  proposed_threshold=&thr;
  set _ov;
  est_recall_pct=round(100*cap_prod/max(prior_productive,1),0.1);
  est_precision_pct=round(100*cap_prod/max(cap_prod+cap_fp,1),0.1);
run;
{persist}
title "Model backtest: {lbl} (score cutoff=&thr, holdout only)";
proc print data=_row noobs; run;
%end;
%mend _main;
%_main;
title; cas s terminate;"""


def _safe_label(text: str, max_len: int = 100) -> str:
    """Single-quote-literal-safe label (also strips macro triggers & and %)."""
    t = str(text).replace("'", "''").replace("&", "＆").replace("%", "％")
    return t[:max_len]


def _safe_filter(text: str) -> str:
    """Whitelist a scenario-name contains-filter (alnum/space/_/- only)."""
    t = str(text)
    if not re.fullmatch(r"[\w \-]+", t):
        raise ValueError("scenario_filter must be alphanumeric/space/_/- only")
    return t


def register_aml_backtest(mcp: FastMCP, get_token) -> None:
    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    async def _run_fixed_sas(sas: str, ctx: Context) -> dict[str, Any]:
        token = await get_token(ctx)
        res = await run_one_snippet(sas, "1", token)
        listing = (res.get("listing") or "").strip()
        # Return the listing whenever there is one; a benign WARNING sets
        # state="warning" but the results are still valid. Only fall back to
        # the raw log when there is genuinely no listing output.
        if listing and listing != "(no listing output)":
            return {"state": res.get("state"), "output": bound_text(listing)}
        return {"state": res.get("state"), "output": bound_text(res.get("log", ""))}

    @mcp.tool()
    async def backtest_scenario(validation_label: str, proposed_threshold: float,
                                ctx: Context, metric: str = "amount_sum",
                                current_threshold: float | None = None,
                                lookback_days: int = 365,
                                scenario_filter: str | None = None,
                                save_results: bool = True,
                                additional_thresholds: list[float] | None = None
                                ) -> dict[str, Any]:
        """Backtest a PROPOSED detection rule/threshold on EXISTING data. Validation gate.

        Run this BEFORE submitting a new/tuned scenario for approval, so the
        approval item carries evidence. Fixed, deterministic aggregations:

        * Simulation on recent raw transactions (AML_TRANS_MODEL_ABT): parties
          whose ``metric`` over the last ``lookback_days`` (relative to the
          latest transaction in the data) reaches ``proposed_threshold`` —
          estimated alerted parties, est. alerts/30d, PEP & high-risk hits.
        * Overlap with historically dispositioned alerts
          (AML_MONITORING_ALERTS, fp_flag 0=productive / 1=false positive):
          productive vs false-positive alerts an amount threshold at the
          proposed level would have captured (est. recall / precision), vs
          ``current_threshold`` when given. Amount-based proxy — states so.

        Appends one summary row to Public/SCENARIO_VALIDATION_RESULTS (saved +
        promoted to CAS memory) so the VA validation dashboard shows the run.
        Writes ONLY this analysis by-product — no detection config is touched.

        Args:
            validation_label: Name for this run — shown on the dashboard.
                KEEP IT SHORT (≤20 chars, e.g. "HCHA_v2"): long labels wrap
                to multiple lines in the dashboard crosstab and crush the
                visible row count. Threshold/baseline details belong in the
                threshold columns (and the multi-threshold suffix), not here.
            proposed_threshold: Proposed threshold value for ``metric``.
            metric: ``amount_sum`` (window total per party) | ``txn_count`` |
                ``max_txn`` (largest single transaction).
            current_threshold: Optional current/comparison threshold (same
                unit) — adds the "current" columns for a before/after story.
            lookback_days: Simulation window in days (default 365; the demo
                data is historical, so the window anchors to its latest date).
            scenario_filter: Optional scenario-name contains-filter for the
                historical-alert overlap. Alert-history scenario names are
                ENGLISH (e.g. "Large Cash Deposits", "Structured Withdrawals")
                — a filter matching 0 alerts is REFUSED (no junk row is
                written) and the available names are returned.
            save_results: False skips writing the results table (preview only).
            additional_thresholds: Optional extra thresholds to sweep in the
                SAME run (max 8 total incl. ``proposed_threshold``) — one
                result row per threshold, labels suffixed "@35,000 USD" etc.
                Use this to compare candidates side-by-side on the dashboard
                in a single job.
        """
        if metric not in _METRICS:
            raise ValueError(f"metric must be one of {sorted(_METRICS)}")
        agg = _METRICS[metric]
        wd = max(1, int(lookback_days))
        thr = float(proposed_threshold)
        # Multi-threshold sweep: dedupe, keep order, cap the fan-out.
        thresholds: list[float] = [thr]
        for t in (additional_thresholds or []):
            tf = float(t)
            if tf not in thresholds:
                thresholds.append(tf)
        if len(thresholds) > 8:
            return {"error": "too_many_thresholds",
                    "note": "Maximum 8 thresholds per run (single job). Split into separate calls."}
        thr_list = " ".join(f"{t:g}" for t in thresholds)
        cmp_ = float(current_threshold) if current_threshold is not None else thr
        # Enforced (not advisory): long labels wrap to 3 lines in the dashboard
        # crosstab and crush the visible rows — hard-cap at 20 chars. Threshold
        # info lives in the threshold columns / the multi-threshold suffix.
        lbl = _safe_label(validation_label)
        label_truncated = len(lbl) > 20
        if label_truncated:
            lbl = lbl[:20]
        alert_where = ""
        if scenario_filter:
            f = _safe_filter(scenario_filter)
            alert_where = f"where upcase(scenario) contains upcase('{f}')"

        persist = _persist_validation_sas() if save_results else ""

        sas = f"""options validvarname=any;
%let wd={wd}; %let cmp={cmp_}; %let thrlist={thr_list};
%let nthr=%sysfunc(countw(&thrlist,%str( )));
cas s; caslib _all_ assign;
proc casutil; load casdata="AML_TRANS_MODEL_ABT.sashdat" incaslib="Public"
  casout="_tx" outcaslib="CASUSER" replace; quit;
data _txw; set CASUSER._tx;
  _dt=input(transaction_dttm, anydtdtm40.); amt=currency_amount;
  keep party_number_cat _dt amt risk_classification politically_exposed_person_ind;
run;
proc sql noprint; select max(_dt) into :maxn from _txw; quit;
%let thrdt=%sysevalf(&maxn - &wd*86400);
proc sql; create table _p as
  select party_number_cat, {agg} as val, count(*) as txn_cnt,
         max(politically_exposed_person_ind) as pep length=8,
         max(risk_classification) as risk length=16
  from _txw where _dt>=&thrdt group by party_number_cat; quit;
proc casutil; load casdata="AML_MONITORING_ALERTS.sashdat" incaslib="Public"
  casout="_al" outcaslib="CASUSER" replace; quit;
proc sql noprint; select count(*) into :ovn trimmed
  from CASUSER._al {alert_where}; quit;
%macro _main;
%if &ovn = 0 %then %do;
  /* Guard: a scenario_filter that matches nothing would write a row with all
     overlap metrics missing (silent junk on the validation dashboard). Refuse
     and show the caller what scenario names actually exist. */
  title "BACKTEST_GUARD_NO_MATCH: scenario_filter matched 0 alerts - nothing was written";
  proc sql outobs=30;
    select scenario, count(*) as alerts, sum(fp_flag=0) as productive
    from CASUSER._al group by scenario order by alerts desc;
  quit;
%end;
%else %do;
/* One row per proposed threshold — the sweep runs in a single job so the
   history table gets a consistent, race-free multi-row append. */
%macro _one(i);
  %let thr=%scan(&thrlist,&i,%str( ));
  proc sql; create table _sim as select
    count(*) as parties_total,
    sum(val>=&thr) as est_parties,
    sum(val>=&thr and upcase(substr(pep,1,1)) in ('Y','1')) as pep_hits,
    sum(val>=&thr and upcase(substr(risk,1,1))='H') as highrisk_hits
    from _p; quit;
  proc sql; create table _ov as select
    sum(fp_flag=0) as prior_productive, sum(fp_flag=1) as prior_fp,
    sum(fp_flag=0 and amount>=&thr) as cap_prod,
    sum(fp_flag=1 and amount>=&thr) as cap_fp,
    sum(fp_flag=0 and amount>=&cmp) as cur_cap_prod,
    sum(fp_flag=1 and amount>=&cmp) as cur_cap_fp
    from CASUSER._al {alert_where}; quit;
  data _row&i;
    length validation_label $100 metric $16;
    validation_dttm=datetime(); format validation_dttm datetime19.;
    %if &nthr = 1 %then %do;
    validation_label='{lbl}';
    %end; %else %do;
    validation_label=catx(' ', '{lbl}', cats('@', put(&thr, comma16.), ' USD'));
    %end;
    metric='{metric}';
    lookback_days=&wd; proposed_threshold=&thr; current_threshold=&cmp;
    set _sim; set _ov;
    est_alerts_30d=round(est_parties*30/&wd,0.1);
    est_recall_pct=round(100*cap_prod/max(prior_productive,1),0.1);
    est_precision_pct=round(100*cap_prod/max(cap_prod+cap_fp,1),0.1);
    cur_recall_pct=round(100*cur_cap_prod/max(prior_productive,1),0.1);
  run;
%mend _one;
%do i=1 %to &nthr; %_one(&i); %end;
data _row; set %do i=1 %to &nthr; _row&i %end;; run;
{persist}
title "Backtest: {lbl} (metric={metric}, proposed=&thrlist, current=&cmp, window=&wd d)";
proc print data=_row noobs; run;
%end;
%mend _main;
%_main;
title; cas s terminate;"""
        out = await _run_fixed_sas(sas, ctx)
        if "BACKTEST_GUARD_NO_MATCH" in (out.get("output") or ""):
            return {"error": "scenario_filter_no_match",
                    "scenario_filter": scenario_filter,
                    "note": ("scenario_filter matched 0 alerts in the historical alert data — "
                             "no validation row was written (prevents junk rows with missing metrics). "
                             "Alert-history scenario names are ENGLISH — pick one from the output list "
                             "and retry, e.g. scenario_filter='Large Cash Deposits'. "
                             "If no history exists for the target scenario, the historical-alert "
                             "recall/precision cannot be computed (omit scenario_filter to run "
                             "simulation only)."),
                    "available_scenarios": out.get("output")}
        out["note"] = ("Estimates are based on a backtest against existing data "
                       "(simulation=applying the rule to recent transactions, "
                       "overlap=amount-based proxy against historical alert dispositions). "
                       "Include these metrics in the approval submission.")
        if label_truncated:
            out["label_truncated"] = (f"Label was too long and was auto-truncated to 20 chars: '{lbl}' "
                                      "(enforced to keep dashboard row height readable).")
        if save_results:
            await _attach_dashboard_link(out, ctx)
        return out

    async def _attach_dashboard_link(out: dict[str, Any], ctx: Context) -> None:
        """Attach the (self-healing) validation-dashboard link. Best-effort."""
        out.update(cas_table_url(VALIDATION_CASLIB, VALIDATION_TABLE))
        try:
            async with viya_session("backtest:dashboard", ctx) as client:
                rid = await _ensure_validation_dashboard(client)
            rep = report_url(rid)
            out["validation_dashboard_url"] = rep["ref_url"]
            out["validation_dashboard"] = rep["ref_label"]
        except Exception as exc:  # link is best-effort; the backtest itself succeeded
            logger.warning("validation dashboard resolve/recreate failed: %s", exc)
            out["validation_dashboard_note"] = (
                f"Dashboard URL could not be resolved ({exc}). "
                f"Data was saved to {VALIDATION_CASLIB}/{VALIDATION_TABLE} — "
                "open that table in VA to view the results.")

    @mcp.tool()
    async def train_alert_scoring_model(ctx: Context,
                                        model_label: str = "GRADBOOST Alert Scoring",
                                        ntrees: int = 100,
                                        learning_rate: float = 0.1,
                                        max_depth: int = 5,
                                        holdout_fraction: float = 0.3,
                                        seed: int = 42,
                                        save_scores: bool = True) -> dict[str, Any]:
        """Train an alert-scoring (false-positive suppression) model on dispositions.

        The ML counterpart of rule tuning in the improvement loop: a
        deterministic CAS GRADBOOST learns which historical alerts were
        productive (fp_flag=0) vs false positives, using ONLY
        pre-investigation features (amount, rule score, customer attributes,
        scenario) — outcome columns are excluded to prevent leakage. Runs
        entirely CAS-side (no MAS dependency).

        Returns holdout AUC and an operating-point table (cutoff → captured
        productive/FP, recall/precision). Persists per-alert scores to
        Public/ALERT_MODEL_SCORES and the astore to Public/ALERT_SCORING_MODEL.
        Next step: pick a cutoff from the operating points and run
        ``backtest_model`` so the model lands on the validation dashboard
        next to the rule candidates. Writes ONLY analysis by-products — no
        detection config, no model registry, no MAS.

        Args:
            model_label: Name recorded with the persisted scores.
            ntrees: GRADBOOST trees (10-500).
            learning_rate: GRADBOOST learning rate (0.01-1).
            max_depth: GRADBOOST max tree depth (2-10).
            holdout_fraction: Fraction of alerts held out of training for
                honest evaluation (0.1-0.5).
            seed: Random seed (split + training) for reproducibility.
            save_scores: False skips persisting scores/astore (preview only).
        """
        lbl = _safe_label(model_label)
        sas = _train_alert_model_sas(
            lbl,
            ntrees=min(max(int(ntrees), 10), 500),
            learning_rate=min(max(float(learning_rate), 0.01), 1.0),
            max_depth=min(max(int(max_depth), 2), 10),
            holdout_fraction=min(max(float(holdout_fraction), 0.1), 0.5),
            seed=int(seed),
            save_scores=save_scores)
        out = await _run_fixed_sas(sas, ctx)
        out["note"] = ("False-positive suppression model trained on dispositioned alerts (fp_flag). "
                       "Outcome columns (investigation result, report flag, etc.) are excluded from "
                       "features to prevent leakage. Pick a cutoff from the operating points, "
                       "then run backtest_model to add it to the validation dashboard alongside "
                       "rule candidates.")
        if save_scores:
            out.update(cas_table_url(VALIDATION_CASLIB, MODEL_SCORES_TABLE))
        return out

    @mcp.tool()
    async def backtest_model(validation_label: str, score_threshold: float,
                             ctx: Context, scenario_filter: str | None = None,
                             save_results: bool = True) -> dict[str, Any]:
        """Backtest the trained alert-scoring model as a detection candidate. Validation gate.

        The model counterpart of ``backtest_scenario``: evaluates the persisted
        model scores (Public/ALERT_MODEL_SCORES, from
        ``train_alert_scoring_model``) at ``score_threshold`` against
        dispositioned alerts, and appends one row (metric='model_score') to
        Public/SCENARIO_VALIDATION_RESULTS — so rule candidates and the model
        appear SIDE BY SIDE on the same VA validation dashboard.

        Honesty note: evaluated on the HOLDOUT split only (the model must not
        be graded on alerts it trained on), so absolute counts sit on a
        smaller base than rule rows (full history) — compare candidates by the
        RATE columns (recall/precision), not raw counts.

        Args:
            validation_label: Dashboard label (e.g. "Model: GB cutoff 0.1").
            score_threshold: Score cutoff in [0,1] — alerts scoring >= this
                count as "kept/escalated" by the model.
            scenario_filter: Optional scenario-name contains-filter, for
                comparing against a rule backtest of the same scenario.
            save_results: False skips writing the results table (preview only).
        """
        thr = min(max(float(score_threshold), 0.0), 1.0)
        lbl = _safe_label(validation_label)
        alert_where = ""
        if scenario_filter:
            f = _safe_filter(scenario_filter)
            alert_where = f"and upcase(scenario) contains upcase('{f}')"
        sas = _backtest_model_sas(lbl, thr, alert_where, save_results)
        out = await _run_fixed_sas(sas, ctx)
        if "MODEL_BACKTEST_GUARD_NO_SCORES" in (out.get("output") or ""):
            return {"error": "no_model_scores",
                    "note": (f"{VALIDATION_CASLIB}/{MODEL_SCORES_TABLE} not found. "
                             "Run train_alert_scoring_model first.")}
        out["note"] = ("Model rows are evaluated on the holdout split only "
                       "(approx. 30% of alerts not used in training). "
                       "Rule rows use the full history, so raw counts have different bases — "
                       "compare candidates using the recall/precision percentage columns. "
                       "Include this row in the approval submission.")
        if save_results:
            await _attach_dashboard_link(out, ctx)
        return out
