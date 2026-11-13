# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SAS Model Manager plumbing shared by Tier 6 (scoring) and Tier 11 (model life cycle).

The tiers may not import each other, so what both need lives here: looking up
models, projects and repositories, running Model Manager's score tests, reading
the scored rows back, and running SAS code in a compute session of its own.

Behaviour this relies on, probed against SAS Viya 2026.10 in November 2026:

* A score test is a score *definition* (the object to score, a CAS input table,
  a column mapping, and the output table in ``properties``) plus a score
  *execution* of it. It runs in the "SAS Model Manager compute context" and
  writes a CAS table; 13–14 s for ten rows. A registered model is scored as
  ``sas.models.model`` (``/modelManagement/models/<id>``), a published copy as
  ``sas.publish.validation`` (``/modelPublish/models/<id>``).
* ``GET /scoreExecution/executions/<id>/state`` answers in ``text/plain``.
* ``/modelManagement/performanceTasks/<id>/state`` is the one to poll: the job
  record still read "running" after the task had completed. Its summary and
  the project KPIs answer 406 to ``application/json``.
* A model's project needs its target fields and its variables before a
  champion can be set or a performance task created.
"""

import asyncio
import contextlib
import json
import re
import secrets
import time
from typing import Any

import httpx

from .. import config
from ..viya_client import filter_literal, get_paged_items, raise_for_viya_status
from ..viya_utils import (
    create_session,
    delete_session,
    get_context_id,
    run_one_snippet,
    submit_job,
    wait_job,
)

CAS_SERVER = "cas-shared-default"
_JSON = {"Accept": "application/json"}
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

PROJECT_TYPE = "application/vnd.sas.models.project+json"
COLLECTION_TYPE = "application/vnd.sas.collection+json"
SCORE_DEFINITION_TYPE = "application/vnd.sas.score.definition+json"
SCORE_EXECUTION_REQUEST_TYPE = "application/vnd.sas.score.execution.request+json"
PERFORMANCE_TASK_TYPE = "application/vnd.sas.models.performance.task+json"
PERFORMANCE_SUMMARY_TYPE = "application/vnd.sas.models.performance.execution.summary+json"
# POST /modelManagement/publish, probed on SAS Viya 2026.10: it takes the request
# as ...request.asynchronous+json (what python-sasctl sends; the spec's
# ...publish+json gets 415) and answers only as ...publish+json (application/json
# and the spec's collection type get 406).
PUBLISH_REQUEST_TYPE = "application/vnd.sas.models.publishing.request.asynchronous+json"
PUBLISH_RESPONSE_TYPE = "application/vnd.sas.models.publishing.publish+json"

# What a score test returns at most, so a big table cannot flood the context.
MAX_TEST_ROWS = 100
# The clock wait_until_done reads; a module attribute so tests can replace it alone.
_clock = time.monotonic


class ModelError(ValueError):
    """A request the model tools cannot carry out. The message says what to do instead."""


def is_uuid(value: str | None) -> bool:
    return bool(value) and bool(_UUID.fullmatch(value.strip()))


def sas_literal(value: str) -> str:
    """*value* as a single-quoted SAS string: quotes doubled, no macro resolution."""
    return "'" + str(value).replace("'", "''") + "'"


# --- models, projects, repositories ---------------------------------------------------


async def _get(client: httpx.AsyncClient, path: str, missing: str, accept: str = "application/json") -> httpx.Response:
    resp = await client.get(f"{config.VIYA_ENDPOINT}{path}", headers={"Accept": accept})
    if resp.status_code == 404:
        raise ModelError(missing)
    raise_for_viya_status(resp)
    return resp


async def fetch_model(client: httpx.AsyncClient, model_id: str) -> dict[str, Any]:
    resp = await _get(
        client,
        f"/modelRepository/models/{model_id}",
        f"No model {model_id!r} exists in the Model Repository, or you may not see it. "
        "list_registered_models lists the models.",
    )
    return resp.json()


async def fetch_project(client: httpx.AsyncClient, project_id: str) -> tuple[dict[str, Any], str]:
    """The project and its ETag (needed to update it)."""
    resp = await _get(
        client,
        f"/modelRepository/projects/{project_id}",
        f"No model project {project_id!r} exists, or you may not see it. list_model_projects lists them.",
    )
    return resp.json(), resp.headers.get("etag", "")


def _variable(var: dict[str, Any]) -> dict[str, Any]:
    out = {"name": var.get("name"), "type": var.get("type")}
    for key in ("level", "role", "length", "description"):
        if var.get(key):
            out[key] = var[key]
    return out


MODEL_FIELDS = (
    "id", "name", "description", "projectId", "projectName", "projectVersionName", "modelVersionName",
    "role", "function", "algorithm", "tool", "modeler", "scoreCodeType", "targetVariable", "targetLevel",
    "targetEvent", "eventProbVar", "createdBy", "creationTimeStamp", "modifiedBy", "modifiedTimeStamp",
)


def model_summary(model: dict[str, Any]) -> dict[str, Any]:
    """What a model tool reports about a model: its identity, target and variables."""
    out = {key: model[key] for key in MODEL_FIELDS if model.get(key) not in (None, "", [])}
    out["inputVariables"] = [_variable(v) for v in model.get("inputVariables") or []]
    out["outputVariables"] = [_variable(v) for v in model.get("outputVariables") or []]
    return out


async def find_repository(client: httpx.AsyncClient, reference: str | None) -> dict[str, Any]:
    """A repository by name or ID; MCP_DEFAULT_MODEL_REPOSITORY when *reference* is empty."""
    ref = (reference or "").strip() or config.MCP_DEFAULT_MODEL_REPOSITORY
    if is_uuid(ref):
        resp = await _get(
            client,
            f"/modelRepository/repositories/{ref}",
            f"No model repository with the ID {ref!r}. list_model_repositories lists them.",
        )
        return resp.json()
    items, _ = await get_paged_items(
        "/modelRepository/repositories", client, limit=20, filters=f"eq(name,'{filter_literal(ref)}')"
    )
    match = next((r for r in items if r.get("name", "").lower() == ref.lower()), None)
    if match is None:
        default = " (the deployment's MCP_DEFAULT_MODEL_REPOSITORY)" if not (reference or "").strip() else ""
        raise ModelError(f"No model repository is named {ref!r}{default}. list_model_repositories lists them.")
    return match


async def find_project(client: httpx.AsyncClient, name: str, repository_id: str) -> dict[str, Any] | None:
    """The project called *name* in the repository, or None."""
    items, _ = await get_paged_items(
        "/modelRepository/projects",
        client,
        limit=20,
        filters=f"and(eq(name,'{filter_literal(name)}'),eq(repositoryId,'{filter_literal(repository_id)}'))",
    )
    return next((p for p in items if p.get("name", "").lower() == name.lower()), None)


async def models_in_project(
    client: httpx.AsyncClient, project_id: str, name: str | None = None
) -> list[dict[str, Any]]:
    """The project's models, optionally only those called *name*."""
    filters = f"eq(projectId,'{filter_literal(project_id)}')"
    if name:
        filters = f"and({filters},eq(name,'{filter_literal(name)}'))"
    items, _ = await get_paged_items("/modelRepository/models", client, limit=200, filters=filters)
    if name:
        items = [m for m in items if m.get("name", "").lower() == name.lower()]
    return items


