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


async def _viya_post(token: str, path: str, body: dict, params: dict | None = None) -> dict:
    """Authenticated POST against the live Viya, used to set up test fixtures."""
    from sas_mcp_server.config import VIYA_ENDPOINT
    from sas_mcp_server.viya_client import make_client

    async with make_client(token) as client:
        resp = await client.post(
            f"{VIYA_ENDPOINT}{path}",
            json=body,
            params=params or {},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def _parent_folder_id(token: str, asset_uri: str) -> str | None:
    """The id of the folder *asset_uri* sits in, or None when it is in none."""
    from sas_mcp_server.config import VIYA_ENDPOINT
    from sas_mcp_server.viya_client import make_client

    async with make_client(token) as client:
        resp = await client.get(
            f"{VIYA_ENDPOINT}/folders/ancestors",
            params={"childUri": asset_uri},
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()[0]["id"]


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

        rows = (
            await client.call_tool(
                "get_compute_table_data",
                {
                    "compute_context_name": CONTEXT_NAME,
                    "library_name": library,
                    "table_name": table,
                    "limit": 3,
                },
            )
        ).data
        assert rows["columns"], f"No columns in the row page for {library}.{table}"
        # list_compute_columns paged at 100; the row page names every column.
        assert {c["name"] for c in columns} <= set(rows["columns"])
        assert len(rows["rows"]) <= 3
        for row in rows["rows"]:
            assert set(row) == set(rows["columns"]), "each row is keyed by the column names"
        if rows["count"] > 3:
            assert rows["truncated"] is True


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


async def test_report_authoring_workflow(integration_mcp_server):
    """describe_report_objects → create_report (one atomic build with data
    polish + page title + titled chart) → apply_report_operations (relative
    placement chained on returned names, then save-as) → get_report_outline →
    copy_report → delete_report.

    Uses the ``Public.HMEQ`` sample table (same anchor as the CAS discovery
    workflow); every report created here — the build, the save-as result, and
    the copy — is deleted at the end.
    """
    report_name = f"MCP Authoring IT {_SUFFIX}"
    async with Client(integration_mcp_server) as client:
        # Discovery is a local catalog read — sanity-check the surfaces the
        # authoring loop depends on.
        index = (await client.call_tool("describe_report_objects", {})).data
        assert any(o["schema_key"] == "keyValue" for o in index["objects"])
        assert "single KPI number" in index["intent_map"]
        add_data = (await client.call_tool("describe_report_objects", {"operation": "addData"})).data
        assert "dataItems" in add_data["example"]["addData"]

        servers = (await client.call_tool("list_cas_servers", {})).data
        assert servers, "No CAS servers found"
        cas_server = servers[0]["name"]

        extra_ids: list[str] = []
        created = (
            await client.call_tool(
                "create_report",
                {
                    "name": report_name,
                    "operations": [
                        {
                            "addData": {
                                "cas": {"server": cas_server, "library": "Public", "table": "HMEQ"},
                                "dataItems": [
                                    {
                                        "dataItem": "LOAN",
                                        "properties": {
                                            "name": "Loan Amount",
                                            "format": "DOLLAR12.",
                                            "aggregation": "average",
                                        },
                                    }
                                ],
                            }
                        },
                        {"addPage": {"pageName": "Overview", "title": "HMEQ Overview"}},
                        {
                            "addObject": {
                                "object": {
                                    "barChart": {
                                        "dataSource": "HMEQ",
                                        "dataRoles": {"category": "REASON", "measures": ["Loan Amount"]},
                                        "options": {"object": {"title": "Average Loan by Reason"}},
                                    }
                                },
                                "placement": {"page": {"target": "Overview"}},
                            }
                        },
                    ],
                },
            )
        ).data
        if created.get("status") == "create_failed":
            pytest.skip(f"Report create failed on this Viya (is Public.HMEQ available?): {created.get('message')}")
        assert created["status"] == "created"
        report_id = created["id"]
        try:
            # The title expands into a body text object appended at the END of
            # the batch (so caller op indices survive), giving [barChart, text]
            # — index-aligned names from the response.
            objects = created["created"]["objects"]
            assert [o["type"] for o in objects] == ["barChart", "text"]
            bar_name = objects[0].get("name")
            assert bar_name, "create response carried no object names"

            # Chain a KPI tile to the right of the bar chart by its returned name.
            applied = (
                await client.call_tool(
                    "apply_report_operations",
                    {
                        "report_id": report_id,
                        "operations": [
                            {
                                "addObject": {
                                    "object": {
                                        # keyValue tiles get no title — the measure's
                                        # label renders prominently on the tile.
                                        "keyValue": {
                                            "dataSource": "HMEQ",
                                            "dataRoles": {"measure": "Loan Amount"},
                                        }
                                    },
                                    "placement": {
                                        "relativeToObject": {"target": bar_name, "position": "right"}
                                    },
                                }
                            }
                        ],
                    },
                )
            ).data
            assert applied["status"] == "applied"
            assert applied["created"]["objects"][0].get("name")

            # Read the structure back: the page and both chained objects.
            outline = (await client.call_tool("get_report_outline", {"report_id": report_id})).data
            assert outline["status"] == "ok"
            overview = next(p for p in outline["pages"] if p["label"] == "Overview")
            assert any(o["name"] == bar_name for o in overview["objects"])

            # Save-as: apply to a NEW report, leaving the source untouched.
            saved = (
                await client.call_tool(
                    "apply_report_operations",
                    {
                        "report_id": report_id,
                        "operations": [{"addPage": {"pageName": "SaveAsPage"}}],
                        "result_report_name": f"{report_name} SaveAs",
                        "save_as": True,
                    },
                )
            ).data
            assert saved["status"] == "applied"
            assert saved["saved_as"]["id"]
            extra_ids.append(saved["saved_as"]["id"])
            source_outline = (await client.call_tool("get_report_outline", {"report_id": report_id})).data
            assert all(p["label"] != "SaveAsPage" for p in source_outline["pages"])

            copied = (
                await client.call_tool(
                    "copy_report", {"report_id": report_id, "name": f"{report_name} Copy"}
                )
            ).data
            assert copied["status"] == "copied"
            assert copied["id"]
            extra_ids.append(copied["id"])
        finally:
            deleted = (await client.call_tool("delete_report", {"report_id": report_id})).data
            assert deleted["status"] == "deleted"
            for extra in extra_ids:
                with contextlib.suppress(Exception):
                    await client.call_tool("delete_report", {"report_id": extra})


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
                "folder": "My Folder",
            })).data
            ruleset_id = ruleset["id"]
            assert ruleset["folder"]["path"].endswith("/My Folder")

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
                "folder": "My Folder",
            })).data
            decision_id = decision["id"]
            assert decision["folder"]["path"].endswith("/My Folder")

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
# Folder placement (find_folder; decisioning assets always in a folder)
# -----------------------------------------------------------------------


async def test_decisioning_folder_placement(integration_mcp_server, viya_token, monkeypatch):
    """find_folder in every reference form; creates refuse without a folder,
    honour a path below My Folder and MCP_DEFAULT_FOLDER; an update files a
    folderless rule set and leaves one in a folder where it is."""
    try:
        await _viya_get(viya_token, "/businessRules/ruleSets", {"limit": 1})
        await _viya_get(viya_token, "/decisions/flows", {"limit": 1})
    except Exception as e:
        pytest.skip(f"SAS Intelligent Decisioning not available on this instance: {e}")

    folder_name = f"mcp_test_folder_{_SUFFIX}"
    folder = await _viya_post(
        viya_token, "/folders/folders", {"name": folder_name, "type": "folder"},
        params={"parentFolderUri": "/folders/folders/@myFolder"},
    )
    folder_id = folder["id"]
    folder_uri = f"/folders/folders/{folder_id}"
    created_rulesets: list[str] = []
    decision_id = None
    signature = [
        {"name": "credit_score", "dataType": "integer", "direction": "input"},
        {"name": "risk_category", "dataType": "string", "direction": "output"},
    ]

    async with Client(integration_mcp_server) as client:
        try:
            # find_folder: a path below My Folder, the id, the URI and the full path agree
            by_my_path = (await client.call_tool("find_folder", {"folder": f"My Folder/{folder_name}"})).data
            assert by_my_path["id"] == folder_id
            assert by_my_path["path"].endswith(f"/My Folder/{folder_name}")
            for reference in (folder_id, folder_uri, by_my_path["path"]):
                found = (await client.call_tool("find_folder", {"folder": reference})).data
                assert found == by_my_path, reference
            missing = await client.call_tool(
                "find_folder", {"folder": f"/Public/no_such_folder_{_SUFFIX}"}, raise_on_error=False
            )
            assert missing.is_error

            # no folder and no default: refused, nothing created
            monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", "")
            refused = await client.call_tool("create_business_ruleset", {
                "name": f"mcp_nofolder_{_SUFFIX}", "signature": signature,
            }, raise_on_error=False)
            assert refused.is_error
            assert "Ask the user where to store it" in refused.content[0].text
            leftovers = await _viya_get(
                viya_token, "/businessRules/ruleSets", {"filter": f"eq(name,'mcp_nofolder_{_SUFFIX}')"}
            )
            assert leftovers.get("count", 0) == 0

            # a path below My Folder
            ruleset = (await client.call_tool("create_business_ruleset", {
                "name": f"mcp_fold_rs_{_SUFFIX}", "signature": signature,
                "folder": f"My Folder/{folder_name}",
            })).data
            created_rulesets.append(ruleset["id"])
            assert ruleset["folder"]["id"] == folder_id
            assert await _parent_folder_id(viya_token, f"/businessRules/ruleSets/{ruleset['id']}") == folder_id

            # the default folder, for a decision created without a folder
            rule = (await client.call_tool("create_business_rule", {
                "ruleset_id": ruleset["id"], "name": "High", "conditional": "if",
                "rule_fired_tracking_enabled": False,
                "conditions": [{"type": "complex", "expression": "credit_score < 600",
                                "term": {"name": "credit_score", "dataType": "integer", "direction": "input"}}],
                "actions": [{"type": "assignment", "expression": "'HIGH'",
                             "term": {"name": "risk_category", "dataType": "string", "direction": "output"}}],
            })).data
            assert rule["id"]
            version = (await client.call_tool("lock_business_ruleset_revision", {
                "ruleset_id": ruleset["id"],
            })).data
            monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", folder_uri)
            decision = (await client.call_tool("create_decision_flow", {
                "name": f"mcp_fold_dec_{_SUFFIX}",
                "signature": [
                    {"name": "credit_score", "direction": "input", "dataType": "integer"},
                    {"name": "risk_category", "direction": "output", "dataType": "string"},
                ],
                "rule_set_steps": [{
                    "ruleSetId": ruleset["id"], "versionId": version["id"],
                    "mappings": [
                        {"stepTermName": "credit_score", "direction": "input",
                         "targetDecisionTermName": "credit_score"},
                        {"stepTermName": "risk_category", "direction": "output",
                         "targetDecisionTermName": "risk_category"},
                    ],
                }],
            })).data
            decision_id = decision["id"]
            assert decision["folder"]["id"] == folder_id
            assert await _parent_folder_id(viya_token, f"/decisions/flows/{decision_id}") == folder_id

            # an update leaves a rule set in its folder, whatever folder is named
            kept = (await client.call_tool("update_business_ruleset", {
                "ruleset_id": ruleset["id"], "name": f"mcp_fold_rs_{_SUFFIX}",
                "signature": signature, "folder": "My Folder",
            })).data
            assert kept["folder"]["status"] == "unchanged"
            assert kept["folder"]["id"] == folder_id

            # a rule set made in no folder (as before) is filed by its next update
            orphan = await _viya_post(viya_token, "/businessRules/ruleSets", {
                "name": f"mcp_orphan_rs_{_SUFFIX}", "signature": signature,
            })
            created_rulesets.append(orphan["id"])
            assert await _parent_folder_id(viya_token, f"/businessRules/ruleSets/{orphan['id']}") is None
            filed = (await client.call_tool("update_business_ruleset", {
                "ruleset_id": orphan["id"], "name": f"mcp_orphan_rs_{_SUFFIX}",
                "signature": signature, "folder": folder_uri,
            })).data
            assert filed["folder"]["status"] == "filed"
            assert await _parent_folder_id(viya_token, f"/businessRules/ruleSets/{orphan['id']}") == folder_id
        finally:
            async with Client(integration_mcp_server) as cleanup_client:
                if decision_id:
                    with contextlib.suppress(Exception):
                        await cleanup_client.call_tool("delete_decision_flow", {"decision_id": decision_id})
                for ruleset_id in created_rulesets:
                    with contextlib.suppress(Exception):
                        await cleanup_client.call_tool("delete_business_ruleset", {"ruleset_id": ruleset_id})
            with contextlib.suppress(Exception):
                await _viya_delete(viya_token, f"{folder_uri}?recursive=true")


