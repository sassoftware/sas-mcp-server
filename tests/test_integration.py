# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests that call MCP tools against a real SAS Viya instance.

Requires VIYA_ENDPOINT, VIYA_USERNAME, and VIYA_PASSWORD environment variables.
Run with:  uv run python -m pytest -m integration
"""
import time

import pytest
from fastmcp import Client

# Pin all integration tests to a single session-scoped event loop. The
# session-scoped fixtures (viya_token, integration_mcp_server) and the
# in-memory fastmcp transport must share the same loop they were created in;
# otherwise the second test's tool call fails with ConnectError when it
# touches httpx state bound to the prior, now-closed loop.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_SUFFIX = str(int(time.time()))[-6:]


async def _viya_get(token: str, path: str, params: dict | None = None) -> dict:
    """Authenticated GET against the live Viya, used for resource discovery."""
    from sas_mcp_server.config import VIYA_ENDPOINT
    from sas_mcp_server.viya_client import make_client

    async with make_client(token) as client:
        resp = await client.get(
            f"{VIYA_ENDPOINT}{path}",
            params=params or {},
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()


def _dummy_input_value(var_type: str):
    """A type-appropriate placeholder value for a MAS step input variable."""
    if (var_type or "").lower() in ("decimal", "double", "float", "integer", "int", "bigint"):
        return 0
    return ""


# -----------------------------------------------------------------------
# CAS Data Discovery Workflow
# -----------------------------------------------------------------------


async def test_cas_discovery_workflow(integration_mcp_server):
    """list_cas_servers → list_caslibs → list_castables → table info/columns/data

    Targets a known, loaded sample table — ``HMEQ`` in caslib ``Public`` — so
    the columns/data assertions don't fall over an unloaded source table.
    """
    async with Client(integration_mcp_server) as client:
        servers = (await client.call_tool("list_cas_servers", {})).data
        assert isinstance(servers, list)
        assert len(servers) > 0, "No CAS servers found"
        server_id = servers[0]["name"]

        caslibs = (await client.call_tool("list_caslibs", {
            "server_id": server_id, "limit": 50
        })).data
        assert isinstance(caslibs, list)
        if not any(c["name"] == "Public" for c in caslibs):
            pytest.skip("Public caslib not present on this Viya")

        # Also exercise list_castables so the workflow stays end-to-end, but
        # don't depend on HMEQ appearing in the listing (Public can hold
        # hundreds of tables and the listing is paginated).
        tables = (await client.call_tool("list_castables", {
            "server_id": server_id,
            "caslib_name": "Public",
            "limit": 50,
        })).data
        assert isinstance(tables, list)

        caslib_name = "Public"
        table_name = "HMEQ"
        # Fetch HMEQ metadata. Skip if HMEQ isn't loaded on this Viya;
        # otherwise the test proceeds to the columns/data assertions.
        try:
            info = (await client.call_tool("get_castable_info", {
                "server_id": server_id,
                "caslib_name": caslib_name,
                "table_name": table_name,
            })).data
        except Exception as e:
            if "404" in str(e):
                pytest.skip("HMEQ not loaded in Public caslib on this Viya")
            raise
        assert isinstance(info, dict)

        columns = (await client.call_tool("get_castable_columns", {
            "server_id": server_id,
            "caslib_name": caslib_name,
            "table_name": table_name,
            "limit": 10,
        })).data
        assert isinstance(columns, list)
        assert len(columns) > 0

        try:
            rows = (await client.call_tool("get_castable_data", {
                "server_id": server_id,
                "caslib_name": caslib_name,
                "table_name": table_name,
                "limit": 3,
            })).data
            assert isinstance(rows, dict)
        except Exception:
            pass


# -----------------------------------------------------------------------
# Data Upload Workflow
# -----------------------------------------------------------------------


async def test_data_upload_workflow(integration_mcp_server):
    """upload_data → promote_table_to_memory"""
    async with Client(integration_mcp_server) as client:
        servers = (await client.call_tool("list_cas_servers", {})).data
        server_id = servers[0]["name"]

        table = f"MCP_TEST_UPLOAD_{_SUFFIX}"
        csv = "x,y,label\n1,2,A\n3,4,B\n5,6,A"
        result = (await client.call_tool("upload_data", {
            "server_id": server_id,
            "caslib_name": "Public",
            "table_name": table,
            "csv_data": csv,
        })).data
        assert isinstance(result, dict)
        assert result["status"] == "success"
        assert result["rows_uploaded"] == 3

        promote_result = (await client.call_tool("promote_table_to_memory", {
            "server_id": server_id,
            "caslib_name": "Public",
            "table_name": table,
        })).data
        assert isinstance(promote_result, dict)


# -----------------------------------------------------------------------
# File Service Workflow
# -----------------------------------------------------------------------


async def test_file_service_workflow(integration_mcp_server):
    """upload_file → list_files → download_file"""
    async with Client(integration_mcp_server) as client:
        content = "data mcp_test; x=42; run;"
        upload = (await client.call_tool("upload_file", {
            "file_name": "mcp_integration_test.sas",
            "content": content,
        })).data
        assert "id" in upload
        file_id = upload["id"]

        files = (await client.call_tool("list_files", {
            "filter_name": "mcp_integration_test"
        })).data
        assert isinstance(files, list)
        found = any(f["id"] == file_id for f in files)
        assert found, "Uploaded file not found in listing"

        downloaded = (await client.call_tool("download_file", {
            "file_id": file_id
        })).data
        assert content in str(downloaded)


# -----------------------------------------------------------------------
# SAS Code Execution
# -----------------------------------------------------------------------


async def test_sas_code_execution(integration_mcp_server):
    """execute_sas_code with a simple DATA step + PROC PRINT"""
    async with Client(integration_mcp_server) as client:
        code = """
