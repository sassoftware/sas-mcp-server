# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 11 — Model Lifecycle (SAS Model Manager) tools.

The steps after a model is trained: register it, keep it in a project, make it
the champion, publish it, check the published copy, and monitor its
performance with KPI alert rules. Scoring and inspecting a single model are
Tier 6's.

Registration runs PROC REGISTERMODEL in a compute session of its own, so an
analytic store goes from CAS to the session's WORK directory and from there to
the repository without passing through the client, and the procedure writes the
variables, properties and assessment files itself. It takes analytic stores and
DATA step score code: SAS models. Python models are not supported yet.
"""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import httpx
from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from .. import config
from ..helpers import model_helpers as mh
from ..viya_client import contains_filter, filter_literal, get_paged_items, raise_for_viya_status
from ._common import coerce_json_list, make_session_helpers

_SAS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")
_KEYWORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
TARGET_LEVELS = ("binary", "nominal", "interval")
_MAX_CODE_LINE = 32000
_REGISTERED = re.compile(r"Model ID\s+([0-9a-fA-F-]{36})")
_JSON = {"Accept": "application/json"}


def _put_lines(code: str) -> list[str]:
    """DATA step PUT statements that write *code* to a file, one line each, no macro resolution."""
    lines = code.replace("\r\n", "\n").split("\n")
    too_long = [i + 1 for i, line in enumerate(lines) if len(line) > _MAX_CODE_LINE]
    if too_long:
        raise mh.ModelError(f"Line {too_long[0]} of the score code is longer than {_MAX_CODE_LINE} characters.")
    return [f"  put {mh.sas_literal(line) if line else repr(' ')};" for line in lines]


def registration_code(
    *,
    name: str,
    description: str,
    algorithm: str | None,
    function: str | None,
    modeler: str | None,
    replace: bool,
    project_id: str | None,
    project_name: str | None,
    project_folder: str | None,
    repository: str,
    astore_caslib: str | None,
    astore_table: str | None,
    score_code: str | None,
    data_caslib: str | None,
    data_table: str | None,
    target_variable: str | None,
    target_level: str | None,
    target_event: str | None,
    assessment: bool,
) -> str:
    """The SAS program that registers one model with PROC REGISTERMODEL."""
    code = [
        "options validvarname=any validmemname=extend nosyntaxcheck;",
        "cas _mcpreg sessopts=(caslib=casuser);",
        # PROC REGISTERMODEL needs a CASUSER libref, which its documentation does
        # not say ("ERROR: Libref CASUSER is not assigned").
        "libname casuser cas caslib=casuser;",
        "%let _mcpwd=%sysfunc(pathname(work));",
    ]
    if astore_table:
        code += [
            f"libname _mcpast cas caslib={mh.sas_literal(astore_caslib or 'casuser')};",
            "proc astore;",
            f"  download rstore=_mcpast.{mh.sas_literal(astore_table)}n store=\"&_mcpwd./mcp_model.sasast\";",
            "run;",
        ]
    else:
        code += ['filename _mcpcode "&_mcpwd./mcp_model.sas";', "data _null_;", "  file _mcpcode lrecl=32767;"]
        code += _put_lines(score_code or "")
        code += ["run;"]
    options = [f"name={mh.sas_literal(name)}"]
    if description:
        options.append(f"description={mh.sas_literal(description)}")
    if data_table:
        code.append(f"libname _mcpdat cas caslib={mh.sas_literal(data_caslib or 'casuser')};")
        options.append(f"data=_mcpdat.{mh.sas_literal(data_table)}n")
    if algorithm:
        options.append(f"algorithm={algorithm}")
    if function:
        options.append(f"function={function}")
    if modeler:
        options.append(f"modeler={mh.sas_literal(modeler)}")
    if replace:
        options.append("replace")
    code.append(f"proc registermodel {' '.join(options)};")
    if project_id:
        code.append(f"  project id={mh.sas_literal(project_id)};")
    else:
        project = [f"name={mh.sas_literal(project_name or '')}", f"repository={mh.sas_literal(repository)}"]
        if project_folder:
            project.append(f"folder={mh.sas_literal(project_folder)}")
        code.append(f"  project {' '.join(project)};")
    if astore_table:
        code.append('  astoremodel store="&_mcpwd./mcp_model.sasast";')
    else:
        code.append("  code file=_mcpcode;")
    if target_variable:
        target = f"  target {target_variable}"
        if target_level:
            target += f" / level={target_level}"
            if target_event is not None and target_level.lower() in ("binary", "nominal"):
                target += f" event={mh.sas_literal(target_event)}"
        code.append(target + ";")
    if assessment:
        code.append("  assessment;")
    code += ["run;", "cas _mcpreg terminate;"]
    return "\n".join(code)


def _state_text(text: str) -> str:
    """A state from a plain-text body, or from a JSON one ({"state": ...} or a quoted string)."""
    body = text.strip()
    if body[:1] in ("{", '"'):
        try:
            data = json.loads(body)
        except ValueError:
            return body
        if isinstance(data, dict):
            return str(data.get("state") or data.get("value") or body)
        return str(data)
    return body


# KPI alert rules (/modelManagement/projects/{id}/alertRules). The published
# specification gives no request body; this is the one SAS Model Manager's own
# UI sends, verified on SAS Viya 2026.10: projectId, name, kpi, severity
# (low/medium/high, the UI's "Priority"), operator (greaterThan/lessThan),
# violations (the UI's "Alert after number of occurrences"), value (the
# threshold), kpiCategory, and for drift and fairness kpiVariable and
# kpiVariableRole. Model Manager checks a rule against the latest KPIs as soon
# as it exists. A rule's own `triggered` flag is not reliable: it stayed false
# for a drift rule that the model's health check reported broken, so the tools
# report the health check (/projects/{id}/models/{id}/healthMetrics) instead.
ACCURACY_KPIS: dict[str, str] = {
    "_AUC_": "AUC",
    "_MISC_": "Misclassification",
    "_TPR_": "True positive rate",
    "_FPR_": "False positive rate",
    "_F1_": "F1 score",
    "_ASE_": "Average squared error",
    "_RASE_": "Root average squared error",
    "_RSquared_": "R-squared",
    "_GLOBAL_ACCURACY_": "Global accuracy",
    "_MICRO_AVG_F1_": "Micro-average F1",
    "_MACRO_AVG_F1_": "Macro-average F1",
    "_WEIGHTED_AVG_F1_": "Weighted-average F1",
    "_Cumlift5_": "Cumulative lift 5%",
    "_Cumlift10_": "Cumulative lift 10%",
    "_Cumlift15_": "Cumulative lift 15%",
    "_Cumlift20_": "Cumulative lift 20%",
    "_Cumlift30_": "Cumulative lift 30%",
}
# KPIs of the other project functions Model Manager's UI offers in a rule.
OTHER_KPIS = (
    "Redundancy", "Support", "Recall", "Lift", "Coverage", "Precision", "Number of clusters", "Cluster radius",
    "Average distance", "Separation", "Pixel accuracy", "Mean accuracy", "MIoU", "FWIoU", "AIOU",
    "PURITY_SEPARATION", "MAPE", "WAPE", "MAE", "MSE", "RMSE",
)
FAIRNESS_KPIS: dict[str, str] = {
    "EqualOpportunity": "Equal opportunity",
    "DemographicParity": "Demographic parity",
    "PredictiveParity": "Predictive parity",
    "EqualAccuracy": "Equal accuracy",
    "EqualizedOdds": "Equalized odds",
}
DRIFT_KPI = "devIndex"
_KPI_ALIASES = {
    "misc": "_MISC_", "misclassification rate": "_MISC_", "tpr": "_TPR_", "fpr": "_FPR_", "f1": "_F1_",
    "ase": "_ASE_", "rase": "_RASE_", "r2": "_RSquared_", "rsquared": "_RSquared_", "r-squared": "_RSquared_",
    "drift": DRIFT_KPI, "deviation index": DRIFT_KPI, "devindex": DRIFT_KPI,
}
_OPERATORS = {
    "greaterthan": "greaterThan", "greater": "greaterThan", "above": "greaterThan", ">": "greaterThan",
    "gt": "greaterThan",
    "lessthan": "lessThan", "less": "lessThan", "below": "lessThan", "<": "lessThan", "lt": "lessThan",
}
_OPERATOR_WORDS = {"greaterThan": "Greater than", "lessThan": "Less than"}
SEVERITIES = ("low", "medium", "high")
_UNFINISHED = ("pending", "running", "submitted", "queued", "")
_RULE_FIELDS = (
    ("id", "id"), ("name", "name"), ("kpi", "kpi"), ("kpiCategory", "category"), ("kpiVariable", "variable"),
    ("operator", "operator"), ("value", "threshold"), ("severity", "severity"), ("violations", "occurrences"),
)


def alert_kpi(kpi: str) -> tuple[str, str, str] | None:
    """A KPI as an alert rule takes it — (name, category, label) — from its name or a common alias."""
    text = kpi.strip()
    key = text.lower()
    if key in _KPI_ALIASES:
        text = _KPI_ALIASES[key]
        key = text.lower()
    if key == DRIFT_KPI.lower():
        return DRIFT_KPI, "MODELDRIFT", "Deviation index"
    for name, label in ACCURACY_KPIS.items():
        if key in (name.lower(), name.strip("_").lower(), label.lower()):
            return name, "ACCURACY", label
    for name, label in FAIRNESS_KPIS.items():
        if key in (name.lower(), label.lower()):
            return name, "FAIRNESS", label
    for name in OTHER_KPIS:
        if key == name.lower():
            return name, "ACCURACY", name
    return None


def alert_operator(operator: str) -> str | None:
    """``greaterThan`` or ``lessThan`` from the ways a caller may write them."""
    return _OPERATORS.get(operator.strip().lower().replace(" ", "").replace("_", ""))


def rule_name(name: str | None, label: str, variable: str, operator: str, threshold: float, several: bool) -> str:
    """A rule's name as Model Manager's UI writes it ("AUC Less than 0.9", "Deviation index: LOAN Greater than 0.1")."""
    if name:
        if "{variable}" in name:
            return name.replace("{variable}", variable)
        return f"{name}: {variable}" if several and variable else name
    subject = f"{label}: {variable}" if variable else label
    return f"{subject} {_OPERATOR_WORDS[operator]} {threshold:g}"


def rule_summary(rule: dict[str, Any]) -> dict[str, Any]:
    """An alert rule in the tools' terms (threshold for value, occurrences for violations)."""
    return {out: rule[key] for key, out in _RULE_FIELDS if rule.get(key) not in (None, "")}


