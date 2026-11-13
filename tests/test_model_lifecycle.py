# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit and tool tests for the model tools (Tiers 2, 6 and 11) against a routed fake Viya (no network)."""

import json
import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from conftest import _make_mock_response
from sas_mcp_server.helpers import folder_helpers
from sas_mcp_server.helpers import model_helpers as mh
from sas_mcp_server.tools import model_lifecycle as ml
from test_decisioning_content_tools import FakeViya, call

MODEL = {
    "id": "m-1", "name": "Tree Astore", "description": "d", "projectId": "p-1", "projectName": "Probe",
    "projectVersionId": "pv-1", "modelVersionName": "1.1", "role": "plain", "function": "classification",
    "targetVariable": "BAD", "targetLevel": "binary", "targetEvent": "1", "eventProbVar": "P_BAD1",
    "scoreCodeType": "dataStep", "createdBy": "jdoe",
    "inputVariables": [
        {"name": "LOAN", "type": "decimal", "role": "input", "level": "interval"},
        {"name": "JOB", "type": "string", "role": "input", "level": "nominal", "length": 7},
    ],
    "outputVariables": [
        {"name": "P_BAD1", "type": "decimal", "role": "output"},
        {"name": "I_BAD", "type": "string", "role": "classification", "length": 1},
    ],
}
PROJECT = {"id": "p-1", "name": "Probe", "repositoryId": "r-1", "folderId": "rf-1", "function": "classification"}
REPO = {"id": "r-1", "name": "Public", "folderId": "rf-0"}
SCORE_ARGS_NO_MODEL = {"caslib_name": "Public", "table_name": "HMEQ"}
SCORE_ARGS = {"model_id": "m-1", **SCORE_ARGS_NO_MODEL}


def respond(data, status=200):
    resp = _make_mock_response(data if not isinstance(data, str) else None, status_code=status)
    if status >= 400:
        request = httpx.Request("GET", "https://test.viya.com/x")
        resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            f"HTTP {status}", request=request, response=httpx.Response(status, request=request)))
    if isinstance(data, str):
        resp.text = data
    if status == 204:
        resp.content = b""
    resp.headers = {"Content-Type": "application/json", "etag": '"e1"'}
    return resp


class ModelViya(FakeViya):
    def route(self, method, pattern, fn):
        """A route whose status depends on the request: fn(url, kwargs) -> (data, status)."""
        self.routes.insert(0, (method, re.compile(pattern), lambda url, kw: respond(*fn(url, kw))))

    def body(self, method, pattern, index=0):
        _, kw = self.calls(method, pattern)[index]
        return json.loads(kw["content"]) if "content" in kw else kw.get("json")


@pytest.fixture
def fake(mcp_server_with_mock_client, monkeypatch):
    monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", "")
    monkeypatch.setattr(mh.asyncio, "sleep", AsyncMock())
    mcp, mock_client = mcp_server_with_mock_client
    viya = ModelViya(mock_client)
    viya.add("GET", r"/modelRepository/models/m-1$", MODEL)
    viya.add("GET", r"/modelRepository/projects/p-1$", PROJECT)
    viya.add("GET", r"/modelRepository/repositories$", {"items": [REPO], "count": 1})
    return mcp, viya


@pytest.fixture
def sas(monkeypatch):
    run = AsyncMock(return_value={"state": "completed", "log": "NOTE: ok", "listing": ""})
    monkeypatch.setattr(mh, "run_sas", run)
    return run


# --- helpers ---------------------------------------------------------------------------


def test_publish_name_keeps_what_destinations_accept():
    assert mh.publish_name("MCP Probe Logistic DS") == "MCP_Probe_Logistic_DS"
    assert mh.publish_name("2026 risk-model (v2)") == "_2026_risk_model_v2"
    assert mh.publish_name("  ") == "model"


def test_sas_literal_doubles_quotes_and_resolves_no_macros():
    assert mh.sas_literal("O'Brien &x %y") == "'O''Brien &x %y'"


def test_project_target_fields_fill_only_what_is_missing():
    assert mh.project_target_fields({"targetVariable": "Y"}, MODEL) == {
        "targetLevel": "binary", "targetEventValue": "1", "eventProbabilityVariable": "P_BAD1",
        "function": "classification",
    }


def test_missing_project_variables_skips_existing_names_case_insensitively():
    new = mh.missing_project_variables([{"name": "loan"}], MODEL)
    assert [v["name"] for v in new] == ["JOB", "P_BAD1", "I_BAD"]
    assert new[0] == {"name": "JOB", "role": "input", "type": "string", "level": "nominal", "length": 7}


def test_sample_code_stops_in_a_sas_step_and_promotes():
    code = mh.sample_code("Public", "HMEQ", "MCP_SCORETEST_1", 10)
    assert "if _n_ > 10 then stop;" in code
    assert "set _mcpin.'HMEQ'n;" in code and "data _mcpout.'MCP_SCORETEST_1'n(promote=yes);" in code


def test_registration_code_for_an_analytic_store_in_a_new_project():
    code = ml.registration_code(
        name="Tree", description="It's a tree", algorithm="TREE", function="CLASSIFICATION", modeler=None,
        replace=True, project_id=None, project_name="Risk", project_folder="Team", repository="Public",
        astore_caslib="Public", astore_table="TREE_STORE", score_code=None, data_caslib="Public",
        data_table="HMEQ", target_variable="BAD", target_level="BINARY", target_event="1", assessment=True,
    )
    assert "libname casuser cas caslib=casuser;" in code
    assert "download rstore=_mcpast.'TREE_STORE'n" in code
    assert "proc registermodel name='Tree' description='It''s a tree' data=_mcpdat.'HMEQ'n" in code
    assert "algorithm=TREE function=CLASSIFICATION replace;" in code
    assert "project name='Risk' repository='Public' folder='Team';" in code
    assert "target BAD / level=BINARY event='1';" in code and "  assessment;" in code


def test_registration_code_writes_datastep_code_without_resolving_macros():
    code = ml.registration_code(
        name="DS", description="", algorithm=None, function=None, modeler="me", replace=False,
        project_id="p-1", project_name=None, project_folder=None, repository="Public", astore_caslib=None,
        astore_table=None, score_code="P_BAD1 = 0.5; * it's &risky;\n\nI_BAD='1';", data_caslib=None,
        data_table=None, target_variable=None, target_level=None, target_event=None, assessment=False,
    )
    assert "  put 'P_BAD1 = 0.5; * it''s &risky;';" in code and "  put ' ';" in code
    assert "  project id='p-1';" in code and "  code file=_mcpcode;" in code
    assert "modeler='me'" in code and "replace" not in code and "assessment" not in code


def test_sas_errors_ignore_a_cas_session_that_would_not_end():
    log = "ERROR: Request for TERMINATE failed for session _MCPREG.\nERROR: real one"
    assert mh.sas_errors({"log": log}) == ["ERROR: real one"]


def test_registration_code_refuses_a_line_sas_cannot_hold():
    with pytest.raises(mh.ModelError, match="longer than"):
        ml._put_lines("x" * 40000)


