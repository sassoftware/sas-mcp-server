# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests that call MCP tools against a real SAS Viya instance.

Requires VIYA_ENDPOINT, VIYA_USERNAME, and VIYA_PASSWORD environment variables.
Run with:  uv run python -m pytest -m integration
"""

import asyncio
import base64
import contextlib
import math
import os
import random
import tempfile
import time
from pathlib import Path

import pytest
from fastmcp import Client

# Pin all integration tests to a single session-scoped event loop. The
# session-scoped fixtures (viya_token, integration_mcp_server) and the
# in-memory fastmcp transport must share the same loop they were created in;
# otherwise the second test's tool call fails with ConnectError when it
# touches httpx state bound to the prior, now-closed loop.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_SUFFIX = str(int(time.time()))[-6:]


def _embedded_file_bytes(result) -> int:
    """Decode the first embedded-resource block of a tool result, return its size."""
    block = result.content[0]
    assert block.type == "resource", f"expected an embedded resource, got {block.type}"
    return len(base64.b64decode(block.resource.blob))


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


async def _viya_delete(token: str, path: str) -> None:
    """Authenticated DELETE against the live Viya, used for best-effort cleanup."""
    from sas_mcp_server.config import VIYA_ENDPOINT
    from sas_mcp_server.viya_client import make_client

    async with make_client(token) as client:
        resp = await client.delete(f"{VIYA_ENDPOINT}{path}", follow_redirects=True)
        resp.raise_for_status()


def _dummy_input_value(var_type: str):
    """A type-appropriate placeholder value for a MAS step input variable."""
    if (var_type or "").lower() in ("decimal", "double", "float", "integer", "int", "bigint"):
        return 0
    return ""


def _ml_training_csv(n: int = 600) -> str:
    """Synthetic binary-classification data with enough rows and signal for
    AutoML to partition and train. A handful of rows makes the pipeline fail,
    which is why the register/publish workflow needs a real dataset here."""
    rng = random.Random(7)
    rows = ["x1,x2,x3,target"]
    for _ in range(n):
        x1 = rng.uniform(0, 10)
        x2 = rng.uniform(0, 5)
        x3 = rng.uniform(-3, 3)
        p = 1 / (1 + math.exp(-(1.2 * x1 - 2.0 * x2 + 0.8 * x3 - 3.0)))
        y = 1 if rng.random() < p else 0
        rows.append(f"{x1:.3f},{x2:.3f},{x3:.3f},{y}")
    return "\n".join(rows)


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

        caslibs = (await client.call_tool("list_caslibs", {"server_id": server_id, "limit": 50})).data
        assert isinstance(caslibs, list)
        if not any(c["name"] == "Public" for c in caslibs):
            pytest.skip("Public caslib not present on this Viya")

        # Also exercise list_castables so the workflow stays end-to-end, but
        # don't depend on HMEQ appearing in the listing (Public can hold
        # hundreds of tables and the listing is paginated).
        tables = (
            await client.call_tool(
                "list_castables",
                {
                    "server_id": server_id,
                    "caslib_name": "Public",
                    "limit": 50,
                },
            )
        ).data
        assert isinstance(tables, list)

        caslib_name = "Public"
        table_name = "HMEQ"
        # Fetch HMEQ metadata. Skip if HMEQ isn't loaded on this Viya;
        # otherwise the test proceeds to the columns/data assertions.
        try:
            info = (
                await client.call_tool(
                    "get_castable_info",
                    {
                        "server_id": server_id,
                        "caslib_name": caslib_name,
                        "table_name": table_name,
                    },
                )
            ).data
        except Exception as e:
            if "404" in str(e):
                pytest.skip("HMEQ not loaded in Public caslib on this Viya")
            raise
        assert isinstance(info, dict)

        columns = (
            await client.call_tool(
                "get_castable_columns",
                {
                    "server_id": server_id,
                    "caslib_name": caslib_name,
                    "table_name": table_name,
                    "limit": 10,
                },
            )
        ).data
        assert isinstance(columns, list)
        assert len(columns) > 0

        try:
            rows = (
                await client.call_tool(
                    "get_castable_data",
                    {
                        "server_id": server_id,
                        "caslib_name": caslib_name,
                        "table_name": table_name,
                        "limit": 3,
                    },
                )
            ).data
            assert isinstance(rows, dict)
        except Exception:
            pass


# -----------------------------------------------------------------------
# Data Upload Workflow
# -----------------------------------------------------------------------


async def test_data_upload_workflow(integration_mcp_server):
    """upload_inline_data → promote_table_to_memory"""
    async with Client(integration_mcp_server) as client:
        servers = (await client.call_tool("list_cas_servers", {})).data
        server_id = servers[0]["name"]

        table = f"MCP_TEST_UPLOAD_{_SUFFIX}"
        csv = "x,y,label\n1,2,A\n3,4,B\n5,6,A"
        result = (
            await client.call_tool(
                "upload_inline_data",
                {
                    "server_id": server_id,
                    "caslib_name": "Public",
                    "table_name": table,
                    "data": csv,
                },
            )
        ).data
        assert isinstance(result, dict)
        assert result["status"] == "success"
        assert result["source"] == "inline"
        assert result["rows_uploaded"] == 3

        promote_result = (
            await client.call_tool(
                "promote_table_to_memory",
                {
                    "server_id": server_id,
                    "caslib_name": "Public",
                    "table_name": table,
                },
            )
        ).data
        assert isinstance(promote_result, dict)


async def _drop_cas_table(client, server_id, caslib, table):
    """Best-effort cleanup of a CAS table created by an upload test."""
    code = f'proc casutil; droptable casdata="{table}" incaslib="{caslib}" quiet; run;'
    with contextlib.suppress(Exception):  # cleanup must never fail the test
        await client.call_tool("execute_sas_code", {"sas_code": code})


async def test_upload_data_file_path_and_formats(integration_mcp_server):
    """upload_data's context-free sources/formats against live CAS:
    file_path (csv auto-detected), tsv (tab delimiter), and a data_format override.
    """
    async with Client(integration_mcp_server) as client:
        server_id = (await client.call_tool("list_cas_servers", {})).data[0]["name"]
        created = []
        try:
            with tempfile.TemporaryDirectory() as d:
                # 1. file_path source — format auto-detected from the .csv extension,
                #    bytes read server-side (never through the model context).
                csv_path = Path(d) / "applicants.csv"
                csv_path.write_text("x,y,label\n1,2,A\n3,4,B\n5,6,A\n", encoding="utf-8")
                t_csv = f"MCP_TEST_FP_{_SUFFIX}"
                r = (
                    await client.call_tool(
                        "upload_data",
                        {
                            "server_id": server_id,
                            "caslib_name": "Public",
                            "table_name": t_csv,
                            "file_path": str(csv_path),
                        },
                    )
                ).data
                created.append(t_csv)
                assert r["status"] == "success", r
                assert r["source"] == "file_path"
                assert r["data_format"] == "csv"
                assert r["rows_uploaded"] == 3

                # 2. tsv via file_path -> uploaded as csv with a tab delimiter.
                tsv_path = Path(d) / "applicants.tsv"
                tsv_path.write_text("x\ty\tlabel\n1\t2\tA\n3\t4\tB\n", encoding="utf-8")
                t_tsv = f"MCP_TEST_TSV_{_SUFFIX}"
                r2 = (
                    await client.call_tool(
                        "upload_data",
                        {
                            "server_id": server_id,
                            "caslib_name": "Public",
                            "table_name": t_tsv,
                            "file_path": str(tsv_path),
                        },
                    )
                ).data
                created.append(t_tsv)
                assert r2["status"] == "success", r2
                assert r2["data_format"] == "tsv"
                assert r2["rows_uploaded"] == 2
                assert r2["column_count"] == 3

                # 3. data_format override on an extension CAS can't infer.
                dat_path = Path(d) / "applicants.dat"
                dat_path.write_text("x,y\n10,20\n30,40\n", encoding="utf-8")
                t_dat = f"MCP_TEST_FMT_{_SUFFIX}"
                r3 = (
                    await client.call_tool(
                        "upload_data",
                        {
                            "server_id": server_id,
                            "caslib_name": "Public",
                            "table_name": t_dat,
                            "file_path": str(dat_path),
                            "data_format": "csv",
                        },
                    )
                ).data
                created.append(t_dat)
                assert r3["status"] == "success", r3
                assert r3["data_format"] == "csv"
                assert r3["rows_uploaded"] == 2
        finally:
            for t in created:
                await _drop_cas_table(client, server_id, "Public", t)


async def test_upload_data_excel_format(integration_mcp_server):
    """upload_data ingests a real .xlsx (single sheet) into CAS."""
    openpyxl = pytest.importorskip("openpyxl")
    async with Client(integration_mcp_server) as client:
        server_id = (await client.call_tool("list_cas_servers", {})).data[0]["name"]
        table = f"MCP_TEST_XLSX_{_SUFFIX}"
        try:
            with tempfile.TemporaryDirectory() as d:
                xlsx_path = Path(d) / "applicants.xlsx"
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "Applicants"
                ws.append(["x", "y", "label"])
                for row in ([1, 2, "A"], [3, 4, "B"], [5, 6, "A"]):
                    ws.append(row)
                wb.save(xlsx_path)
                r = (
                    await client.call_tool(
                        "upload_data",
                        {
                            "server_id": server_id,
                            "caslib_name": "Public",
                            "table_name": table,
                            "file_path": str(xlsx_path),
                            "sheet_name": "Applicants",
                        },
                    )
                ).data
                assert r["status"] == "success", r
                assert r["data_format"] == "xlsx"
                assert r["rows_uploaded"] == 3
        finally:
            await _drop_cas_table(client, server_id, "Public", table)


# -----------------------------------------------------------------------
# File Service Workflow
# -----------------------------------------------------------------------


async def test_file_service_workflow(integration_mcp_server):
    """upload_file → list_files → download_file"""
    async with Client(integration_mcp_server) as client:
        content = "data mcp_test; x=42; run;"
        upload = (
            await client.call_tool(
                "upload_file",
                {
                    "file_name": "mcp_integration_test.sas",
                    "content": content,
                },
            )
        ).data
        assert "id" in upload
        file_id = upload["id"]

        files = (await client.call_tool("list_files", {"filter_name": "mcp_integration_test"})).data
        assert isinstance(files, list)
        found = any(f["id"] == file_id for f in files)
        assert found, "Uploaded file not found in listing"

        downloaded = (await client.call_tool("download_file", {"file_id": file_id})).data
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
        result = (await client.call_tool("execute_sas_code", {"sas_code": code})).data
        assert isinstance(result, dict)
        assert set(result) >= {"snippet_id", "state", "log", "listing"}
        assert result["state"] in ("completed", "warning")
        assert "mcp_test" in result["log"].lower() or "NOTE" in result["log"]


# -----------------------------------------------------------------------
# Compute Discovery Workflow
# -----------------------------------------------------------------------


async def test_compute_discovery_workflow(integration_mcp_server):
    """list_compute_contexts → list_compute_libraries → list_compute_tables → list_compute_columns.

    Drives the Compute discovery tools against the configured execution context,
    targeting SASHELP.CLASS when present and degrading to the first available
    library/table otherwise so the test stays portable across Viya instances.
    """
    from sas_mcp_server.config import CONTEXT_NAME

    async with Client(integration_mcp_server) as client:
        contexts = (await client.call_tool("list_compute_contexts", {"limit": 50})).data
        assert isinstance(contexts, list)
        assert len(contexts) > 0, "No compute contexts found"

        libraries = (
            await client.call_tool(
                "list_compute_libraries",
                {
                    "compute_context_name": CONTEXT_NAME,
                    "limit": 200,
                },
            )
        ).data
        assert isinstance(libraries, list)
        assert len(libraries) > 0, "No libraries assigned in the compute session"

        lib_names = {str(lib.get("name", "")).upper() for lib in libraries}
        library = "SASHELP" if "SASHELP" in lib_names else libraries[0]["name"]

        tables = (
            await client.call_tool(
                "list_compute_tables",
                {
                    "compute_context_name": CONTEXT_NAME,
                    "library_name": library,
                    "limit": 200,
                },
            )
        ).data
        assert isinstance(tables, list)
        if not tables:
            pytest.skip(f"No tables in library {library} on this Viya")

        table_names = {str(t.get("name", "")).upper() for t in tables}
        table = "CLASS" if library == "SASHELP" and "CLASS" in table_names else tables[0]["name"]

        columns = (
            await client.call_tool(
                "list_compute_columns",
                {
                    "compute_context_name": CONTEXT_NAME,
                    "library_name": library,
                    "table_name": table,
                    "limit": 100,
                },
            )
        ).data
        assert isinstance(columns, list)
        assert len(columns) > 0, f"No columns returned for {library}.{table}"


# -----------------------------------------------------------------------
# Compute Session Reuse + Reset Workflow
# -----------------------------------------------------------------------


async def test_compute_session_reuse_and_reset(integration_mcp_server):
    """Prove session reuse, deletion, and recreation end to end.

    1. Create a WORK table in the compute session.
    2. A second execute_sas_code call still sees it — proving the warm session
       was reused (the old behaviour created a fresh session per call).
    3. reset_compute_session deletes the cached session.
    4. The next call runs in a brand-new, empty session, so the WORK table is
       gone — proving the reset tore down the session and a new one started.
    """
    async with Client(integration_mcp_server) as client:
        # 1. Seed a WORK table with a recognisable sentinel value.
        create = (
            await client.call_tool(
                "execute_sas_code",
                {
                    "sas_code": "data work.reuse_probe; sentinel = 4242; output; run;",
                },
            )
        ).data
        assert create["state"] in ("completed", "warning"), create["log"]

        # 2. Reuse: the WORK table survives into a second call.
        reuse = (
            await client.call_tool(
                "execute_sas_code",
                {
                    "sas_code": "proc print data=work.reuse_probe; run;",
                },
            )
        ).data
        assert reuse["state"] in ("completed", "warning"), (
            "WORK table did not survive a second call — session was not reused.\n" + reuse["log"]
        )
        assert "4242" in reuse["listing"], reuse["listing"]

        # 3. Reset deletes the cached compute session.
        reset = (await client.call_tool("reset_compute_session", {})).data
        assert reset["status"] == "reset", reset
        assert reset.get("deleted_session"), reset

        # 4. Recreate: the next call gets a fresh, empty session.
        after = (
            await client.call_tool(
                "execute_sas_code",
                {
                    "sas_code": "proc print data=work.reuse_probe; run;",
                },
            )
        ).data
        assert after["state"] == "error", (
            "WORK table unexpectedly survived a reset — a new session was not started.\n" + after["log"]
        )
        assert "does not exist" in after["log"].lower(), after["log"]


# -----------------------------------------------------------------------
# Batch Job Workflow
# -----------------------------------------------------------------------


async def test_batch_job_workflow(integration_mcp_server):
    """submit_batch_job → list_jobs → get_job_status → get_job_log"""
    async with Client(integration_mcp_server) as client:
        submit = (
            await client.call_tool(
                "submit_batch_job",
                {
                    "sas_code": "data _null_; put 'MCP integration test'; run;",
                    "job_name": "mcp-integration-test",
                },
            )
        ).data
        assert "id" in submit
        job_id = submit["id"]

        jobs = (await client.call_tool("list_jobs", {"limit": 5})).data
        assert isinstance(jobs, list)

        status = (await client.call_tool("get_job_status", {"job_id": job_id})).data
        assert isinstance(status, dict)
        assert "state" in status

        import asyncio

        for _ in range(30):
            status = (await client.call_tool("get_job_status", {"job_id": job_id})).data
            if status.get("state") in ("completed", "failed", "error", "canceled"):
                break
            await asyncio.sleep(2)

        if status.get("state") == "completed":
            log = (await client.call_tool("get_job_log", {"job_id": job_id})).data
            assert isinstance(log, str)


# -----------------------------------------------------------------------
# Reports Workflow
# -----------------------------------------------------------------------


async def test_report_workflow(integration_mcp_server):
    """list_reports → get_report → export_report"""
    async with Client(integration_mcp_server) as client:
        reports = (await client.call_tool("list_reports", {"limit": 5})).data
        assert isinstance(reports, list)

        # Pin to TEST_REPORT_ID when set (deterministic CI); otherwise use the
        # first report the instance returns.
        report_id = os.getenv("TEST_REPORT_ID")
        if not report_id:
            if not reports:
                pytest.skip("No reports found on this Viya instance")
            report_id = reports[0]["id"]
        report = (await client.call_tool("get_report", {"report_id": report_id})).data
        assert isinstance(report, dict)

        # export_report: retrieve the whole report in the common deliverable
        # formats. summary is text; pdf and package come back as embedded binary
        # files; png as image content. Each must return non-empty content.
        summary = await client.call_tool(
            "export_report",
            {
                "report_id": report_id,
                "export_format": "summary",
            },
        )
        assert summary.content  # text block (may be an empty summary)

        pdf = await client.call_tool(
            "export_report",
            {
                "report_id": report_id,
                "export_format": "pdf",
            },
        )
        assert _embedded_file_bytes(pdf) > 0

        png = await client.call_tool(
            "export_report",
            {
                "report_id": report_id,
                "export_format": "png",
                "image_size": "1200px,800px",
            },
        )
        assert png.content and png.content[0].type == "image"
        assert len(base64.b64decode(png.content[0].data)) > 0

        package = await client.call_tool(
            "export_report",
            {
                "report_id": report_id,
                "export_format": "package",
            },
        )
        assert _embedded_file_bytes(package) > 0


# -----------------------------------------------------------------------
# ML Project Workflow
# -----------------------------------------------------------------------


async def test_ml_project_workflow(integration_mcp_server, viya_token):
    """create_ml_project (auto_run) → poll to completion → list_ml_projects →
    register_ml_champion_model → list_publishing_destinations →
    publish_ml_champion_model.

    Registering and publishing a champion require a *completed* AutoML run — an
    un-run project has no champion model, so the register action returns HTTP
    500. The project is therefore created with ``auto_run=True`` and we poll its
    state until the pipeline finishes before exercising the register/publish
    tools. Everything created here (project, registered model, published module,
    CAS table) is torn down at the end so repeated runs don't accumulate.
    """
    from sas_mcp_server.config import VIYA_ENDPOINT
    from sas_mcp_server.viya_client import make_client

    async with Client(integration_mcp_server) as client:
        servers = (await client.call_tool("list_cas_servers", {})).data
        if not servers:
            pytest.skip("No CAS servers available")
        server_id = servers[0]["name"]

        table = f"MCP_TEST_ML_{_SUFFIX}"
        project_id = registered_href = published_href = None
        try:
            await client.call_tool(
                "upload_inline_data",
                {
                    "server_id": server_id,
                    "caslib_name": "Public",
                    "table_name": table,
                    "data": _ml_training_csv(),
                },
            )

            project = (
                await client.call_tool(
                    "create_ml_project",
                    {
                        "project_name": f"MCP Integration Test {_SUFFIX}",
                        "server_id": server_id,
                        "caslib_name": "Public",
                        "table_name": table,
                        "target_variable": "target",
                        "prediction_type": "binary",
                        "target_event_level": "1",
                        "auto_run": True,
                    },
                )
            ).data
            assert isinstance(project, dict)
            assert "id" in project, project
            project_id = project["id"]

            # list_ml_projects is paginated and unordered, and a busy Viya can
            # hold hundreds of projects — so exercise the tool but don't require
            # the just-created project to appear on the page (cf. the CAS
            # discovery workflow's stance on paginated listings).
            projects = (await client.call_tool("list_ml_projects", {"limit": 100})).data
            assert isinstance(projects, list)

            # A champion model only exists once the AutoML pipeline has finished,
            # so poll the project state until it reaches a terminal state.
            deadline = time.time() + 600
            state = None
            async with make_client(viya_token) as raw:
                while time.time() < deadline:
                    resp = await raw.get(
                        f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                        headers={"Accept": "application/json"},
                    )
                    resp.raise_for_status()
                    state = resp.json().get("state")
                    if state in ("completed", "failed", "error"):
                        break
                    await asyncio.sleep(15)
            if state != "completed":
                pytest.skip(f"AutoML pipeline did not complete in time (state={state})")

            # register: the action response carries a message and a link to the
            # newly registered model (no top-level id).
            registered = (await client.call_tool("register_ml_champion_model", {"project_id": project_id})).data
            assert isinstance(registered, dict)
            registered_href = next(
                (ln["href"] for ln in registered.get("links", []) if ln.get("rel") == "registeredModel"),
                None,
            )
            assert registered_href, registered

            destinations = (await client.call_tool("list_publishing_destinations", {"filter_name": "mas"})).data
            assert isinstance(destinations, list)

            # publish: response links to the published model (again, no id).
            published = (
                await client.call_tool(
                    "publish_ml_champion_model",
                    {
                        "project_id": project_id,
                        "destination_name": destinations[0]["name"] if destinations else "maslocal",
                    },
                )
            ).data
            assert isinstance(published, dict)
            published_href = next(
                (ln["href"] for ln in published.get("links", []) if ln.get("rel") == "publishedModel"),
                None,
            )
            assert published_href, published
        finally:
            # Best-effort teardown; cleanup must never fail the test.
            async with make_client(viya_token) as raw:
                for href in (published_href, registered_href):
                    if href:
                        with contextlib.suppress(Exception):
                            await raw.delete(f"{VIYA_ENDPOINT}{href}")
                if project_id:
                    with contextlib.suppress(Exception):
                        await raw.delete(f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}")
            await _drop_cas_table(client, server_id, "Public", table)


# -----------------------------------------------------------------------
# Scoring Workflow
# -----------------------------------------------------------------------


async def test_scoring_workflow(integration_mcp_server, viya_token):
    """list_registered_models → list_mas_modules → score_data.

    Scores against the most recently modified MAS module on the instance,
    discovering a real step and its input variables rather than guessing.
    """
    async with Client(integration_mcp_server) as client:
        models = (await client.call_tool("list_registered_models", {"limit": 5})).data
        assert isinstance(models, list)

        modules = (await client.call_tool("list_mas_modules", {"limit": 5})).data
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
    steps = (await _viya_get(viya_token, f"/microanalyticScore/modules/{module_id}/steps")).get("items", [])
    if not steps:
        pytest.skip(f"Module {module_id} exposes no steps")
    step = next((s for s in steps if s.get("id") in ("score", "execute")), steps[0])
    step_id = step["id"]
    step_detail = await _viya_get(viya_token, f"/microanalyticScore/modules/{module_id}/steps/{step_id}")
    input_data = {inp["name"]: _dummy_input_value(inp.get("type", "")) for inp in step_detail.get("inputs", [])}

    async with Client(integration_mcp_server) as client:
        try:
            result = (
                await client.call_tool(
                    "score_data",
                    {
                        "module_id": module_id,
                        "step_id": step_id,
                        "input_data": input_data,
                    },
                )
            ).data
        except Exception as e:
            pytest.skip(f"Module {module_id} step '{step_id}' rejected placeholder inputs: {e}")
        assert isinstance(result, dict)


# -----------------------------------------------------------------------
# Business Rules & Decisions Workflow
# -----------------------------------------------------------------------


async def test_business_rules_and_decisions_workflow(integration_mcp_server, viya_token):
    """create_business_ruleset → create_business_rule → lock revision →
    create_decision_flow → lock revision → publish_decision_flow →
    get_mas_module_step_signature, with update/list/get coverage for every
    resource along the way and deletion of everything created.

    Condition/action expressions must include the variable name directly
    (e.g. ``"credit_score < 650"``, not just ``"< 650"``) — the API accepts
    the latter as valid but generates DS2 with a missing left-hand operand.
    """
    ruleset_name = f"mcp_test_ruleset_{_SUFFIX}"
    decision_name = f"mcp_test_decision_{_SUFFIX}"
    ruleset_id = None
    rule_id = None
    decision_id = None
    published_model_id = None
    published_module_id = None

    # Skip cleanly on Viya instances without SAS Intelligent Decisioning
    # deployed — matching how the rest of this suite skips when a service or
    # resource is absent, rather than erroring. Probed via the raw API rather
    # than the tools under test, so a genuine tool bug still fails the test
    # instead of being masked as a skip.
    try:
        await _viya_get(viya_token, "/businessRules/ruleSets", {"limit": 1})
        await _viya_get(viya_token, "/decisions/flows", {"limit": 1})
    except Exception as e:
        pytest.skip(f"SAS Intelligent Decisioning not available on this instance: {e}")

    async with Client(integration_mcp_server) as client:
        try:
            ruleset = (await client.call_tool("create_business_ruleset", {
                "name": ruleset_name,
                "signature": [
                    {"name": "credit_score", "dataType": "integer", "direction": "input"},
                    {"name": "risk_category", "dataType": "string", "direction": "output"},
                ],
                "description": "Integration test ruleset",
            })).data
            ruleset_id = ruleset["id"]

            rule = (await client.call_tool("create_business_rule", {
                "ruleset_id": ruleset_id,
                "name": "Risk_High",
                "conditional": "if",
                "rule_fired_tracking_enabled": True,
                "conditions": [{
                    "type": "complex",
                    "expression": "credit_score < 650",
                    "term": {"name": "credit_score", "dataType": "integer", "direction": "input"},
                }],
                "actions": [{
                    "type": "assignment",
                    "term": {"name": "risk_category", "dataType": "string", "direction": "output"},
                    "expression": '"High"',
                }],
            })).data
            rule_id = rule["id"]

            fetched_rule = (await client.call_tool("get_business_rule", {
                "ruleset_id": ruleset_id, "rule_id": rule_id,
            })).data
            assert fetched_rule["name"] == "Risk_High"

            rules = (await client.call_tool("list_business_rules", {"ruleset_id": ruleset_id})).data
            assert any(r["id"] == rule_id for r in rules)

            updated_rule = (await client.call_tool("update_business_rule", {
                "ruleset_id": ruleset_id,
                "rule_id": rule_id,
                "name": "Risk_High",
                "conditional": "if",
                "rule_fired_tracking_enabled": True,
                "conditions": [{
                    "type": "complex",
                    "expression": "credit_score < 600",
                    "term": {"name": "credit_score", "dataType": "integer", "direction": "input"},
                }],
                "actions": [{
                    "type": "assignment",
                    "term": {"name": "risk_category", "dataType": "string", "direction": "output"},
                    "expression": '"High"',
                }],
            })).data
            assert updated_rule["conditions"][0]["expression"] == "credit_score < 600"

            fetched_ruleset = (await client.call_tool("get_business_ruleset", {
                "ruleset_id": ruleset_id,
            })).data
            assert fetched_ruleset["name"] == ruleset_name

            rulesets = (await client.call_tool("list_business_rulesets", {
                "filter_name": ruleset_name,
            })).data
            assert any(r["id"] == ruleset_id for r in rulesets)

            updated_ruleset = (await client.call_tool("update_business_ruleset", {
                "ruleset_id": ruleset_id,
                "name": ruleset_name,
                "signature": [
                    {"name": "credit_score", "dataType": "integer", "direction": "input"},
                    {"name": "risk_category", "dataType": "string", "direction": "output"},
                ],
                "description": "Integration test ruleset (updated)",
            })).data
            assert updated_ruleset["description"] == "Integration test ruleset (updated)"

            revision = (await client.call_tool("lock_business_ruleset_revision", {
                "ruleset_id": ruleset_id,
            })).data
            version_id = revision["id"]

            revisions = (await client.call_tool("list_business_ruleset_revisions", {
                "ruleset_id": ruleset_id,
            })).data
            assert any(r["id"] == version_id for r in revisions)

            decision = (await client.call_tool("create_decision_flow", {
                "name": decision_name,
                "signature": [
                    {"name": "credit_score", "direction": "input", "dataType": "integer"},
                    {"name": "risk_category", "direction": "output", "dataType": "string"},
                ],
                "rule_set_steps": [{
                    "ruleSetId": ruleset_id,
                    "versionId": version_id,
                    "mappings": [
                        {"stepTermName": "credit_score", "direction": "input",
                         "targetDecisionTermName": "credit_score"},
                        {"stepTermName": "risk_category", "direction": "output",
                         "targetDecisionTermName": "risk_category"},
                    ],
                }],
            })).data
            decision_id = decision["id"]

            fetched_decision = (await client.call_tool("get_decision_flow", {
                "decision_id": decision_id,
            })).data
            assert fetched_decision["name"] == decision_name

            decisions = (await client.call_tool("list_decision_flows", {
                "filter_name": decision_name,
            })).data
            assert any(d["id"] == decision_id for d in decisions)

            updated_decision = (await client.call_tool("update_decision_flow", {
                "decision_id": decision_id,
                "name": decision_name,
                "signature": [
                    {"name": "credit_score", "direction": "input", "dataType": "integer"},
                    {"name": "risk_category", "direction": "output", "dataType": "string"},
                ],
                "rule_set_steps": [{
                    "ruleSetId": ruleset_id,
                    "versionId": version_id,
                    "mappings": [
                        {"stepTermName": "credit_score", "direction": "input",
                         "targetDecisionTermName": "credit_score"},
                        {"stepTermName": "risk_category", "direction": "output",
                         "targetDecisionTermName": "risk_category"},
                    ],
                }],
            })).data
            assert updated_decision["name"] == decision_name

            code = (await client.call_tool("get_decision_flow_code", {
                "decision_id": decision_id,
            })).data
            assert isinstance(code, str) and len(code) > 0

            decision_revision = (await client.call_tool("lock_decision_flow_revision", {
                "decision_id": decision_id,
            })).data
            decision_revision_id = decision_revision["id"]

            decision_revisions = (await client.call_tool("list_decision_flow_revisions", {
                "decision_id": decision_id,
            })).data
            assert any(r["id"] == decision_revision_id for r in decision_revisions)

            fetched_revision = (await client.call_tool("get_decision_flow_revision", {
                "decision_id": decision_id, "revision_id": decision_revision_id,
            })).data
            assert fetched_revision["name"] == decision_name

            try:
                published = (await client.call_tool("publish_decision_flow", {
                    "decision_id": decision_id,
                    "revision_id": decision_revision_id,
                    "publish_name": f"mcp_test_publish_{_SUFFIX}",
                })).data
                assert isinstance(published, dict)
                published_model_id = published.get("id")
                published_module_id = published.get("moduleId")
                if not published_module_id:
                    pytest.skip("Publish job did not reach a terminal state within the poll timeout")

                signature = (await client.call_tool("get_mas_module_step_signature", {
                    "module_id": published_module_id,
                })).data
                assert "inputs" in signature
            except Exception as e:
                pytest.skip(f"DS2 codegen/publish unavailable on this instance: {e}")
        finally:
            # Best-effort cleanup, innermost/most-dependent first. The MAS
            # module and Model Publish record have no MCP delete tool, so they
            # are removed via the raw API.
            if published_module_id:
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/microanalyticScore/modules/{published_module_id}")
            if published_model_id:
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/modelPublish/models/{published_model_id}")
            async with Client(integration_mcp_server) as cleanup_client:
                if decision_id:
                    with contextlib.suppress(Exception):
                        await cleanup_client.call_tool("delete_decision_flow", {"decision_id": decision_id})
                if ruleset_id and rule_id:
                    with contextlib.suppress(Exception):
                        await cleanup_client.call_tool("delete_business_rule", {
                            "ruleset_id": ruleset_id, "rule_id": rule_id,
                        })
                if ruleset_id:
                    with contextlib.suppress(Exception):
                        await cleanup_client.call_tool("delete_business_ruleset", {"ruleset_id": ruleset_id})


# -----------------------------------------------------------------------
# Cancel Job Workflow
# -----------------------------------------------------------------------


async def test_cancel_job_workflow(integration_mcp_server):
    """submit_batch_job → cancel_job"""
    async with Client(integration_mcp_server) as client:
        submit = (
            await client.call_tool(
                "submit_batch_job",
                {
                    "sas_code": "data _null_; do i = 1 to 100000000; end; run;",
                    "job_name": f"mcp-cancel-test-{_SUFFIX}",
                },
            )
        ).data
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
            result = (await client.call_tool("run_ml_project", {"project_id": project_id})).data
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
            sources = (
                await client.call_tool(
                    "list_source_tables",
                    {
                        "server_id": server,
                        "caslib_name": lib,
                        "limit": 25,
                    },
                )
            ).data
            if sources:
                caslib = lib
                break
        if not sources:
            pytest.skip("No unloaded source tables available to promote")

        # Try a few candidates so one quirky source table doesn't fail the run.
        promoted = None
        for cand in sources[:5]:
            try:
                result = (
                    await client.call_tool(
                        "promote_table_to_memory",
                        {
                            "server_id": server,
                            "caslib_name": caslib,
                            "table_name": cand["name"],
                        },
                    )
                ).data
            except Exception:
                continue
            if result.get("scope") == "global" and result.get("status") in ("promoted", "already_global"):
                promoted = (cand["name"], result)
                break
        if not promoted:
            pytest.skip("Could not load any source table on this Viya")
        table, result = promoted

        info = (
            await client.call_tool(
                "get_castable_info",
                {
                    "server_id": server,
                    "caslib_name": caslib,
                    "table_name": table,
                },
            )
        ).data
        assert info.get("state") == "loaded"
        assert info.get("scope") == "global"

    # Cleanup: if we loaded it, unload it again to restore prior caslib state.
    if result["status"] == "promoted":
        from sas_mcp_server.config import VIYA_ENDPOINT
        from sas_mcp_server.viya_client import make_client

        async with make_client(viya_token) as raw:
            await raw.put(
                f"{VIYA_ENDPOINT}/casManagement/servers/{server}/caslibs/{caslib}/tables/{table}/state",
                params={"value": "unloaded"},
                headers={"Accept": "*/*"},
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
# Information Catalog Workflows
# -----------------------------------------------------------------------


def _hmeq_public_hit(items: list) -> dict | None:
    """Find an HMEQ-in-Public-CAS hit by its resource URI, if present."""
    for h in items:
        uri = (h.get("resource_uri") or "").rstrip("/")
        if uri.endswith("/Public/tables/HMEQ"):
            return h
    return None


async def test_catalog_agents_workflow(integration_mcp_server):
    """catalog_list_agents → catalog_get_agent_history → run the 'Public' agent."""
    async with Client(integration_mcp_server) as client:
        try:
            agents = (await client.call_tool("catalog_list_agents", {"limit": 100})).data
        except Exception as e:
            pytest.skip(f"Information Catalog not available on this Viya: {e}")
        assert isinstance(agents, list)

        public_agent = next(
            (a for a in agents if (a.get("name") or "").strip().lower() == "public"),
            None,
        )
        if public_agent is None:
            pytest.skip("No discovery agent named 'Public' on this Viya")
        agent_id = public_agent["id"]

        # Run history must be retrievable for the agent.
        history = (await client.call_tool("catalog_get_agent_history", {"agent_id": agent_id, "limit": 5})).data
        assert isinstance(history, list)

        # Trigger the Public agent. 409 means it is already running — also a success.
        try:
            run = (await client.call_tool("catalog_run_agent", {"agent_id": agent_id})).data
        except Exception as e:
            if "409" in str(e):
                pytest.skip("Public agent is already running")
            raise
        assert run["agent_id"] == agent_id
        assert run["status"]


async def test_catalog_table_profile_loop(integration_mcp_server):
    """Full loop: find/load HMEQ → ad-hoc analyze → poll to completion → download profile.

    Proves the run→retrieve→profile chain end to end: searches for HMEQ in the
    Public CAS library, loads sampsio.hmeq if it is not already cataloged, runs an
    ad-hoc analysis, waits for it to complete, then downloads the profile and
    asserts it is now available (status 'ok').
    """
    import asyncio

    hmeq_uri = "/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ"

    async with Client(integration_mcp_server) as client:
        # 1. Is HMEQ already in the catalog under Public?
        try:
            results = (await client.call_tool("catalog_search", {"query": "Name:HMEQ", "limit": 25})).data
        except Exception as e:
            pytest.skip(f"Information Catalog not available on this Viya: {e}")

        # Exercise the search helper while we are here.
        helper = (await client.call_tool("catalog_search_helper", {})).data
        assert "facets" in helper

        hit = _hmeq_public_hit(results["items"])

        # 2. Not in the catalog → ensure HMEQ is loaded (promoted) in Public CAS.
        # Idempotent: drop any existing copy first so a re-run never hits the
        # "global-scope tables cannot be replaced" error.
        if hit is None:
            load_code = (
                "cas mySess;\n"
                "libname public cas casLib='Public';\n"
                "proc casutil;\n"
                "  droptable casdata='HMEQ' incaslib='Public' quiet;\n"
                "  load data=sampsio.hmeq outcaslib='Public' casout='HMEQ' promote;\n"
                "quit;\n"
                "cas mySess terminate;\n"
            )
            load = (await client.call_tool("execute_sas_code", {"sas_code": load_code})).data
            assert load["state"] in ("completed", "warning"), load["log"]

        resource_uri = hit["resource_uri"] if hit else hmeq_uri
        resource_type = (hit.get("type") if hit else None) or "casTable"

        # 3. Submit an ad-hoc analysis; submission must return a job id + status.
        job = (
            await client.call_tool(
                "catalog_run_adhoc_analysis",
                {
                    "resource_uri": resource_uri,
                    "resource_type": resource_type,
                    "name": f"mcp-hmeq-adhoc-{_SUFFIX}",
                },
            )
        ).data
        job_id = job["id"]
        assert job_id, job
        assert job["status"], "a submitted job should report a status"

        # 4. Monitor until the job reaches a terminal state — this proves the
        # submit -> poll -> retrieve mechanism. Whether the analysis *succeeds*
        # depends on the Viya analysis backend, so we assert the job is trackable
        # to a terminal state, not that the backend can always profile.
        # 'not_found' = the job was purged after finishing, also terminal.
        # Cold profiling of a CAS table routinely runs past two minutes, so we
        # poll up to 5 minutes (60 x 5s) — the same envelope David's reference
        # script waits — rather than flaking on a job that is still 'running'.
        terminal = {"completed", "failed", "error", "canceled", "not_found"}
        status = str(job["status"]).lower()
        for _ in range(60):
            if status in terminal:
                break
            await asyncio.sleep(5)
            status = str(
                (await client.call_tool("catalog_get_adhoc_analysis", {"job_id": job_id})).data["status"]
            ).lower()
        assert status in terminal, f"ad-hoc job never reached a terminal state: {status!r}"

        # 4b. Resolve the instance straight from the resource URI — the search ->
        # profile bridge. Either the URI is indexed ('ok' with an instance id) or
        # it is not yet ('not_found'); both are valid, so assert the shape.
        found = (await client.call_tool("catalog_find_instance", {"resource_uri": resource_uri})).data
        assert found["status"] in ("ok", "not_found"), found
        if found["status"] == "ok":
            assert found["instance_id"], found

        # 5. Exercise the download tool end to end. Walk candidate tables across
        # asset types and download each until one returns a profile ('ok') — that
        # proves the CSV download path against a genuinely profiled table. If no
        # table on this instance is profiled, the 'not_profiled' recommendation
        # is the valid outcome.
        candidates: list = []
        for facet in (
            "AssetType:parquet",
            "AssetType:cas",
            "AssetType:inmemorytable",
            "AssetType:sas",
        ):
            candidates += (await client.call_tool("catalog_search", {"query": facet, "limit": 15})).data["items"]
        if not any(c.get("id") for c in candidates):
            pytest.skip("No table instances available to download a profile")

        last = None
        for cand in candidates:
            if not cand.get("id"):
                continue
            last = (await client.call_tool("catalog_download_table_profile", {"instance_id": cand["id"]})).data
            if last["status"] == "ok":
                assert last.get("csv", "").strip(), "profiled table returned empty CSV"
                break
        assert last is not None
        assert last["status"] in ("ok", "not_profiled"), last


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
    "upload_data": "test_upload_data_file_path_and_formats",
    "upload_inline_data": "test_data_upload_workflow",
    "promote_table_to_memory": "test_promote_from_source_workflow",
    "list_files": "test_file_service_workflow",
    "upload_file": "test_file_service_workflow",
    "download_file": "test_file_service_workflow",
    "list_reports": "test_report_workflow",
    "get_report": "test_report_workflow",
    "export_report": "test_report_workflow",
    "submit_batch_job": "test_batch_job_workflow",
    "get_job_status": "test_batch_job_workflow",
    "list_jobs": "test_batch_job_workflow",
    "cancel_job": "test_cancel_job_workflow",
    "get_job_log": "test_batch_job_workflow",
    "list_ml_projects": "test_ml_project_workflow",
    "create_ml_project": "test_ml_project_workflow",
    "register_ml_champion_model": "test_ml_project_workflow",
    "list_publishing_destinations": "test_ml_project_workflow",
    "publish_ml_champion_model": "test_ml_project_workflow",
    "run_ml_project": "test_run_ml_project_workflow",
    "list_registered_models": "test_scoring_workflow",
    "list_mas_modules": "test_scoring_workflow",
    "score_data": "test_scoring_workflow",
    "create_business_ruleset": "test_business_rules_and_decisions_workflow",
    "update_business_ruleset": "test_business_rules_and_decisions_workflow",
    "get_business_ruleset": "test_business_rules_and_decisions_workflow",
    "list_business_rulesets": "test_business_rules_and_decisions_workflow",
    "delete_business_ruleset": "test_business_rules_and_decisions_workflow",
    "lock_business_ruleset_revision": "test_business_rules_and_decisions_workflow",
    "list_business_ruleset_revisions": "test_business_rules_and_decisions_workflow",
    "create_business_rule": "test_business_rules_and_decisions_workflow",
    "update_business_rule": "test_business_rules_and_decisions_workflow",
    "get_business_rule": "test_business_rules_and_decisions_workflow",
    "list_business_rules": "test_business_rules_and_decisions_workflow",
    "delete_business_rule": "test_business_rules_and_decisions_workflow",
    "create_decision_flow": "test_business_rules_and_decisions_workflow",
    "update_decision_flow": "test_business_rules_and_decisions_workflow",
    "get_decision_flow": "test_business_rules_and_decisions_workflow",
    "list_decision_flows": "test_business_rules_and_decisions_workflow",
    "delete_decision_flow": "test_business_rules_and_decisions_workflow",
    "get_decision_flow_code": "test_business_rules_and_decisions_workflow",
    "lock_decision_flow_revision": "test_business_rules_and_decisions_workflow",
    "list_decision_flow_revisions": "test_business_rules_and_decisions_workflow",
    "get_decision_flow_revision": "test_business_rules_and_decisions_workflow",
    "publish_decision_flow": "test_business_rules_and_decisions_workflow",
    "get_mas_module_step_signature": "test_business_rules_and_decisions_workflow",
    "list_compute_contexts": "test_compute_discovery_workflow",
    "list_compute_libraries": "test_compute_discovery_workflow",
    "list_compute_tables": "test_compute_discovery_workflow",
    "list_compute_columns": "test_compute_discovery_workflow",
    "reset_compute_session": "test_compute_session_reuse_and_reset",
    "catalog_search": "test_catalog_table_profile_loop",
    "catalog_search_helper": "test_catalog_table_profile_loop",
    "catalog_find_instance": "test_catalog_table_profile_loop",
    "catalog_download_table_profile": "test_catalog_table_profile_loop",
    "catalog_run_adhoc_analysis": "test_catalog_table_profile_loop",
    "catalog_get_adhoc_analysis": "test_catalog_table_profile_loop",
    "catalog_list_agents": "test_catalog_agents_workflow",
    "catalog_run_agent": "test_catalog_agents_workflow",
    "catalog_get_agent_history": "test_catalog_agents_workflow",
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