def publish_name(name: str) -> str:
    """A name Model Publishing accepts: letters, digits and underscores, not starting with a digit."""
    cleaned = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", name.strip())).strip("_") or "model"
    return f"_{cleaned}" if cleaned[0].isdigit() else cleaned


# --- project preparation (champion, monitoring) ---------------------------------------


def project_target_fields(project: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """The target fields the project lacks and the model can supply.

    PROC REGISTERMODEL leaves them empty; Model Manager needs them before a
    champion can be monitored. Fields the project already has are kept.
    """
    wanted = {
        "targetVariable": model.get("targetVariable"),
        "targetLevel": model.get("targetLevel"),
        "targetEventValue": model.get("targetEvent"),
        "eventProbabilityVariable": model.get("eventProbVar"),
        "function": model.get("function"),
    }
    return {key: value for key, value in wanted.items() if value and not project.get(key)}


def missing_project_variables(project_variables: list[dict[str, Any]], model: dict[str, Any]) -> list[dict[str, Any]]:
    """The model's input and output variables the project does not have yet."""
    existing = {(v.get("name") or "").upper() for v in project_variables}
    new: list[dict[str, Any]] = []
    for var in (model.get("inputVariables") or []) + (model.get("outputVariables") or []):
        name = var.get("name")
        if not name or name.upper() in existing:
            continue
        existing.add(name.upper())
        item: dict[str, Any] = {"name": name, "role": var.get("role"), "type": var.get("type")}
        level = (var.get("level") or "").lower()
        if level in ("binary", "interval", "nominal", "ordinal"):
            item["level"] = level
        if var.get("length"):
            item["length"] = var["length"]
        new.append(item)
    return new


# --- scoring --------------------------------------------------------------------------


def score_definition(
    *,
    name: str,
    object_type: str,
    object_uri: str,
    object_name: str,
    input_caslib: str,
    input_table: str,
    output_caslib: str,
    output_table: str,
    input_variables: list[str],
    extra_properties: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The body of a score definition: what to score, on which table, into which table."""
    return {
        "name": name,
        "objectDescriptor": {"uri": object_uri, "name": object_name, "type": object_type},
        "inputData": {
            "type": "CASTable",
            "serverName": CAS_SERVER,
            "libraryName": input_caslib,
            "tableName": input_table,
        },
        "mappings": [
            {"variableName": var, "mappingType": "datasource", "mappingValue": var} for var in input_variables
        ],
        "properties": {
            "outputLibraryName": output_caslib,
            "outputTableName": output_table,
            "inputLibraryName": input_caslib,
            **(extra_properties or {}),
        },
    }


async def start_score(
    client: httpx.AsyncClient, definition: dict[str, Any], object_uri: str
) -> tuple[str, dict[str, Any]]:
    """Create the score definition and start one execution of it. Returns (definition ID, execution)."""
    resp = await client.post(
        f"{config.VIYA_ENDPOINT}/scoreDefinitions/definitions",
        content=json.dumps(definition).encode(),
        headers={"Content-Type": SCORE_DEFINITION_TYPE, **_JSON},
    )
    raise_for_viya_status(resp)
    definition_id = resp.json()["id"]
    inputs = definition["inputData"]
    resp = await client.post(
        f"{config.VIYA_ENDPOINT}/scoreExecution/executions",
        content=json.dumps({
            "name": f"Execution of {definition['name']}",
            "type": "scoreDefinition",
            "scoreDefinitionId": definition_id,
            "hints": {
                "objectURI": object_uri,
                "inputLibraryName": inputs["libraryName"],
                "inputTableName": inputs["tableName"],
            },
        }).encode(),
        headers={"Content-Type": SCORE_EXECUTION_REQUEST_TYPE, **_JSON},
    )
    raise_for_viya_status(resp)
    return definition_id, resp.json()


async def execution_state(client: httpx.AsyncClient, execution_id: str) -> str:
    resp = await client.get(
        f"{config.VIYA_ENDPOINT}/scoreExecution/executions/{execution_id}/state", headers={"Accept": "text/plain"}
    )
    raise_for_viya_status(resp)
    return resp.text.strip()


async def wait_until_done(poll, timeout: float, interval: float = 2.0) -> str:
    """Call ``await poll()`` until it returns a state that is not pending or running."""
    deadline = _clock() + timeout
    while True:
        state = await poll()
        if state.lower() not in ("pending", "running", "submitted", "queued", "") or _clock() >= deadline:
            return state
        await asyncio.sleep(interval)


def _error_lines(log: str, limit: int = 15) -> list[str]:
    return [line.strip() for line in log.splitlines() if line.lstrip().startswith("ERROR")][:limit]


async def execution_report(client: httpx.AsyncClient, execution_id: str) -> dict[str, Any]:
    """A score execution as a tool reports it: state, output table, and the log's errors if it failed."""
    resp = await _get(
        client,
        f"/scoreExecution/executions/{execution_id}",
        f"No score execution {execution_id!r} exists, or you may not see it.",
    )
    execution = resp.json()
    output = execution.get("outputTable") or {}
    report: dict[str, Any] = {
        "id": execution.get("id"),
        "state": execution.get("state"),
        "scoreDefinitionId": execution.get("scoreDefinitionId"),
        "createdBy": execution.get("createdBy"),
        "creationTimeStamp": execution.get("creationTimeStamp"),
        "modifiedTimeStamp": execution.get("modifiedTimeStamp"),
        "outputTable": {"caslib": output.get("libraryName"), "table": output.get("tableName")} if output else None,
    }
    if execution.get("state") not in ("completed", "running", "pending") and execution.get("logFileUri"):
        log = await client.get(
            f"{config.VIYA_ENDPOINT}{execution['logFileUri']}/content", headers={"Accept": "text/plain"}
        )
        if log.status_code == 200:
            report["log_errors"] = _error_lines(log.text)
    if execution.get("error"):
        report["error"] = execution["error"]
    return report


async def table_rows(client: httpx.AsyncClient, caslib: str, table: str, limit: int) -> dict[str, Any]:
    """Up to *limit* rows of a CAS table, as column-name dictionaries."""
    source = f"cas~fs~{CAS_SERVER}~fs~{caslib}"
    columns: list[str] = []
    start = 0
    while True:
        resp = await client.get(
            f"{config.VIYA_ENDPOINT}/dataTables/dataSources/{source}/tables/{table}/columns",
            params={"start": start, "limit": 100},
            follow_redirects=True,
        )
        raise_for_viya_status(resp)
        data = resp.json()
        columns.extend(item.get("name") for item in data.get("items", []))
        start += 100
        if start >= data.get("count", 0):
            break
    resp = await client.get(
        f"{config.VIYA_ENDPOINT}/rowSets/tables/{source}~fs~{table}/rows",
        params={"start": 0, "limit": limit},
        follow_redirects=True,
    )
    raise_for_viya_status(resp)
    rows = [dict(zip(columns, item.get("cells", []), strict=False)) for item in resp.json().get("items", [])]
    return {"columns": columns, "rows": rows}


async def unload_table(client: httpx.AsyncClient, caslib: str, table: str) -> None:
    """Drop a scratch table from memory; best effort, it has no source to lose."""
    with contextlib.suppress(httpx.HTTPError):
        await client.put(
            f"{config.VIYA_ENDPOINT}/casManagement/servers/{CAS_SERVER}/caslibs/{caslib}/tables/{table}/state",
            params={"value": "unloaded"},
            headers={"Accept": "*/*"},
        )


def scratch_table_name(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4).upper()}"


def sample_code(source_caslib: str, source_table: str, target_table: str, rows: int) -> str:
    """SAS code that copies the first *rows* rows of a CAS table into a global CASUSER table.

    The rows are read by a SAS DATA step, which stops after *rows*: a DATA step
    running in CAS reads in parallel threads, so a row limit there is per thread,
    and the OBS= data set option is refused for a CAS input table.
    """
    return "\n".join([
        "cas _mcpsamp sessopts=(caslib=casuser);",
        f"libname _mcpin cas caslib={sas_literal(source_caslib)};",
        "libname _mcpout cas caslib=casuser;",
        "data work._mcpsamp;",
        f"  set _mcpin.{_sas_name(source_table)};",
        f"  if _n_ > {int(rows)} then stop;",
        "run;",
        f"data _mcpout.{_sas_name(target_table)}(promote=yes);",
        "  set work._mcpsamp;",
        "run;",
        "cas _mcpsamp terminate;",
    ])


def _sas_name(name: str) -> str:
    """A table name as a SAS name literal, so any CAS table name can be named."""
    return f"{sas_literal(name)}n"


async def personal_caslib(client: httpx.AsyncClient) -> str:
    """The caller's own caslib by its full name, such as ``CASUSER(jdoe)``."""
    resp = await client.get(
        f"{config.VIYA_ENDPOINT}/casManagement/servers/{CAS_SERVER}/caslibs/CASUSER",
        headers={"Accept": "application/json"},
    )
    raise_for_viya_status(resp)
    return resp.json().get("name") or "CASUSER"


async def score_test(
    client: httpx.AsyncClient,
    token: str,
    *,
    object_type: str,
    object_uri: str,
    object_name: str,
    input_variables: list[str],
    caslib_name: str,
    table_name: str,
    rows: int,
    timeout_seconds: float,
    extra_properties: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Score the first *rows* rows of a table, wait, return the scored rows, and clean up.

    The rows are copied to a scratch table in the caller's CASUSER, scored
    into a second one, read back, and both tables and the score definition are
    removed again: a test leaves nothing behind.
    """
    rows = max(1, min(int(rows), MAX_TEST_ROWS))
    lib = await personal_caslib(client)
    sample = scratch_table_name("MCP_SCORETEST")
    scored = f"{sample}_OUT"
    run = await run_sas(client, token, sample_code(caslib_name, table_name, sample, rows))
    errors = sas_errors(run)
    if errors:
        await unload_table(client, lib, sample)
        return {
            "status": "sample_failed",
            "message": f"The first {rows} rows of {caslib_name}.{table_name} could not be copied for the test.",
            "log_errors": errors,
        }
    definition_id = execution_id = None
    still_running = False
    try:
        definition = score_definition(
            name=f"MCP score test {sample}",
            object_type=object_type,
            object_uri=object_uri,
            object_name=object_name,
            input_caslib=lib,
            input_table=sample,
            output_caslib=lib,
            output_table=scored,
            input_variables=input_variables,
            extra_properties=extra_properties,
        )
        definition_id, execution = await start_score(client, definition, object_uri)
        execution_id = execution["id"]
        state = await wait_until_done(lambda: execution_state(client, execution_id), timeout_seconds)
        report = await execution_report(client, execution_id)
        result: dict[str, Any] = {
            "status": state,
            "input": f"{caslib_name}.{table_name}",
            "rows_scored": rows,
        }
        if state == "completed":
            result.update(await table_rows(client, lib, scored, rows))
        elif state in ("pending", "running"):
            result["message"] = (
                f"The score test did not finish within {timeout_seconds:.0f} s. Its execution "
                f"{execution_id} keeps running; get_score_execution reports it."
            )
            still_running = True  # leave it for the caller to follow
        else:
            result["log_errors"] = report.get("log_errors") or []
            if report.get("error"):
                result["error"] = report["error"]
        return result
    finally:
        if not still_running:
            await unload_table(client, lib, sample)
            await unload_table(client, lib, scored)
            paths = []
            if execution_id:
                paths.append(f"/scoreExecution/executions/{execution_id}")
            if definition_id:
                paths.append(f"/scoreDefinitions/definitions/{definition_id}")
            for path in paths:
                with contextlib.suppress(httpx.HTTPError):
                    await client.delete(f"{config.VIYA_ENDPOINT}{path}")


# --- running SAS code -----------------------------------------------------------------


async def run_sas(client: httpx.AsyncClient, token: str, code: str) -> dict[str, str]:
    """Run *code* in a compute session of its own and return state, log and listing.

    A session of its own leaves the person's shared session as it was: the code
    assigns librefs and a CAS session, and an error can switch SAS into syntax
    check mode (OBS=0), which would break their next ``execute_sas_code`` call.
    With a fixed COMPUTE_SESSION_ID there is no other session to use.
    """
    if config.COMPUTE_SESSION_ID:
        return await run_one_snippet(code, "1", token)
    context_id = await get_context_id(client, config.CONTEXT_NAME)
    try:
        session_id = await create_session(client, context_id, name="sas-mcp-model-tools")
    except httpx.HTTPStatusError as exc:
        # Seen on SAS Viya 2026.10: "The Compute server ... terminated
        # unexpectedly" (500) while starting a session, the next attempt fine.
        if exc.response.status_code < 500:
            raise
        await asyncio.sleep(2)
        session_id = await create_session(client, context_id, name="sas-mcp-model-tools")
    try:
        job_id = await submit_job(client, session_id, code)
        state, log, listing = await wait_job(client, session_id, job_id)
    finally:
        # Best effort: Viya reaps an idle session anyway.
        with contextlib.suppress(Exception):
            await delete_session(client, session_id)
    return {"state": state, "log": log, "listing": listing}


# Ending the CAS session can fail after the work itself succeeded (seen after a
# DATA step model was registered). The compute session is deleted right after,
# which ends the CAS session too, so that line is not a failure of the run.
_HARMLESS = ("Request for TERMINATE failed",)


def sas_errors(result: dict[str, str]) -> list[str]:
    """The ERROR lines of a SAS run's log, without the harmless ones."""
    lines = _error_lines(result.get("log", ""), limit=40)
    return [line for line in lines if not any(text in line for text in _HARMLESS)][:20]


__all__ = [
    "CAS_SERVER",
    "MAX_TEST_ROWS",
    "ModelError",
    "execution_report",
    "execution_state",
    "fetch_model",
    "fetch_project",
    "find_project",
    "find_repository",
    "is_uuid",
    "missing_project_variables",
    "model_summary",
    "models_in_project",
    "project_target_fields",
    "publish_name",
    "run_sas",
    "sample_code",
    "sas_errors",
    "sas_literal",
    "score_definition",
    "personal_caslib",
    "scratch_table_name",
    "score_test",
    "start_score",
    "table_rows",
    "unload_table",
    "wait_until_done",
]