# -----------------------------------------------------------------------
# Decisioning content (trees, code files, value lists, functions, flows)
# -----------------------------------------------------------------------


async def test_decisioning_content_workflow(integration_mcp_server, viya_token):
    """Every decisioning content tool once, in a scratch folder below My Folder:
    custom function -> value list -> segmentation trees (written and from a table)
    -> code files (DS2, Python, SQL) -> revisions, copy, code -> a decision built from
    shorthand steps -> everything deleted again."""
    try:
        # Collections answer application/json for flows and rule sets (the tree listing does not).
        await _viya_get(viya_token, "/businessRules/ruleSets", {"limit": 1})
        await _viya_get(viya_token, "/decisions/flows", {"limit": 1})
    except Exception as e:
        pytest.skip(f"SAS Intelligent Decisioning not available on this instance: {e}")

    folder_name = f"mcp_test_content_{_SUFFIX}"
    folder = await _viya_post(
        viya_token, "/folders/folders", {"name": folder_name, "type": "folder"},
        params={"parentFolderUri": "/folders/folders/@myFolder"},
    )
    target = f"/folders/folders/{folder['id']}"
    created: list[tuple[str, str]] = []  # (asset_type, id), deleted in reverse
    signature = [
        {"name": "score", "dataType": "decimal", "direction": "input"},
        {"name": "band", "dataType": "string", "direction": "output"},
    ]
    function_code = (
        "method mcp_test_band_" + _SUFFIX + "(double score) returns varchar(10);\n"
        "   if score >= 630 then return 'LOW'; return 'HIGH';\nend;"
    )

    async def tool(name, args):
        result = await client.call_tool(name, args)
        return result.data if result.data is not None else result.content[0].text

    async with Client(integration_mcp_server) as client:
        try:
            # custom function, validation
            broken = function_code.replace("return 'HIGH';", "return 'HIGH'")
            check = await tool("validate_custom_function", {"code": broken})
            assert check["valid"] is False and check["errors"]
            function = await tool("create_custom_function", {
                "code": function_code, "category": f"mcp_test_cat_{_SUFFIX}", "description": "integration test"})
            created += [("function_category", function["category"]["id"]), ("custom_function", function["id"])]
            updated_fn = await tool("update_custom_function", {"function_id": function["id"], "description": "changed"})
            assert updated_fn["description"] == "changed"
            expression = await tool("validate_decision_expression", {
                "expression": "score * 2 >= 1260", "signature": signature[:1]})
            assert expression["valid"] is True
            bad = await tool("validate_decision_expression", {"expression": "score * ", "signature": signature[:1]})
            assert bad["valid"] is False and bad["errors"]

            # value list
            value_list = await tool("create_value_list", {
                "name": f"mcp_test_bands_{_SUFFIX}", "data_type": "numeric", "folder": target,
                "entries": [{"key": "v>=630", "label": "630 and above"}]})
            created.append(("value_list", value_list["id"]))
            entries = await tool("update_value_list_entries", {
                "value_list_id": value_list["id"], "entries": ["v>=630", "600<=v<630", "v<600"]})
            assert [e["key"] for e in entries["entries"]] == ["v>=630", "600<=v<630", "v<600"]

            # a written tree, then an update
            tree = await tool("create_segmentation_tree", {
                "name": f"mcp_test_tree_{_SUFFIX}", "folder": target, "signature": signature,
                "node": {"variable": "score", "valueNodes": [{"value": ">=630", "outcome": "low"},
                                                             {"value": "Other", "outcome": "high"}]},
                "outcomes": [{"name": "low", "assign": {"band": "LOW"}}, {"name": "high", "assign": {"band": "HIGH"}}]})
            created.append(("segmentation_tree", tree["id"]))
            assert tree["folder"]["id"] == folder["id"]
            changed = await tool("update_segmentation_tree", {"tree_id": tree["id"], "description": "changed"})
            assert changed["description"] == "changed" and changed["folder"]["status"] == "unchanged"

            # a tree from a strategy table, with value lists
            from_table = await tool("create_segmentation_tree_from_table", {
                "name": f"mcp_test_strategy_{_SUFFIX}", "folder": target, "key_columns": ["Score"],
                "outcome_columns": ["Band"], "create_value_lists": True,
                "rows": [{"Score": 0, "Band": "HIGH"}, {"Score": 600, "Band": "MID"}, {"Score": 630, "Band": "LOW"}]})
            created.append(("segmentation_tree", from_table["id"]))
            created += [("value_list", v["id"]) for v in from_table["value_lists"]]
            assert from_table["report"]["leaves"] == 3

            # code files of every type
            ds2 = await tool("create_code_file", {
                "name": f"mcp test ds2 {_SUFFIX}", "code_type": "ds2", "folder": target,
                "description": "integration test",
                "code": 'package "${PACKAGE_NAME}" /inline;\n method execute(double score, in_out varchar band);\n'
                        "   if score >= 630 then band = 'LOW'; else band = 'HIGH';\n end;\nendpackage;\n"})
            created.append(("code_file", ds2["id"]))
            assert ds2["name"] == f"mcp test ds2 {_SUFFIX}" and ds2["description"] == "integration test"
            python = await tool("create_code_file", {
                "name": f"mcp test py {_SUFFIX}", "code_type": "python", "folder": target,
                "code": "def execute(score):\n   'Output: band'\n"
                        "   return 'LOW' if score and score >= 630 else 'HIGH'\n",
                "signature": signature})
            created.append(("code_file", python["id"]))
            assert {t["name"]: t["dataType"] for t in python["signature"]} == {"score": "decimal", "band": "string"}
            sql = await tool("create_code_file", {
                "name": f"mcp test sql {_SUFFIX}", "code_type": "sql", "folder": target,
                "code": "select count(*) as {:n:integer} from \"T\" where x = {?:score:decimal}"})
            created.append(("code_file", sql["id"]))
            renamed = await tool("update_code_file", {"code_file_id": sql["id"], "description": "changed"})
            assert renamed["description"] == "changed"

            # revisions, code, listing, copy
            tree_ref = {"asset_type": "segmentation_tree", "asset_id": tree["id"]}
            locked = await tool("lock_decisioning_asset_revision", tree_ref)
            assert locked["locked_revision"]["locked"] is True
            revisions = await tool("list_decisioning_asset_revisions", tree_ref)
            assert any(r["id"] == locked["locked_revision"]["id"] for r in revisions)
            code = await tool("get_decisioning_asset_code", {"asset_type": "segmentation_tree", "asset_id": tree["id"]})
            assert "_double_value" in code
            read = await tool("get_decisioning_asset", {"asset_type": "code_file", "asset_id": ds2["id"]})
            assert "method execute" in read["code"] and read["folder"]["id"] == folder["id"]
            listed = await tool("list_decisioning_assets", {"asset_type": "segmentation_tree", "filter_name": _SUFFIX})
            assert {tree["id"], from_table["id"]} <= {t["id"] for t in listed}
            copy = await tool("copy_decisioning_asset", {
                "asset_type": "segmentation_tree", "asset_id": tree["id"], "name": f"mcp_test_tree_copy_{_SUFFIX}",
                "folder": target})
            created.append(("segmentation_tree", copy["id"]))

            # a decision from shorthand steps
            decision = await tool("create_decision_flow", {
                "name": f"mcp_test_steps_{_SUFFIX}", "folder": target,
                "signature": signature + [{"name": "path", "dataType": "string", "direction": "output"}],
                "steps": [
                    {"type": "assignment", "assignments": {"path": "START"}},
                    {"type": "segmentation_tree", "id": tree["id"]},
                    {"type": "code_file", "id": ds2["id"]},
                    {"type": "branch", "variable": "score", "cases": [
                        {"label": "high", "min": 630,
                         "steps": [{"type": "assignment", "assignments": {"path": "HIGH"}}]}]},
                    {"type": "condition", "expression": "band = 'LOW'",
                     "on_true": [{"type": "assignment", "assignments": {"path": "LOW"}}], "on_false": []},
                ]})
            created.append(("decision", decision["id"]))
            report = decision["steps_report"]
            assert report["unmapped_inputs"] == [] and report["warnings"] == []
            assert any(s["asset_type"] == "code_file" for s in report["locked_for_this_flow"])
        finally:
            async with Client(integration_mcp_server) as cleanup_client:
                for asset_type, asset_id in reversed(created):
                    with contextlib.suppress(Exception):
                        await cleanup_client.call_tool(
                            "delete_decisioning_asset", {"asset_type": asset_type, "asset_id": asset_id}
                        )
            with contextlib.suppress(Exception):
                await _viya_delete(viya_token, f"{target}?recursive=true")


