# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Payload assertion tests for all MCP tools.

Each test calls a tool through the MCP protocol and verifies the exact HTTP
request that would be sent to Viya — URL path, method, body structure, query
params, and headers.  These tests use a mock httpx client (no network calls).
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastmcp import Client


EXPECTED_TOOLS = [
    "execute_sas_code",
    "list_cas_servers", "list_caslibs", "list_castables",
    "get_castable_info", "get_castable_columns", "get_castable_data",
    "upload_data", "promote_table_to_memory",
    "list_files", "upload_file", "download_file",
    "list_reports", "get_report", "get_report_image",
    "submit_batch_job", "get_job_status", "list_jobs",
    "cancel_job", "get_job_log",
    "list_ml_projects", "create_ml_project", "run_ml_project",
    "list_registered_models", "list_models_and_decisions", "score_data",
]


# -----------------------------------------------------------------------
# Schema validation
# -----------------------------------------------------------------------


async def test_all_tools_registered(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        for expected in EXPECTED_TOOLS:
            assert expected in names, f"Tool '{expected}' not registered"


async def test_tool_schemas(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    async with Client(mcp) as client:
        tools = await client.list_tools()
        tool_map = {t.name: t for t in tools}

        create_ml = tool_map["create_ml_project"]
        props = create_ml.inputSchema["properties"]
        assert "project_name" in props
        assert "data_table_uri" in props
        assert "target_variable" in props
        assert "prediction_type" in props
        assert "target_event_level" in props
        assert "auto_run" in props
        required = create_ml.inputSchema.get("required", [])
        assert "project_name" in required
        assert "data_table_uri" in required
        assert "target_variable" in required

        score = tool_map["score_data"]
        props = score.inputSchema["properties"]
        assert "module_id" in props
        assert "step_id" in props
        assert "input_data" in props

        submit = tool_map["submit_batch_job"]
        props = submit.inputSchema["properties"]
        assert "sas_code" in props
        assert "job_name" in props


# -----------------------------------------------------------------------
# Tier 1 — Data Discovery (CAS Management)
# -----------------------------------------------------------------------


async def test_list_cas_servers_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_cas_servers", {})

    url = mock_client.get.call_args[0][0]
    assert url.endswith("/casManagement/servers")
    params = mock_client.get.call_args[1]["params"]
    assert "start" in params
    assert "limit" in params


async def test_list_caslibs_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_caslibs", {"server_id": "cas-shared-default"})

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas-shared-default/caslibs" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 50


async def test_list_castables_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_castables", {
            "server_id": "cas1", "caslib_name": "Public", "limit": 10
        })

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 10


async def test_get_castable_info_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("get_castable_info", {
            "server_id": "cas1", "caslib_name": "Public", "table_name": "HMEQ"
        })

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/HMEQ" in url
    headers = mock_client.get.call_args[1]["headers"]
    assert headers["Accept"] == "application/json"


async def test_get_castable_columns_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("get_castable_columns", {
            "server_id": "cas1", "caslib_name": "Public",
            "table_name": "HMEQ", "limit": 100
        })

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/HMEQ/columns" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 100


async def test_get_castable_data_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("get_castable_data", {
            "server_id": "cas1", "caslib_name": "Public",
            "table_name": "HMEQ", "limit": 5, "start": 10
        })

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/HMEQ/rows" in url
    params = mock_client.get.call_args[1]["params"]
    assert params == {"start": 10, "limit": 5}


# -----------------------------------------------------------------------
# Tier 2 — Data Operations & Files
# -----------------------------------------------------------------------


async def test_upload_data_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("upload_data", {
            "server_id": "cas1", "caslib_name": "Public",
            "table_name": "MY_TABLE", "csv_data": "a,b\n1,2\n3,4"
        })

    url = mock_client.put.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/MY_TABLE" in url
    content = mock_client.put.call_args[1]["content"]
    assert content == b"a,b\n1,2\n3,4"
    headers = mock_client.put.call_args[1]["headers"]
    assert headers["Content-Type"] == "text/csv"


async def test_promote_table_to_memory_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("promote_table_to_memory", {
            "server_id": "cas1", "caslib_name": "Public", "table_name": "MY_TABLE"
        })

    url = mock_client.post.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/MY_TABLE" in url
    params = mock_client.post.call_args[1]["params"]
    assert params == {"scope": "global"}


async def test_list_files_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_files", {"limit": 25})

    url = mock_client.get.call_args[0][0]
    assert "/files/files" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 25