async def test_run_sas_uses_a_session_of_its_own_and_deletes_it(fake):
    _, viya = fake
    viya.add("GET", r"/compute/contexts$", {"items": [{"id": "ctx-1"}]})
    viya.add("POST", r"/compute/contexts/ctx-1/sessions$", {"id": "s-1"})
    viya.add("POST", r"/compute/sessions/s-1/jobs$", {"id": "j-1"})
    viya.add("GET", r"/jobs/j-1/state$", "completed")
    viya.add("GET", r"/jobs/j-1/log$", {"items": [{"line": "ERROR: boom"}]})
    viya.add("GET", r"/jobs/j-1/listing$", {"items": []})
    viya.add("DELETE", r"/compute/sessions/s-1$", {}, status=204)
    client = viya_client(viya)
    result = await mh.run_sas(client, "t", "data x; run;")
    assert result["state"] == "completed" and mh.sas_errors(result) == ["ERROR: boom"]
    assert viya.calls("DELETE", r"/compute/sessions/s-1$")


async def test_run_sas_retries_a_session_that_failed_to_start(fake, monkeypatch):
    _, viya = fake
    attempts = []

    def start(url, kw):
        attempts.append(url)
        return ({"message": "The Compute server terminated unexpectedly."}, 500) if len(attempts) == 1 else (
            {"id": "s-2"}, 201)

    viya.add("GET", r"/compute/contexts$", {"items": [{"id": "ctx-1"}]})
    viya.route("POST", r"/compute/contexts/ctx-1/sessions$", start)
    viya.add("POST", r"/compute/sessions/s-2/jobs$", {"id": "j-2"})
    viya.add("GET", r"/jobs/j-2/state$", "completed")
    viya.add("GET", r"/jobs/j-2/(log|listing)$", {"items": []})
    viya.add("DELETE", r"/compute/sessions/s-2$", {}, status=204)
    result = await mh.run_sas(viya_client(viya), "t", "data x; run;")
    assert result["state"] == "completed" and len(attempts) == 2


async def test_run_sas_in_a_fixed_session_uses_it(monkeypatch):
    monkeypatch.setattr("sas_mcp_server.config.COMPUTE_SESSION_ID", "0001")
    snippet = AsyncMock(return_value={"state": "completed", "log": "", "listing": ""})
    monkeypatch.setattr(mh, "run_one_snippet", snippet)
    assert (await mh.run_sas(MagicMock(), "t", "x"))["state"] == "completed"
    snippet.assert_awaited_once()


def viya_client(viya):
    """The mock client FakeViya answers for."""
    method = viya._handler  # noqa: SLF001 - reach the shared mock through its routes
    client = MagicMock()
    for verb in ("get", "post", "put", "delete"):
        setattr(client, verb, AsyncMock(side_effect=method(verb.upper())))
    return client


# --- Tier 2: folders and CAS tables ----------------------------------------------------


def _paths(existing):
    def answer(url, kw):
        names = kw["json"]["items"]
        key = "/".join(names)
        return (existing[key], 200) if key in existing else ({"message": "not found"}, 404)
    return answer


async def test_create_folder_makes_the_missing_levels(fake):
    mcp, viya = fake
    viya.route("POST", r"/folders/paths$", _paths({"Public": {"id": "pub", "name": "Public"}}))
    viya.route("POST", r"/folders/folders$", lambda url, kw: (
        {"id": f"id-{kw['json']['name']}", "name": kw["json"]["name"]}, 201))
    result = await call(mcp, "create_folder", {"path": "/Public/Risk/2026"})
    assert result["status"] == "created" and result["path"] == "/Public/Risk/2026"
    assert result["created"] == ["/Public/Risk", "/Public/Risk/2026"]
    first, second = viya.calls("POST", r"/folders/folders$")
    assert first[1]["params"] == {"parentFolderUri": "/folders/folders/pub"}
    assert second[1]["params"] == {"parentFolderUri": "/folders/folders/id-Risk"}


async def test_create_folder_returns_an_existing_folder_unchanged(fake):
    mcp, viya = fake
    viya.route("POST", r"/folders/paths$", _paths({"Public/Risk": {"id": "risk", "name": "Risk"}}))
    result = await call(mcp, "create_folder", {"path": "/Public/Risk"})
    assert result["status"] == "exists" and result["created"] == [] and result["id"] == "risk"
    assert not viya.calls("POST", r"/folders/folders$")


async def test_create_folder_refuses_a_relative_path_without_a_default(fake):
    mcp, _ = fake
    result = await call(mcp, "create_folder", {"path": "Risk/2026"})
    assert result["status"] == "refused" and "MCP_DEFAULT_FOLDER" in result["message"]


async def test_create_folder_puts_a_relative_path_under_the_default(fake, monkeypatch):
    mcp, viya = fake
    monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", "/folders/folders/f-1")
    viya.route("POST", r"/folders/paths$", _paths({"Public/Content/MCP Created": {"id": "f-1", "name": "MCP Created"}}))
    viya.add("POST", r"/folders/folders$", {"id": "new", "name": "Risk"}, status=201)
    result = await call(mcp, "create_folder", {"path": "Risk"})
    assert result["status"] == "created" and result["path"] == "/Public/Content/MCP Created/Risk"


async def test_create_folder_refuses_a_top_level_folder_that_does_not_exist(fake):
    mcp, viya = fake
    viya.route("POST", r"/folders/paths$", _paths({}))
    result = await call(mcp, "create_folder", {"path": "/Nowhere/Risk"})
    assert result["status"] == "refused" and "administrators" in result["message"]


async def test_create_folder_path_under_my_folder_and_dot_segments(fake):
    _, viya = fake
    viya.add("GET", r"/folders/folders/@myFolder$", {"id": "me", "name": "My Folder"})
    viya.route("POST", r"/folders/paths$", _paths({
        "Public/Content/My Folder": {"id": "me", "name": "My Folder"},
        "Public/Content/My Folder/Risk": {"id": "risk", "name": "Risk"},
    }))
    client = viya_client(viya)
    assert (await folder_helpers.create_folder_path(client, "My Folder/Risk"))["status"] == "exists"
    with pytest.raises(folder_helpers.FolderError, match="'..'"):
        await folder_helpers.create_folder_path(client, "/Public/../x")


TABLE = r"/casManagement/servers/cas-shared-default/caslibs/Public/tables/HMEQ$"
SOURCES = r"/caslibs/Public/sources$"


async def test_save_castable_saves_and_refuses_to_overwrite(fake):
    mcp, viya = fake
    viya.add("GET", TABLE, {"name": "HMEQ", "state": "loaded"})
    viya.add("GET", SOURCES, {"items": [{"name": "hmeq.sashdat"}], "count": 1})
    refused = await call(mcp, "save_castable", {"caslib_name": "Public", "table_name": "HMEQ"})
    assert refused["status"] == "exists" and refused["existing"] == ["hmeq.sashdat"]
    viya.add("POST", TABLE, {}, status=200)
    saved = await call(mcp, "save_castable", {"caslib_name": "Public", "table_name": "HMEQ", "replace": True})
    assert saved["status"] == "saved" and saved["replaced"] is True
    (_, kw), = viya.calls("POST", TABLE)
    assert kw["json"] == {"caslibName": "Public", "tableName": "HMEQ", "format": "sashdat", "replace": True}


async def test_save_castable_checks_the_table_and_format(fake):
    mcp, viya = fake
    assert (await call(mcp, "save_castable", {"caslib_name": "Public", "table_name": "HMEQ", "file_format": "xlsx"}))[
        "status"] == "refused"
    viya.add("GET", TABLE, {}, status=404)
    assert (await call(mcp, "save_castable", {"caslib_name": "Public", "table_name": "HMEQ"}))["status"] == "not_found"
    viya.add("GET", TABLE, {"state": "unloaded"})
    assert (await call(mcp, "save_castable", {"caslib_name": "Public", "table_name": "HMEQ"}))["status"] == "not_loaded"