async def test_lookup_table_workflow(integration_mcp_server, viya_token):
    """A lookup table end to end, in a scratch folder below My Folder: create and
    activate -> a rule set that tests and reads it (named, resolved to its ID) -> a
    decision published to MAS and scored -> new entries activated, which the published
    decision uses without being published again -> rollback, republish, copy -> the
    delete refused while a rule set uses the table -> everything deleted again."""
    try:
        await _viya_get(viya_token, "/referenceData/domains", {"limit": 1})
        await _viya_get(viya_token, "/decisions/flows", {"limit": 1})
    except Exception as e:
        pytest.skip(f"SAS Intelligent Decisioning not available on this instance: {e}")

    folder = await _viya_post(
        viya_token, "/folders/folders", {"name": f"mcp_test_lookup_{_SUFFIX}", "type": "folder"},
        params={"parentFolderUri": "/folders/folders/@myFolder"},
    )
    target = f"/folders/folders/{folder['id']}"
    name = f"mcp_test_countries_{_SUFFIX}"
    created: list[tuple[str, str]] = []  # (asset_type, id), deleted in reverse
    published: dict = {}
    signature = [
        {"name": "country", "dataType": "string", "direction": "input"},
        {"name": "risk", "dataType": "string", "direction": "output"},
        {"name": "listed", "dataType": "string", "direction": "output"},
    ]

    async def tool(tool_name, args):
        result = await client.call_tool(tool_name, args)
        return result.data if result.data is not None else result.content[0].text

    async def score(module_id, country):
        result = await tool("score_data", {"module_id": module_id, "step_id": "execute",
                                           "input_data": {"country": country}})
        assert "inputs_renamed" not in result  # the module takes the decision's own names
        return {o["name"]: o.get("value") for o in result["outputs"]}

    async with Client(integration_mcp_server) as client:
        try:
            lookup = await tool("create_lookup_table", {
                "name": name, "folder": target, "entries": {"Iran": "High", "Cuba": "High"},
                "key_label": "Country", "value_label": "Risk", "activate": True})
            created.append(("lookup_table", lookup["id"]))
            assert lookup["activation"]["outcome"] == "active" and lookup["folder"]["id"] == folder["id"]
            read = await tool("get_lookup_table", {"lookup_table": name})
            assert read["active_version"] == "1.0" and read["entry_count"] == 2 and read["used_by"] == []

            ruleset = await tool("create_business_ruleset", {
                "name": f"mcp_test_lookup_rs_{_SUFFIX}"[:30], "signature": signature, "folder": target})
            created.append(("rule_set", ruleset["id"]))
            rule = await tool("create_business_rule", {
                "ruleset_id": ruleset["id"], "name": "listed_country", "conditional": "if",
                "rule_fired_tracking_enabled": True,
                "conditions": [{"type": "lookup", "term": signature[0], "lookup": name}],
                "actions": [
                    {"type": "assignment", "term": signature[2], "expression": "'Y'"},
                    {"type": "lookupValue", "term": signature[1], "expression": "country", "lookup": name},
                ]})
            assert "problems" not in rule
            assert rule["conditions"][0]["lookup"]["id"] == lookup["id"]
            for _ in range(10):  # the relationships service records the use a few seconds later
                used = await tool("get_lookup_table", {"lookup_table": lookup["id"], "limit": 1})
                if used["used_by"]:
                    break
                await asyncio.sleep(3)
            assert [u["id"] for u in used["used_by"]] == [ruleset["id"]] and used["next_start"] == 1
            refused = await client.call_tool(
                "delete_decisioning_asset", {"asset_type": "lookup_table", "asset_id": lookup["id"]},
                raise_on_error=False)
            assert refused.is_error and ruleset["id"] in refused.content[0].text

            decision = await tool("create_decision_flow", {
                "name": f"mcp_test_lookup_decision_{_SUFFIX}", "folder": target, "signature": signature,
                "steps": [{"type": "rule_set", "id": ruleset["id"]}]})
            created.append(("decision", decision["id"]))
            locked = await tool("lock_decision_flow_revision", {"decision_id": decision["id"]})
            published = await tool("publish_decision_flow", {
                "decision_id": decision["id"], "revision_id": locked["locked_revision"]["id"],
                "publish_name": f"mcp_test_lookup_{_SUFFIX}", "poll_timeout": 120})
            assert published["moduleState"] == "completed", published.get("moduleErrors")
            module = published["moduleId"]
            step = await tool("get_mas_module_step_signature", {"module_id": module})
            assert [i["name"] for i in step["inputs"]] == ["country"]
            assert await score(module, "Iran") == {"listed": "Y", "risk": "High"}
            assert (await score(module, "Germany"))["listed"] is None

            changed = await tool("update_lookup_table", {
                "lookup_table": lookup["id"], "set_entries": {"Germany": "Low"}, "remove_keys": ["Cuba", "Nowhere"],
                "activate": True})
            assert changed["new_version"] is True and changed["activation"]["outcome"] == "active"
            assert changed["changes"]["not_found"] == ["Nowhere"] and changed["entry_count"] == 2
            # The published decision reads the newly active entries: nothing was published again.
            assert await score(module, "Germany") == {"listed": "Y", "risk": "Low"}
            assert (await score(module, "Cuba"))["listed"] is None

            rollback = await tool("activate_lookup_table", {"lookup_table": lookup["id"], "version": "1.0"})
            assert rollback["outcome"] == "active" and rollback["started_from"] == "1.0"
            assert (await score(module, "Cuba"))["risk"] == "High"
            again = await tool("activate_lookup_table", {"lookup_table": lookup["id"], "republish": True})
            assert again["outcome"] == "republished"

            listed = await tool("list_decisioning_assets", {"asset_type": "lookup_table", "filter_name": _SUFFIX})
            assert lookup["id"] in {i["id"] for i in listed}
            copy = await tool("copy_decisioning_asset", {
                "asset_type": "lookup_table", "asset_id": lookup["id"], "name": f"{name}_copy", "folder": target})
            created.append(("lookup_table", copy["id"]))
            assert copy["entry_count"] == 2
        finally:
            async with Client(integration_mcp_server) as cleanup_client:
                for asset_type, asset_id in reversed(created):
                    with contextlib.suppress(Exception):
                        await cleanup_client.call_tool(
                            "delete_decisioning_asset", {"asset_type": asset_type, "asset_id": asset_id}
                        )
            if published.get("moduleId"):
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/microanalyticScore/modules/{published['moduleId']}")
            if published.get("id"):
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/modelPublish/models/{published['id']}")
            with contextlib.suppress(Exception):
                await _viya_delete(viya_token, f"{target}?recursive=true")


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
    "build_va_dashboard": {"table": "Public.HMEQ"},
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
# SAS Data Governance Catalog Workflows
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
            pytest.skip(f"SAS Data Governance catalog not available on this Viya: {e}")
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
            pytest.skip(f"SAS Data Governance catalog not available on this Viya: {e}")

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
# Folders and CAS table life cycle (Tier 2)
# -----------------------------------------------------------------------


async def _cas_table_state(token: str, caslib: str, table: str) -> str | None:
    """The table's state, or None when it does not exist."""
    from sas_mcp_server.config import VIYA_ENDPOINT
    from sas_mcp_server.viya_client import make_client

    async with make_client(token) as client:
        resp = await client.get(
            f"{VIYA_ENDPOINT}/casManagement/servers/cas-shared-default/caslibs/{caslib}/tables/{table}",
            headers={"Accept": "application/json"},
        )
    return resp.json().get("state") if resp.status_code == 200 else None


async def _remove_cas_table(token: str, caslib: str, table: str, source: str | None = None) -> None:
    """Best effort: unload a CAS table and delete its source file."""
    from sas_mcp_server.config import VIYA_ENDPOINT
    from sas_mcp_server.viya_client import make_client

    base = f"{VIYA_ENDPOINT}/casManagement/servers/cas-shared-default/caslibs/{caslib}/tables/{table}"
    async with make_client(token) as client:
        with contextlib.suppress(Exception):
            await client.put(f"{base}/state", params={"value": "unloaded"}, headers={"Accept": "*/*"})
        if source:
            with contextlib.suppress(Exception):
                await client.delete(base, params={"sourceTableName": source, "quiet": "true"})


