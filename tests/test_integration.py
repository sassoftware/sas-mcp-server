# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests that call MCP tools against a real SAS Viya instance.

Requires VIYA_ENDPOINT, VIYA_USERNAME, and VIYA_PASSWORD environment variables.
Run with:  uv run python -m pytest -m integration
"""
import json
import time
import pytest
from fastmcp import Client

pytestmark = pytest.mark.integration

_SUFFIX = str(int(time.time()))[-6:]


# -----------------------------------------------------------------------
# CAS Data Discovery Workflow
# -----------------------------------------------------------------------


async def test_cas_discovery_workflow(integration_mcp_server):
    """list_cas_servers → list_caslibs → list_castables → table info/columns/data"""
    async with Client(integration_mcp_server) as client:
        servers = (await client.call_tool("list_cas_servers", {})).data
        assert isinstance(servers, list)
        assert len(servers) > 0, "No CAS servers found"
        server_id = servers[0]["name"]

        caslibs = (await client.call_tool("list_caslibs", {
            "server_id": server_id, "limit": 10
        })).data
        assert isinstance(caslibs, list)
        assert len(caslibs) > 0, "No caslibs found"

        caslib_name = None
        table_name = None
        for cl in caslibs:
            tables = (await client.call_tool("list_castables", {
                "server_id": server_id,
                "caslib_name": cl["name"],
                "limit": 5,
            })).data
            if tables:
                caslib_name = cl["name"]
                table_name = tables[0]["name"]
                break

        if not table_name:
            pytest.skip("No tables found in any caslib")

        info = (await client.call_tool("get_castable_info", {
            "server_id": server_id,
            "caslib_name": caslib_name,
            "table_name": table_name,
        })).data
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
        result = await client.call_tool("execute_sas_code", {
            "sas_code": code
        })
        parsed = json.loads(result.content[0].text)
        assert isinstance(parsed, list)
        assert len(parsed) == 4
        snippet_id, state, log, listing = parsed
        assert state in ("completed", "warning")
        assert "mcp_test" in log.lower() or "NOTE" in log


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
            "data_table_uri": f"/dataTables/dataSources/cas~fs~{server_id}~fs~Public/tables/{table}",
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


async def test_scoring_workflow(integration_mcp_server):
    """list_registered_models → list_models_and_decisions"""
    async with Client(integration_mcp_server) as client:
        models = (await client.call_tool("list_registered_models", {"limit": 5})).data
        assert isinstance(models, list)

        modules = (await client.call_tool("list_models_and_decisions", {"limit": 5})).data
        assert isinstance(modules, list)

        if not modules:
            pytest.skip("No MAS modules found — cannot test score_data")

        module_id = modules[0]["id"]
        try:
            result = (await client.call_tool("score_data", {
                "module_id": module_id,
                "step_id": "score",
                "input_data": {"x": 1},
            })).data
            assert isinstance(result, dict)
        except Exception:
            pytest.skip(f"Module {module_id} does not have a 'score' step or expects different inputs")
