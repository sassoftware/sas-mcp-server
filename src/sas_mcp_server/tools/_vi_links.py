# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build human-openable SAS Viya UI URLs for objects the tools create/change.

Every write/creation tool returns a reference URL so the chat can
show a clickable link straight to where the created object is viewable in the
SAS Viya web apps. This is the answer to "I created something but can't find it
in SAS" — the tool tells you exactly where to look.

Deep links: reports and Model Manager models have stable, version-independent
deep links, so those go straight to the object. CAS tables (e.g. the AML
``SCENARIO_PARAM`` config) and Model Studio / ML-pipeline projects have no
stable query-param deep link across Viya releases, so we link to the owning app
and name the object + filter in the label. All URLs require a browser login
(SAS Logon) — that is expected.

IMPORTANT (AML scenarios): ``create_scenario`` etc. write to the
``Public/SCENARIO_PARAM`` CAS table (the detection-engine config), NOT the
Visual Investigator product scenario store. A newly created scenario is
therefore visible in **Data Explorer** (via this URL) but will NOT appear in
the VI scenario-administration UI until the AML Alert Generation Process / MAS
reloads the config. ``scenario_note()`` states this so the tool is honest.
"""

from urllib.parse import quote

from ..config import VIYA_ENDPOINT

# The CAS table that backs AML detection-scenario configuration.
SCENARIO_CASLIB = "Public"
SCENARIO_TABLE = "SCENARIO_PARAM"


def _ep() -> str:
    return VIYA_ENDPOINT.rstrip("/")


def link_fields(label: str, url: str, api_path: str | None = None) -> dict[str, str]:
    """Standard reference-URL fields merged into a tool's return dict.

    ``ref_url`` is the human-facing link; ``ref_label`` names what it opens.
    ``api_url`` (optional) is the object's REST URL — always resolves to the
    object as JSON in a logged-in browser, so it works even where the product has
    no admin UI page. Always surface at least one of these after a create/modify.
    """
    d = {"ref_url": url, "ref_label": label}
    if api_path:
        d["api_url"] = f"{_ep()}{api_path}" if api_path.startswith("/") else api_path
    return d


def cas_table_url(caslib: str, table: str) -> dict[str, str]:
    """Link to SAS Data Explorer for a CAS table (names the table in the label).

    No stable per-table deep link exists across Viya versions, so this opens the
    Data Explorer app; the label tells the user which caslib/table to open.
    """
    url = f"{_ep()}/SASDataExplorer/"
    return link_fields(f"Open {caslib} / {table} in Data Explorer", url)


def scenario_table_url(scenario_name: str | None = None) -> dict[str, str]:
    """Link to the SCENARIO_PARAM CAS table (optionally naming the scenario)."""
    if scenario_name:
        label = (f"Open {SCENARIO_CASLIB} / {SCENARIO_TABLE} in Data Explorer "
                 f"and filter by scenario_nm='{scenario_name}'")
    else:
        label = f"Open {SCENARIO_CASLIB} / {SCENARIO_TABLE} in Data Explorer"
    return link_fields(label, f"{_ep()}/SASDataExplorer/")


def scenario_note() -> str:
    """Honest note: sashdat config edit vs. product UI visibility."""
    return (
        "The scenario is created in Public/SCENARIO_PARAM (the detection-engine config table). "
        "It is visible in Data Explorer, but will NOT appear in the Visual Investigator "
        "scenario-administration UI until the AML Alert Generation Process / MAS reloads the config."
    )


def strategy_url(strategy_id: str | None = None) -> dict[str, str]:
    """Link to Visual Investigator for a triage strategy."""
    base = f"{_ep()}/SASVisualInvestigator/"
    if strategy_id:
        return link_fields(f"Open strategy {strategy_id} in Visual Investigator", base)
    return link_fields("Open Visual Investigator", base)


def flow_url(flow_name: str | None = None, flow_id: str | None = None) -> dict[str, str]:
    """Link to the Visual Investigator Flows admin (VSD detection flows).

    This is the page where a created flow IS visible AND investigators can open
    and edit it — the Financial Crimes Visual Scenario Designer (svi-vsd-service).
    Also emits the flow's REST URL as a guaranteed verification link.
    """
    url = f"{_ep()}/SASVisualInvestigator/admin.html#/admin-scenario-alerts/flows"
    api = f"/svi-vsd-service/flows/{flow_id}" if flow_id else None
    label = (f"Open flow '{flow_name}' in the Flows admin (investigators can edit)"
             if flow_name else "Open Flows admin (detection flows)")
    return link_fields(label, url, api)


def approval_item_url(entity_type: str, doc_id: str) -> dict[str, str]:
    """Link to a VI approval item (inquiry/case document) for a change awaiting review.

    Opens the document in Visual Investigator where an approver reviews and actions
    it in the workflow queue. Also emits the datahub REST URL as a verify link.
    """
    ui = f"{_ep()}/SASVisualInvestigator/index.html#/document/{entity_type}/{doc_id}"
    api = f"/svi-datahub/documents/{entity_type}"
    label = f"Open approval item {entity_type}/{doc_id} in Visual Investigator (for approver review)"
    return link_fields(label, ui, api)


def routing_rule_url(routing_rule_id: str | None = None) -> dict[str, str]:
    """Verification link for an svi-alert routing rule.

    This deployment has NO admin UI page for routing rules (they are managed via
    the svi-alert API / SVI transport), so the primary link is the rule's REST URL
    — open it logged in to see the rule as JSON.
    """
    if routing_rule_id:
        api = f"/svi-alert/routingRules/{routing_rule_id}"
        return link_fields(
            f"View routing rule {routing_rule_id} via API (no dedicated admin UI page)",
            f"{_ep()}{api}", api,
        )
    return link_fields("Open Visual Investigator", f"{_ep()}/SASVisualInvestigator/")


def model_url(model_id: str | None = None) -> dict[str, str]:
    """Link to SAS Model Manager (deep link to the model when id is known)."""
    if model_id:
        url = f"{_ep()}/SASModelManager/models/{quote(str(model_id), safe='')}"
        return link_fields(f"Open model {model_id} in Model Manager", url)
    return link_fields("Open Model Manager", f"{_ep()}/SASModelManager/")


def ml_project_url(project_id: str | None = None) -> dict[str, str]:
    """Link to SAS Model Studio for an ML-pipeline/analytics project."""
    base = f"{_ep()}/SASModelStudio/"
    if project_id:
        return link_fields(f"Open project {project_id} in Model Studio", base)
    return link_fields("Open Model Studio", base)


def report_url(report_id: str) -> dict[str, str]:
    """Deep link to a Visual Analytics report (stable across versions)."""
    rid = str(report_id).split("/")[-1]  # accept a bare id or a /reports/reports/{id} uri
    url = f"{_ep()}/SASVisualAnalytics/?reportUri=%2Freports%2Freports%2F{quote(rid, safe='')}"
    return link_fields(f"Open report {rid} in Visual Analytics", url)


def content_file_url(folder_path: str, filename: str | None = None) -> dict[str, str]:
    """Link to SAS Studio's Explorer for a SAS Content folder/file.

    SAS Studio has no stable per-file deep link across Viya releases, so this
    opens SAS Studio; the label names the exact SAS Content path to expand in
    the Explorer tree.
    """
    target = f"{folder_path}/{filename}" if filename else folder_path
    label = f"Open SAS Content path {target} in SAS Studio Explorer"
    return link_fields(label, f"{_ep()}/SASStudio/")


def job_url(job_id: str | None = None) -> dict[str, str]:
    """Link to SAS Job Execution (jobs are transient; app root + id label)."""
    base = f"{_ep()}/SASJobExecution/"
    if job_id:
        return link_fields(f"Open Job Execution (job {job_id})", base)
    return link_fields("Open Job Execution", base)