async def test_list_files_with_filter_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_files", {"filter_name": "report"})

    params = mock_client.get.call_args[1]["params"]
    assert params["filter"] == "contains(name,'report')"


async def test_upload_file_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("upload_file", {
            "file_name": "test.sas",
            "content": "data test; run;",
            "content_type": "application/x-sas"
        })

    url = mock_client.post.call_args[0][0]
    assert url.endswith("/files/files")
    kwargs = mock_client.post.call_args[1]
    assert kwargs["content"] == b"data test; run;"
    assert kwargs["headers"]["Content-Type"] == "application/x-sas"
    assert 'filename="test.sas"' in kwargs["headers"]["Content-Disposition"]
    assert kwargs["headers"]["Accept"] == "application/json"


async def test_download_file_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value.text = "file content here"
    async with Client(mcp) as client:
        result = await client.call_tool("download_file", {"file_id": "abc-123"})

    url = mock_client.get.call_args[0][0]
    assert "/files/files/abc-123/content" in url


# -----------------------------------------------------------------------
# Tier 3 — Reports & Visualization
# -----------------------------------------------------------------------


async def test_list_reports_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_reports", {"limit": 10})

    url = mock_client.get.call_args[0][0]
    assert "/reports/reports" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 10


async def test_list_reports_with_filter_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_reports", {"filter_name": "sales"})

    params = mock_client.get.call_args[1]["params"]
    assert params["filter"] == "contains(name,'sales')"


async def test_get_report_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("get_report", {"report_id": "rpt-456"})

    url = mock_client.get.call_args[0][0]
    assert "/reports/reports/rpt-456" in url


async def test_get_report_image_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("get_report_image", {
            "report_id": "rpt-456", "section_index": 2
        })

    url = mock_client.post.call_args[0][0]
    assert "/reportImages/jobs" in url
    body = mock_client.post.call_args[1]["json"]
    assert body["reportUri"] == "/reports/reports/rpt-456"
    assert body["layoutType"] == "thumbnail"
    assert body["selectionType"] == "perSection"
    assert body["sectionIndex"] == 2
    assert body["size"] == "800x600"
    assert body["renderLimit"] == 1
    headers = mock_client.post.call_args[1]["headers"]
    assert headers["Accept"] == "application/vnd.sas.report.images.job+json"


# -----------------------------------------------------------------------
# Tier 4 — Batch Jobs & Async Execution
# -----------------------------------------------------------------------


async def test_submit_batch_job_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("submit_batch_job", {
            "sas_code": "data test; x=1; run;",
            "job_name": "my-test-job"
        })

    url = mock_client.post.call_args[0][0]
    assert "/jobExecution/jobs" in url
    body = mock_client.post.call_args[1]["json"]
    assert body["name"] == "my-test-job"
    assert body["jobDefinition"]["type"] == "Compute"
    assert body["jobDefinition"]["code"] == "data test; x=1; run;"