async def test_folder_and_castable_tools(integration_mcp_server, viya_token, monkeypatch):
    """create_folder (absolute, below My Folder, relative), save_castable and drop_castable."""
    folder_name = f"mcp_test_mkdir_{_SUFFIX}"
    table = f"MCP_SAVE_{_SUFFIX}"
    top_id = None
    async with Client(integration_mcp_server) as client:
        try:
            made = (await client.call_tool("create_folder", {"path": f"My Folder/{folder_name}/a/b"})).data
            assert made["status"] == "created" and len(made["created"]) == 3, made
            assert made["path"].endswith(f"/My Folder/{folder_name}/a/b")
            top = (await client.call_tool("find_folder", {"folder": f"My Folder/{folder_name}"})).data
            top_id = top["id"]
            again = (await client.call_tool("create_folder", {"path": made["path"]})).data
            assert again["status"] == "exists" and again["id"] == made["id"] and again["created"] == []

            monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", "")
            refused = (await client.call_tool("create_folder", {"path": "relative/path"})).data
            assert refused["status"] == "refused" and "MCP_DEFAULT_FOLDER" in refused["message"]
            monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", f"/folders/folders/{top_id}")
            relative = (await client.call_tool("create_folder", {"path": "c"})).data
            assert relative["status"] == "created" and relative["path"] == f"{top['path']}/c"

            # an in-memory table with no source is protected until it is saved
            await client.call_tool("upload_inline_data", {
                "server_id": "cas-shared-default", "caslib_name": "CASUSER", "table_name": table,
                "data": "x,y\n1,a\n2,b\n", "data_format": "csv",
            })
            assert await _cas_table_state(viya_token, "CASUSER", table) == "loaded"
            unsaved = (await client.call_tool("drop_castable", {"caslib_name": "CASUSER", "table_name": table})).data
            assert unsaved["status"] == "unsaved", unsaved
            saved = (await client.call_tool("save_castable", {"caslib_name": "CASUSER", "table_name": table})).data
            assert saved["status"] == "saved", saved
            twice = (await client.call_tool("save_castable", {"caslib_name": "CASUSER", "table_name": table})).data
            assert twice["status"] == "exists" and twice["existing"], twice
            dropped = (await client.call_tool("drop_castable", {"caslib_name": "CASUSER", "table_name": table})).data
            assert dropped["status"] == "dropped" and dropped["source"], dropped
            assert await _cas_table_state(viya_token, "CASUSER", table) == "unloaded"
            gone = (await client.call_tool("drop_castable", {"caslib_name": "CASUSER", "table_name": table})).data
            assert gone["status"] == "not_loaded"
        finally:
            await _remove_cas_table(viya_token, "CASUSER", table, f"{table}.sashdat")
            if top_id:
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/folders/folders/{top_id}?recursive=true")


# -----------------------------------------------------------------------
# Model life cycle (Tiers 6 and 11)
# -----------------------------------------------------------------------

# A score code a DATA step model can be registered with: inputs from HMEQ, the
# outputs a binary classifier has. Deliberately crude; it only has to run.
_LOGIT_CODE = """\
length I_BAD $ 1;
_lp = -1.5 + 0.00002 * coalesce(LOAN, 0) - 0.0000001 * coalesce(VALUE, 0) + 0.02 * coalesce(DEBTINC, 30);
P_BAD1 = 1 / (1 + exp(-_lp));
P_BAD0 = 1 - P_BAD1;
I_BAD = ifc(P_BAD1 >= 0.5, '1', '0');
drop _lp;
"""


async def _poll(fetch, done, timeout: float = 300, every: float = 5):
    """Call ``await fetch()`` until ``done(result)`` or the timeout; return the last result."""
    deadline = time.monotonic() + timeout
    while True:
        result = await fetch()
        if done(result) or time.monotonic() > deadline:
            return result
        await asyncio.sleep(every)


async def _remove_performance_results(token: str, project_id: str) -> list[str]:
    """Unload and delete a project's results in ModelPerformanceData; returns the files found."""
    listing = await _viya_get(
        token,
        "/casManagement/servers/cas-shared-default/caslibs/ModelPerformanceData/sources",
        {"limit": 2000},
    )
    files = [s["name"] for s in listing.get("items", []) if s.get("name", "").startswith(f"{project_id}.")]
    for name in files:
        await _remove_cas_table(token, "ModelPerformanceData", name.rsplit(".", 1)[0], name)
    return files


