# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Payload assertion tests for all MCP tools.

Each test calls a tool through the MCP protocol and verifies the exact HTTP
request that would be sent to Viya — URL path, method, body structure, query
params, and headers.  These tests use a mock httpx client (no network calls).
"""

import json
from unittest.mock import MagicMock, patch

import httpx
from fastmcp import Client

from conftest import _make_mock_response

EXPECTED_TOOLS = [
    "execute_sas_code",
    "list_cas_servers",
    "list_caslibs",
    "list_castables",
    "list_source_tables",
    "get_castable_info",
    "get_castable_columns",
    "get_castable_data",
    "upload_data",
    "promote_table_to_memory",
    "list_files",
    "upload_file",
    "download_file",
    "list_reports",
    "get_report",
    "get_report_image",
    "submit_batch_job",
    "get_job_status",
    "list_jobs",
    "cancel_job",
    "get_job_log",
    "list_ml_projects",
    "create_ml_project",
    "run_ml_project",
    "list_registered_models",
    "list_models_and_decisions",
    "score_data",
    "list_compute_contexts",
    "list_compute_libraries",
    "list_compute_tables",
    "list_compute_columns",
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
        assert "caslib_name" in props
        assert "table_name" in props
        assert "server_id" in props
        assert "target_variable" in props
        assert "prediction_type" in props
        assert "target_event_level" in props
        assert "auto_run" in props
        required = create_ml.inputSchema.get("required", [])
        assert "project_name" in required
        assert "caslib_name" in required
        assert "table_name" in required
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
        await client.call_tool(
            "list_castables",
            {"server_id": "cas1", "caslib_name": "Public", "limit": 10},
        )

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 10


async def test_list_source_tables_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool(
            "list_source_tables",
            {"server_id": "cas1", "caslib_name": "Public", "limit": 10},
        )

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["state"] == "unloaded"
    assert params["limit"] == 10


async def test_get_castable_info_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool(
            "get_castable_info",
            {"server_id": "cas1", "caslib_name": "Public", "table_name": "HMEQ"},
        )

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/HMEQ" in url
    headers = mock_client.get.call_args[1]["headers"]
    assert headers["Accept"] == "application/json"


async def test_get_castable_columns_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool(
            "get_castable_columns",
            {
                "server_id": "cas1",
                "caslib_name": "Public",
                "table_name": "HMEQ",
                "limit": 100,
            },
        )

    url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/HMEQ/columns" in url
    params = mock_client.get.call_args[1]["params"]
    assert params["limit"] == 100


async def test_get_castable_data_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client

    col_resp = _make_mock_response(
        {
            "items": [
                {"name": "x", "type": "double", "index": 0},
                {"name": "y", "type": "double", "index": 1},
            ],
            "count": 2,
        }
    )
    row_resp = _make_mock_response(
        {
            "items": [{"cells": ["1", "2"]}, {"cells": ["3", "4"]}],
            "count": 2,
        }
    )

    original_get = mock_client.get.return_value

    def route_get(url, **kwargs):
        if "/dataTables/dataSources/" in url and "/columns" in url:
            return col_resp
        if "/rowSets/tables/" in url and "/rows" in url:
            return row_resp
        return original_get

    mock_client.get.side_effect = route_get

    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_castable_data",
            {
                "server_id": "cas1",
                "caslib_name": "Public",
                "table_name": "HMEQ",
                "limit": 5,
                "start": 10,
            },
        )

    mock_client.get.side_effect = None
    mock_client.get.return_value = original_get

    calls = mock_client.get.call_args_list
    col_call = next(c for c in calls if "/dataTables/dataSources/" in c[0][0])
    row_call = next(c for c in calls if "/rowSets/tables/" in c[0][0])

    assert (
        "/dataTables/dataSources/cas~fs~cas1~fs~Public/tables/HMEQ/columns"
        in col_call[0][0]
    )
    assert col_call[1]["params"]["limit"] == 100

    assert "/rowSets/tables/cas~fs~cas1~fs~Public~fs~HMEQ/rows" in row_call[0][0]
    assert row_call[1]["params"] == {"start": 10, "limit": 5}

    assert result.data["columns"] == ["x", "y"]
    assert result.data["rows"] == [{"x": "1", "y": "2"}, {"x": "3", "y": "4"}]


# -----------------------------------------------------------------------
# Tier 2 — Data Operations & Files
# -----------------------------------------------------------------------


async def test_upload_data_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value.json = MagicMock(
        return_value={
            "name": "MY_TABLE",
            "rowCount": 2,
            "columnCount": 2,
            "caslibName": "Public",
            "scope": "global",
        }
    )
    async with Client(mcp) as client:
        result = await client.call_tool(
            "upload_data",
            {
                "server_id": "cas1",
                "caslib_name": "Public",
                "table_name": "MY_TABLE",
                "csv_data": "a,b\n1,2\n3,4",
            },
        )

    url = mock_client.post.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables" in url
    kwargs = mock_client.post.call_args[1]
    assert kwargs["data"]["tableName"] == "MY_TABLE"
    assert kwargs["data"]["format"] == "csv"
    assert kwargs["data"]["containsHeaderRow"] == "true"
    assert "file" in kwargs["files"]
    file_tuple = kwargs["files"]["file"]
    assert file_tuple[0] == "data.csv"
    assert file_tuple[2] == "text/csv"
    assert result.data["status"] == "success"
    assert result.data["rows_uploaded"] == 2


async def test_promote_table_to_memory_request(mcp_server_with_mock_client):
    """Unloaded table: idempotency GET, then PUT state=loaded&scope=global."""
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"name": "MY_TABLE", "state": "unloaded"}
    )
    put_state = _make_mock_response(status_code=200)
    put_state.text = "loaded"
    mock_client.put.return_value = put_state
    async with Client(mcp) as client:
        result = await client.call_tool(
            "promote_table_to_memory",
            {"server_id": "cas1", "caslib_name": "Public", "table_name": "MY_TABLE"},
        )

    get_url = mock_client.get.call_args[0][0]
    assert "/casManagement/servers/cas1/caslibs/Public/tables/MY_TABLE" in get_url
    put_url = mock_client.put.call_args[0][0]
    assert put_url.endswith(
        "/casManagement/servers/cas1/caslibs/Public/tables/MY_TABLE/state"
    )
    assert mock_client.put.call_args[1]["params"] == {
        "value": "loaded",
        "scope": "global",
    }
    assert result.data["status"] == "promoted"
    assert result.data["scope"] == "global"


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
        await client.call_tool(
            "upload_file",
            {
                "file_name": "test.sas",
                "content": "data test; run;",
                "content_type": "application/x-sas",
            },
        )

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
        await client.call_tool("download_file", {"file_id": "abc-123"})

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
        await client.call_tool(
            "get_report_image", {"report_id": "rpt-456", "section_index": 2}
        )

    url = mock_client.post.call_args[0][0]
    assert "/reportImages/jobs" in url
    kwargs = mock_client.post.call_args[1]
    body = json.loads(kwargs["content"])
    assert body["reportUri"] == "/reports/reports/rpt-456"
    assert body["layoutType"] == "thumbnail"
    assert body["selectionType"] == "perSection"
    assert body["sectionIndex"] == 2
    assert body["size"] == "800x600"
    assert body["renderLimit"] == 1
    headers = kwargs["headers"]
    assert (
        headers["Content-Type"] == "application/vnd.sas.report.images.job.request+json"
    )
    assert headers["Accept"] == "application/vnd.sas.report.images.job+json"


# -----------------------------------------------------------------------
# Tier 4 — Batch Jobs & Async Execution
# -----------------------------------------------------------------------


async def test_submit_batch_job_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool(
            "submit_batch_job",
            {"sas_code": "data test; x=1; run;", "job_name": "my-test-job"},
        )

    url = mock_client.post.call_args[0][0]
    assert "/jobExecution/jobs" in url
    body = mock_client.post.call_args[1]["json"]
    assert body["name"] == "my-test-job"
    assert body["jobDefinition"]["type"] == "Compute"
    assert body["jobDefinition"]["code"] == "data test; x=1; run;"
    assert "_contextName" in body["arguments"]


async def test_submit_batch_job_default_name(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("submit_batch_job", {"sas_code": "data test; run;"})

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

    job_resp = _make_mock_response(
        {
            "state": "completed",
            "results": {
                "COMPUTE_JOB": "ABC123",
                "ABC123.log.txt": "/files/files/log-file-id",
            },
        }
    )
    log_content_resp = _make_mock_response()
    log_content_resp.text = "NOTE: The data set has 1 observation"

    original_get = mock_client.get.return_value

    def route_get(url, **kwargs):
        if "/jobExecution/jobs/job-789" in url and "/content" not in url:
            return job_resp
        if "/files/files/log-file-id/content" in url:
            return log_content_resp
        return original_get

    mock_client.get.side_effect = route_get

    async with Client(mcp) as client:
        await client.call_tool("get_job_log", {"job_id": "job-789"})

    mock_client.get.side_effect = None
    mock_client.get.return_value = original_get

    calls = mock_client.get.call_args_list
    job_call = next(
        c
        for c in calls
        if "/jobExecution/jobs/job-789" in c[0][0] and "/content" not in c[0][0]
    )
    assert "/jobExecution/jobs/job-789" in job_call[0][0]

    log_call = next(c for c in calls if "/files/files/log-file-id/content" in c[0][0])
    assert "/files/files/log-file-id/content" in log_call[0][0]


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
    mock_client.get.return_value = _make_mock_response(
        {"state": "loaded", "scope": "global"}
    )
    async with Client(mcp) as client:
        await client.call_tool(
            "create_ml_project",
            {
                "project_name": "Fraud Detection",
                "caslib_name": "Public",
                "table_name": "HMEQ",
                "target_variable": "BAD",
                "description": "Binary classification project",
                "prediction_type": "binary",
                "target_event_level": "1",
            },
        )

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
    mock_client.get.return_value = _make_mock_response(
        {"state": "loaded", "scope": "global"}
    )
    async with Client(mcp) as client:
        await client.call_tool(
            "create_ml_project",
            {
                "project_name": "Price Prediction",
                "caslib_name": "Public",
                "table_name": "CARS",
                "target_variable": "MSRP",
                "prediction_type": "interval",
            },
        )

    body = mock_client.post.call_args[1]["json"]
    attrs = body["analyticsProjectAttributes"]
    assert attrs["targetLevel"] == "interval"
    assert attrs["classSelectionStatistic"] == "ase"
    assert "targetEventLevel" not in attrs


async def test_create_ml_project_nominal_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"state": "loaded", "scope": "global"}
    )
    async with Client(mcp) as client:
        await client.call_tool(
            "create_ml_project",
            {
                "project_name": "Multi Class",
                "caslib_name": "Public",
                "table_name": "IRIS",
                "target_variable": "Species",
                "prediction_type": "nominal",
                "target_event_level": "setosa",
            },
        )

    body = mock_client.post.call_args[1]["json"]
    attrs = body["analyticsProjectAttributes"]
    assert attrs["targetLevel"] == "nominal"
    assert attrs["classSelectionStatistic"] == "ks"
    assert attrs["targetEventLevel"] == "setosa"


async def test_create_ml_project_auto_run_false(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"state": "loaded", "scope": "global"}
    )
    async with Client(mcp) as client:
        await client.call_tool(
            "create_ml_project",
            {
                "project_name": "No Auto Run",
                "caslib_name": "Public",
                "table_name": "T",
                "target_variable": "Y",
                "auto_run": False,
            },
        )

    body = mock_client.post.call_args[1]["json"]
    assert body["settings"]["autoRun"] is False


async def test_create_ml_project_default_server_in_uri(mcp_server_with_mock_client):
    """server_id defaults to cas-shared-default and is woven into the data-table URI."""
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"state": "loaded", "scope": "global"}
    )
    async with Client(mcp) as client:
        await client.call_tool(
            "create_ml_project",
            {
                "project_name": "Defaults",
                "caslib_name": "Public",
                "table_name": "HMEQ",
                "target_variable": "BAD",
            },
        )
    body = mock_client.post.call_args[1]["json"]
    assert body["dataTableUri"] == (
        "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ"
    )


async def test_create_ml_project_rejects_non_global_table(mcp_server_with_mock_client):
    """Pre-flight: an unloaded/session-scoped table is rejected without a POST."""
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"state": "unloaded", "scope": None}
    )
    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_ml_project",
            {
                "project_name": "Bad",
                "caslib_name": "Public",
                "table_name": "NOT_LOADED",
                "target_variable": "Y",
            },
        )
    assert result.data["status"] == "table_not_global"
    mock_client.post.assert_not_called()


async def test_create_ml_project_table_not_found(mcp_server_with_mock_client):
    """Pre-flight: a missing table returns a not_found status, no POST."""
    mcp, mock_client = mcp_server_with_mock_client
    resp = _make_mock_response(status_code=404)
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "missing", request=MagicMock(), response=MagicMock(status_code=404)
        )
    )
    mock_client.get.return_value = resp
    async with Client(mcp) as client:
        result = await client.call_tool(
            "create_ml_project",
            {
                "project_name": "Bad",
                "caslib_name": "Public",
                "table_name": "GHOST",
                "target_variable": "Y",
            },
        )
    assert result.data["status"] == "table_not_found"
    mock_client.post.assert_not_called()


async def test_run_ml_project_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value.headers = {
        "etag": '"test-etag"',
        "Content-Type": "application/json",
    }
    mock_client.get.return_value.json = MagicMock(
        return_value={"id": "proj-123", "name": "Test"}
    )
    async with Client(mcp) as client:
        await client.call_tool("run_ml_project", {"project_id": "proj-123"})

    get_url = mock_client.get.call_args[0][0]
    assert "/mlPipelineAutomation/projects/proj-123" in get_url

    put_url = mock_client.put.call_args[0][0]
    assert "/mlPipelineAutomation/projects/proj-123" in put_url
    params = mock_client.put.call_args[1]["params"]
    assert params == {"action": "retrainProject"}
    headers = mock_client.put.call_args[1]["headers"]
    assert headers["If-Match"] == '"test-etag"'
    assert headers["Accept-Language"] == "en"
    assert "content" in mock_client.put.call_args[1]


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
        await client.call_tool(
            "score_data",
            {
                "module_id": "mod-1",
                "step_id": "score",
                "input_data": {"age": 35, "income": 50000},
            },
        )

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
        mock_run.return_value = {
            "snippet_id": "1",
            "state": "completed",
            "log": "LOG",
            "listing": "LISTING",
        }
        async with Client(mcp) as client:
            result = await client.call_tool(
                "execute_sas_code", {"sas_code": "data test; x=1; run;"}
            )

        mock_run.assert_called_once_with("data test; x=1; run;", "1", "test-token")
        assert result.data == {
            "snippet_id": "1",
            "state": "completed",
            "log": "LOG",
            "listing": "LISTING",
        }


# -----------------------------------------------------------------------
# Error / edge-path coverage
# -----------------------------------------------------------------------


async def test_upload_data_conflict_returns_structured_error(
    mcp_server_with_mock_client,
):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response(status_code=409)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "upload_data",
            {
                "server_id": "cas1",
                "caslib_name": "Public",
                "table_name": "MY_TABLE",
                "csv_data": "a,b\n1,2",
            },
        )
    assert result.data["status"] == "table_already_exists"
    assert result.data["table_name"] == "MY_TABLE"
    assert result.data["caslib"] == "Public"


async def test_promote_table_already_global_is_noop(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"state": "loaded", "scope": "global"}
    )
    async with Client(mcp) as client:
        result = await client.call_tool(
            "promote_table_to_memory",
            {
                "server_id": "cas1",
                "caslib_name": "Public",
                "table_name": "MY_TABLE",
            },
        )
    assert result.data["status"] == "already_global"
    assert result.data["table"] == "Public.MY_TABLE"
    mock_client.put.assert_not_called()


async def test_promote_table_not_found(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    resp = _make_mock_response(status_code=404)
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "missing", request=MagicMock(), response=MagicMock(status_code=404)
        )
    )
    mock_client.get.return_value = resp
    async with Client(mcp) as client:
        result = await client.call_tool(
            "promote_table_to_memory",
            {
                "server_id": "cas1",
                "caslib_name": "Public",
                "table_name": "GHOST",
            },
        )
    assert result.data["status"] == "not_found"
    mock_client.put.assert_not_called()


async def test_get_job_log_no_log_uri_returns_state(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"state": "completed", "results": {}}
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_job_log", {"job_id": "j1"})
    assert result.data == "No log available. Job state: completed"


async def test_get_job_log_error_dict_returns_message(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {
            "state": "failed",
            "results": {},
            "error": {"message": "boom"},
        }
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_job_log", {"job_id": "j1"})
    assert result.data == "Job failed: boom"


async def test_get_job_log_dot_log_fallback(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    job_resp = _make_mock_response(
        {
            "state": "completed",
            "results": {"run.log": "/files/files/LOGID"},
        }
    )
    content_resp = _make_mock_response()
    content_resp.text = "LOG CONTENT"
    original_get = mock_client.get.return_value

    def route_get(url, **kwargs):
        if "/jobExecution/jobs/j1" in url and "/content" not in url:
            return job_resp
        if "/files/files/LOGID/content" in url:
            return content_resp
        return original_get

    mock_client.get.side_effect = route_get
    async with Client(mcp) as client:
        result = await client.call_tool("get_job_log", {"job_id": "j1"})
    mock_client.get.side_effect = None
    mock_client.get.return_value = original_get
    assert result.data == "LOG CONTENT"


async def test_run_ml_project_204_returns_running(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    get_resp = _make_mock_response({"id": "p1"})
    get_resp.headers = {"etag": '"e"', "Content-Type": "application/json"}
    mock_client.get.return_value = get_resp
    mock_client.put.return_value = _make_mock_response(status_code=204)
    async with Client(mcp) as client:
        result = await client.call_tool("run_ml_project", {"project_id": "p1"})
    assert result.data == {"status": "running", "projectId": "p1"}


# -----------------------------------------------------------------------
# Compute Contexts & Libraries
# -----------------------------------------------------------------------


async def test_list_compute_contexts_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_compute_contexts", {})

    url = mock_client.get.call_args[0][0]
    assert url.endswith("/compute/contexts")
    params = mock_client.get.call_args[1]["params"]
    assert "start" in params
    assert "limit" in params


async def test_list_compute_libraries_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    context_resp = _make_mock_response({"items": [{"id": "test-context-id"}]})
    libs_resp = _make_mock_response({"items": [], "count": 0})
    original_get = mock_client.get.return_value

    def route_get(url, **kwargs):
        if "/compute/contexts?name=" in url:
            return context_resp
        if "/compute/sessions/test-session-id/data" in url:
            return libs_resp
        return original_get

    mock_client.get.side_effect = route_get
    mock_client.post.return_value = _make_mock_response(
        {"id": "test-session-id"}, status_code=201
    )

    async with Client(mcp) as client:
        await client.call_tool(
            "list_compute_libraries",
            {"compute_context_name": "Test Context", "limit": 25, "start": 5},
        )

    mock_client.get.side_effect = None
    mock_client.get.return_value = original_get

    calls = mock_client.get.call_args_list
    context_call = next(
        call for call in calls if "/compute/contexts?name=" in call[0][0]
    )
    libs_call = next(
        call for call in calls if "/compute/sessions/test-session-id/data" in call[0][0]
    )

    assert "/compute/contexts?name=Test Context" in context_call[0][0]

    post_url = mock_client.post.call_args[0][0]
    assert "/compute/contexts/test-context-id/sessions" in post_url
    assert mock_client.post.call_args[1]["json"]["name"] == "compute-libraries-session"

    assert "/compute/sessions/test-session-id/data" in libs_call[0][0]
    assert libs_call[1]["params"] == {"start": 5, "limit": 25}

    delete_url = mock_client.delete.call_args[0][0]
    assert "/compute/sessions/test-session-id" in delete_url


async def test_list_compute_tables_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    context_resp = _make_mock_response({"items": [{"id": "test-context-id"}]})
    tables_resp = _make_mock_response({"items": [], "count": 0})
    original_get = mock_client.get.return_value

    def route_get(url, **kwargs):
        if "/compute/contexts?name=" in url:
            return context_resp
        if "/compute/sessions/test-session-id/data/Public" in url:
            return tables_resp
        return original_get

    mock_client.get.side_effect = route_get
    mock_client.post.return_value = _make_mock_response(
        {"id": "test-session-id"}, status_code=201
    )

    async with Client(mcp) as client:
        await client.call_tool(
            "list_compute_tables",
            {
                "compute_context_name": "Test Context",
                "library_name": "Public",
                "limit": 10,
                "start": 2,
            },
        )

    mock_client.get.side_effect = None
    mock_client.get.return_value = original_get

    calls = mock_client.get.call_args_list
    context_call = next(
        call for call in calls if "/compute/contexts?name=" in call[0][0]
    )
    tables_call = next(
        call
        for call in calls
        if "/compute/sessions/test-session-id/data/Public" in call[0][0]
    )

    assert "/compute/contexts?name=Test Context" in context_call[0][0]

    post_url = mock_client.post.call_args[0][0]
    assert "/compute/contexts/test-context-id/sessions" in post_url
    assert mock_client.post.call_args[1]["json"]["name"] == "compute-tables-session"

    assert "/compute/sessions/test-session-id/data/Public" in tables_call[0][0]
    assert tables_call[1]["params"] == {"start": 2, "limit": 10}

    delete_url = mock_client.delete.call_args[0][0]
    assert "/compute/sessions/test-session-id" in delete_url


async def test_list_compute_columns_request(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    context_resp = _make_mock_response({"items": [{"id": "test-context-id"}]})
    columns_resp = _make_mock_response({"items": [], "count": 0})
    original_get = mock_client.get.return_value

    def route_get(url, **kwargs):
        if "/compute/contexts?name=" in url:
            return context_resp
        if "/compute/sessions/test-session-id/data/Public/MY_TABLE/columns" in url:
            return columns_resp
        return original_get

    mock_client.get.side_effect = route_get
    mock_client.post.return_value = _make_mock_response(
        {"id": "test-session-id"}, status_code=201
    )

    async with Client(mcp) as client:
        await client.call_tool(
            "list_compute_columns",
            {
                "compute_context_name": "Test Context",
                "library_name": "Public",
                "table_name": "MY_TABLE",
                "limit": 50,
                "start": 0,
            },
        )

    mock_client.get.side_effect = None
    mock_client.get.return_value = original_get

    calls = mock_client.get.call_args_list
    context_call = next(
        call for call in calls if "/compute/contexts?name=" in call[0][0]
    )
    columns_call = next(
        call
        for call in calls
        if "/compute/sessions/test-session-id/data/Public/MY_TABLE/columns"
        in call[0][0]
    )

    assert "/compute/contexts?name=Test Context" in context_call[0][0]

    post_url = mock_client.post.call_args[0][0]
    assert "/compute/contexts/test-context-id/sessions" in post_url
    assert mock_client.post.call_args[1]["json"]["name"] == "compute-columns-session"

    assert (
        "/compute/sessions/test-session-id/data/Public/MY_TABLE/columns"
        in columns_call[0][0]
    )
    assert columns_call[1]["params"] == {"start": 0, "limit": 50}

    delete_url = mock_client.delete.call_args[0][0]
    assert "/compute/sessions/test-session-id" in delete_url