async def test_drop_castable_refuses_a_table_with_no_source(fake):
    mcp, viya = fake
    viya.add("GET", TABLE, {"state": "loaded"})
    viya.add("GET", SOURCES, {"items": [{"name": "OTHER.sashdat"}], "count": 1})
    result = await call(mcp, "drop_castable", {"caslib_name": "Public", "table_name": "HMEQ"})
    assert result["status"] == "unsaved" and "save_castable" in result["message"]
    viya.add("PUT", TABLE[:-1] + "/state$", "unloaded")
    forced = await call(mcp, "drop_castable", {"caslib_name": "Public", "table_name": "HMEQ", "allow_unsaved": True})
    assert forced["status"] == "dropped" and forced["state"] == "unloaded"


async def test_drop_castable_drops_a_saved_table_and_leaves_others(fake):
    mcp, viya = fake
    viya.add("GET", TABLE, {"state": "loaded"})
    viya.add("GET", SOURCES, {"items": [{"name": "HMEQ.sashdat"}], "count": 1})
    viya.add("PUT", TABLE[:-1] + "/state$", "unloaded")
    result = await call(mcp, "drop_castable", {"caslib_name": "Public", "table_name": "HMEQ"})
    assert result == {"status": "dropped", "table": "Public.HMEQ", "state": "unloaded", "source": ["HMEQ.sashdat"]}
    (_, kw), = viya.calls("PUT", r"/state$")
    assert kw["params"] == {"value": "unloaded"}
    viya.add("GET", TABLE, {"state": "unloaded"})
    assert (await call(mcp, "drop_castable", {"caslib_name": "Public", "table_name": "HMEQ"}))["status"] == "not_loaded"
    viya.add("GET", TABLE, {}, status=404)
    assert (await call(mcp, "drop_castable", {"caslib_name": "Public", "table_name": "HMEQ"}))["status"] == "not_found"


# --- Tier 6: models and scoring ---------------------------------------------------------


async def test_get_model_summarises_variables_and_files(fake):
    mcp, viya = fake
    viya.add("GET", r"/models/m-1/contents$", {"items": [{"name": "score.sas", "role": "score"}], "count": 1})
    result = await call(mcp, "get_model", {"model_id": "m-1"})
    assert result["projectName"] == "Probe" and result["eventProbVar"] == "P_BAD1"
    assert result["inputVariables"][1] == {
        "name": "JOB", "type": "string", "level": "nominal", "role": "input", "length": 7,
    }
    assert result["files"] == [{"name": "score.sas", "role": "score"}]
    viya.add("GET", r"/modelRepository/models/m-9$", {}, status=404)
    assert (await call(mcp, "get_model", {"model_id": "m-9"}))["status"] == "not_found"


async def test_list_model_versions_puts_the_current_one_first(fake):
    mcp, viya = fake
    viya.add("GET", r"/models/m-1/modelVersions$", {"items": [{"id": "old", "modelVersionName": "1.0"}], "count": 1})
    result = await call(mcp, "list_model_versions", {"model_id": "m-1"})
    assert [v["modelVersionName"] for v in result["versions"]] == ["1.1", "1.0"]
    assert result["versions"][0]["current"] is True


async def test_list_published_models_filters_and_names_the_source_model(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelPublish/models$", {"items": [{
        "id": "pub-1", "name": "Tree", "destinationName": "maslocal", "state": "completed",
        "sourceUri": "/modelRepository/models/m-1", "errorResponse": {},
    }], "count": 1})
    result = await call(mcp, "list_published_models", {"destination_name": "maslocal", "name_filter": "Tr"})
    assert result == [{"id": "pub-1", "name": "Tree", "destinationName": "maslocal", "state": "completed",
                       "sourceUri": "/modelRepository/models/m-1", "modelId": "m-1"}]
    (_, kw), = viya.calls("GET", r"/modelPublish/models$")
    assert kw["params"]["filter"] == "and(eq(destinationName,'maslocal'),contains(name,'Tr'))"


def _scoring(viya, state="completed"):
    viya.add("GET", r"/caslibs/CASUSER$", {"name": "CASUSER(jdoe)"})
    viya.add("POST", r"/scoreDefinitions/definitions$", {"id": "def-1"}, status=201)
    viya.add("POST", r"/scoreExecution/executions$", {"id": "ex-1", "state": "pending"}, status=201)
    viya.add("GET", r"/executions/ex-1/state$", state)
    viya.add("GET", r"/scoreExecution/executions/ex-1$", {
        "id": "ex-1", "state": state, "scoreDefinitionId": "def-1", "logFileUri": "/files/files/log-1",
        "outputTable": {"libraryName": "CASUSER(jdoe)", "tableName": "OUT"},
    })
    viya.add("GET", r"/files/files/log-1/content$", "NOTE: a\nERROR: Variable LOAN not found.")
    viya.add("GET", r"/tables/[^/]+/columns$", {"items": [{"name": "LOAN"}, {"name": "P_BAD1"}], "count": 2})
    viya.add("GET", r"/rowSets/tables/.+/rows$", {"items": [{"cells": [100, 0.25]}]})
    viya.add("PUT", r"/state$", "unloaded")
    viya.add("DELETE", r"/(executions|definitions)/", {}, status=204)


async def test_test_score_model_scores_a_sample_and_cleans_up(fake, sas):
    mcp, viya = fake
    _scoring(viya)
    result = await call(mcp, "test_score_model", {**SCORE_ARGS, "rows": 500})
    assert result["status"] == "completed" and result["rows_scored"] == 100
    assert result["rows"] == [{"LOAN": 100, "P_BAD1": 0.25}]
    definition = viya.body("POST", r"/scoreDefinitions/definitions$")
    assert definition["objectDescriptor"]["type"] == "sas.models.model"
    assert definition["inputData"]["libraryName"] == "CASUSER(jdoe)"
    assert [m["variableName"] for m in definition["mappings"]] == ["LOAN", "JOB"]
    assert definition["properties"]["projectVersionUri"] == "/modelRepository/projects/p-1/projectVersions/pv-1"
    assert len(viya.calls("PUT", r"/state$")) == 2 and len(viya.calls("DELETE", r"/")) == 2
    assert "if _n_ > 100 then stop;" in sas.call_args.args[2]


async def test_test_score_model_reports_the_log_errors_of_a_failed_test(fake, sas):
    mcp, viya = fake
    _scoring(viya, state="failed")
    result = await call(mcp, "test_score_model", {"model_id": "m-1", "caslib_name": "Public", "table_name": "HMEQ"})
    assert result["status"] == "failed" and result["log_errors"] == ["ERROR: Variable LOAN not found."]


async def test_test_score_model_keeps_a_test_that_is_still_running(fake, sas, monkeypatch):
    mcp, viya = fake
    _scoring(viya, state="running")
    monkeypatch.setattr(mh, "_clock", MagicMock(side_effect=[0, 0, 1000]))
    result = await call(mcp, "test_score_model", {"model_id": "m-1", "caslib_name": "Public", "table_name": "HMEQ"})
    assert result["status"] == "running" and "get_score_execution" in result["message"]
    assert not viya.calls("DELETE", r"/")


async def test_test_score_model_reports_a_sample_that_could_not_be_made(fake, sas):
    mcp, viya = fake
    _scoring(viya)
    sas.return_value = {"state": "error", "log": "ERROR: File PUBLIC.NOPE.DATA does not exist.", "listing": ""}
    result = await call(mcp, "test_score_model", {"model_id": "m-1", "caslib_name": "Public", "table_name": "NOPE"})
    assert result["status"] == "sample_failed" and result["log_errors"]
    assert not viya.calls("POST", r"/scoreDefinitions")