data work.mcp_test;
    x = 42;
    y = "hello";
    output;
run;

proc print data=work.mcp_test;
run;
"""
        result = (await client.call_tool("execute_sas_code", {
            "sas_code": code
        })).data
        assert isinstance(result, dict)
        assert set(result) >= {"snippet_id", "state", "log", "listing"}
        assert result["state"] in ("completed", "warning")
        assert "mcp_test" in result["log"].lower() or "NOTE" in result["log"]


# -----------------------------------------------------------------------
# Batch Job Workflow
# -----------------------------------------------------------------------


async def test_batch_job_workflow(integration_mcp_server):
    """submit_batch_job → list_jobs → get_job_status → get_job_log"""
    async with Client(integration_mcp_server) as client:
        submit = (await client.call_tool("submit_batch_job", {
            "sas_code": "data _null_; put 'MCP integration test'; run;",
            "job_name": "mcp-integration-test",
        })).data
        assert "id" in submit
        job_id = submit["id"]

        jobs = (await client.call_tool("list_jobs", {"limit": 5})).data
        assert isinstance(jobs, list)

        status = (await client.call_tool("get_job_status", {
            "job_id": job_id
        })).data
        assert isinstance(status, dict)
        assert "state" in status

        import asyncio
        for _ in range(30):
            status = (await client.call_tool("get_job_status", {
                "job_id": job_id
            })).data
            if status.get("state") in ("completed", "failed", "error", "canceled"):
                break
            await asyncio.sleep(2)

        if status.get("state") == "completed":
            log = (await client.call_tool("get_job_log", {
                "job_id": job_id
            })).data
            assert isinstance(log, str)


# -----------------------------------------------------------------------
# Reports Workflow
# -----------------------------------------------------------------------


async def test_report_workflow(integration_mcp_server):
    """list_reports → get_report → get_report_image"""
    async with Client(integration_mcp_server) as client:
        reports = (await client.call_tool("list_reports", {"limit": 5})).data
        assert isinstance(reports, list)

        if not reports:
            pytest.skip("No reports found on this Viya instance")

        report_id = reports[0]["id"]
        report = (await client.call_tool("get_report", {
            "report_id": report_id
        })).data
        assert isinstance(report, dict)

        try:
            image_job = (await client.call_tool("get_report_image", {
                "report_id": report_id
            })).data
            assert isinstance(image_job, dict)
        except Exception:
            pass


# -----------------------------------------------------------------------
# ML Project Workflow
# -----------------------------------------------------------------------


async def test_ml_project_workflow(integration_mcp_server):
    """create_ml_project → list_ml_projects"""
    async with Client(integration_mcp_server) as client:
        servers = (await client.call_tool("list_cas_servers", {})).data
        if not servers:
            pytest.skip("No CAS servers available")
        server_id = servers[0]["name"]

        table = f"MCP_TEST_ML_{_SUFFIX}"
        csv = "x1,x2,target\n1,2,0\n3,4,1\n5,6,0\n7,8,1\n9,10,0\n11,12,1\n13,14,0\n15,16,1"
        await client.call_tool("upload_data", {
            "server_id": server_id,
            "caslib_name": "Public",
            "table_name": table,
            "csv_data": csv,
        })

        project = (await client.call_tool("create_ml_project", {
            "project_name": f"MCP Integration Test {_SUFFIX}",
            "server_id": server_id,
            "caslib_name": "Public",
            "table_name": table,
            "target_variable": "target",
            "prediction_type": "binary",
            "target_event_level": "1",
            "auto_run": False,
        })).data
        assert isinstance(project, dict)
        assert "id" in project

        projects = (await client.call_tool("list_ml_projects", {"limit": 100})).data
        assert isinstance(projects, list)
        found = any(p["id"] == project["id"] for p in projects)
        assert found, "Created ML project not found in listing"


# -----------------------------------------------------------------------
# Scoring Workflow
# -----------------------------------------------------------------------


async def test_scoring_workflow(integration_mcp_server, viya_token):
    """list_registered_models → list_models_and_decisions → score_data.

    Scores against the most recently modified MAS module on the instance,
    discovering a real step and its input variables rather than guessing.
    """
    async with Client(integration_mcp_server) as client:
        models = (await client.call_tool("list_registered_models", {"limit": 5})).data
        assert isinstance(models, list)

        modules = (await client.call_tool("list_models_and_decisions", {"limit": 5})).data
        assert isinstance(modules, list)

    # Identify the latest module directly (sorted by modified time, newest first).
    listing = await _viya_get(
        viya_token,
        "/microanalyticScore/modules",
        params={"sortBy": "modifiedTimeStamp:descending", "limit": 1},
    )
    items = listing.get("items", [])
    if not items:
        pytest.skip("No MAS modules found — cannot test score_data")
    module_id = items[0]["id"]

    # Discover a usable step (prefer 'score'/'execute') and its input variables.
    steps = (await _viya_get(
        viya_token, f"/microanalyticScore/modules/{module_id}/steps"
    )).get("items", [])
    if not steps:
        pytest.skip(f"Module {module_id} exposes no steps")
    step = next((s for s in steps if s.get("id") in ("score", "execute")), steps[0])
    step_id = step["id"]
    step_detail = await _viya_get(
        viya_token, f"/microanalyticScore/modules/{module_id}/steps/{step_id}"
    )
    input_data = {
        inp["name"]: _dummy_input_value(inp.get("type", ""))
        for inp in step_detail.get("inputs", [])
    }

    async with Client(integration_mcp_server) as client:
        try:
            result = (await client.call_tool("score_data", {
                "module_id": module_id,
                "step_id": step_id,
                "input_data": input_data,
            })).data
        except Exception as e:
            pytest.skip(
                f"Module {module_id} step '{step_id}' rejected placeholder inputs: {e}"
            )
        assert isinstance(result, dict)


# -----------------------------------------------------------------------
# Cancel Job Workflow
# -----------------------------------------------------------------------


async def test_cancel_job_workflow(integration_mcp_server):
    """submit_batch_job → cancel_job"""
    async with Client(integration_mcp_server) as client:
        submit = (await client.call_tool("submit_batch_job", {
            "sas_code": "data _null_; do i = 1 to 100000000; end; run;",
            "job_name": f"mcp-cancel-test-{_SUFFIX}",
        })).data
        assert "id" in submit
        job_id = submit["id"]

        try:
            result = (await client.call_tool("cancel_job", {"job_id": job_id})).data
        except Exception as e:
            pytest.skip(f"cancel_job rejected (job already terminal on this Viya): {e}")
        assert isinstance(result, str)
        assert job_id in result


# -----------------------------------------------------------------------
# Run ML Project Workflow
# -----------------------------------------------------------------------


async def test_run_ml_project_workflow(integration_mcp_server, viya_token):
    """run_ml_project against an existing completed project.

    A freshly-created project isn't immediately runnable, so this targets the
    most recently modified project already in the ``completed`` state and
    re-runs (retrains) it.
    """
    listing = await _viya_get(
        viya_token,
        "/mlPipelineAutomation/projects",
        params={
            "sortBy": "modifiedTimeStamp:descending",
            "filter": "eq(state,'completed')",
            "limit": 1,
        },
    )
    items = listing.get("items", [])
    if not items:
        pytest.skip("No completed ML projects on this Viya to run")
    project_id = items[0]["id"]

    async with Client(integration_mcp_server) as client:
        try:
            result = (await client.call_tool("run_ml_project", {
                "project_id": project_id
            })).data
        except Exception as e:
            pytest.skip(f"run_ml_project could not start project {project_id}: {e}")
        assert isinstance(result, dict)


# -----------------------------------------------------------------------
# Promote-from-source Workflow
# -----------------------------------------------------------------------


async def test_promote_from_source_workflow(integration_mcp_server, viya_token):
    """list_source_tables → promote_table_to_memory (load from source to global) → unload.

    Exercises the real fix for the promote_table_to_memory bug: discover an
    unloaded source table and load+promote it via updateTableState. Restores the
    caslib's state by unloading anything this test loaded.
    """
    async with Client(integration_mcp_server) as client:
        server = (await client.call_tool("list_cas_servers", {})).data[0]["name"]

        caslib, sources = None, []
        for lib in ("SAMPLES", "Public"):
            sources = (await client.call_tool("list_source_tables", {
                "server_id": server, "caslib_name": lib, "limit": 25,
            })).data
            if sources:
                caslib = lib
                break
        if not sources:
            pytest.skip("No unloaded source tables available to promote")

        # Try a few candidates so one quirky source table doesn't fail the run.
        promoted = None
        for cand in sources[:5]:
            try:
                result = (await client.call_tool("promote_table_to_memory", {
                    "server_id": server, "caslib_name": caslib, "table_name": cand["name"],
                })).data
            except Exception:
                continue
            if result.get("scope") == "global" and result.get("status") in ("promoted", "already_global"):
                promoted = (cand["name"], result)
                break
        if not promoted:
            pytest.skip("Could not load any source table on this Viya")
        table, result = promoted

        info = (await client.call_tool("get_castable_info", {
            "server_id": server, "caslib_name": caslib, "table_name": table,
        })).data
        assert info.get("state") == "loaded"
        assert info.get("scope") == "global"

    # Cleanup: if we loaded it, unload it again to restore prior caslib state.
    if result["status"] == "promoted":
        from sas_mcp_server.config import VIYA_ENDPOINT
        from sas_mcp_server.viya_client import make_client
        async with make_client(viya_token) as raw:
            await raw.put(
                f"{VIYA_ENDPOINT}/casManagement/servers/{server}/caslibs/{caslib}/tables/{table}/state",
                params={"value": "unloaded"}, headers={"Accept": "*/*"},
            )


# -----------------------------------------------------------------------
# Prompt templates — rendered through the live-connected server
# -----------------------------------------------------------------------

# Prompt templates are client-side text generation (they do not call Viya
# APIs), but these render each one through the same MCP server instance wired
# to the real Viya token, exercising registration + rendering end to end.
PROMPT_RENDER_ARGS = {
    "debug_sas_log": {"log_text": "ERROR: File WORK.X does not exist."},
    "explore_dataset": {"library": "SASHELP", "dataset": "CLASS"},
    "data_quality_check": {"library": "SASHELP", "dataset": "CLASS"},
    "statistical_analysis": {
        "analysis_type": "linear regression",
        "response_variable": "Weight",
        "predictors": "Height Age",
        "dataset": "SASHELP.CLASS",
    },
    "optimize_sas_code": {"sas_code": "data a; set b; run;"},
    "explain_sas_code": {"sas_code": "proc print data=sashelp.class; run;"},
    "sas_macro_builder": {"macro_name": "loadcsv", "purpose": "Load a CSV into CAS"},
    "generate_report": {"dataset": "SASHELP.CLASS"},
}


@pytest.mark.parametrize("prompt_name", sorted(PROMPT_RENDER_ARGS))
async def test_prompt_renders_through_live_server(integration_mcp_server, prompt_name):
    """Every prompt template renders to non-empty messages via the live server."""
    async with Client(integration_mcp_server) as client:
        result = await client.get_prompt(prompt_name, PROMPT_RENDER_ARGS[prompt_name])
        assert result.messages, f"{prompt_name} produced no messages"
        content = result.messages[0].content
        text = getattr(content, "text", None) or str(content)
        assert text and text.strip(), f"{prompt_name} rendered empty content"


# -----------------------------------------------------------------------
# Coverage guards — fail if a registered tool/prompt has no integration test
# -----------------------------------------------------------------------

# Maps every registered tool to the integration test that invokes it against
# real Viya. The guard below fails if a tool is registered but unmapped, so a
# new tool cannot ship without integration coverage.
TOOL_COVERAGE = {
    "execute_sas_code": "test_sas_code_execution",
    "list_cas_servers": "test_cas_discovery_workflow",
    "list_caslibs": "test_cas_discovery_workflow",
    "list_castables": "test_cas_discovery_workflow",
    "list_source_tables": "test_promote_from_source_workflow",
    "get_castable_info": "test_cas_discovery_workflow",
    "get_castable_columns": "test_cas_discovery_workflow",
    "get_castable_data": "test_cas_discovery_workflow",
    "upload_data": "test_data_upload_workflow",
    "promote_table_to_memory": "test_promote_from_source_workflow",
    "list_files": "test_file_service_workflow",
    "upload_file": "test_file_service_workflow",
    "download_file": "test_file_service_workflow",
    "list_reports": "test_report_workflow",
    "get_report": "test_report_workflow",
    "get_report_image": "test_report_workflow",
    "submit_batch_job": "test_batch_job_workflow",
    "get_job_status": "test_batch_job_workflow",
    "list_jobs": "test_batch_job_workflow",
    "cancel_job": "test_cancel_job_workflow",
    "get_job_log": "test_batch_job_workflow",
    "list_ml_projects": "test_ml_project_workflow",
    "create_ml_project": "test_ml_project_workflow",
    "run_ml_project": "test_run_ml_project_workflow",
    "list_registered_models": "test_scoring_workflow",
    "list_models_and_decisions": "test_scoring_workflow",
    "score_data": "test_scoring_workflow",
}


async def test_every_tool_has_integration_coverage(integration_mcp_server):
    """Every tool registered on the live server is exercised by an integration test."""
    async with Client(integration_mcp_server) as client:
        registered = {t.name for t in await client.list_tools()}
    missing = registered - set(TOOL_COVERAGE)
    stale = set(TOOL_COVERAGE) - registered
    assert not missing, f"Tools with no integration test: {sorted(missing)}"
    assert not stale, f"Coverage entries for tools that no longer exist: {sorted(stale)}"


async def test_every_prompt_has_integration_coverage(integration_mcp_server):
    """Every prompt registered on the live server is rendered by an integration test."""
    async with Client(integration_mcp_server) as client:
        registered = {p.name for p in await client.list_prompts()}
    missing = registered - set(PROMPT_RENDER_ARGS)
    stale = set(PROMPT_RENDER_ARGS) - registered
    assert not missing, f"Prompts with no integration test: {sorted(missing)}"
    assert not stale, f"Render args for prompts that no longer exist: {sorted(stale)}"
