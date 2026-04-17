# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared tool registration for both HTTP and stdio MCP servers.
All tools are registered via ``register_tools(mcp, get_token)``.
"""

from typing import Optional
import httpx as _httpx
from fastmcp import Context
from fastmcp.tools.tool import ToolResult
from .viya_utils import (
    _get_json,
    _get_paged_items,
    _post_json,
    _delete_resource,
    _make_client,
    run_one_snippet,
    logger,
)


def register_tools(mcp, get_token):
    """Register all tools on *mcp*.

    Parameters
    ----------
    mcp : FastMCP
        The server instance to register tools on.
    get_token : callable
        ``async def get_token(ctx: Context) -> str`` — returns a Viya access
        token.  HTTP mode pulls it from context state; stdio mode acquires it
        via password grant.
    """

    # ------------------------------------------------------------------
    # Original tool
    # ------------------------------------------------------------------

    @mcp.tool()
    async def execute_sas_code(sas_code: str, ctx: Context) -> ToolResult:
        """
        Executes the provided SAS code in the Viya environment and returns information about the completed Job.
        This will create a job definition for the SAS code, execute it, and then retrieve the results.

        Args:
            sas_code (str): the SAS code snippet to be executed using the Viya Job Execution API Service

        Returns:
            Structured output data containing detailed information about the executed sas code.
            This includes a listing field and a log field. The listing output represents the intended output
            of the SAS code when executed, if the code ran successfully. The log output represents information
            about the execution of the sas code, such as if it ran successfully or not and whether or not there are
            errors or issues with the execution.

        """
        logger.info("--- TOOL USED: execute_sas_code ---")
        token = await get_token(ctx)
        output = await run_one_snippet(sas_code, "1", token)
        return output

    # ------------------------------------------------------------------
    # Tier 1 — Data Discovery (CAS Management)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_cas_servers(ctx: Context) -> list:
        """List available CAS servers on the Viya environment."""
        logger.info("--- TOOL USED: list_cas_servers ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/casManagement/servers", client)
            return [{"name": s.get("name"), "id": s.get("id"),
                     "description": s.get("description", "")} for s in items]

    @mcp.tool()
    async def list_caslibs(server_id: str, ctx: Context,
                           limit: int = 50) -> list:
        """List CAS libraries (caslibs) available on a CAS server.

        Args:
            server_id: CAS server name or ID (e.g. 'cas-shared-default').
            limit: Maximum number of caslibs to return (default 50).
        """
        logger.info("--- TOOL USED: list_caslibs ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs", client, limit=limit)
            return [{"name": c.get("name"), "type": c.get("type", ""),
                     "description": c.get("description", "")} for c in items]

    @mcp.tool()
    async def list_castables(server_id: str, caslib_name: str, ctx: Context,
                             limit: int = 50) -> list:
        """List tables in a CAS library.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            limit: Maximum number of tables to return (default 50).
        """
        logger.info("--- TOOL USED: list_castables ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
                client, limit=limit)
            return [{"name": t.get("name"),
                     "rowCount": t.get("rowCount"),
                     "columnCount": t.get("columnCount")} for t in items]

    @mcp.tool()
    async def get_castable_info(server_id: str, caslib_name: str,
                                table_name: str, ctx: Context) -> dict:
        """Get metadata for a CAS table (row count, column count, size, etc.).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
        """
        logger.info("--- TOOL USED: get_castable_info ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}",
                client)

    @mcp.tool()
    async def get_castable_columns(server_id: str, caslib_name: str,
                                   table_name: str, ctx: Context,
                                   limit: int = 200) -> list:
        """Get column metadata for a CAS table (names, types, labels, formats).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
            limit: Maximum columns to return (default 200).
        """
        logger.info("--- TOOL USED: get_castable_columns ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}/columns",
                client, limit=limit)
            return [{"name": c.get("name"), "type": c.get("type"),
                     "rawLength": c.get("rawLength"),
                     "label": c.get("label", ""),
                     "format": c.get("format", "")} for c in items]

    @mcp.tool()
    async def get_castable_data(server_id: str, caslib_name: str,
                                table_name: str, ctx: Context,
                                limit: int = 100, start: int = 0) -> dict:
        """Fetch rows from a CAS table with column names.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
            limit: Maximum rows to return (default 100).
            start: Row offset (default 0).
        """
        logger.info("--- TOOL USED: get_castable_data ---")
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        data_source_id = f"cas~fs~{server_id}~fs~{caslib_name}"
        table_id = f"cas~fs~{server_id}~fs~{caslib_name}~fs~{table_name}"
        async with _make_client(token) as client:
            columns = []
            col_start = 0
            col_limit = 100
            while True:
                col_resp = await client.get(
                    f"{VIYA_ENDPOINT}/dataTables/dataSources/{data_source_id}/tables/{table_name}/columns",
                    params={"start": col_start, "limit": col_limit},
                    follow_redirects=True,
                )
                col_resp.raise_for_status()
                col_data = col_resp.json()
                for item in col_data.get("items", []):
                    columns.append({"name": item.get("name"), "type": item.get("type"),
                                    "index": item.get("index")})
                total = col_data.get("count", 0)
                col_start += col_limit
                if col_start >= total:
                    break

            row_resp = await client.get(
                f"{VIYA_ENDPOINT}/rowSets/tables/{table_id}/rows",
                params={"start": start, "limit": limit},
                follow_redirects=True,
            )
            row_resp.raise_for_status()
            row_data = row_resp.json()

            col_names = [c["name"] for c in columns]
            rows = []
            for item in row_data.get("items", []):
                cells = item.get("cells", [])
                rows.append(dict(zip(col_names, cells)))

            return {
                "columns": col_names,
                "rows": rows,
                "count": row_data.get("count", len(rows)),
                "start": start,
                "limit": limit,
            }

    # ------------------------------------------------------------------
    # Tier 2 — Data Operations & Files
    # ------------------------------------------------------------------

    @mcp.tool()
    async def upload_data(server_id: str, caslib_name: str, table_name: str,
                          csv_data: str, ctx: Context) -> dict:
        """Upload CSV data into a CAS table.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Target caslib name.
            table_name: Name for the new table.
            csv_data: CSV-formatted data string (including header row).
        """
        logger.info("--- TOOL USED: upload_data ---")
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
                data={
                    "tableName": table_name,
                    "format": "csv",
                    "containsHeaderRow": "true",
                },
                files={"file": ("data.csv", csv_data.encode("utf-8"), "text/csv")},
            )
            if resp.status_code == 409:
                return {
                    "status": "table_already_exists",
                    "table_name": table_name,
                    "caslib": caslib_name,
                    "message": f"Table '{table_name}' already exists in caslib '{caslib_name}'. Drop or rename before re-uploading.",
                }
            resp.raise_for_status()
            body = resp.json()
            return {
                "status": "success",
                "table_name": body.get("name"),
                "rows_uploaded": body.get("rowCount", 0),
                "column_count": body.get("columnCount", 0),
                "caslib": body.get("caslibName"),
                "scope": body.get("scope"),
            }

    @mcp.tool()
    async def promote_table_to_memory(server_id: str, caslib_name: str,
                                      table_name: str, ctx: Context) -> dict:
        """Promote a CAS table to global scope (makes it visible to all sessions).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Caslib containing the table.
            table_name: Table to promote.
        """
        logger.info("--- TOOL USED: promote_table_to_memory ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            try:
                return await _post_json(
                    f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}",
                    client, body={"scope": "global"})
            except _httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    return {"status": "already_promoted", "table": f"{caslib_name}.{table_name}"}
                raise

    @mcp.tool()
    async def list_files(ctx: Context, limit: int = 50,
                         filter_name: Optional[str] = None) -> list:
        """List files in the Viya Files Service.

        Args:
            limit: Maximum files to return (default 50).
            filter_name: Optional name filter (substring match).
        """
        logger.info("--- TOOL USED: list_files ---")
        token = await get_token(ctx)
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/files/files", client,
                                              limit=limit, filters=filters)
            return [{"id": f.get("id"), "name": f.get("name"),
                     "contentType": f.get("contentType", ""),
                     "size": f.get("size")} for f in items]

    @mcp.tool()
    async def upload_file(file_name: str, content: str, ctx: Context,
                          content_type: str = "text/plain") -> dict:
        """Upload a file to the Viya Files Service.

        Args:
            file_name: Name for the file.
            content: File content as a string.
            content_type: MIME type (default 'text/plain').
        """
        logger.info("--- TOOL USED: upload_file ---")
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}/files/files",
                content=content.encode("utf-8"),
                headers={"Content-Type": content_type,
                         "Content-Disposition": f'attachment; filename="{file_name}"',
                         "Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()

    @mcp.tool()
    async def download_file(file_id: str, ctx: Context) -> str:
        """Download file content from the Viya Files Service.

        Args:
            file_id: ID of the file to download.
        """
        logger.info("--- TOOL USED: download_file ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            from .viya_utils import VIYA_ENDPOINT
            resp = await client.get(f"{VIYA_ENDPOINT}/files/files/{file_id}/content")
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Tier 3 — Reports & Visualization
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_reports(ctx: Context, limit: int = 50,
                           filter_name: Optional[str] = None) -> list:
        """List Visual Analytics reports.

        Args:
            limit: Maximum reports to return (default 50).
            filter_name: Optional name filter (substring match).
        """
        logger.info("--- TOOL USED: list_reports ---")
        token = await get_token(ctx)
        filters = f"contains(name,'{filter_name}')" if filter_name else None
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/reports/reports", client,
                                              limit=limit, filters=filters)
            return [{"id": r.get("id"), "name": r.get("name"),
                     "description": r.get("description", ""),
                     "createdBy": r.get("createdBy", "")} for r in items]

    @mcp.tool()
    async def get_report(report_id: str, ctx: Context) -> dict:
        """Get a Visual Analytics report's metadata and definition.

        Args:
            report_id: ID of the report.
        """
        logger.info("--- TOOL USED: get_report ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(f"/reports/reports/{report_id}", client)

    @mcp.tool()
    async def get_report_image(report_id: str, ctx: Context,
                               image_type: str = "png",
                               section_index: int = 0) -> dict:
        """Render a Visual Analytics report section as an image.

        Args:
            report_id: ID of the report.
            image_type: Image format — 'png' or 'svg' (default 'png').
            section_index: Report section/page index (default 0).
        """
        logger.info("--- TOOL USED: get_report_image ---")
        token = await get_token(ctx)
        import json as _json
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            body = {
                "reportUri": f"/reports/reports/{report_id}",
                "layoutType": "thumbnail",
                "selectionType": "perSection",
                "sectionIndex": section_index,
                "size": "800x600",
                "renderLimit": 1,
            }
            resp = await client.post(
                f"{VIYA_ENDPOINT}/reportImages/jobs",
                content=_json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/vnd.sas.report.images.job.request+json",
                    "Accept": "application/vnd.sas.report.images.job+json",
                },
            )
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Tier 4 — Batch Jobs & Async Execution
    # ------------------------------------------------------------------

    @mcp.tool()
    async def submit_batch_job(sas_code: str, ctx: Context,
                               job_name: Optional[str] = None) -> dict:
        """Submit a SAS job for asynchronous execution via the Job Execution service.

        Args:
            sas_code: SAS code to execute.
            job_name: Optional descriptive name for the job.
        """
        logger.info("--- TOOL USED: submit_batch_job ---")
        token = await get_token(ctx)
        from .viya_utils import CONTEXT_NAME
        body = {
            "name": job_name or "mcp-batch-job",
            "jobDefinition": {
                "type": "Compute",
                "code": sas_code,
            },
            "arguments": {
                "_contextName": CONTEXT_NAME,
            },
        }
        async with _make_client(token) as client:
            return await _post_json("/jobExecution/jobs", client, body=body)

    @mcp.tool()
    async def get_job_status(job_id: str, ctx: Context) -> dict:
        """Check the status of a submitted job.

        Args:
            job_id: ID of the job.
        """
        logger.info("--- TOOL USED: get_job_status ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            return await _get_json(f"/jobExecution/jobs/{job_id}", client)

    @mcp.tool()
    async def list_jobs(ctx: Context, limit: int = 20) -> list:
        """List recent jobs from the Job Execution service.

        Args:
            limit: Maximum jobs to return (default 20).
        """
        logger.info("--- TOOL USED: list_jobs ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/jobExecution/jobs", client,
                                              limit=limit)
            return [{"id": j.get("id"), "name": j.get("name", ""),
                     "state": j.get("state", ""),
                     "creationTimeStamp": j.get("creationTimeStamp", "")} for j in items]

    @mcp.tool()
    async def cancel_job(job_id: str, ctx: Context) -> str:
        """Cancel a running job.

        Args:
            job_id: ID of the job to cancel.
        """
        logger.info("--- TOOL USED: cancel_job ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            await _delete_resource(f"/jobExecution/jobs/{job_id}", client)
            return f"Job {job_id} cancelled."

    @mcp.tool()
    async def get_job_log(job_id: str, ctx: Context) -> str:
        """Retrieve the log of a completed job.

        Args:
            job_id: ID of the job.
        """
        logger.info("--- TOOL USED: get_job_log ---")
        token = await get_token(ctx)
        from .viya_utils import VIYA_ENDPOINT
        async with _make_client(token) as client:
            data = await _get_json(f"/jobExecution/jobs/{job_id}", client)
            results = data.get("results", {})

            log_uri = None
            for key, value in results.items():
                if key.endswith(".log.txt"):
                    log_uri = value
                    break
            if not log_uri:
                for key, value in results.items():
                    if key.endswith(".log"):
                        log_uri = value
                        break

            if not log_uri:
                state = data.get("state", "unknown")
                error = data.get("error", {})
                if error:
                    return f"Job {state}: {error.get('message', 'No error details')}"
                return f"No log available. Job state: {state}"

            resp = await client.get(f"{VIYA_ENDPOINT}{log_uri}/content")
            resp.raise_for_status()
            return resp.text

    # ------------------------------------------------------------------
    # Tier 5 — Model Management & Scoring
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_ml_projects(ctx: Context, limit: int = 50) -> list:
        """List AutoML pipeline automation projects.

        Args:
            limit: Maximum projects to return (default 50).
        """
        logger.info("--- TOOL USED: list_ml_projects ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items(
                "/mlPipelineAutomation/projects", client, limit=limit)
            return [{"id": p.get("id"), "name": p.get("name", ""),
                     "state": p.get("state", ""),
                     "description": p.get("description", "")} for p in items]

    @mcp.tool()
    async def create_ml_project(project_name: str, data_table_uri: str,
                                target_variable: str, ctx: Context,
                                description: str = "",
                                prediction_type: str = "binary",
                                target_event_level: str = "1",
                                auto_run: bool = True) -> dict:
        """Create a new AutoML pipeline automation project.

        Args:
            project_name: Name for the project.
            data_table_uri: URI of the training data table (e.g. '/dataTables/dataSources/cas~fs~cas-shared-default~fs~Public/tables/HMEQ').
            target_variable: Name of the target/response variable.
            description: Optional project description.
            prediction_type: 'binary', 'interval', or 'nominal' (default 'binary').
            target_event_level: Target event level for binary/nominal classification (default '1').
            auto_run: Whether to automatically run pipelines after creation (default True).
        """
        logger.info("--- TOOL USED: create_ml_project ---")
        token = await get_token(ctx)
        analytics_attrs = {
            "targetVariable": target_variable,
            "targetLevel": prediction_type,
            "partitionEnabled": True,
            "classSelectionStatistic": "ks" if prediction_type in ("binary", "nominal") else "ase",
        }
        if prediction_type in ("binary", "nominal"):
            analytics_attrs["targetEventLevel"] = target_event_level
        body = {
            "name": project_name,
            "description": description,
            "type": "predictive",
            "dataTableUri": data_table_uri,
            "pipelineBuildMethod": "automatic",
            "settings": {
                "applyGlobalMetadata": True,
                "autoRun": auto_run,
                "numberOfModels": 5,
            },
            "analyticsProjectAttributes": analytics_attrs,
        }
        async with _make_client(token) as client:
            return await _post_json("/mlPipelineAutomation/projects", client, body=body)

    @mcp.tool()
    async def run_ml_project(project_id: str, ctx: Context) -> dict:
        """Run an AutoML pipeline automation project.

        Args:
            project_id: ID of the project to run.
        """
        logger.info("--- TOOL USED: run_ml_project ---")
        token = await get_token(ctx)
        import json as _json
        from .viya_utils import VIYA_ENDPOINT
        mlpa_type = "application/vnd.sas.analytics.ml.pipeline.automation.project+json"
        async with _make_client(token) as client:
            get_resp = await client.get(
                f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                headers={"Accept": mlpa_type},
            )
            get_resp.raise_for_status()
            project_body = get_resp.json()
            etag = get_resp.headers.get("etag", "")
            resp = await client.put(
                f"{VIYA_ENDPOINT}/mlPipelineAutomation/projects/{project_id}",
                params={"action": "retrainProject"},
                content=_json.dumps(project_body).encode(),
                headers={
                    "Content-Type": mlpa_type,
                    "Accept": mlpa_type,
                    "If-Match": etag,
                    "Accept-Language": "en",
                },
            )
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return {"status": "running", "projectId": project_id}
            return resp.json()

    @mcp.tool()
    async def list_registered_models(ctx: Context, limit: int = 50) -> list:
        """List models in the Model Repository.

        Args:
            limit: Maximum models to return (default 50).
        """
        logger.info("--- TOOL USED: list_registered_models ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/modelRepository/models", client,
                                              limit=limit)
            return [{"id": m.get("id"), "name": m.get("name", ""),
                     "description": m.get("description", ""),
                     "modelVersionName": m.get("modelVersionName", "")} for m in items]

    @mcp.tool()
    async def list_models_and_decisions(ctx: Context, limit: int = 50) -> list:
        """List published scoring models and decisions (MAS modules).

        Args:
            limit: Maximum modules to return (default 50).
        """
        logger.info("--- TOOL USED: list_models_and_decisions ---")
        token = await get_token(ctx)
        async with _make_client(token) as client:
            items, _ = await _get_paged_items("/microanalyticScore/modules", client,
                                              limit=limit)
            return [{"id": m.get("id"), "name": m.get("name", ""),
                     "description": m.get("description", "")} for m in items]

    @mcp.tool()
    async def score_data(module_id: str, step_id: str, input_data: dict,
                         ctx: Context) -> dict:
        """Score data against a published model or decision (MAS module).

        Args:
            module_id: MAS module ID.
            step_id: Step ID within the module (usually 'score' or 'execute').
            input_data: Dictionary of input variable name-value pairs.
        """
        logger.info("--- TOOL USED: score_data ---")
        token = await get_token(ctx)
        body = {"inputs": [{"name": k, "value": v} for k, v in input_data.items()]}
        async with _make_client(token) as client:
            return await _post_json(
                f"/microanalyticScore/modules/{module_id}/steps/{step_id}", client,
                body=body)