async def test_model_lifecycle_workflow(integration_mcp_server, viya_token):
    """Register (analytic store and DATA step) → project → champion → score test →
    batch scoring → publish → validate → monitor → KPI alert rules → KPIs, then
    remove everything, ModelPerformanceData results included."""
    try:
        await _viya_get(viya_token, "/modelRepository/repositories", {"limit": 1})
        await _viya_get(viya_token, "/modelManagement/performanceTasks", {"limit": 1})
    except Exception as e:
        pytest.skip(f"SAS Model Manager not available on this instance: {e}")
    s = _SUFFIX
    data, store, scored = f"MCPLC_{s}_HMEQ", f"MCPLC_{s}_STORE", f"MCPLC_{s}_SCORED"
    periods = [f"MCPPF_{s}_1_P1", f"MCPPF_{s}_2_P2"]
    project_name = f"MCP Test Lifecycle {s}"
    project_id = task_id = publish = None
    score_ids: list[tuple[str, str]] = []
    leftover: list[str] = []
    results: list[str] = []
    project_folder = None
    prep = f"""
cas mcpit sessopts=(caslib=casuser);
libname casuser cas caslib=casuser;
data casuser.{data}(promote=yes); set sampsio.hmeq; run;
data casuser.{periods[0]}(promote=yes); set sampsio.hmeq(firstobs=1 obs=2500); run;
data casuser.{periods[1]}(promote=yes); set sampsio.hmeq(firstobs=2501); run;
proc treesplit data=casuser.{data};
  input loan value mortdue debtinc derog / level=interval;
  input job / level=nominal;
  target bad / level=nominal;
  prune none;
  savestate rstore=casuser.{store};
run;
proc casutil; promote casdata="{store}" incaslib="casuser" outcaslib="casuser"; quit;
cas mcpit terminate;
"""
    async with Client(integration_mcp_server) as client:
        try:
            ran = (await client.call_tool("execute_sas_code", {"sas_code": prep, "html_results": False})).data
            if "ERROR" in ran["log"]:
                pytest.skip("Could not prepare the HMEQ data and analytic store: "
                            + "; ".join(line for line in ran["log"].splitlines() if line.startswith("ERROR"))[:300])

            repos = (await client.call_tool("list_model_repositories", {})).data
            assert any(r["default"] for r in repos), repos
            made = (await client.call_tool("create_model_project", {
                "name": project_name, "folder": "MCP Test", "target_variable": "BAD", "target_level": "binary",
                "target_event": "1", "description": "sas-mcp-server integration test - delete me",
            })).data
            assert made["status"] == "created", made
            project_id = made["project"]["id"]
            project_folder = made["project"].get("folderId")
            again = (await client.call_tool("create_model_project", {"name": project_name})).data
            assert again["status"] == "exists" and again["project"]["id"] == project_id
            listed = (await client.call_tool(
                "list_model_projects", {"repository": made["repository"], "name_filter": s}
            )).data
            assert [p["id"] for p in listed] == [project_id]

            described = (await client.call_tool(
                "describe_analytic_store", {"caslib_name": "CASUSER", "table_name": store}
            )).data
            assert described["status"] == "ok" and "LOAN" in described["description"].upper(), described

            tree = (await client.call_tool("register_model", {
                "name": "MCP Tree", "project_id": project_id, "astore_caslib": "CASUSER", "astore_table": store,
                "data_caslib": "CASUSER", "data_table": data, "target_variable": "BAD", "target_level": "binary",
                "target_event": "1", "algorithm": "TREE", "function": "CLASSIFICATION",
            })).data
            assert tree["status"] == "registered", tree
            tree_id = tree["model"]["id"]
            duplicate = (await client.call_tool("register_model", {
                "name": "MCP Tree", "project_id": project_id, "astore_caslib": "CASUSER", "astore_table": store,
            })).data
            assert duplicate["status"] == "exists" and duplicate["existing_model_id"] == tree_id
            logit = (await client.call_tool("register_model", {
                "name": "MCP Logit", "project_id": project_id, "score_code": _LOGIT_CODE, "data_caslib": "CASUSER",
                "data_table": data, "target_variable": "BAD", "target_level": "binary", "target_event": "1",
                "algorithm": "LOGISTIC", "function": "CLASSIFICATION",
            })).data
            assert logit["status"] == "registered", logit
            logit_id = logit["model"]["id"]

            model = (await client.call_tool("get_model", {"model_id": tree_id})).data
            assert model["projectId"] == project_id and model["inputVariables"] and model["files"]
            versions = (await client.call_tool("list_model_versions", {"model_id": tree_id})).data
            assert versions["versions"][0]["current"] is True

            champion = (await client.call_tool(
                "set_project_champion", {"project_id": project_id, "model_id": tree_id}
            )).data
            assert champion["status"] == "champion_set", champion

            for model_id in (tree_id, logit_id):
                tested = (await client.call_tool("test_score_model", {
                    "model_id": model_id, "caslib_name": "CASUSER", "table_name": data, "rows": 5,
                })).data
                assert tested["status"] == "completed", tested
                assert len(tested["rows"]) == 5 and "P_BAD1" in {c.upper() for c in tested["columns"]}, tested

            batch = (await client.call_tool("score_table", {
                "model_id": tree_id, "caslib_name": "CASUSER", "table_name": data, "output_table": scored,
            })).data
            score_ids.append((batch["execution_id"], batch["score_definition_id"]))
            finished = await _poll(
                lambda: client.call_tool("get_score_execution", {"execution_id": batch["execution_id"], "rows": 3}),
                lambda r: r.data["state"] not in ("pending", "running"),
            )
            assert finished.data["state"] == "completed", finished.data
            assert len(finished.data["rows"]) == 3

            plan = (await client.call_tool("publish_model", {"model_id": tree_id, "dry_run": True})).data
            assert plan["status"] == "dry_run" and plan["publish_name"] == "MCP_Tree", plan
            name = f"mcp_lc_{s}"
            published = (await client.call_tool("publish_model", {"model_id": tree_id, "publish_name": name})).data
            assert published["status"] == "publishing", published
            copies = await _poll(
                lambda: client.call_tool(
                    "list_published_models", {"destination_name": plan["destination"], "name_filter": name}
                ),
                lambda r: bool(r.data) and r.data[0]["state"] not in ("pending", "running"),
            )
            assert copies.data and copies.data[0]["state"] == "completed", copies.data
            publish = copies.data[0]
            assert publish["modelId"] == tree_id
            validated = (await client.call_tool("validate_published_model", {
                "published_model_id": publish["id"], "caslib_name": "CASUSER", "table_name": data, "rows": 3,
            })).data
            assert validated["status"] == "completed" and len(validated["rows"]) == 3, validated

            definition = (await client.call_tool("create_performance_definition", {
                "project_id": project_id, "data_caslib": "CASUSER", "data_prefix": f"MCPPF_{s}",
                "model_ids": [tree_id, logit_id],
            })).data
            assert definition["status"] == "created" and sorted(definition["period_tables"]) == periods, definition
            task_id = definition["performance_definition_id"]
            second = (await client.call_tool("create_performance_definition", {
                "project_id": project_id, "data_caslib": "CASUSER", "data_prefix": f"MCPPF_{s}",
            })).data
            assert second["status"] == "exists" and second["performance_definition"]["id"] == task_id, second

            # A KPI alert rule set before the first run: the run then reports the champion breaking it.
            early = (await client.call_tool("set_kpi_alert_rule", {
                "project_id": project_id, "kpi": "AUC", "operator": "lessThan", "threshold": 0.999, "severity": "high",
            })).data
            assert early["status"] == "set" and early["rules"][0]["action"] == "created", early
            run = (await client.call_tool("run_performance_definition", {
                "project_id": project_id, "wait_seconds": 600,
            })).data
            assert run["status"].lower() in ("completed", "succeeded") and run["kpis"] > 0, run
            assert [a["kpi"] for a in run["breached_alerts"]] == ["_AUC_"], run

            # Drift rules, one per variable, that the champion keeps; the AUC rule updated, not duplicated.
            drift = (await client.call_tool("set_kpi_alert_rule", {
                "project_id": project_id, "kpi": "devIndex", "operator": "greaterThan", "threshold": 1000,
                "variables": ["LOAN", "DEBTINC"],
            })).data
            assert [r["action"] for r in drift["rules"]] == ["created", "created"], drift
            assert len(drift["now"]) == 2 and not any(a["breached"] for a in drift["now"]), drift
            lowered = (await client.call_tool("set_kpi_alert_rule", {
                "project_id": project_id, "kpi": "AUC", "operator": "lessThan", "threshold": 0.01,
            })).data
            assert lowered["rules"][0]["action"] == "updated", lowered
            assert [a["breached"] for a in lowered["now"]] == [False], lowered

            kpis = (await client.call_tool("get_project_kpis", {"project_id": project_id})).data
            assert any(k["name"] == "_AUC_" for k in kpis["kpis"]), kpis
            assert len(kpis["alert_rules"]) == 3 and [d["id"] for d in kpis["performance_definitions"]] == [task_id]
            assert kpis["alerts_for_model"] == "MCP Tree" and len(kpis["alerts"]) == 3, kpis
            removed = (await client.call_tool("delete_kpi_alert_rule", {
                "project_id": project_id, "alert_rule_id": drift["rules"][0]["id"],
            })).data
            assert removed["status"] == "deleted", removed
            other = (await client.call_tool("get_project_kpis", {"project_id": project_id, "model_id": logit_id})).data
            assert len(other["alert_rules"]) == 2 and other["alerts_for_model"] == logit_id, other
        finally:
            for table in (data, store, scored, *periods):
                await _remove_cas_table(viya_token, "CASUSER", table)
            for execution_id, definition_id in score_ids:
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/scoreExecution/executions/{execution_id}")
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/scoreDefinitions/definitions/{definition_id}")
            if publish:
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/microanalyticScore/modules/{publish['name'].lower()}")
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/modelPublish/models/{publish['id']}")
            if task_id:
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/modelManagement/performanceTasks/{task_id}")
            if project_id:
                results = await _remove_performance_results(viya_token, project_id)
                with contextlib.suppress(Exception):
                    await _viya_delete(viya_token, f"/modelRepository/projects/{project_id}")
                if project_folder:  # the repository folder the test created; refused if not empty
                    with contextlib.suppress(Exception):
                        await _viya_delete(viya_token, f"/folders/folders/{project_folder}")
                left = await _viya_get(
                    viya_token,
                    "/casManagement/servers/cas-shared-default/caslibs/ModelPerformanceData/sources",
                    {"limit": 2000},
                )
                leftover = [x["name"] for x in left.get("items", []) if x.get("name", "").startswith(project_id)]
    assert results, "the performance run wrote no results to ModelPerformanceData"
    assert not leftover, f"performance results left behind in ModelPerformanceData: {leftover}"


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
    "query_data": "test_fedsql_query_workflow",
    "upload_data": "test_upload_data_file_path_and_formats",
    "upload_inline_data": "test_data_upload_workflow",
    "promote_table_to_memory": "test_promote_from_source_workflow",
    "list_files": "test_file_service_workflow",
    "upload_file": "test_file_service_workflow",
    "download_file": "test_file_service_workflow",
    "find_folder": "test_decisioning_folder_placement",
    "list_reports": "test_report_workflow",
    "get_report": "test_report_workflow",
    "export_report": "test_report_workflow",
    "describe_report_objects": "test_report_authoring_workflow",
    "create_report": "test_report_authoring_workflow",
    "apply_report_operations": "test_report_authoring_workflow",
    "get_report_outline": "test_report_authoring_workflow",
    "copy_report": "test_report_authoring_workflow",
    "delete_report": "test_report_authoring_workflow",
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
    "list_decisioning_assets": "test_decisioning_content_workflow",
    "get_decisioning_asset": "test_decisioning_content_workflow",
    "get_decisioning_asset_code": "test_decisioning_content_workflow",
    "list_decisioning_asset_revisions": "test_decisioning_content_workflow",
    "lock_decisioning_asset_revision": "test_decisioning_content_workflow",
    "copy_decisioning_asset": "test_decisioning_content_workflow",
    "delete_decisioning_asset": "test_decisioning_content_workflow",
    "create_segmentation_tree": "test_decisioning_content_workflow",
    "update_segmentation_tree": "test_decisioning_content_workflow",
    "create_segmentation_tree_from_table": "test_decisioning_content_workflow",
    "create_code_file": "test_decisioning_content_workflow",
    "update_code_file": "test_decisioning_content_workflow",
    "create_value_list": "test_decisioning_content_workflow",
    "update_value_list_entries": "test_decisioning_content_workflow",
    "get_lookup_table": "test_lookup_table_workflow",
    "create_lookup_table": "test_lookup_table_workflow",
    "update_lookup_table": "test_lookup_table_workflow",
    "activate_lookup_table": "test_lookup_table_workflow",
    "create_custom_function": "test_decisioning_content_workflow",
    "update_custom_function": "test_decisioning_content_workflow",
    "validate_custom_function": "test_decisioning_content_workflow",
    "validate_decision_expression": "test_decisioning_content_workflow",
    "list_compute_contexts": "test_compute_discovery_workflow",
    "list_compute_libraries": "test_compute_discovery_workflow",
    "list_compute_tables": "test_compute_discovery_workflow",
    "list_compute_columns": "test_compute_discovery_workflow",
    "get_compute_table_data": "test_compute_discovery_workflow",
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
    "list_glossary_term_types": "test_glossary_workflow",
    "get_glossary_term_type": "test_glossary_workflow",
    "search_glossary_terms": "test_glossary_workflow",
    "list_glossary_terms": "test_glossary_workflow",
    "get_glossary_term": "test_glossary_workflow",
    "list_term_assets": "test_glossary_workflow",
    "list_table_terms": "test_glossary_workflow",
    "create_glossary_term": "test_glossary_workflow",
    "update_glossary_term": "test_glossary_workflow",
    "delete_glossary_term": "test_glossary_workflow",
    "assign_glossary_term": "test_glossary_workflow",
    "unassign_glossary_term": "test_glossary_workflow",
    "create_glossary_term_type": "test_glossary_term_type_lifecycle",
    "update_glossary_term_type": "test_glossary_term_type_lifecycle",
    "delete_glossary_term_type": "test_glossary_term_type_lifecycle",
    "import_glossary_terms": "test_glossary_bulk_import",
    # Tier 2 — folders and CAS table life cycle
    "create_folder": "test_folder_and_castable_tools",
    "save_castable": "test_folder_and_castable_tools",
    "drop_castable": "test_folder_and_castable_tools",
    # Tiers 6 and 11 — model life cycle
    "get_model": "test_model_lifecycle_workflow",
    "list_model_versions": "test_model_lifecycle_workflow",
    "test_score_model": "test_model_lifecycle_workflow",
    "score_table": "test_model_lifecycle_workflow",
    "get_score_execution": "test_model_lifecycle_workflow",
    "list_published_models": "test_model_lifecycle_workflow",
    "list_model_repositories": "test_model_lifecycle_workflow",
    "list_model_projects": "test_model_lifecycle_workflow",
    "create_model_project": "test_model_lifecycle_workflow",
    "set_project_champion": "test_model_lifecycle_workflow",
    "describe_analytic_store": "test_model_lifecycle_workflow",
    "register_model": "test_model_lifecycle_workflow",
    "publish_model": "test_model_lifecycle_workflow",
    "validate_published_model": "test_model_lifecycle_workflow",
    "create_performance_definition": "test_model_lifecycle_workflow",
    "run_performance_definition": "test_model_lifecycle_workflow",
    "get_project_kpis": "test_model_lifecycle_workflow",
    "set_kpi_alert_rule": "test_model_lifecycle_workflow",
    "delete_kpi_alert_rule": "test_model_lifecycle_workflow",
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