async def test_score_table_starts_an_execution_and_says_how_to_follow_it(fake):
    mcp, viya = fake
    _scoring(viya)
    result = await call(mcp, "score_table", {"model_id": "m-1", "caslib_name": "Public", "table_name": "HMEQ"})
    assert result["execution_id"] == "ex-1" and result["output"] == "Public.HMEQ_SCORED"
    hints = viya.body("POST", r"/scoreExecution/executions$")["hints"]
    assert hints == {"objectURI": "/modelManagement/models/m-1", "inputLibraryName": "Public", "inputTableName": "HMEQ"}


async def test_get_score_execution_returns_rows_once_completed(fake):
    mcp, viya = fake
    _scoring(viya)
    result = await call(mcp, "get_score_execution", {"execution_id": "ex-1", "rows": 5})
    assert result["state"] == "completed" and result["rows"] == [{"LOAN": 100, "P_BAD1": 0.25}]
    viya.add("GET", r"/scoreExecution/executions/ex-9$", {}, status=404)
    assert (await call(mcp, "get_score_execution", {"execution_id": "ex-9"}))["status"] == "not_found"


async def test_publish_ml_champion_model_defaults_to_the_setting(fake, monkeypatch):
    mcp, viya = fake
    monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_PUBLISH_DESTINATION", "casdest")
    viya.add("PUT", r"@championModel$", {"state": "ok"})
    await call(mcp, "publish_ml_champion_model", {"project_id": "ml-1"})
    (_, kw), = viya.calls("PUT", r"@championModel$")
    assert kw["params"]["destinationName"] == "casdest"


# --- Tier 11: repositories, projects, champion -------------------------------------------


async def test_list_model_repositories_marks_the_default(fake):
    mcp, _ = fake
    assert await call(mcp, "list_model_repositories", {}) == [
        {"id": "r-1", "name": "Public", "description": "", "default": True}
    ]


async def test_list_model_projects_filters_by_repository(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects$", {"items": [PROJECT], "count": 1})
    result = await call(mcp, "list_model_projects", {"repository": "public", "name_filter": "Pro"})
    assert result[0]["name"] == "Probe"
    _, kw = viya.calls("GET", r"/modelRepository/projects$")[-1]
    assert kw["params"]["filter"] == "and(eq(repositoryId,'r-1'),contains(name,'Pro'))"
    missing = await call(mcp, "list_model_projects", {"repository": "Nope"})
    assert missing[0]["status"] == "not_found"


async def test_create_model_project_reuses_or_creates_with_a_folder(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects$", {"items": [PROJECT], "count": 1})
    exists = await call(mcp, "create_model_project", {"name": "probe"})
    assert exists["status"] == "exists"
    viya.add("GET", r"/modelRepository/projects$", {"items": [], "count": 0})
    viya.add("GET", r"/folders/folders/rf-0/members$", {"items": [], "count": 0})
    viya.add("POST", r"/folders/folders$", {"id": "team"}, status=201)
    viya.add("POST", r"/modelRepository/projects$", {**PROJECT, "id": "p-2", "name": "New"}, status=201)
    created = await call(mcp, "create_model_project", {
        "name": "New", "folder": "Team", "target_variable": "BAD", "target_level": "Binary", "target_event": "1",
    })
    assert created["status"] == "created" and created["project"]["id"] == "p-2"
    body = viya.body("POST", r"/modelRepository/projects$")
    assert body["folderId"] == "team" and body["targetLevel"] == "binary" and body["repositoryId"] == "r-1"
    assert (await call(mcp, "create_model_project", {"name": "x", "folder": "a/b"}))["status"] == "refused"
    assert (await call(mcp, "create_model_project", {"name": "x", "target_level": "odd"}))["status"] == "refused"


async def test_create_model_project_finds_an_existing_repository_folder(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects$", {"items": [], "count": 0})
    viya.add("GET", r"/folders/folders/rf-0/members$", {"items": [{"name": "team", "uri": "/folders/folders/t-1"}]})
    viya.add("POST", r"/modelRepository/projects$", {**PROJECT, "id": "p-3"}, status=201)
    await call(mcp, "create_model_project", {"name": "New", "folder": "Team", "repository": "Public"})
    assert viya.body("POST", r"/modelRepository/projects$")["folderId"] == "t-1"
    assert not viya.calls("POST", r"/folders/folders$")


async def test_set_project_champion_prepares_the_project_first(fake):
    mcp, viya = fake
    viya.add("PUT", r"/modelRepository/projects/p-1$", {})
    viya.add("GET", r"/projects/p-1/variables$", {"items": [{"name": "LOAN"}], "count": 1})
    viya.add("POST", r"/projects/p-1/variables$", {"items": []}, status=201)
    viya.add("POST", r"/projects/p-1/champion$", {"id": "m-1"})
    result = await call(mcp, "set_project_champion", {"project_id": "p-1", "model_id": "m-1"})
    assert result["status"] == "champion_set" and result["variables_added"] == ["JOB", "P_BAD1", "I_BAD"]
    put = viya.body("PUT", r"/modelRepository/projects/p-1$")
    assert put["targetVariable"] == "BAD" and put["eventProbabilityVariable"] == "P_BAD1"
    (_, put_kw), = viya.calls("PUT", r"/modelRepository/projects/p-1$")
    assert put_kw["headers"]["If-Match"] == '"e1"'
    (_, var_kw), = viya.calls("POST", r"/variables$")
    assert var_kw["headers"]["Content-Type"] == mh.COLLECTION_TYPE
    (_, ch_kw), = viya.calls("POST", r"/champion$")
    assert ch_kw["params"] == {"modelId": "m-1"}


async def test_set_project_champion_refuses_a_model_from_another_project(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/models/m-2$", {**MODEL, "id": "m-2", "projectId": "p-9", "projectName": "Other"})
    result = await call(mcp, "set_project_champion", {"project_id": "p-1", "model_id": "m-2"})
    assert result["status"] == "refused" and "Other" in result["message"]


# --- Tier 11: registration ------------------------------------------------------------


async def test_describe_analytic_store_returns_the_listing(fake, sas):
    mcp, _ = fake
    sas.return_value = {"state": "completed", "log": "", "listing": "Input Variables\nLOAN"}
    result = await call(mcp, "describe_analytic_store", {"caslib_name": "Public", "table_name": "STORE"})
    assert result == {"status": "ok", "store": "Public.STORE", "description": "Input Variables\nLOAN"}
    assert "describe rstore=_mcpast.'STORE'n;" in sas.call_args.args[2]
    sas.return_value = {"state": "error", "log": "ERROR: not an analytic store", "listing": ""}
    failed = await call(mcp, "describe_analytic_store", {"caslib_name": "Public", "table_name": "X"})
    assert failed["status"] == "failed"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"name": "M"}, "either astore_table"),
        ({"name": "M", "astore_table": "S", "score_code": "x"}, "either astore_table"),
        ({"name": "M", "astore_table": "S"}, "project"),
        ({"name": "M", "astore_table": "S", "project": "P", "project_folder": "a/b"}, "one folder name"),
        ({"name": "M", "astore_table": "S", "project": "P", "target_level": "odd"}, "target_level"),
        ({"name": "M", "astore_table": "S", "project": "P", "target_level": "binary"}, "target_event"),
        ({"name": "M", "astore_table": "S", "project": "P", "target_variable": "a b"}, "SAS variable name"),
        ({"name": "M", "astore_table": "S", "project": "P", "algorithm": "TREE; x"}, "one keyword"),
        ({"name": "M", "astore_table": "S", "project": "P", "assessment": True}, "assessment needs"),
    ],
)
async def test_register_model_refuses_what_it_cannot_register(fake, args, expected):
    mcp, _ = fake
    result = await call(mcp, "register_model", args)
    assert result["status"] == "refused" and expected in result["message"]