async def test_submit_batch_job_default_name(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("submit_batch_job", {
            "sas_code": "data test; run;"
        })

    body = mock_client.post.call_args[1]["json"]
    assert body["name"] == "mcp-batch-job"


async def test_get_job_status_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("get_job_status", {"job_id": "job-789"})

    url = mock_client.get.call_args[0][0]
    assert "/jobExecution/jobs/job-789" in url


async def test_list_jobs_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_jobs", {"limit": 5})

    url = mock_client.get.call_args[0][0]
    assert "/jobExecution/jobs" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 5


async def test_cancel_job_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("cancel_job", {"job_id": "job-789"})

    url = mock_client.delete.call_args[0][0]
    assert "/jobExecution/jobs/job-789" in url


async def test_get_job_log_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value.json = MagicMock(return_value={
        "items": [{"line": "NOTE: The data set has 1 observation"}]
    })
    async with Client(mcp) as client:
        result = await client.call_tool("get_job_log", {"job_id": "job-789"})

    url = mock_client.get.call_args[0][0]
    assert "/jobExecution/jobs/job-789/log" in url
    headers = mock_client.get.call_args[1]["headers"]
    assert headers["Accept"] == "application/vnd.sas.collection+json"


# -----------------------------------------------------------------------
# Tier 5 — Model Management & Scoring
# -----------------------------------------------------------------------


async def test_list_ml_projects_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_ml_projects", {"limit": 10})

    url = mock_client.get.call_args[0][0]
    assert "/mlPipelineAutomation/projects" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 10


async def test_create_ml_project_binary_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("create_ml_project", {
            "project_name": "Fraud Detection",
            "data_table_uri": "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ",
            "target_variable": "BAD",
            "description": "Binary classification project",
            "prediction_type": "binary",
            "target_event_level": "1",
        })

    url = mock_client.post.call_args[0][0]
    assert "/mlPipelineAutomation/projects" in url
    body = mock_client.post.call_args[1]["json"]

    assert body["name"] == "Fraud Detection"
    assert body["description"] == "Binary classification project"
    assert body["type"] == "predictive"
    assert body["dataTableUri"].endswith("/tables/HMEQ")
    assert body["pipelineBuildMethod"] == "automatic"

    settings = body["settings"]
    assert settings["applyGlobalMetadata"] is True
    assert settings["autoRun"] is True
    assert settings["numberOfModels"] == 5

    attrs = body["analyticsProjectAttributes"]
    assert attrs["targetVariable"] == "BAD"
    assert attrs["targetLevel"] == "binary"
    assert attrs["partitionEnabled"] is True
    assert attrs["classSelectionStatistic"] == "ks"
    assert attrs["targetEventLevel"] == "1"

    assert "predictionType" not in body
    assert "predictionType" not in attrs
    assert "targetVariable" not in body


async def test_create_ml_project_interval_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("create_ml_project", {
            "project_name": "Price Prediction",
            "data_table_uri": "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/CARS",
            "target_variable": "MSRP",
            "prediction_type": "interval",
        })

    body = mock_client.post.call_args[1]["json"]
    attrs = body["analyticsProjectAttributes"]
    assert attrs["targetLevel"] == "interval"
    assert attrs["classSelectionStatistic"] == "ase"
    assert "targetEventLevel" not in attrs


async def test_create_ml_project_nominal_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("create_ml_project", {
            "project_name": "Multi Class",
            "data_table_uri": "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/IRIS",
            "target_variable": "Species",
            "prediction_type": "nominal",
            "target_event_level": "setosa",
        })

    body = mock_client.post.call_args[1]["json"]
    attrs = body["analyticsProjectAttributes"]
    assert attrs["targetLevel"] == "nominal"
    assert attrs["classSelectionStatistic"] == "ks"
    assert attrs["targetEventLevel"] == "setosa"


async def test_create_ml_project_auto_run_false(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("create_ml_project", {
            "project_name": "No Auto Run",
            "data_table_uri": "/dataTables/dataSources/x/tables/T",
            "target_variable": "Y",
            "auto_run": False,
        })

    body = mock_client.post.call_args[1]["json"]
    assert body["settings"]["autoRun"] is False


async def test_run_ml_project_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("run_ml_project", {"project_id": "proj-123"})

    url = mock_client.post.call_args[0][0]
    assert "/mlPipelineAutomation/projects/proj-123" in url
    params = mock_client.post.call_args[1]["params"]
    assert params == {"action": "start"}


async def test_list_registered_models_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_registered_models", {})

    url = mock_client.get.call_args[0][0]
    assert "/modelRepository/models" in url


async def test_list_models_and_decisions_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_models_and_decisions", {})

    url = mock_client.get.call_args[0][0]
    assert "/microanalyticScore/modules" in url


async def test_score_data_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("score_data", {
            "module_id": "mod-1",
            "step_id": "score",
            "input_data": {"age": 35, "income": 50000}
        })

    url = mock_client.post.call_args[0][0]
    assert "/microanalyticScore/modules/mod-1/steps/score" in url
    body = mock_client.post.call_args[1]["json"]
    assert "inputs" in body
    input_names = {inp["name"] for inp in body["inputs"]}
    assert input_names == {"age", "income"}
    input_values = {inp["name"]: inp["value"] for inp in body["inputs"]}
    assert input_values["age"] == 35
    assert input_values["income"] == 50000


# -----------------------------------------------------------------------
# execute_sas_code (uses run_one_snippet, not _make_client)
# -----------------------------------------------------------------------


async def test_execute_sas_code_request(mcp_server_with_mock_client):
    mcp, _ = mcp_server_with_mock_client
    with patch("sas_mcp_server.tools.run_one_snippet") as mock_run:
        mock_run.return_value = ("1", "completed", "LOG", "LISTING")
        async with Client(mcp) as client:
            result = await client.call_tool("execute_sas_code", {
                "sas_code": "data test; x=1; run;"
            })

        mock_run.assert_called_once_with("data test; x=1; run;", "1", "test-token")