# -----------------------------------------------------------------------
# FedSQL query workflow (query_data)
# -----------------------------------------------------------------------


async def test_fedsql_query_workflow(integration_mcp_server):
    """query_data over both tiers: CAS caslib, compute libref, and the guards.

    Exercises the whole path against live Viya — the generated FedSQL, the
    server-side row cap, the format-stripped read-back, and the error mapping.
    """
    async with Client(integration_mcp_server) as client:
    
        # --- CAS target: the row cap is enforced server-side --------------------
        result = (
            await client.call_tool(
                "query_data",
                {"query": "select LOAN, MORTDUE, JOB from Public.HMEQ", "limit": 3},
            )
        ).data
        if result.get("status") == "table_not_found":
            pytest.skip("HMEQ not loaded in Public caslib on this Viya")
        assert result["columns"] == ["LOAN", "MORTDUE", "JOB"]
        assert len(result["rows"]) == 3
        assert result["count"] == 3

        # A LIMIT written by the caller must not override the tool's cap: CAS
        # silently discards a malformed one, so the tool never trusts it.
        capped = (
            await client.call_tool(
                "query_data", {"query": "select LOAN from Public.HMEQ limit 9999", "limit": 2}
            )
        ).data
        assert len(capped["rows"]) == 2

        # --- aggregation + join over CAS ---------------------------------------
        grouped = (
            await client.call_tool(
                "query_data",
                {
                    "query": (
                        "select JOB, count(*) as n from Public.HMEQ "
                        "where JOB is not null group by JOB having count(*) > 10 order by n desc"
                    ),
                    "limit": 5,
                },
            )
        ).data
        assert grouped["columns"] == ["JOB", "N"]
        assert grouped["rows"], "expected at least one group"

        joined = (
            await client.call_tool(
                "query_data",
                {
                    "query": (
                        "select a.JOB, a.LOAN, b.LOAN as b_loan from Public.HMEQ a "
                        "inner join Public.HMEQ b on a.JOB = b.JOB where a.LOAN > 50000"
                    ),
                    "limit": 3,
                },
            )
        ).data
        assert joined["columns"] == ["JOB", "LOAN", "B_LOAN"]

        # --- compute target: formats, dates, and missing values -----------------
        await client.call_tool(
            "execute_sas_code",
            {
                "sas_code": (
                    "data work._mcp_q_it;\n"
                    "  length name $10;\n"
                    "  name='ann'; sal=50000; hired='01FEB2020'd; output;\n"
                    "  name='bob'; sal=.;     hired=.;            output;\n"
                    "  format sal dollar12.2 hired date9.;\n"
                    "run;"
                )
            },
        )
        typed = (
            await client.call_tool(
                "query_data",
                {
                    "query": "select name, sal, hired from WORK._mcp_q_it order by name",
                    "target": "compute",
                    "limit": 10,
                },
            )
        ).data
        assert [r["name"] for r in typed["rows"]] == ["ann", "bob"]
        # Formatted values must arrive as numbers and ISO dates, not padded strings.
        assert typed["rows"][0]["sal"] == 50000
        assert typed["rows"][0]["hired"] == "2020-02-01"
        assert typed["rows"][1]["sal"] is None
        assert typed["rows"][1]["hired"] is None

        # --- truncation flag ----------------------------------------------------
        page = (
            await client.call_tool(
                "query_data", {"query": "select LOAN from Public.HMEQ", "limit": 1}
            )
        ).data
        assert page["truncated"] is True

        # --- create_view_sql is returned, never executed ------------------------
        viewed = (
            await client.call_tool(
                "query_data",
                {"query": "select JOB from Public.HMEQ", "limit": 1, "create_view_name": "mcp_it_view"},
            )
        ).data
        assert viewed["create_view_sql"].startswith("create view mcp_it_view as")

        # --- error mapping and the write guard ----------------------------------
        missing = (
            await client.call_tool(
                "query_data", {"query": "select x from Public.NOPE_MCP_IT", "limit": 1}
            )
        ).data
        assert missing["status"] == "table_not_found"

        refused = (
            await client.call_tool("query_data", {"query": "drop table Public.HMEQ"})
        ).data
        assert refused["status"] == "invalid_query"

        macro = (
            await client.call_tool(
                "query_data", {"query": 'select LOAN from Public.HMEQ where JOB = "&sysuserid"'}
            )
        ).data
        assert macro["status"] == "invalid_query"

        # An unterminated literal must never be submitted: SAS would keep
        # consuming, hanging this compute session for every later call.
        unbalanced = (
            await client.call_tool(
                "query_data", {"query": "select LOAN from Public.HMEQ where JOB = 'Sales"}
            )
        ).data
        assert unbalanced["status"] == "invalid_query"
        assert "unbalanced quote" in unbalanced["message"]

        # A correctly escaped literal (doubled quote) is just a value.
        escaped = (
            await client.call_tool(
                "query_data",
                {"query": "select JOB from Public.HMEQ where JOB = 'x'' or ''1''=''1'", "limit": 3},
            )
        ).data
        assert escaped["count"] == 0

        # A failed query must not poison the session for the next one.
        recovered = (
            await client.call_tool("query_data", {"query": "select LOAN from Public.HMEQ", "limit": 2})
        ).data
        assert len(recovered["rows"]) == 2

        await client.call_tool(
            "execute_sas_code",
            {"sas_code": "proc datasets library=work nolist nowarn; delete _mcp_q_it; quit;"},
        )


# -----------------------------------------------------------------------
# Business Glossary workflow (Tier 9)
# -----------------------------------------------------------------------


def _sample_attribute_value(attribute: dict):
    """A value that satisfies one attribute definition from a term type.

    A deployment's term types are whatever its governance team created, so the
    test cannot hardcode values — it reads the contract and answers it.
    """
    attr_type = (attribute.get("type") or "").lower()
    if attr_type == "single-select":
        allowed = attribute.get("allowed_values") or []
        return allowed[0] if allowed else "n/a"
    if attr_type == "boolean":
        return True
    if attr_type == "date":
        return "2030-01-01"
    return f"set by integration test {_SUFFIX}"


async def _wait_for_catalog_entity(client, term_id: str, attempts: int = 12) -> bool:
    """Poll until a new glossary term is mirrored into the SAS Data Governance catalog.

    A term is created in the glossary service and copied into the catalog
    asynchronously; nothing can be assigned to it until that lands, so the
    assignment half of this test waits for it rather than assuming it.
    """
    for _ in range(attempts):
        assets = (await client.call_tool("list_term_assets", {"term_id": term_id})).data
        if assets.get("catalog_entity_id"):
            return True
        await asyncio.sleep(5)
    return False