async def test_register_model_refuses_a_duplicate_name_without_replace(fake, sas):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects$", {"items": [PROJECT], "count": 1})
    viya.add("GET", r"/modelRepository/models$", {"items": [MODEL], "count": 1})
    result = await call(mcp, "register_model", {"name": "tree astore", "project": "Probe", "astore_table": "S"})
    assert result["status"] == "exists" and result["existing_model_id"] == "m-1"
    sas.assert_not_called()


async def test_register_model_runs_the_procedure_and_reports_the_new_model(fake, sas):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects$", {"items": [PROJECT], "count": 1})
    viya.add("GET", r"/modelRepository/models$", {"items": [MODEL], "count": 1})
    sas.return_value = {"state": "completed", "log": "NOTE: done", "listing": "Model ID   m-1".replace(
        "m-1", "12345678-1234-1234-1234-123456789012")}
    viya.add("GET", r"/modelRepository/models/12345678-1234-1234-1234-123456789012$", {**MODEL, "id": "new"})
    result = await call(mcp, "register_model", {
        "name": "Tree Astore", "project": "Probe", "astore_caslib": "Public", "astore_table": "STORE",
        "data_caslib": "Public", "data_table": "HMEQ", "target_variable": "BAD", "target_level": "binary",
        "target_event": "1", "algorithm": "tree", "replace": True,
    })
    assert result["status"] == "registered" and result["model"]["id"] == "new"
    assert result["replaced_model_ids"] == ["m-1"] and "set_project_champion" in result["next"]
    code = sas.call_args.args[2]
    assert "project id='p-1';" in code and "algorithm=TREE" in code and "assessment;" in code


async def test_register_model_finds_the_model_by_name_and_reports_failures(fake, sas):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects$", {"items": [], "count": 0})
    viya.add("GET", r"/modelRepository/models$", {"items": [], "count": 0})
    sas.return_value = {"state": "completed", "log": "", "listing": ""}
    unknown = await call(mcp, "register_model", {"name": "DS", "project": "New", "score_code": "P=1;"})
    assert unknown["status"] == "unknown"
    sas.return_value = {"state": "error", "log": "ERROR: Libref CASUSER is not assigned.", "listing": ""}
    failed = await call(mcp, "register_model", {"name": "DS", "project": "New", "score_code": "P=1;"})
    assert failed == {"status": "failed", "log_errors": ["ERROR: Libref CASUSER is not assigned."]}
    viya.add("GET", r"/modelRepository/repositories$", {"items": [], "count": 0})
    missing = await call(mcp, "register_model", {"name": "DS", "project": "New", "score_code": "P=1;"})
    assert missing["status"] == "not_found" and "MCP_DEFAULT_MODEL_REPOSITORY" in missing["message"]


# --- Tier 11: publishing ---------------------------------------------------------------


async def test_publish_model_dry_run_and_refusals(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelPublish/destinations/maslocal$",
             {"name": "maslocal", "destinationType": "microAnalyticService"})
    viya.add("GET", r"/modelPublish/models$", {"items": [], "count": 0})
    plan = await call(mcp, "publish_model", {"model_id": "m-1", "dry_run": True})
    assert plan["status"] == "dry_run" and plan["publish_name"] == "Tree_Astore" and plan["would_replace"] is False
    assert not viya.calls("POST", r"/modelManagement/publish")
    viya.add("GET", r"/modelPublish/models$", {"items": [{"id": "pub-1", "state": "completed"}], "count": 1})
    exists = await call(mcp, "publish_model", {"model_id": "m-1"})
    assert exists["status"] == "exists"
    viya.add("GET", r"/modelPublish/destinations/nope$", {}, status=404)
    assert (await call(mcp, "publish_model", {"model_id": "m-1", "destination_name": "nope"}))["status"] == "not_found"
    viya.add("GET", r"/modelPublish/destinations/git1$", {"name": "git1", "destinationType": "git"})
    viya.add("GET", r"/modelPublish/models$", {"items": [], "count": 0})
    git = await call(mcp, "publish_model", {"model_id": "m-1", "destination_name": "git1"})
    assert git["status"] == "refused" and "git_remote_path" in git["message"]