def same_rule(rule: dict[str, Any], kpi: str, variable: str, operator: str) -> bool:
    """Whether an existing rule watches the same KPI and variable in the same direction."""
    return (
        (rule.get("kpi") or "").lower() == kpi.lower()
        and (rule.get("kpiVariable") or "").lower() == variable.lower()
        and rule.get("operator") == operator
    )


def _number(value: Any) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def alert_status(item: dict[str, Any]) -> dict[str, Any]:
    """One health-check row: a rule, the KPI's latest value, and Model Manager's verdict."""
    row: dict[str, Any] = {"rule": item.get("alertRuleName"), "kpi": item.get("metric")}
    if item.get("kpiVariable"):
        row["variable"] = item["kpiVariable"]
    row.update({
        "value": _number(item.get("value")),
        "operator": item.get("alertCondition"),
        "threshold": item.get("alertThreshold"),
        "breached": item.get("success") is False,
    })
    return row


def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id", "name", "description", "repositoryId", "folderId", "function", "targetVariable", "targetLevel",
        "targetEventValue", "eventProbabilityVariable", "championModelName", "createdBy", "modifiedTimeStamp",
    )
    return {key: project[key] for key in keys if project.get(key) not in (None, "")}


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 11 (Model Lifecycle) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    # --- repositories and projects ----------------------------------------------------

    @mcp.tool()
    async def list_model_repositories(ctx: Context) -> list[dict[str, Any]]:
        """List the SAS Model Manager repositories (where model projects live).

        ``default`` marks the one new projects and registrations use when a call
        names none (the deployment's ``MCP_DEFAULT_MODEL_REPOSITORY``).
        """
        async with viya_session("list_model_repositories", ctx) as client:
            items, _ = await get_paged_items("/modelRepository/repositories", client, limit=200)
            default = config.MCP_DEFAULT_MODEL_REPOSITORY.lower()
            return [
                {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "description": r.get("description") or "",
                    "default": (r.get("name") or "").lower() == default or r.get("id") == default,
                }
                for r in items
            ]

    @mcp.tool()
    async def list_model_projects(
        ctx: Context,
        repository: str | None = None,
        name_filter: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List SAS Model Manager projects, most recently changed first.

        Args:
            repository: Only this repository (name or ID); all when omitted.
            name_filter: Only projects whose name contains this text.
            limit: Maximum projects (default 50).
        """
        async with viya_session("list_model_projects", ctx) as client:
            parts = []
            if repository:
                try:
                    repo = await mh.find_repository(client, repository)
                except mh.ModelError as exc:
                    return [{"status": "not_found", "message": str(exc)}]
                parts.append(f"eq(repositoryId,'{filter_literal(repo['id'])}')")
            if name_filter:
                parts.append(contains_filter(name_filter))
            filters = parts[0] if len(parts) == 1 else (f"and({','.join(parts)})" if parts else None)
            items, _ = await get_paged_items(
                "/modelRepository/projects",
                client,
                limit=limit,
                filters=filters,
                extra_params={"sortBy": "modifiedTimeStamp:descending"},
            )
            return [_project_summary(p) for p in items]

    @mcp.tool()
    async def create_model_project(
        name: str,
        ctx: Context,
        repository: str | None = None,
        folder: str | None = None,
        function: str = "classification",
        target_variable: str | None = None,
        target_level: str | None = None,
        target_event: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a SAS Model Manager project to hold models.

        Projects live in a model repository, not in SAS Content. A project of
        that name already in the repository is returned instead of a second one
        (``status`` ``exists``). ``register_model`` also creates the project
        it names when it does not exist, so this is needed only to prepare one.

        Args:
            name: The project's name.
            repository: Repository name or ID (default: the deployment's
                ``MCP_DEFAULT_MODEL_REPOSITORY``, ``Public`` unless changed; see
                ``list_model_repositories``).
            folder: A folder inside the repository to put the project in,
                created when missing. One folder name, not a path.
            function: ``classification`` (default), ``prediction`` or another
                Model Manager function.
            target_variable: The target the project's models predict.
            target_level: ``binary``, ``nominal`` or ``interval``.
            target_event: The event value, for a binary or nominal target.
            description: Optional description.
        """
        if folder and "/" in folder:
            return {"status": "refused", "message": "folder is one folder name inside the repository, not a path."}
        if target_level and target_level.lower() not in TARGET_LEVELS:
            return {"status": "refused", "message": f"target_level must be one of {', '.join(TARGET_LEVELS)}."}
        async with viya_session("create_model_project", ctx) as client:
            try:
                repo = await mh.find_repository(client, repository)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            existing = await mh.find_project(client, name, repo["id"])
            if existing is not None:
                return {"status": "exists", "repository": repo.get("name"), "project": _project_summary(existing)}
            folder_id = repo.get("folderId")
            if folder:
                folder_id = await _repository_folder(client, repo, folder)
            body: dict[str, Any] = {
                "name": name,
                "description": description,
                "function": function,
                "repositoryId": repo["id"],
                "folderId": folder_id,
            }
            for key, value in (
                ("targetVariable", target_variable),
                ("targetLevel", (target_level or "").lower() or None),
                ("targetEventValue", target_event),
            ):
                if value:
                    body[key] = value
            resp = await client.post(
                f"{config.VIYA_ENDPOINT}/modelRepository/projects",
                json=body,
                headers={"Content-Type": mh.PROJECT_TYPE, **_JSON},
            )
            raise_for_viya_status(resp)
            return {"status": "created", "repository": repo.get("name"), "project": _project_summary(resp.json())}

    async def _repository_folder(client: httpx.AsyncClient, repo: dict[str, Any], name: str) -> str:
        """The ID of the folder *name* directly inside the repository's folder, created when missing."""
        parent = repo["folderId"]
        items, _ = await get_paged_items(
            f"/folders/folders/{parent}/members",
            client,
            limit=50,
            filters=f"and(eq(contentType,'folder'),eq(name,'{filter_literal(name)}'))",
        )
        match = next((m for m in items if (m.get("name") or "").lower() == name.lower()), None)
        if match is not None:
            return (match.get("uri") or "").rsplit("/", 1)[-1]
        resp = await client.post(
            f"{config.VIYA_ENDPOINT}/folders/folders",
            params={"parentFolderUri": f"/folders/folders/{parent}"},
            json={"name": name, "type": "folder"},
            headers={"Content-Type": "application/vnd.sas.content.folder+json", **_JSON},
        )
        raise_for_viya_status(resp)
        return resp.json()["id"]

    @mcp.tool()
    async def set_project_champion(project_id: str, model_id: str, ctx: Context) -> dict[str, Any]:
        """Make a model its project's champion, preparing the project as Model Manager's UI does.

        The champion is the model a project publishes and monitors. Model
        Manager refuses a champion until the project has its target fields
        and its variables, which PROC REGISTERMODEL does not set, so this
        first copies the model's target, event and event probability variable
        to the project where they are missing, and adds the model's input and
        output variables the project lacks. Fields already set are kept.

        Args:
            project_id: The project's ID.
            model_id: A model in that project.
        """
        async with viya_session("set_project_champion", ctx) as client:
            try:
                project, etag = await mh.fetch_project(client, project_id)
                model = await mh.fetch_model(client, model_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            if model.get("projectId") != project_id:
                return {
                    "status": "refused",
                    "message": (
                        f"Model {model.get('name')!r} belongs to project {model.get('projectName')!r}, "
                        "not this one."
                    ),
                }
            fields = mh.project_target_fields(project, model)
            if fields:
                updated = {key: value for key, value in project.items() if key != "links"}
                updated.update(fields)
                resp = await client.put(
                    f"{config.VIYA_ENDPOINT}/modelRepository/projects/{project_id}",
                    json=updated,
                    headers={"Content-Type": mh.PROJECT_TYPE, "If-Match": etag, **_JSON},
                )
                raise_for_viya_status(resp)
            variables, _ = await get_paged_items(
                f"/modelRepository/projects/{project_id}/variables", client, limit=1000
            )
            new_variables = mh.missing_project_variables(variables, model)
            if new_variables:
                resp = await client.post(
                    f"{config.VIYA_ENDPOINT}/modelRepository/projects/{project_id}/variables",
                    json={"items": new_variables},
                    headers={"Content-Type": mh.COLLECTION_TYPE, **_JSON},
                )
                raise_for_viya_status(resp)
            resp = await client.post(
                f"{config.VIYA_ENDPOINT}/modelRepository/projects/{project_id}/champion",
                params={"modelId": model_id},
                headers=_JSON,
            )
            raise_for_viya_status(resp)
            return {
                "status": "champion_set",
                "project": project.get("name"),
                "champion": model.get("name"),
                "model_id": model_id,
                "project_fields_set": fields,
                "variables_added": [v["name"] for v in new_variables],
            }

    # --- registration -----------------------------------------------------------------

    @mcp.tool()
    async def describe_analytic_store(caslib_name: str, table_name: str, ctx: Context) -> dict[str, Any]:
        """Describe an analytic store (a model saved in CAS): its inputs, outputs and algorithm.

        Runs PROC ASTORE DESCRIBE. Use it before ``register_model`` to see
        which variables the model expects and produces, and so which target
        and event to register it with.

        Args:
            caslib_name: Caslib of the analytic store table.
            table_name: The analytic store table (for example one written by
                a ``savestate rstore=`` statement).
        """
        code = "\n".join([
            "options validmemname=extend nosyntaxcheck;",
            "cas _mcpdesc;",
            f"libname _mcpast cas caslib={mh.sas_literal(caslib_name)};",
            "proc astore;",
            f"  describe rstore=_mcpast.{mh.sas_literal(table_name)}n;",
            "run;",
            "cas _mcpdesc terminate;",
        ])
        async with viya_session("describe_analytic_store", ctx) as client:
            token = await get_token(ctx)
            run = await mh.run_sas(client, token, code)
        errors = mh.sas_errors(run)
        if errors:
            return {"status": "failed", "store": f"{caslib_name}.{table_name}", "log_errors": errors}
        return {"status": "ok", "store": f"{caslib_name}.{table_name}", "description": run.get("listing", "")}

    @mcp.tool()
    async def register_model(
        name: str,
        ctx: Context,
        project: str | None = None,
        project_id: str | None = None,
        repository: str | None = None,
        project_folder: str | None = None,
        astore_caslib: str | None = None,
        astore_table: str | None = None,
        score_code: str | None = None,
        data_caslib: str | None = None,
        data_table: str | None = None,
        target_variable: str | None = None,
        target_level: str | None = None,
        target_event: str | None = None,
        algorithm: str | None = None,
        function: str | None = None,
        description: str = "",
        modeler: str | None = None,
        assessment: bool | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Register a SAS model in SAS Model Manager with PROC REGISTERMODEL.

        Takes either an analytic store in CAS (``astore_caslib`` +
        ``astore_table``, as written by ``savestate rstore=``) or DATA step
        score code (``score_code``). Not Python or DS2 models. The procedure
        stores the model's variables, properties and score code, and with
        ``assessment`` its fit statistics, ROC and lift. It runs in a compute
        session of its own and takes 5–30 seconds.

        The project is ``project_id``, or the project named ``project`` in the
        repository (created there when missing, in ``project_folder``).

        A model with the same name already in the project is **refused**
        unless ``replace`` is true. Replacing deletes that model and creates
        one with a new ID, which loses its score tests, publish history and
        monitoring: ask the user before passing ``replace``.

        Args:
            name: The model's name in the project.
            project: Project name (found or created in ``repository``).
            project_id: An existing project's ID, instead of ``project``.
            repository: Repository name (default: the deployment's
                ``MCP_DEFAULT_MODEL_REPOSITORY``, ``Public`` unless changed).
            project_folder: Folder in the repository for a new project; one
                folder name, not a path.
            astore_caslib: Caslib of the analytic store table.
            astore_table: The analytic store table.
            score_code: DATA step score code, instead of an analytic store.
            data_caslib: Caslib of the training or validation table.
            data_table: The table the model was trained on. It defines the
                input variables of DATA step code and is needed for assessment.
            target_variable: The target variable.
            target_level: ``binary``, ``nominal`` or ``interval``.
            target_event: The event value of a binary or nominal target, such as ``1``.
            algorithm: The algorithm, as a keyword: TREE, FOREST, GRADBOOST,
                LOGISTIC, REGRESSION, NEURAL, SVM, ...
            function: CLASSIFICATION or PREDICTION, as a keyword.
            description: Optional description.
            modeler: Optional name of who built the model.
            assessment: Compute fit statistics, ROC and lift (default: when a
                data table and a target are given).
            replace: Replace a model of the same name (see above).
        """
        if bool(astore_table) == bool(score_code):
            return {
                "status": "refused",
                "message": "Give either astore_table (an analytic store) or score_code (DATA step code), not both.",
            }
        if not project and not project_id:
            return {"status": "refused", "message": "Give project (a project name) or project_id."}
        if project_folder and "/" in project_folder:
            return {
                "status": "refused",
                "message": "project_folder is one folder name inside the repository, not a path.",
            }
        level = (target_level or "").lower() or None
        if level and level not in TARGET_LEVELS:
            return {"status": "refused", "message": f"target_level must be one of {', '.join(TARGET_LEVELS)}."}
        if level in ("binary", "nominal") and target_event is None:
            return {"status": "refused", "message": f"A {level} target needs target_event, the event value."}
        if target_variable and not _SAS_NAME.fullmatch(target_variable):
            return {"status": "refused", "message": f"{target_variable!r} is not a SAS variable name."}
        for label, keyword in (("algorithm", algorithm), ("function", function)):
            if keyword and not _KEYWORD.fullmatch(keyword):
                return {"status": "refused", "message": f"{label} must be one keyword, such as TREE or CLASSIFICATION."}
        assess = bool(data_table and target_variable) if assessment is None else assessment
        if assess and not (data_table and target_variable):
            return {"status": "refused", "message": "assessment needs data_table and target_variable."}
        async with viya_session("register_model", ctx) as client:
            token = await get_token(ctx)
            try:
                repo = await mh.find_repository(client, repository)
                existing_project = (
                    (await mh.fetch_project(client, project_id))[0]
                    if project_id
                    else await mh.find_project(client, project or "", repo["id"])
                )
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            replaced: list[str] = []
            if existing_project is not None:
                same = await mh.models_in_project(client, existing_project["id"], name)
                if same and not replace:
                    return {
                        "status": "exists",
                        "message": (
                            f"Project {existing_project.get('name')!r} already has a model named {name!r} "
                            f"(ID {same[0].get('id')}). Register it under another name, or ask the user "
                            "whether to replace it: replacing gives it a new ID and loses its score tests, "
                            "publish history and monitoring."
                        ),
                        "existing_model_id": same[0].get("id"),
                    }
                replaced = [m.get("id") for m in same]
            try:
                code = registration_code(
                    name=name,
                    description=description,
                    algorithm=algorithm.upper() if algorithm else None,
                    function=function.upper() if function else None,
                    modeler=modeler,
                    replace=replace,
                    project_id=existing_project["id"] if existing_project else None,
                    project_name=project,
                    project_folder=project_folder,
                    repository=repo.get("name", "Public"),
                    astore_caslib=astore_caslib,
                    astore_table=astore_table,
                    score_code=score_code,
                    data_caslib=data_caslib,
                    data_table=data_table,
                    target_variable=target_variable,
                    target_level=level.upper() if level else None,
                    target_event=target_event,
                    assessment=assess,
                )
            except mh.ModelError as exc:
                return {"status": "refused", "message": str(exc)}
            run = await mh.run_sas(client, token, code)
            errors = mh.sas_errors(run)
            if errors:
                return {"status": "failed", "log_errors": errors}
            found = _REGISTERED.search(run.get("listing", ""))
            model: dict[str, Any] | None = None
            if found:
                model = await mh.fetch_model(client, found.group(1))
            else:
                target_project = existing_project or await mh.find_project(client, project or "", repo["id"])
                if target_project is not None:
                    models = await mh.models_in_project(client, target_project["id"], name)
                    model = models[0] if models else None
            if model is None:
                return {
                    "status": "unknown",
                    "message": "The procedure ran without errors, but the model was not found.",
                    "listing": run.get("listing", ""),
                }
            result: dict[str, Any] = {"status": "registered", "model": mh.model_summary(model)}
            if replaced:
                result["replaced_model_ids"] = replaced
                result["note"] = "The model of that name was replaced; it has a new ID now."
            if not model.get("targetVariable") or model.get("role") != "champion":
                result["next"] = (
                    "To publish or monitor it as the project's main model, call set_project_champion, which "
                    "also fills in the project's target fields that PROC REGISTERMODEL leaves empty."
                )
            return result

    # --- publishing -------------------------------------------------------------------

    @mcp.tool()
    async def publish_model(
        model_id: str,
        ctx: Context,
        destination_name: str | None = None,
        publish_name: str | None = None,
        replace: bool = False,
        dry_run: bool = False,
        git_remote_path: str | None = None,
    ) -> dict[str, Any]:
        """Publish a registered model to a destination: MAS, CAS, a container registry, Git, ...

        Any model in the Model Repository can be published, whatever made it.
        With ``dry_run`` the tool only reports what it would do: the
        destination and its type, the published name, and whether a published
        model of that name is already there. A published model of the same
        name is not replaced unless ``replace`` is true. Publishing runs in the
        background; ``list_published_models`` shows its state and
        ``validate_published_model`` checks the published copy.

        Args:
            model_id: The model's ID.
            destination_name: Destination (see ``list_publishing_destinations``).
                Default: the deployment's ``MCP_DEFAULT_PUBLISH_DESTINATION``,
                ``maslocal`` unless an administrator changed it.
            publish_name: The published name (default: the model's name, with
                characters a destination refuses turned into underscores).
            replace: Replace a published model of the same name.
            dry_run: Only report what would happen.
            git_remote_path: For a Git destination: the directory in the
                repository to publish to.
        """
        destination = destination_name or config.MCP_DEFAULT_PUBLISH_DESTINATION
        async with viya_session("publish_model", ctx) as client:
            try:
                model = await mh.fetch_model(client, model_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            resp = await client.get(f"{config.VIYA_ENDPOINT}/modelPublish/destinations/{destination}", headers=_JSON)
            if resp.status_code == 404:
                return {
                    "status": "not_found",
                    "message": (
                        f"No publishing destination is named {destination!r}. "
                        "list_publishing_destinations lists them."
                    ),
                }
            raise_for_viya_status(resp)
            dest_type = resp.json().get("destinationType")
            name = mh.publish_name(publish_name or model.get("name", "model"))
            existing, _ = await get_paged_items(
                "/modelPublish/models",
                client,
                limit=10,
                filters=f"and(eq(destinationName,'{filter_literal(destination)}'),eq(name,'{filter_literal(name)}'))",
            )
            plan = {
                "model": model.get("name"),
                "model_id": model_id,
                "destination": destination,
                "destination_type": dest_type,
                "publish_name": name,
                "already_published": [
                    {"id": e.get("id"), "state": e.get("state"), "sourceUri": e.get("sourceUri")} for e in existing
                ],
            }
            if existing and not replace:
                return {
                    **plan,
                    "status": "exists",
                    "message": (
                        f"{destination} already has a published model named {name!r}. Ask the user whether to "
                        "replace it (replace=true) or choose another publish_name."
                    ),
                }
            if dest_type == "git" and not git_remote_path:
                return {**plan, "status": "refused", "message": "A Git destination needs git_remote_path."}
            if dry_run:
                return {**plan, "status": "dry_run", "would_replace": bool(existing)}
            body: dict[str, Any] = {
                "name": name,
                "notes": model.get("description") or "",
                "destinationName": destination,
                "modelContents": [{
                    "modelName": name,
                    "sourceUri": f"/modelRepository/models/{model_id}",
                    "publishLevel": "model",
                }],
            }
            if git_remote_path:
                body["properties"] = {"gitRemotePath": git_remote_path}
            resp = await client.post(
                f"{config.VIYA_ENDPOINT}/modelManagement/publish",
                params={"force": "true" if replace else "false"},
                json=body,
                headers={"Content-Type": mh.PUBLISH_REQUEST_TYPE, "Accept": mh.PUBLISH_RESPONSE_TYPE},
            )
            raise_for_viya_status(resp)
            data = resp.json() if resp.content else {}
            published = data.get("items", [data]) if isinstance(data, dict) else data
            return {
                **plan,
                "status": "publishing",
                "published": [
                    {key: p.get(key) for key in ("id", "name", "state", "publishType", "destinationName")}
                    for p in published if isinstance(p, dict)
                ],
                "next": "list_published_models shows when it has completed; validate_published_model checks it.",
            }

    @mcp.tool()
    async def validate_published_model(
        caslib_name: str,
        table_name: str,
        ctx: Context,
        published_model_id: str | None = None,
        model_id: str | None = None,
        destination_name: str | None = None,
        rows: int = 10,
        timeout_seconds: int = 180,
    ) -> dict[str, Any]:
        """Check a published model by scoring a few rows with the published copy.

        Model Manager's publish validation: the first ``rows`` rows of the
        table are scored in the destination itself (MAS, CAS, ...), and the
        scored rows come back. Compare them with ``test_score_model`` on the
        registered model to confirm the copy behaves the same. Name the copy
        by ``published_model_id`` (see ``list_published_models``), or by
        ``model_id`` and ``destination_name``.

        Args:
            caslib_name: Caslib of the input table.
            table_name: A loaded CAS table holding the model's inputs.
            published_model_id: The published copy's ID.
            model_id: The registered model, instead of published_model_id.
            destination_name: With model_id (default: the deployment's
                ``MCP_DEFAULT_PUBLISH_DESTINATION``).
            rows: How many rows to score (default 10, at most 100).
            timeout_seconds: How long to wait (default 180).
        """
        async with viya_session("validate_published_model", ctx) as client:
            token = await get_token(ctx)
            if published_model_id:
                resp = await client.get(
                    f"{config.VIYA_ENDPOINT}/modelPublish/models/{published_model_id}", headers=_JSON
                )
                if resp.status_code == 404:
                    return {"status": "not_found", "message": f"No published model {published_model_id!r}."}
                raise_for_viya_status(resp)
                published = resp.json()
            elif model_id:
                destination = destination_name or config.MCP_DEFAULT_PUBLISH_DESTINATION
                items, _ = await get_paged_items(
                    "/modelPublish/models",
                    client,
                    limit=200,
                    filters=f"eq(destinationName,'{filter_literal(destination)}')",
                    extra_params={"sortBy": "modifiedTimeStamp:descending"},
                )
                source = f"/modelRepository/models/{model_id}"
                published = next((p for p in items if p.get("sourceUri") == source), None)
                if published is None:
                    return {
                        "status": "not_found",
                        "message": (
                            f"Model {model_id} has no published copy in {destination!r}. "
                            "Publish it with publish_model."
                        ),
                    }
            else:
                return {"status": "refused", "message": "Give published_model_id, or model_id and destination_name."}
            if published.get("state") not in ("completed", None):
                return {
                    "status": "not_ready",
                    "message": f"The published model is {published.get('state')!r}; validate it once it has completed.",
                }
            source_uri = published.get("sourceUri") or ""
            input_variables: list[str] = []
            extra = {"publishDestination": published.get("destinationName", ""), "modelName": published.get("name", "")}
            if source_uri.startswith("/modelRepository/models/"):
                try:
                    model = await mh.fetch_model(client, source_uri.rsplit("/", 1)[-1])
                    input_variables = [v["name"] for v in model.get("inputVariables") or [] if v.get("name")]
                    extra["modelOrigUri"] = source_uri
                    if model.get("projectId"):
                        extra["projectUri"] = f"/modelRepository/projects/{model['projectId']}"
                except mh.ModelError:
                    pass
            result = await mh.score_test(
                client,
                token,
                object_type="sas.publish.validation",
                object_uri=f"/modelPublish/models/{published['id']}",
                object_name=published.get("name", published["id"]),
                input_variables=input_variables,
                caslib_name=caslib_name,
                table_name=table_name,
                rows=rows,
                timeout_seconds=timeout_seconds,
                extra_properties=extra,
            )
            return {
                "published_model_id": published.get("id"),
                "publish_name": published.get("name"),
                "destination": published.get("destinationName"),
                **result,
            }

    # --- performance monitoring -------------------------------------------------------

    @mcp.tool()
    async def create_performance_definition(
        project_id: str,
        data_caslib: str,
        data_prefix: str,
        ctx: Context,
        model_ids: Annotated[list[str] | None, BeforeValidator(coerce_json_list)] = None,
        name: str | None = None,
        input_variables: Annotated[list[str] | None, BeforeValidator(coerce_json_list)] = None,
        output_variables: Annotated[list[str] | None, BeforeValidator(coerce_json_list)] = None,
        score_data: bool = True,
        monitor_champion: bool = False,
        monitor_challengers: bool = False,
        max_bins: int = 10,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Define how a project's models are monitored over time (a performance definition).

        The data comes as one CAS table per period in ``data_caslib``, named
        ``<data_prefix>_<n>_<label>``: ``CREDIT_1_Q1``, ``CREDIT_2_Q2``, ... Each
        holds the model's inputs and the actual target. With ``score_data``
        (the default) Model Manager scores them itself; otherwise they must
        already hold the model's outputs. Results go to the
        ``ModelPerformanceData`` caslib, and ``run_performance_definition``
        computes them.

        The project needs its target fields and a champion first:
        ``set_project_champion`` sets both. A project has one performance
        definition: when it has one already, that one is returned, and
        ``replace`` changes it to these settings instead (it keeps its ID).

        Args:
            project_id: The project.
            data_caslib: Caslib of the period tables.
            data_prefix: Their common prefix.
            model_ids: Models to monitor (default: the project's champion).
            name: Definition name (default: "<project> performance").
            input_variables: Inputs to check for drift (default: the first
                model's inputs).
            output_variables: Outputs to assess (default: the event
                probability variable, else the model's outputs).
            score_data: Score the period tables first (default true).
            monitor_champion: Also follow whichever model is champion later.
            monitor_challengers: Also monitor the project's challengers.
            max_bins: Bins for the drift statistics (default 10).
            replace: Change the project's existing definition to these settings.
        """
        async with viya_session("create_performance_definition", ctx) as client:
            try:
                project, _ = await mh.fetch_project(client, project_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            if not project.get("targetVariable"):
                return {
                    "status": "refused",
                    "message": (
                        "The project has no target variable. Call set_project_champion first: "
                        "it fills in the target fields."
                    ),
                }
            existing = await _definitions(client, project_id)
            if existing and not replace:
                return {
                    "status": "exists",
                    "performance_definition": existing[0],
                    "message": (
                        "A project has one performance definition, and this one has it. Run it "
                        "(run_performance_definition), or pass replace=true to change it to these settings."
                    ),
                }
            ids = list(model_ids or [])
            if not ids:
                resp = await client.get(
                    f"{config.VIYA_ENDPOINT}/modelRepository/projects/{project_id}/champion", headers=_JSON
                )
                if resp.status_code == 404:
                    return {
                        "status": "refused",
                        "message": "The project has no champion. Call set_project_champion, or pass model_ids.",
                    }
                raise_for_viya_status(resp)
                ids = [resp.json()["id"]]
            try:
                model = await mh.fetch_model(client, ids[0])
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            inputs = list(input_variables or [v["name"] for v in model.get("inputVariables") or [] if v.get("name")])
            outputs = list(output_variables or [])
            if not outputs:
                outputs = [model["eventProbVar"]] if model.get("eventProbVar") else [
                    v["name"] for v in model.get("outputVariables") or [] if v.get("name")
                ]
            tables, _ = await get_paged_items(
                f"/casManagement/servers/{mh.CAS_SERVER}/caslibs/{data_caslib}/tables",
                client,
                limit=100,
                filters=f"startsWith(name,'{filter_literal(data_prefix.upper())}_')",
            )
            body = {
                "name": name or f"{project.get('name')} performance",
                "projectId": project_id,
                "modelIds": ids,
                "inputVariables": inputs,
                "outputVariables": outputs,
                "dataLibrary": data_caslib,
                "dataPrefix": data_prefix,
                "resultLibrary": "ModelPerformanceData",
                "casServerId": mh.CAS_SERVER,
                "scoreExecutionRequired": score_data,
                "championMonitored": monitor_champion,
                "challengerMonitored": monitor_challengers,
                "maxBins": max_bins,
                "includeAllData": True,
                "loadPerformanceResult": True,
                "performanceResultSaved": True,
                "traceOn": False,
            }
            if existing:
                path = f"{config.VIYA_ENDPOINT}/modelManagement/performanceTasks/{existing[0]['id']}"
                current = await client.get(path, headers=_JSON)
                raise_for_viya_status(current)
                merged = {k: v for k, v in current.json().items() if k != "links"}
                merged.update(body)
                resp = await client.put(
                    path,
                    json=merged,
                    headers={
                        "Content-Type": mh.PERFORMANCE_TASK_TYPE,
                        "If-Match": current.headers.get("etag", "*"),
                        **_JSON,
                    },
                )
            else:
                resp = await client.post(
                    f"{config.VIYA_ENDPOINT}/modelManagement/performanceTasks",
                    json=body,
                    headers={"Content-Type": mh.PERFORMANCE_TASK_TYPE, **_JSON},
                )
            raise_for_viya_status(resp)
            task = resp.json()
            result = {
                "status": "replaced" if existing else "created",
                "performance_definition_id": task.get("id"),
                "name": task.get("name"),
                "models": ids,
                "input_variables": inputs,
                "output_variables": outputs,
                "period_tables": [t.get("name") for t in tables],
                "next": "run_performance_definition computes the results.",
            }
            if not tables:
                result["warning"] = (
                    f"No tables named {data_prefix.upper()}_<n>_<label> were found in {data_caslib}; "
                    "the run will find nothing until they exist."
                )
            return result

    async def _definitions(client: httpx.AsyncClient, project_id: str) -> list[dict[str, Any]]:
        """A project's performance definitions."""
        items, _ = await get_paged_items(
            "/modelManagement/performanceTasks",
            client,
            limit=50,
            filters=f"eq(projectId,'{filter_literal(project_id)}')",
        )
        return [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "models": t.get("modelIds") or [],
                "data_caslib": t.get("dataLibrary"),
                "data_prefix": t.get("dataPrefix"),
            }
            for t in items
        ]

    async def _calculate_kpis(client: httpx.AsyncClient, project_id: str) -> list[dict[str, Any]]:
        """Have Model Manager calculate the project's KPIs from its performance results."""
        resp = await client.post(
            f"{config.VIYA_ENDPOINT}/modelManagement/projects/{project_id}/kpis",
            json={},
            headers={"Content-Type": "application/json", "Accept": mh.COLLECTION_TYPE},
        )
        if resp.status_code == 404:  # no performance results yet
            return []
        raise_for_viya_status(resp)
        return resp.json().get("items", [])

    async def _alerts(client: httpx.AsyncClient, project_id: str, model_id: str | None) -> dict[str, Any]:
        """How the champion (or a model) stands against the project's KPI alert rules."""
        label = model_id
        if not model_id:
            resp = await client.get(
                f"{config.VIYA_ENDPOINT}/modelRepository/projects/{project_id}/champion", headers=_JSON
            )
            if resp.status_code == 404:
                return {
                    "alerts": [],
                    "alerts_note": "The project has no champion: pass model_id to check a model against the rules.",
                }
            raise_for_viya_status(resp)
            champion = resp.json()
            model_id, label = champion.get("id"), champion.get("name")
        resp = await client.get(
            f"{config.VIYA_ENDPOINT}/modelManagement/projects/{project_id}/models/{model_id}/healthMetrics",
            params={"limit": 500},
            headers=_JSON,
        )
        if resp.status_code == 404:
            return {"alerts": [], "alerts_note": f"No model {model_id!r} in this project."}
        raise_for_viya_status(resp)
        data = resp.json()
        result: dict[str, Any] = {
            "alerts_for_model": label,
            "alerts": [alert_status(item) for item in data.get("items", []) if item.get("alertRuleName")],
        }
        if not data.get("kpisAvailable", True):
            result["alerts_note"] = "No KPIs yet: the rules are checked once a performance definition has run."
        return result

    @mcp.tool()
    async def run_performance_definition(
        ctx: Context,
        performance_definition_id: str | None = None,
        project_id: str | None = None,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        """Run a performance definition: score and assess each period table, then calculate the KPIs.

        Name the definition, or the project when it has just one. The run
        goes on in the background. With ``wait_seconds`` the tool waits up to
        that long (at most 600); when the run has finished it has Model
        Manager calculate the project's KPIs, as Model Manager's own
        monitoring workflow does, and reports the KPI alert rules the
        champion breaks. Otherwise call again with ``wait_seconds``, or read
        ``get_project_kpis`` later. Runs over two small periods took from
        half a minute to two minutes on SAS Viya 2026.10.

        Args:
            performance_definition_id: From ``create_performance_definition``.
            project_id: The project instead, when it has one definition.
            wait_seconds: How long to wait for the run (default 0).
        """
        async with viya_session("run_performance_definition", ctx) as client:
            if not performance_definition_id:
                if not project_id:
                    return {"status": "refused", "message": "Give performance_definition_id or project_id."}
                definitions = await _definitions(client, project_id)
                if len(definitions) != 1:
                    return {
                        "status": "choose" if definitions else "refused",
                        "definitions": definitions,
                        "message": (
                            "The project has several performance definitions: pass performance_definition_id."
                            if definitions
                            else "The project has no performance definition: create_performance_definition makes one."
                        ),
                    }
                performance_definition_id = definitions[0]["id"]
            base = f"/modelManagement/performanceTasks/{performance_definition_id}"
            resp = await client.get(f"{config.VIYA_ENDPOINT}{base}", headers=_JSON)
            if resp.status_code == 404:
                return {"status": "not_found", "message": f"No performance definition {performance_definition_id!r}."}
            raise_for_viya_status(resp)
            task = resp.json()
            resp = await client.post(f"{config.VIYA_ENDPOINT}{base}", headers=_JSON)
            raise_for_viya_status(resp)

            async def state() -> str:
                # Answers text/plain or, while a run starts, application/json
                # (a text/plain-only Accept then gets 406 on SAS Viya 2026.10).
                r = await client.get(f"{config.VIYA_ENDPOINT}{base}/state", headers={"Accept": "text/plain, */*"})
                raise_for_viya_status(r)
                return _state_text(r.text)

            current = await mh.wait_until_done(state, max(0, min(int(wait_seconds), 600)), interval=5)
            # One job per model, kept across runs: this run's is each model's latest.
            jobs, _ = await get_paged_items(f"{base}/performanceJobs", client, limit=200)
            latest: dict[str, dict[str, Any]] = {}
            for job in jobs:
                key = job.get("modelId") or job.get("id")
                if key not in latest or (job.get("startTime") or "") > (latest[key].get("startTime") or ""):
                    latest[key] = job
            run_jobs = list(latest.values())
            report: dict[str, Any] = {
                "status": current,
                "performance_definition_id": performance_definition_id,
                "name": task.get("name"),
                "project_id": task.get("projectId"),
                "jobs": [
                    {key: j.get(key) for key in ("id", "model", "modelId", "state", "startTime", "stopTime")}
                    for j in run_jobs
                ],
            }
            troubled = [j for j in run_jobs if (j.get("state") or "").lower() not in _UNFINISHED + ("completed",)]
            if troubled:
                report["log_errors"] = {}
                for job in troubled[:3]:
                    log = await client.get(
                        f"{config.VIYA_ENDPOINT}{base}/performanceJobs/{job['id']}/log",
                        headers={"Accept": "text/plain, */*"},
                    )
                    lines = log.text.splitlines() if log.status_code == 200 else []
                    report["log_errors"][job.get("model") or job["id"]] = [
                        line for line in lines if line.startswith("ERROR")
                    ][:15]
            finished = current.replace(" ", "").lower() in ("completed", "succeeded", "completedwitherrors")
            if finished and current.replace(" ", "").lower() == "completedwitherrors":
                report["warning"] = (
                    "The run completed with errors in some jobs (log_errors); the KPIs come from what completed."
                )
            if finished:
                summary = await client.get(
                    f"{config.VIYA_ENDPOINT}{base}/summary",
                    headers={"Accept": f"{mh.PERFORMANCE_SUMMARY_TYPE}, application/json"},
                )
                if summary.status_code == 200:
                    names = {j.get("modelId"): j.get("model") for j in run_jobs}
                    report["summary"] = [
                        {
                            "model": names.get(m.get("modelId")) or m.get("modelId"),
                            "rank": m.get("rank"),
                            "observations": m.get("totalObservations"),
                            "periods": [p.get("name") for p in m.get("periods") or []],
                            "kpi": {k: _number(v) for k, v in (m.get("kpi") or {}).items()},
                        }
                        for m in summary.json().get("models", [])
                    ]
            if finished and task.get("projectId"):
                report["kpis"] = len(await _calculate_kpis(client, task["projectId"]))
                standing = await _alerts(client, task["projectId"], None)
                report["breached_alerts"] = [a for a in standing.get("alerts", []) if a["breached"]]
                if standing.get("alerts_note"):
                    report["alerts_note"] = standing["alerts_note"]
                report["next"] = "get_project_kpis reads the KPIs, the alert rules and how the champion stands."
            return report

    @mcp.tool()
    async def get_project_kpis(
        project_id: str, ctx: Context, model_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Read how a project's models are monitored: KPIs, KPI alert rules, alerts and performance definitions.

        ``kpis`` are the latest values from the performance runs, each with a
        ``name`` (``_AUC_``, ``_MISC_``, ``_F1_``, ``_ASE_``, ...; ``devIndex``
        once per variable for drift), a ``value`` and a ``kpiCategory``.
        ``alert_rules`` are the project's KPI alert rules
        (``set_kpi_alert_rule``), and ``alerts`` checks the champion — or
        ``model_id`` — against each: ``breached`` is Model Manager's own
        verdict on the latest KPI. ``performance_definitions`` are what
        ``run_performance_definition`` runs.

        Args:
            project_id: The project.
            model_id: Check this model against the rules instead of the champion.
            limit: Maximum KPIs (default 100).
        """
        async with viya_session("get_project_kpis", ctx) as client:
            resp = await client.get(
                f"{config.VIYA_ENDPOINT}/modelManagement/projects/{project_id}/kpis",
                params={"limit": limit},
                headers={"Accept": mh.COLLECTION_TYPE},
            )
            if resp.status_code == 404:
                return {"status": "not_found", "message": f"No model project {project_id!r}."}
            raise_for_viya_status(resp)
            data = resp.json()
            kpis = [{k: v for k, v in item.items() if k != "links"} for item in data.get("items", [])]
            rules, _ = await get_paged_items(f"/modelManagement/projects/{project_id}/alertRules", client, limit=500)
            result: dict[str, Any] = {
                "project_id": project_id,
                "count": data.get("count", len(kpis)),
                "kpis": kpis,
                "alert_rules": [rule_summary(r) for r in rules],
            }
            if rules:
                result.update(await _alerts(client, project_id, model_id))
            result["performance_definitions"] = await _definitions(client, project_id)
            if not kpis:
                result["message"] = "No KPIs yet: run a performance definition first (run_performance_definition)."
            return result

    @mcp.tool()
    async def set_kpi_alert_rule(
        project_id: str,
        kpi: str,
        operator: str,
        threshold: float,
        ctx: Context,
        variables: Annotated[list[str] | None, BeforeValidator(coerce_json_list)] = None,
        severity: str = "medium",
        occurrences: int = 1,
        name: str | None = None,
        alert_rule_id: str | None = None,
    ) -> dict[str, Any]:
        """Set a KPI alert rule on a model project: alert when a monitoring KPI crosses a threshold.

        Model Manager checks the project's models against its rules whenever
        their KPIs are calculated (``run_performance_definition``) and lists
        them as the project's KPI rules. ``kpi`` is one of:

        - accuracy: ``AUC``, ``misclassification``, ``TPR``, ``FPR``, ``F1``,
          ``ASE``, ``RASE``, ``R-squared``, ``global accuracy``, cumulative
          lift ``_Cumlift5_`` to ``_Cumlift30_``, or the KPI names
          ``get_project_kpis`` lists;
        - drift: ``devIndex`` (the deviation index) of each variable in
          ``variables``, input or output — one rule per variable;
        - fairness: ``EqualOpportunity``, ``DemographicParity``,
          ``PredictiveParity``, ``EqualAccuracy`` or ``EqualizedOdds`` for the
          sensitive variable in ``variables``.

        ``operator`` is ``greaterThan`` (alert above the threshold, as for
        misclassification or drift) or ``lessThan`` (alert below it, as for
        AUC). A rule for the same KPI, variable and direction is updated, not
        duplicated; ``alert_rule_id`` updates a given rule, its direction too.
        The result says whether the champion breaks the rule now.

        Args:
            project_id: The project.
            kpi: The KPI to watch.
            operator: ``greaterThan`` or ``lessThan``.
            threshold: The value the KPI should not cross.
            variables: The variables of a drift or fairness KPI.
            severity: ``low``, ``medium`` (default) or ``high``: Model
                Manager's priority.
            occurrences: Alert after this many occurrences (default 1).
            name: The rule's name (default as Model Manager names it,
                "AUC Less than 0.9"); ``{variable}`` is replaced by each variable.
            alert_rule_id: A rule to update instead.
        """
        resolved_kpi = alert_kpi(kpi)
        if resolved_kpi is None:
            return {
                "status": "refused",
                "message": (
                    f"Unknown KPI {kpi!r}. Use an accuracy KPI ({', '.join(ACCURACY_KPIS.values())}), devIndex "
                    f"for drift, or a fairness KPI ({', '.join(FAIRNESS_KPIS)})."
                ),
            }
        kpi_name, category, label = resolved_kpi
        op = alert_operator(operator)
        if op is None:
            return {"status": "refused", "message": "operator is greaterThan or lessThan."}
        level = severity.strip().lower()
        if level not in SEVERITIES:
            return {"status": "refused", "message": "severity is low, medium or high."}
        if occurrences < 1:
            return {"status": "refused", "message": "occurrences is 1 or more."}
        wanted = [str(v).strip() for v in variables or [] if str(v).strip()]
        if category != "ACCURACY" and not wanted:
            kind = "drift" if category == "MODELDRIFT" else "fairness"
            return {"status": "refused", "message": f"A {kind} KPI needs the variables it watches (variables)."}
        if category == "ACCURACY" and wanted:
            return {"status": "refused", "message": f"{label} is a KPI of the whole model: it takes no variables."}
        if alert_rule_id and len(wanted) > 1:
            return {"status": "refused", "message": "alert_rule_id updates one rule: give at most one variable."}
        async with viya_session("set_kpi_alert_rule", ctx) as client:
            try:
                project, _ = await mh.fetch_project(client, project_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            targets: list[tuple[str, str]] = [("", "")]
            if wanted:
                project_variables, _ = await get_paged_items(
                    f"/modelRepository/projects/{project_id}/variables", client, limit=1000
                )
                known = {v["name"].lower(): v for v in project_variables if v.get("name")}
                unknown = [v for v in wanted if v.lower() not in known]
                if unknown:
                    return {
                        "status": "refused",
                        "message": f"Not variables of the project: {', '.join(unknown)}.",
                        "project_variables": sorted(v["name"] for v in known.values())[:50],
                    }
                targets = [
                    (
                        known[v.lower()]["name"],
                        "SENSITIVE" if category == "FAIRNESS" else (known[v.lower()].get("role") or "").upper(),
                    )
                    for v in wanted
                ]
            rules_base = f"/modelManagement/projects/{project_id}/alertRules"
            rules, _ = await get_paged_items(rules_base, client, limit=500)
            if alert_rule_id and not any(r.get("id") == alert_rule_id for r in rules):
                return {"status": "not_found", "message": f"The project has no alert rule {alert_rule_id!r}."}
            done: list[dict[str, Any]] = []
            for variable, role in targets:
                body = {
                    "projectId": project_id,
                    "name": rule_name(name, label, variable, op, threshold, len(targets) > 1),
                    "kpi": kpi_name,
                    "severity": level,
                    "operator": op,
                    "violations": occurrences,
                    "value": threshold,
                    "kpiCategory": category,
                    "kpiVariable": variable,
                    "kpiVariableRole": role,
                }
                existing = alert_rule_id or next(
                    (r["id"] for r in rules if same_rule(r, kpi_name, variable, op)), None
                )
                if existing:
                    current = await client.get(f"{config.VIYA_ENDPOINT}{rules_base}/{existing}", headers=_JSON)
                    raise_for_viya_status(current)
                    merged = {k: v for k, v in current.json().items() if k != "links"}
                    merged.update(body)
                    resp = await client.put(
                        f"{config.VIYA_ENDPOINT}{rules_base}/{existing}",
                        json=merged,
                        headers={
                            "Content-Type": "application/json",
                            "If-Match": current.headers.get("etag", "*"),
                            **_JSON,
                        },
                    )
                    action = "updated"
                else:
                    resp = await client.post(
                        f"{config.VIYA_ENDPOINT}{rules_base}",
                        json=body,
                        headers={"Content-Type": "application/json", **_JSON},
                    )
                    action = "created"
                raise_for_viya_status(resp)
                done.append({**rule_summary(resp.json()), "action": action})
            standing = await _alerts(client, project_id, None)
            names = {rule.get("name") for rule in done}
            result: dict[str, Any] = {
                "status": "set",
                "project": project.get("name"),
                "rules": done,
                "now": [a for a in standing.get("alerts", []) if a["rule"] in names],
            }
            if standing.get("alerts_note"):
                result["note"] = standing["alerts_note"]
            return result

    @mcp.tool()
    async def delete_kpi_alert_rule(project_id: str, alert_rule_id: str, ctx: Context) -> dict[str, Any]:
        """Delete one of a project's KPI alert rules (``get_project_kpis`` lists them).

        Args:
            project_id: The project.
            alert_rule_id: The rule.
        """
        path = f"/modelManagement/projects/{project_id}/alertRules/{alert_rule_id}"
        async with viya_session("delete_kpi_alert_rule", ctx) as client:
            resp = await client.get(f"{config.VIYA_ENDPOINT}{path}", headers=_JSON)
            if resp.status_code == 404:
                return {"status": "not_found", "message": f"No alert rule {alert_rule_id!r} in project {project_id!r}."}
            raise_for_viya_status(resp)
            rule = rule_summary(resp.json())
            raise_for_viya_status(await client.delete(f"{config.VIYA_ENDPOINT}{path}"))
            return {"status": "deleted", "rule": rule}