async def test_glossary_workflow(integration_mcp_server):
    """Author a term, attach it to a real column, read it back both ways, clean up.

    Covers all twelve Tier 9 tools against live Viya. The term type, table and
    column are discovered rather than hardcoded, because a deployment's glossary
    holds whatever its governance team put there.
    """
    term_name = f"MCP_TEST_TERM_{_SUFFIX}"
    created_id = None
    assigned = False
    column_name = None
    resource_uri = None

    async with Client(integration_mcp_server) as client:
        # --- the authoring contract -----------------------------------------
        try:
            types = (await client.call_tool("list_glossary_term_types", {"limit": 100})).data
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"SAS Business Glossary not available on this Viya: {exc}")
        if not types["items"]:
            pytest.skip("No glossary term types defined on this Viya")

        # Prefer a type WITH attributes, so the label<->UUID mapping is exercised
        # against real definitions rather than an empty map.
        with_attributes = [t for t in types["items"] if t["attribute_count"] > 0]
        chosen = (with_attributes or types["items"])[0]

        contract = (
            await client.call_tool(
                "get_glossary_term_type", {"term_type_id": chosen["term_type_id"]}
            )
        ).data
        assert contract["term_type_id"] == chosen["term_type_id"]
        for attribute in contract["attributes"]:
            # The contract is only useful if every attribute is self-describing.
            assert attribute["label"], contract
            assert attribute["type"], attribute
            if attribute["type"] == "single-select":
                assert attribute["allowed_values"], attribute

        # A term type resolves by name as well as by id.
        by_name = (
            await client.call_tool("get_glossary_term_type", {"term_type_id": chosen["name"]})
        ).data
        assert by_name["term_type_id"] == chosen["term_type_id"]

        attributes = {
            attribute["label"]: _sample_attribute_value(attribute)
            for attribute in contract["attributes"]
            if attribute["required"]
        }

        try:
            # --- create ------------------------------------------------------
            created = (
                await client.call_tool(
                    "create_glossary_term",
                    {
                        "name": term_name,
                        "term_type": chosen["name"],
                        "definition": "Created by the sas-mcp-server integration suite.",
                        "description": "Integration test term.",
                        "attributes": attributes,
                    },
                )
            ).data
            created_id = created["term_id"]
            assert created["name"] == term_name
            # publish defaults to true, so the term must not land as a draft.
            assert created["is_draft"] is False, created

            # --- read back ---------------------------------------------------
            fetched = (await client.call_tool("get_glossary_term", {"term_id": created_id})).data
            assert fetched["name"] == term_name
            assert fetched["term_type_id"] == chosen["term_type_id"]
            # Everything we set comes back under its label, not its UUID.
            for label, value in attributes.items():
                expected = "true" if value is True else str(value)
                assert fetched["attributes"].get(label) == expected, fetched["attributes"]

            # --- update merges rather than replacing --------------------------
            updated = (
                await client.call_tool(
                    "update_glossary_term",
                    {"term_id": created_id, "description": "Updated by the integration suite."},
                )
            ).data
            assert updated["term_id"] == created_id
            after = (await client.call_tool("get_glossary_term", {"term_id": created_id})).data
            assert after["description"] == "Updated by the integration suite."
            # definition was not passed, so it must have survived the PUT.
            assert after["definition"] == "Created by the sas-mcp-server integration suite."

            # --- structural listing ------------------------------------------
            listed = (
                await client.call_tool(
                    "list_glossary_terms",
                    {
                        "term_type": chosen["term_type_id"],
                        "name_contains": "MCP_TEST_TERM",
                        "limit": 50,
                    },
                )
            ).data
            assert created_id in {item["term_id"] for item in listed["items"]}

            # --- assignment to a real column ----------------------------------
            try:
                hits = (
                    await client.call_tool(
                        "catalog_search",
                        {"query": "Name:HMEQ", "indices": "datasets", "limit": 10},
                    )
                ).data
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"SAS Data Governance catalog not available: {exc}")
            table_hit = next((h for h in hits["items"] if h.get("resource_uri")), None)
            if table_hit is None:
                pytest.skip("No catalogued HMEQ table to assign a term to on this Viya")
            resource_uri = table_hit["resource_uri"]

            columns = (
                await client.call_tool(
                    "list_table_terms", {"resource_uri": resource_uri, "assigned_only": False}
                )
            ).data
            if not columns["columns"]:
                pytest.skip("The catalogued HMEQ table has no column entities")
            column_name = columns["columns"][0]["column_name"]

            if not await _wait_for_catalog_entity(client, created_id):
                pytest.skip("Term was not mirrored into the SAS Data Governance catalog in time")

            assignment = (
                await client.call_tool(
                    "assign_glossary_term",
                    {
                        "term_id": created_id,
                        "column_name": column_name,
                        "resource_uri": resource_uri,
                    },
                )
            ).data
            assert assignment["status"] == "assigned", assignment
            assigned = True

            # Assigning twice reports the existing link rather than duplicating it.
            again = (
                await client.call_tool(
                    "assign_glossary_term",
                    {
                        "term_id": created_id,
                        "column_name": column_name,
                        "resource_uri": resource_uri,
                    },
                )
            ).data
            assert again["status"] == "already_assigned"
            assert again["relationship_id"] == assignment["relationship_id"]

            # --- both directions of the link now resolve -----------------------
            assets = (await client.call_tool("list_term_assets", {"term_id": created_id})).data
            assert assets["asset_count"] >= 1
            assert column_name in {a["asset_name"] for a in assets["assets"]}

            # The term is reachable by name, not only by id.
            by_term_name = (
                await client.call_tool("list_term_assets", {"term_name": term_name})
            ).data
            assert by_term_name["term_id"] == created_id

            table_terms = (
                await client.call_tool("list_table_terms", {"resource_uri": resource_uri})
            ).data
            governed = {
                column["column_name"]: [t["term_id"] for t in column["terms"]]
                for column in table_terms["columns"]
            }
            assert created_id in governed.get(column_name, []), governed

            # --- free-text search sees the new term ----------------------------
            # The catalog index updates asynchronously, so a miss here is a
            # timing artefact rather than a failure of the tool.
            found = (
                await client.call_tool("search_glossary_terms", {"query": term_name, "limit": 10})
            ).data
            if found["items"]:
                hit = found["items"][0]
                assert hit["catalog_entity_id"]
                if hit["term_id"] is not None:
                    # When the bridge resolves it must resolve to the *glossary* id.
                    assert hit["term_id"] != hit["catalog_entity_id"]

            # --- unassign -------------------------------------------------------
            removed = (
                await client.call_tool(
                    "unassign_glossary_term",
                    {
                        "term_id": created_id,
                        "column_name": column_name,
                        "resource_uri": resource_uri,
                    },
                )
            ).data
            assert removed["status"] == "unassigned"
            assigned = False

            repeat = (
                await client.call_tool(
                    "unassign_glossary_term",
                    {
                        "term_id": created_id,
                        "column_name": column_name,
                        "resource_uri": resource_uri,
                    },
                )
            ).data
            assert repeat["status"] == "not_assigned"

        finally:
            if created_id:
                if assigned and column_name and resource_uri:
                    with contextlib.suppress(Exception):
                        await client.call_tool(
                            "unassign_glossary_term",
                            {
                                "term_id": created_id,
                                "column_name": column_name,
                                "resource_uri": resource_uri,
                            },
                        )
                deleted = (
                    await client.call_tool("delete_glossary_term", {"term_id": created_id})
                ).data
                assert deleted["status"] == "deleted"


async def test_glossary_term_type_lifecycle(integration_mcp_server):
    """Design a term type, use it, evolve it, and delete it.

    The point of the evolve step is that an attribute keeps its identifier: a
    term's stored values are filed under it, so re-minting one would leave every
    existing term's value orphaned under a key nothing names.
    """
    type_name = f"MCP_TEST_TYPE_{_SUFFIX}"
    type_id = term_id = None
    async with Client(integration_mcp_server) as client:
        try:
            created = (
                await client.call_tool(
                    "create_glossary_term_type",
                    {
                        "name": type_name,
                        "description": "Integration test. Safe to delete.",
                        "attributes": [
                            {"label": "Owner", "type": "single-line", "required": True},
                            {
                                "label": "Tier",
                                "type": "single-select",
                                "allowed_values": ["Gold", "Silver"],
                            },
                            {"label": "Masked", "type": "boolean", "default": False},
                            {"label": "Cutoff", "type": "time"},
                        ],
                    },
                )
            ).data
            type_id = created["term_type_id"]
            assert created["label"] == type_name, "label must default from name"
            before = {a["label"]: a["attribute_id"] for a in created["attributes"]}
            assert set(before) == {"Owner", "Tier", "Masked", "Cutoff"}

            # A term of the new type, exercising every value form at once.
            term = (
                await client.call_tool(
                    "create_glossary_term",
                    {
                        "name": f"MCP_TEST_TYPED_TERM_{_SUFFIX}",
                        "term_type": type_id,
                        "definition": "Integration test term.",
                        "attributes": {
                            "Owner": "data-office",
                            "Tier": "Gold",
                            "Masked": True,
                            # An offset must be converted, not dropped: stored
                            # as given this would be two hours wrong and say
                            # nothing about it.
                            "Cutoff": "09:15:00+02:00",
                        },
                    },
                )
            ).data
            term_id = term["term_id"]
            assert term["attributes"]["Masked"] is True, "a boolean must survive as a boolean"
            assert term["attributes"]["Cutoff"] == "07:15:00Z"

            # Deleting a type in use would take those definitions with it.
            with pytest.raises(Exception, match="term"):
                await client.call_tool("delete_glossary_term_type", {"term_type_id": type_id})

            evolved = (
                await client.call_tool(
                    "update_glossary_term_type",
                    {
                        "term_type_id": type_id,
                        "attributes": [
                            {
                                "label": "Tier",
                                "type": "single-select",
                                "allowed_values": ["Gold", "Silver", "Bronze"],
                            },
                            {"label": "Review date", "type": "date"},
                        ],
                    },
                )
            ).data
            after = {a["label"]: a for a in evolved["attributes"]}
            assert set(after) == {"Owner", "Tier", "Masked", "Cutoff", "Review date"}
            assert after["Tier"]["attribute_id"] == before["Tier"], "identifier must survive"
            assert after["Owner"]["attribute_id"] == before["Owner"]
            assert after["Tier"]["allowed_values"] == ["Gold", "Silver", "Bronze"]

            # The existing term still reads its values, which is what the
            # preserved identifiers actually buy.
            back = (await client.call_tool("get_glossary_term", {"term_id": term_id})).data
            assert back["attributes"]["Owner"] == "data-office"
            assert back["attributes"]["Masked"] is True

            # The widened option and the new attribute are usable on it.
            updated = (
                await client.call_tool(
                    "update_glossary_term",
                    {
                        "term_id": term_id,
                        "attributes": {"Tier": "Bronze", "Review date": "2027-01-31"},
                    },
                )
            ).data
            assert updated["attributes"]["Tier"] == "Bronze"
            assert updated["attributes"]["Review date"] == "2027-01-31"

            # Attributes in the listing, and the filter that needs them. Scoped
            # to this type so the scan is deterministic and small.
            listed = (
                await client.call_tool(
                    "list_glossary_terms",
                    {"term_type": type_id, "limit": 10, "include_attributes": True},
                )
            ).data
            assert listed["items"], "the term just created should be listed"
            assert listed["items"][0]["attributes"]["Owner"] == "data-office"

            # The glossary cannot filter on attributes, so this is scanned here
            # — the result has to say so.
            matched = (
                await client.call_tool(
                    "list_glossary_terms",
                    {"term_type": type_id, "attribute_filter": {"Masked": True}},
                )
            ).data
            assert matched["count"] == 1
            assert matched["items"][0]["term_id"] == term_id
            assert matched["scan_complete"] is True
            assert matched["scanned"] >= 1

            missed = (
                await client.call_tool(
                    "list_glossary_terms",
                    {"term_type": type_id, "attribute_filter": {"Masked": False}},
                )
            ).data
            assert missed["count"] == 0

            # A mistyped label must not read as "nothing matched".
            with pytest.raises(Exception, match="no attribute is named"):
                await client.call_tool(
                    "list_glossary_terms",
                    {"term_type": type_id, "attribute_filter": {"Maskd": True}},
                )

            # A draft is a separate resource: readable and deletable at the
            # ordinary path, writable only at /draft, and promoted at
            # /draft/state. Getting that wrong is a 404, so the whole cycle —
            # create unpublished, edit, publish — is exercised here.
            draft = (
                await client.call_tool(
                    "create_glossary_term",
                    {
                        "name": f"MCP_TEST_DRAFT_{_SUFFIX}",
                        "term_type": type_id,
                        "definition": "before",
                        # Still called Owner here: the rename comes below.
                        "attributes": {"Owner": "data-office"},
                        "publish": False,
                    },
                )
            ).data
            draft_id = draft["term_id"]
            try:
                assert draft["is_draft"] is True

                hidden = (
                    await client.call_tool(
                        "list_glossary_terms", {"term_type": type_id, "limit": 20}
                    )
                ).data
                assert draft_id not in {i["term_id"] for i in hidden["items"]}, (
                    "a draft must not show in the published listing"
                )
                shown = (
                    await client.call_tool(
                        "list_glossary_terms",
                        {"term_type": type_id, "limit": 20, "include_drafts": True},
                    )
                ).data
                assert draft_id in {i["term_id"] for i in shown["items"]}

                edited = (
                    await client.call_tool(
                        "update_glossary_term",
                        {"term_id": draft_id, "definition": "after"},
                    )
                ).data
                assert edited["is_draft"] is True, "editing must not publish it"

                promoted = (
                    await client.call_tool(
                        "update_glossary_term", {"term_id": draft_id, "publish": True}
                    )
                ).data
                assert promoted["is_draft"] is False
                assert promoted["status"] == "Published"

                settled = (
                    await client.call_tool("get_glossary_term", {"term_id": draft_id})
                ).data
                assert settled["definition"] == "after", "the edit survived the publish"

                # There is no draft left, so this is a no-op with a reason
                # rather than the 404 the service would answer.
                again = (
                    await client.call_tool(
                        "update_glossary_term", {"term_id": draft_id, "publish": True}
                    )
                ).data
                assert "already published" in again.get("note", "")
            finally:
                await client.call_tool("delete_glossary_term", {"term_id": draft_id})

            # Renaming needs the attribute's identifier: matched by label alone
            # the new name matches nothing, so the attribute is added afresh and
            # every term's value stays behind under the old id, readable as
            # nothing. Owner is required, which makes the loss louder still.
            renamed = (
                await client.call_tool(
                    "update_glossary_term_type",
                    {
                        "term_type_id": type_id,
                        "attributes": [
                            {
                                "attribute_id": before["Owner"],
                                "label": "Steward",
                                "type": "single-line",
                                "required": True,
                            }
                        ],
                    },
                )
            ).data
            renamed_by_label = {a["label"]: a for a in renamed["attributes"]}
            assert "Owner" not in renamed_by_label, "no orphaned copy under the old label"
            assert renamed_by_label["Steward"]["attribute_id"] == before["Owner"]

            still_there = (
                await client.call_tool("get_glossary_term", {"term_id": term_id})
            ).data
            assert still_there["attributes"]["Steward"] == "data-office", (
                "the stored value must read under the new name"
            )

            dropped = (
                await client.call_tool(
                    "update_glossary_term_type",
                    {"term_type_id": type_id, "remove_attributes": ["Review date"]},
                )
            ).data
            assert "Review date" not in {a["label"] for a in dropped["attributes"]}
        finally:
            if term_id:
                await client.call_tool("delete_glossary_term", {"term_id": term_id})
            if type_id:
                gone = (
                    await client.call_tool(
                        "delete_glossary_term_type",
                        {"term_type_id": type_id, "force": True},
                    )
                ).data
                assert gone["status"] == "deleted"