async def test_publish_model_posts_the_publish_request(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelPublish/destinations/git1$", {"name": "git1", "destinationType": "git"})
    viya.add("GET", r"/modelPublish/models$", {"items": [{"id": "pub-0"}], "count": 1})
    viya.add("POST", r"/modelManagement/publish$",
             {"items": [{"id": "pub-1", "name": "Risk", "state": "pending"}]}, status=201)
    result = await call(mcp, "publish_model", {
        "model_id": "m-1", "destination_name": "git1", "publish_name": "Risk", "replace": True,
        "git_remote_path": "models",
    })
    assert result["status"] == "publishing" and result["published"][0]["id"] == "pub-1"
    (_, kw), = viya.calls("POST", r"/modelManagement/publish$")
    assert kw["params"] == {"force": "true"} and kw["headers"]["Content-Type"] == mh.PUBLISH_REQUEST_TYPE
    assert kw["json"]["properties"] == {"gitRemotePath": "models"}
    assert kw["json"]["modelContents"][0]["sourceUri"] == "/modelRepository/models/m-1"


async def test_validate_published_model_scores_with_the_published_copy(fake, monkeypatch):
    mcp, viya = fake
    score = AsyncMock(return_value={"status": "completed", "rows": []})
    monkeypatch.setattr(mh, "score_test", score)
    viya.add("GET", r"/modelPublish/models/pub-1$", {
        "id": "pub-1", "name": "Tree", "destinationName": "maslocal", "state": "completed",
        "sourceUri": "/modelRepository/models/m-1",
    })
    result = await call(mcp, "validate_published_model", {"published_model_id": "pub-1", **SCORE_ARGS_NO_MODEL})
    assert result["status"] == "completed" and result["destination"] == "maslocal"
    kwargs = score.call_args.kwargs
    assert kwargs["object_type"] == "sas.publish.validation" and kwargs["object_uri"] == "/modelPublish/models/pub-1"
    assert kwargs["input_variables"] == ["LOAN", "JOB"]
    assert kwargs["extra_properties"]["publishDestination"] == "maslocal"


async def test_validate_published_model_finds_the_copy_or_says_why_not(fake, monkeypatch):
    mcp, viya = fake
    monkeypatch.setattr(mh, "score_test", AsyncMock(return_value={"status": "completed"}))
    viya.add("GET", r"/modelPublish/models$", {"items": [
        {"id": "pub-2", "name": "Tree", "destinationName": "maslocal", "state": "running",
         "sourceUri": "/modelRepository/models/m-1"},
    ], "count": 1})
    args = {"model_id": "m-1", "caslib_name": "Public", "table_name": "HMEQ"}
    assert (await call(mcp, "validate_published_model", args))["status"] == "not_ready"
    assert (await call(mcp, "validate_published_model", {**args, "model_id": "m-7"}))["status"] == "not_found"
    assert (await call(mcp, "validate_published_model", {"caslib_name": "P", "table_name": "T"}))["status"] == "refused"
    viya.add("GET", r"/modelPublish/models/pub-9$", {}, status=404)
    gone = await call(mcp, "validate_published_model",
                      {"published_model_id": "pub-9", "caslib_name": "P", "table_name": "T"})
    assert gone["status"] == "not_found"


# --- Tier 11: monitoring ----------------------------------------------------------------


async def test_create_performance_definition_uses_the_champion_by_default(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects/p-1$", {**PROJECT, "targetVariable": "BAD"})
    viya.add("GET", r"/projects/p-1/champion$", {"id": "m-1"})
    viya.add("GET", r"/caslibs/Public/tables$", {"items": [{"name": "CREDIT_1_Q1"}], "count": 1})
    viya.add("GET", r"/modelManagement/performanceTasks$", {"items": [], "count": 0})
    viya.add("POST", r"/modelManagement/performanceTasks$", {"id": "task-1", "name": "Probe performance"}, status=201)
    args = {"project_id": "p-1", "data_caslib": "Public", "data_prefix": "credit"}
    result = await call(mcp, "create_performance_definition", args)
    assert result["performance_definition_id"] == "task-1" and result["period_tables"] == ["CREDIT_1_Q1"]
    body = viya.body("POST", r"/performanceTasks$")
    assert body["modelIds"] == ["m-1"] and body["outputVariables"] == ["P_BAD1"]
    assert body["inputVariables"] == ["LOAN", "JOB"] and body["scoreExecutionRequired"] is True
    (_, kw), = viya.calls("GET", r"/caslibs/Public/tables$")
    assert kw["params"]["filter"] == "startsWith(name,'CREDIT_')"


async def test_create_performance_definition_needs_a_prepared_project(fake):
    mcp, viya = fake
    args = {"project_id": "p-1", "data_caslib": "Public", "data_prefix": "credit"}
    assert "set_project_champion" in (await call(mcp, "create_performance_definition", args))["message"]
    viya.add("GET", r"/modelRepository/projects/p-1$", {**PROJECT, "targetVariable": "BAD"})
    viya.add("GET", r"/modelManagement/performanceTasks$", {"items": [], "count": 0})
    viya.add("GET", r"/projects/p-1/champion$", {}, status=404)
    assert "no champion" in (await call(mcp, "create_performance_definition", args))["message"]
    viya.add("GET", r"/caslibs/Public/tables$", {"items": [], "count": 0})
    viya.add("POST", r"/performanceTasks$", {"id": "task-2"}, status=201)
    more = {**args, "model_ids": ["m-1"], "output_variables": ["I_BAD"]}
    result = await call(mcp, "create_performance_definition", more)
    assert "warning" in result and viya.body("POST", r"/performanceTasks$")["outputVariables"] == ["I_BAD"]


async def test_create_performance_definition_returns_or_replaces_the_projects_one(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects/p-1$", {**PROJECT, "targetVariable": "BAD"})
    viya.add("GET", r"/modelManagement/performanceTasks$", {"items": [
        {"id": "task-1", "name": "Old", "modelIds": ["m-0"], "dataLibrary": "Public", "dataPrefix": "OLD"},
    ], "count": 1})
    args = {"project_id": "p-1", "data_caslib": "Public", "data_prefix": "credit"}
    kept = await call(mcp, "create_performance_definition", args)
    assert kept["status"] == "exists" and kept["performance_definition"]["id"] == "task-1"
    assert "replace=true" in kept["message"] and not viya.calls("POST", r"/performanceTasks$")
    viya.add("GET", r"/projects/p-1/champion$", {"id": "m-1"})
    viya.add("GET", r"/caslibs/Public/tables$", {"items": [], "count": 0})
    viya.add("GET", r"/performanceTasks/task-1$", {"id": "task-1", "name": "Old", "traceOn": True, "links": []})
    viya.add("PUT", r"/performanceTasks/task-1$", lambda url, kw: {**kw["json"], "id": "task-1"})
    replaced = await call(mcp, "create_performance_definition", {**args, "replace": True})
    assert replaced["status"] == "replaced" and replaced["performance_definition_id"] == "task-1"
    (_, put), = viya.calls("PUT", r"/performanceTasks/task-1$")
    assert put["headers"]["If-Match"] == '"e1"' and put["headers"]["Content-Type"] == mh.PERFORMANCE_TASK_TYPE
    assert put["json"]["dataPrefix"] == "credit" and put["json"]["traceOn"] is False and "links" not in put["json"]


async def test_run_performance_definition_waits_and_reports_failed_jobs(fake):
    mcp, viya = fake
    base = r"/modelManagement/performanceTasks/task-1"
    viya.add("GET", base + "$", {"id": "task-1", "name": "Probe performance", "projectId": "p-1"})
    viya.add("POST", base + "$", {}, status=202)
    viya.add("GET", base + "/state$", "failed")
    viya.add("GET", base + "/performanceJobs$", {"items": [
        {"id": "job-0", "model": "Tree", "modelId": "m-1", "state": "completed", "startTime": "2026-11-12T09:00:00Z"},
        {"id": "job-1", "model": "Tree", "modelId": "m-1", "state": "failed", "startTime": "2026-11-13T09:00:00Z"},
    ]})
    viya.add("GET", base + "/performanceJobs/job-1/log$", "NOTE: x\nERROR: no table")
    result = await call(mcp, "run_performance_definition", {"performance_definition_id": "task-1", "wait_seconds": 30})
    assert result["status"] == "failed" and result["log_errors"] == {"Tree": ["ERROR: no table"]}
    assert [j["id"] for j in result["jobs"]] == ["job-1"]
    assert result["name"] == "Probe performance" and not viya.calls("POST", r"/kpis$")
    viya.add("GET", r"/performanceTasks/task-9$", {}, status=404)
    gone = await call(mcp, "run_performance_definition", {"performance_definition_id": "task-9"})
    assert gone["status"] == "not_found" and not viya.calls("POST", r"/task-9$")


def test_state_text_reads_plain_and_json_bodies():
    assert ml._state_text("completed\n") == "completed"
    assert ml._state_text('{"state": "running"}') == "running"
    assert ml._state_text('"pending"') == "pending"
    assert ml._state_text("{not json") == "{not json"


RULE_AUC = {
    "id": "r-1", "projectId": "p-1", "name": "AUC Less than 0.9", "kpi": "_AUC_", "severity": "high",
    "operator": "lessThan", "violations": 1, "value": 0.9, "kpiCategory": "ACCURACY", "triggered": True,
    "kpiVariable": None, "links": [],
}
HEALTH = {
    "items": [
        {"category": "ACCURACY", "metric": "_AUC_", "value": "0.7171", "success": False,
         "alertRuleName": "AUC Less than 0.9", "alertCondition": "lessThan", "alertThreshold": 0.9},
        {"category": "MODELDRIFT", "metric": "devIndex", "value": "0.05", "success": True,
         "alertRuleName": "Deviation index: LOAN Greater than 0.1", "alertCondition": "greaterThan",
         "alertThreshold": 0.1, "kpiVariable": "LOAN", "kpiVariableRole": "INPUT"},
    ],
    "kpisAvailable": True,
    "rulesAvailable": True,
}


def _rules(viya, rules=()):
    viya.add("GET", r"/projects/p-1/alertRules$", {"items": list(rules), "count": len(rules)})


def _champion(viya, health=HEALTH):
    viya.add("GET", r"/projects/p-1/champion$", {"id": "m-1", "name": "Tree Astore"})
    viya.add("GET", r"/projects/p-1/models/m-1/healthMetrics$", health)


async def test_run_performance_definition_calculates_kpis_and_reports_breached_alerts(fake):
    mcp, viya = fake
    base = r"/modelManagement/performanceTasks/task-1"
    viya.add("GET", r"/modelManagement/performanceTasks$", {"items": [
        {"id": "task-1", "name": "Probe performance", "modelIds": ["m-1"], "dataLibrary": "Public",
         "dataPrefix": "CREDIT"},
    ], "count": 1})
    viya.add("GET", base + "$", {"id": "task-1", "name": "Probe performance", "projectId": "p-1"})
    viya.add("POST", base + "$", {}, status=202)
    viya.add("GET", base + "/state$", '{"state":"completed"}')
    viya.add("GET", base + "/performanceJobs$",
             {"items": [{"id": "job-1", "state": "completed", "model": "Tree Astore", "modelId": "m-1"}]})
    viya.add("GET", base + "/summary$", {"models": [{
        "modelId": "m-1", "rank": 1, "totalObservations": 5000, "periods": [{"name": "P1"}, {"name": "P2"}],
        "kpi": {"misClassification": "0.15", "recall": "0.46"},
    }]})
    viya.add("POST", r"/modelManagement/projects/p-1/kpis$", {"items": [{"name": "_AUC_"}, {"name": "_MISC_"}]})
    _champion(viya)
    done = await call(mcp, "run_performance_definition", {"project_id": "p-1", "wait_seconds": 30})
    assert done["status"] == "completed" and done["performance_definition_id"] == "task-1" and done["kpis"] == 2
    assert done["summary"] == [{"model": "Tree Astore", "rank": 1, "observations": 5000, "periods": ["P1", "P2"],
                                "kpi": {"misClassification": 0.15, "recall": 0.46}}]
    assert "log_errors" not in done and "warning" not in done
    assert [(a["rule"], a["value"]) for a in done["breached_alerts"]] == [("AUC Less than 0.9", 0.7171)]
    assert "get_project_kpis" in done["next"]
    (_, kw), = viya.calls("GET", r"/modelManagement/performanceTasks$")
    assert kw["params"]["filter"] == "eq(projectId,'p-1')"
    (_, kw), = viya.calls("POST", r"/kpis$")
    assert kw["json"] == {} and kw["headers"]["Accept"] == mh.COLLECTION_TYPE


async def test_run_performance_definition_completed_with_errors_still_calculates_the_kpis(fake):
    mcp, viya = fake
    base = r"/modelManagement/performanceTasks/task-1"
    viya.add("GET", base + "$", {"id": "task-1", "name": "Probe performance", "projectId": "p-1"})
    viya.add("POST", base + "$", {}, status=202)
    viya.add("GET", base + "/state$", "completedWithErrors")
    viya.add("GET", base + "/performanceJobs$", {"items": [
        {"id": "job-1", "state": "completed", "model": "Tree", "modelId": "m-1"},
        {"id": "job-2", "state": "completedWithErrors", "model": "Logit", "modelId": "m-2"},
    ]})
    viya.add("GET", base + "/performanceJobs/job-2/log$", "ERROR: Variable P_BAD1 not found.\nNOTE: done")
    viya.add("GET", base + "/summary$", {}, status=404)
    viya.add("POST", r"/modelManagement/projects/p-1/kpis$", {"items": [{"name": "_AUC_"}]})
    viya.add("GET", r"/projects/p-1/champion$", {}, status=404)
    result = await call(mcp, "run_performance_definition", {"performance_definition_id": "task-1", "wait_seconds": 30})
    assert result["log_errors"] == {"Logit": ["ERROR: Variable P_BAD1 not found."]}
    assert "log_errors" in result["warning"]
    assert result["kpis"] == 1 and "summary" not in result and "no champion" in result["alerts_note"]


async def test_run_performance_definition_by_project_needs_exactly_one_definition(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelManagement/performanceTasks$", {"items": [], "count": 0})
    none = await call(mcp, "run_performance_definition", {"project_id": "p-1"})
    assert none["status"] == "refused" and "create_performance_definition" in none["message"]
    viya.add("GET", r"/modelManagement/performanceTasks$", {"items": [{"id": "a"}, {"id": "b"}], "count": 2})
    several = await call(mcp, "run_performance_definition", {"project_id": "p-1"})
    assert several["status"] == "choose" and [d["id"] for d in several["definitions"]] == ["a", "b"]
    assert (await call(mcp, "run_performance_definition", {}))["status"] == "refused"


async def test_get_project_kpis_reports_rules_alerts_and_definitions(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelManagement/projects/p-1/kpis$",
             {"items": [{"name": "_AUC_", "value": "0.71", "links": []}], "count": 1})
    viya.add("GET", r"/modelManagement/performanceTasks$", {"items": [{"id": "task-1", "name": "Probe"}], "count": 1})
    _rules(viya)
    plain = await call(mcp, "get_project_kpis", {"project_id": "p-1"})
    assert plain["kpis"] == [{"name": "_AUC_", "value": "0.71"}] and plain["alert_rules"] == []
    assert "alerts" not in plain and plain["performance_definitions"][0]["id"] == "task-1"
    (_, kw), = viya.calls("GET", r"/kpis$")
    assert kw["headers"]["Accept"] == mh.COLLECTION_TYPE
    _rules(viya, [RULE_AUC])
    viya.add("GET", r"/projects/p-1/models/m-2/healthMetrics$", HEALTH)
    checked = await call(mcp, "get_project_kpis", {"project_id": "p-1", "model_id": "m-2"})
    assert checked["alert_rules"] == [{
        "id": "r-1", "name": "AUC Less than 0.9", "kpi": "_AUC_", "category": "ACCURACY", "operator": "lessThan",
        "threshold": 0.9, "severity": "high", "occurrences": 1,
    }]
    assert checked["alerts_for_model"] == "m-2" and checked["alerts"][0]["breached"] is True
    assert checked["alerts"][1] == {
        "rule": "Deviation index: LOAN Greater than 0.1", "kpi": "devIndex", "variable": "LOAN", "value": 0.05,
        "operator": "greaterThan", "threshold": 0.1, "breached": False,
    }
    viya.add("GET", r"/projects/p-1/champion$", {}, status=404)
    no_champion = await call(mcp, "get_project_kpis", {"project_id": "p-1"})
    assert no_champion["alerts"] == [] and "no champion" in no_champion["alerts_note"]
    viya.add("GET", r"/modelManagement/projects/p-1/kpis$", {"items": [], "count": 0})
    assert "run_performance_definition" in (await call(mcp, "get_project_kpis", {"project_id": "p-1"}))["message"]
    viya.add("GET", r"/modelManagement/projects/p-9/kpis$", {}, status=404)
    assert (await call(mcp, "get_project_kpis", {"project_id": "p-9"}))["status"] == "not_found"


# --- Tier 11: KPI alert rules -----------------------------------------------------------


def test_alert_kpi_reads_names_labels_and_aliases():
    assert ml.alert_kpi("AUC") == ("_AUC_", "ACCURACY", "AUC")
    assert ml.alert_kpi("_misc_") == ml.alert_kpi("misclassification") == ("_MISC_", "ACCURACY", "Misclassification")
    assert ml.alert_kpi("r2")[0] == "_RSquared_" and ml.alert_kpi("cumlift10")[0] == "_Cumlift10_"
    assert ml.alert_kpi("drift") == ml.alert_kpi("devIndex") == ("devIndex", "MODELDRIFT", "Deviation index")
    assert ml.alert_kpi("predictive parity") == ("PredictiveParity", "FAIRNESS", "Predictive parity")
    assert ml.alert_kpi("RMSE") == ("RMSE", "ACCURACY", "RMSE")
    assert ml.alert_kpi("KS") is None


def test_alert_operator_and_rule_names():
    assert ml.alert_operator("greater than") == ml.alert_operator(">") == "greaterThan"
    assert ml.alert_operator("below") == ml.alert_operator("less_than") == "lessThan"
    assert ml.alert_operator("equal") is None
    assert ml.rule_name(None, "AUC", "", "lessThan", 0.9, False) == "AUC Less than 0.9"
    assert ml.rule_name(None, "Deviation index", "LOAN", "greaterThan", 0.1, True) == (
        "Deviation index: LOAN Greater than 0.1")
    assert ml.rule_name("Drift of {variable}", "x", "JOB", "greaterThan", 1, True) == "Drift of JOB"
    assert ml.rule_name("Drift", "x", "JOB", "greaterThan", 1, True) == "Drift: JOB"
    assert ml.rule_name("Mine", "x", "", "greaterThan", 1, False) == "Mine"


async def test_set_kpi_alert_rule_creates_a_rule_and_reports_the_champion_now(fake):
    mcp, viya = fake
    _rules(viya)
    viya.add("POST", r"/projects/p-1/alertRules$", lambda url, kw: {**kw["json"], "id": "r-1"}, status=201)
    _champion(viya)
    args = {"project_id": "p-1", "kpi": "auc", "operator": "below", "threshold": 0.9, "severity": "High"}
    result = await call(mcp, "set_kpi_alert_rule", args)
    assert viya.body("POST", r"/alertRules$") == {
        "projectId": "p-1", "name": "AUC Less than 0.9", "kpi": "_AUC_", "severity": "high", "operator": "lessThan",
        "violations": 1, "value": 0.9, "kpiCategory": "ACCURACY", "kpiVariable": "", "kpiVariableRole": "",
    }
    assert result["status"] == "set" and result["project"] == "Probe"
    assert result["rules"][0]["action"] == "created" and result["rules"][0]["id"] == "r-1"
    assert result["now"] == [{
        "rule": "AUC Less than 0.9", "kpi": "_AUC_", "value": 0.7171, "operator": "lessThan", "threshold": 0.9,
        "breached": True,
    }]


async def test_set_kpi_alert_rule_sets_one_drift_rule_per_variable_updating_an_existing_one(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects/p-1/variables$", {"items": [
        {"name": "LOAN", "role": "input"}, {"name": "P_BAD1", "role": "output"},
    ], "count": 2})
    _rules(viya, [{"id": "r-7", "kpi": "devIndex", "kpiVariable": "LOAN", "operator": "greaterThan", "value": 0.5}])
    viya.add("GET", r"/alertRules/r-7$", {"id": "r-7", "kpi": "devIndex", "versionNum": 3, "links": [{"rel": "self"}]})
    viya.add("PUT", r"/alertRules/r-7$", lambda url, kw: {**kw["json"], "id": "r-7"})
    viya.add("POST", r"/projects/p-1/alertRules$", lambda url, kw: {**kw["json"], "id": "r-8"}, status=201)
    viya.add("GET", r"/projects/p-1/champion$", {}, status=404)
    args = {"project_id": "p-1", "kpi": "drift", "operator": "greaterThan", "threshold": 0.1,
            "variables": ["loan", "P_BAD1"]}
    result = await call(mcp, "set_kpi_alert_rule", args)
    assert [(r["id"], r["action"], r["variable"]) for r in result["rules"]] == [
        ("r-7", "updated", "LOAN"), ("r-8", "created", "P_BAD1")]
    (_, put), = viya.calls("PUT", r"/alertRules/r-7$")
    assert put["headers"]["If-Match"] == '"e1"' and put["json"]["versionNum"] == 3 and "links" not in put["json"]
    assert put["json"]["value"] == 0.1 and put["json"]["kpiVariableRole"] == "INPUT"
    assert put["json"]["name"] == "Deviation index: LOAN Greater than 0.1"
    assert viya.body("POST", r"/alertRules$")["kpiVariableRole"] == "OUTPUT"
    assert result["now"] == [] and "no champion" in result["note"]


@pytest.mark.parametrize(
    "args, expected",
    [
        ({"kpi": "KS"}, "Unknown KPI"),
        ({"operator": "equal"}, "greaterThan or lessThan"),
        ({"severity": "urgent"}, "low, medium or high"),
        ({"occurrences": 0}, "1 or more"),
        ({"kpi": "devIndex"}, "needs the variables"),
        ({"kpi": "PredictiveParity"}, "needs the variables"),
        ({"variables": ["LOAN"]}, "takes no variables"),
        ({"kpi": "devIndex", "variables": ["A", "B"], "alert_rule_id": "r-1"}, "at most one variable"),
    ],
)
async def test_set_kpi_alert_rule_refuses_what_model_manager_would_not_take(fake, args, expected):
    mcp, _ = fake
    base = {"project_id": "p-1", "kpi": "AUC", "operator": "lessThan", "threshold": 0.9}
    result = await call(mcp, "set_kpi_alert_rule", {**base, **args})
    assert result["status"] == "refused" and expected in result["message"]


async def test_set_kpi_alert_rule_checks_variables_rule_ids_and_the_project(fake):
    mcp, viya = fake
    viya.add("GET", r"/modelRepository/projects/p-1/variables$", {"items": [{"name": "GENDER", "role": "input"}]})
    _rules(viya)
    fairness = {"project_id": "p-1", "kpi": "predictive parity", "operator": ">", "threshold": 0.1}
    wrong = await call(mcp, "set_kpi_alert_rule", {**fairness, "variables": ["AGE"]})
    assert wrong["status"] == "refused" and wrong["project_variables"] == ["GENDER"]
    missing = await call(mcp, "set_kpi_alert_rule", {
        "project_id": "p-1", "kpi": "AUC", "operator": "<", "threshold": 0.9, "alert_rule_id": "r-x"})
    assert missing["status"] == "not_found"
    viya.add("POST", r"/projects/p-1/alertRules$", lambda url, kw: {**kw["json"], "id": "r-2"}, status=201)
    viya.add("GET", r"/projects/p-1/champion$", {}, status=404)
    await call(mcp, "set_kpi_alert_rule", {**fairness, "variables": ["gender"], "name": "Parity {variable}"})
    body = viya.body("POST", r"/alertRules$")
    assert (body["kpi"], body["kpiCategory"], body["kpiVariable"], body["kpiVariableRole"], body["name"]) == (
        "PredictiveParity", "FAIRNESS", "GENDER", "SENSITIVE", "Parity GENDER")
    viya.add("GET", r"/modelRepository/projects/p-9$", {}, status=404)
    gone = await call(mcp, "set_kpi_alert_rule", {"project_id": "p-9", "kpi": "AUC", "operator": "<", "threshold": 1})
    assert gone["status"] == "not_found"


async def test_delete_kpi_alert_rule(fake):
    mcp, viya = fake
    viya.add("GET", r"/projects/p-1/alertRules/r-1$", RULE_AUC)
    viya.add("DELETE", r"/projects/p-1/alertRules/r-1$", {}, status=204)
    result = await call(mcp, "delete_kpi_alert_rule", {"project_id": "p-1", "alert_rule_id": "r-1"})
    assert result["status"] == "deleted" and result["rule"]["name"] == "AUC Less than 0.9"
    assert len(viya.calls("DELETE", r"/alertRules/r-1$")) == 1
    viya.add("GET", r"/alertRules/r-404$", {}, status=404)
    gone = await call(mcp, "delete_kpi_alert_rule", {"project_id": "p-1", "alert_rule_id": "r-404"})
    assert gone["status"] == "not_found" and not viya.calls("DELETE", r"r-404")