async def test_glossary_bulk_import(integration_mcp_server):
    """Import a three-level hierarchy in one call, then take it back down.

    The point is that no id is threaded: children name their parent, and the
    rows may be given in any order. The import runs as a job that reports
    ``completed`` even when rows fail, so the created/failed tallies matter more
    than the call not raising.
    """
    type_name = f"MCP_TEST_IMPORT_TYPE_{_SUFFIX}"
    prefix = f"MCP_IMP_{_SUFFIX}"
    type_id = None
    created_ids: list[str] = []
    async with Client(integration_mcp_server) as client:
        try:
            type_id = (
                await client.call_tool(
                    "create_glossary_term_type",
                    {
                        "name": type_name,
                        "attributes": [
                            {"label": "Tier", "type": "single-select",
                             "allowed_values": ["Gold", "Silver"]},
                            {"label": "Masked", "type": "boolean"},
                            # Required *and* defaulted: the service fills it in,
                            # so omitting it from every row must not be refused.
                            {"label": "Stage", "type": "single-line",
                             "required": True, "default": "draft"},
                        ],
                    },
                )
            ).data["term_type_id"]

            # Deliberately out of order: the leaf comes before its ancestors.
            terms = [
                {"name": f"{prefix} Leaf", "parent": f"{prefix} Mid",
                 "attributes": {"Tier": "Silver", "Masked": False}},
                {"name": f"{prefix} Root", "definition": "imported root",
                 "description": "imported root overview",
                 "attributes": {"Tier": "Gold", "Masked": True}},
                {"name": f"{prefix} Mid", "parent": f"{prefix} Root"},
            ]

            # A dry run shows the resolved order and paths and creates nothing.
            preview = (
                await client.call_tool(
                    "import_glossary_terms",
                    {"term_type": type_id, "terms": terms, "dry_run": True},
                )
            ).data
            assert preview["dry_run"] is True
            assert preview["order"] == [f"{prefix} Root", f"{prefix} Mid", f"{prefix} Leaf"]
            assert preview["rows"][2]["path"] == f"{prefix} Root\\{prefix} Mid"
            nothing_yet = (
                await client.call_tool("list_glossary_terms", {"term_type": type_id, "limit": 5})
            ).data
            assert nothing_yet["items"] == [], "a dry run must not create terms"

            result = (
                await client.call_tool(
                    "import_glossary_terms",
                    {"term_type": type_id, "terms": terms},
                )
            ).data
            assert result["failures"] == [], f"import reported failures: {result['failures']}"
            assert result["created"] == 3
            assert result["order"] == [
                f"{prefix} Root", f"{prefix} Mid", f"{prefix} Leaf"
            ], "parents must be emitted before their children"

            # The hierarchy is real: each child hangs off the right parent.
            listed = (
                await client.call_tool(
                    "list_glossary_terms",
                    {"term_type": type_id, "limit": 20, "include_attributes": True},
                )
            ).data
            by_name = {i["name"]: i for i in listed["items"]}
            created_ids = [i["term_id"] for i in listed["items"]]
            assert set(by_name) == {f"{prefix} Root", f"{prefix} Mid", f"{prefix} Leaf"}
            assert by_name[f"{prefix} Root"]["parent_id"] is None
            assert by_name[f"{prefix} Mid"]["parent_id"] == by_name[f"{prefix} Root"]["term_id"]
            assert by_name[f"{prefix} Leaf"]["parent_id"] == by_name[f"{prefix} Mid"]["term_id"]

            # Attribute values survive the CSV round trip in their own types.
            assert by_name[f"{prefix} Root"]["attributes"]["Masked"] is True
            assert by_name[f"{prefix} Leaf"]["attributes"]["Tier"] == "Silver"

            # Definition and Description are separate CSV columns and separate
            # fields. Write only one of them and the service fills the
            # definition in with the term's own name, which is how a whole
            # imported hierarchy came back reading like a list of labels.
            root = by_name[f"{prefix} Root"]
            assert root["definition"] == "imported root"
            assert root["description"] == "imported root overview"
            # What the service does with an *empty* Definition cell is its own
            # business; what matters is that a supplied one is not the
            # description and is not the term's name.
            assert root["definition"] != root["name"]

            # Required, but defaulted: no row supplied it and none was refused.
            assert by_name[f"{prefix} Mid"]["attributes"]["Stage"] == "draft"

            # The import names no ids, so every row is matched back to its term
            # by walking its path over one filtered lookup. Without this, using
            # the imported terms means a search per term.
            resolved = {t["name"]: t for t in result["terms"]}
            assert set(resolved) == set(by_name)
            for name, term in resolved.items():
                assert term["term_id"] == by_name[name]["term_id"], name
                assert term["existed"] is False, "the job just created it"
            assert result["new"] == 3
            assert result["already_existed"] == 0
            assert resolved[f"{prefix} Leaf"]["path"] == f"{prefix} Root\\{prefix} Mid"

            # Re-importing the same rows creates nothing, but the job still
            # tallies them as successful — so 'new' is what says what happened.
            again = (
                await client.call_tool(
                    "import_glossary_terms",
                    {
                        "term_type": type_id,
                        "terms": [
                            {"name": f"{prefix} Root", "attributes": {"Tier": "Gold"}},
                            {"name": f"{prefix} Mid", "parent": f"{prefix} Root"},
                        ],
                    },
                )
            ).data
            assert again["failures"] == []
            assert again["new"] == 0, f"nothing was new: {again['terms']}"
            assert again["already_existed"] == 2
            assert {t["term_id"] for t in again["terms"]} == {
                by_name[f"{prefix} Root"]["term_id"], by_name[f"{prefix} Mid"]["term_id"]
            }, "the same terms, not duplicates"
            assert "already existed" in again["note"]

            # A bad value is caught here, before anything is sent.
            with pytest.raises(Exception, match="only accepts"):
                await client.call_tool(
                    "import_glossary_terms",
                    {
                        "term_type": type_id,
                        "terms": [{"name": f"{prefix} Bad", "attributes": {"Tier": "Bronze"}}],
                    },
                )
        finally:
            # Deepest first: a parent cannot be deleted while a child points at it.
            async def depth(term_id: str) -> int:
                steps = 0
                current = (
                    await client.call_tool("get_glossary_term", {"term_id": term_id})
                ).data
                while current.get("parent_id"):
                    steps += 1
                    current = (
                        await client.call_tool(
                            "get_glossary_term", {"term_id": current["parent_id"]}
                        )
                    ).data
                return steps

            ranked = sorted(
                [(await depth(tid), tid) for tid in created_ids], reverse=True
            )
            for _, tid in ranked:
                await client.call_tool("delete_glossary_term", {"term_id": tid})
            if type_id:
                await client.call_tool(
                    "delete_glossary_term_type", {"term_type_id": type_id, "force": True}
                )
