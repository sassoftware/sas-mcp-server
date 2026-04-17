# Changelog

## [Unreleased]

### Added
- **26 new MCP tools** across five tiers:
  - **Tier 1 — Data Discovery**: `list_cas_servers`, `list_caslibs`, `list_castables`, `get_castable_info`, `get_castable_columns`, `get_castable_data`
  - **Tier 2 — Data Operations & Files**: `upload_data`, `promote_table_to_memory`, `list_files`, `upload_file`, `download_file`
  - **Tier 3 — Reports & Visualization**: `list_reports`, `get_report`, `get_report_image`
  - **Tier 4 — Batch Jobs**: `submit_batch_job`, `get_job_status`, `list_jobs`, `cancel_job`, `get_job_log`
  - **Tier 5 — Model Management & Scoring**: `list_ml_projects`, `create_ml_project`, `run_ml_project`, `list_registered_models`, `list_models_and_decisions`, `score_data`
- **8 prompt templates**: `debug_sas_log`, `explore_dataset`, `data_quality_check`, `statistical_analysis`, `optimize_sas_code`, `explain_sas_code`, `sas_macro_builder`, `generate_report`
- Shared tool/prompt registration (`tools.py`, `prompts.py`) — eliminates duplication between HTTP and stdio servers
- Generic API helpers in `viya_utils.py`: `_get_json`, `_get_paged_items`, `_post_json`, `_put_data`, `_delete_resource`, `_make_client`
- `SSL_VERIFY` environment variable to disable SSL certificate verification for Viya instances with self-signed certificates
- `examples/register_mcp_client.py` — Python script to register the OAuth client in Viya, as an alternative to manual curl commands
- `SSL_VERIFY` option documented in `.env.sample` and `examples/configuration.md`
- Startup INFO log showing which SAS Viya endpoint the server connects to
- SSL certificate chain configuration guide in `examples/configuration.md`
- Deployment mode comparison (HTTP vs stdio vs Docker) in README
- `MCP_BASE_URL` environment variable — configurable OAuth callback base URL for Kubernetes / reverse proxy deployments (defaults to `http://localhost:{HOST_PORT}`)
- Kubernetes deployment guide in `examples/configuration.md` (Ingress, env vars, multi-user OAuth setup)

- **Payload assertion test suite** (`tests/test_tool_payloads.py`) — 34 unit tests verifying the exact HTTP request (URL, body, params, headers) sent by every tool, including branch coverage for `create_ml_project` (binary/interval/nominal)
- **Integration test suite** (`tests/test_integration.py`) — 8 end-to-end workflow tests against a real Viya instance covering CAS discovery, data upload, file service, SAS code execution, batch jobs, reports, ML projects, and scoring
- **Test runner script** (`run_tests.sh`) — Accepts credentials via CLI args or `.env`; supports `--integration` and `--integration-only` modes
- **Gemini CLI configuration** — `examples/gemini-settings.json` with recommended `timeout` setting, Gemini CLI section in `examples/configuration.md`, and Gemini CLI snippets in README

### Changed
- `src/sas_mcp_server/config.py` — Added SSL verification bypass via httpx monkey-patch when `SSL_VERIFY=false`
- `src/sas_mcp_server/viya_utils.py` — Respects `SSL_VERIFY` setting for Viya API calls
- `examples/configuration.md` — Added Python registration script instructions and `SSL_VERIFY` to environment variables table

### Fixed
- `upload_data` — Rewrote to use the CAS Management REST API (`POST /casManagement/servers/{server}/caslibs/{caslib}/tables` with `multipart/form-data`) instead of a SAS DATA step workaround; handles 409 (table already exists) gracefully
- `_make_client` — Removed default `Content-Type: application/json` header that conflicted with multipart form-data uploads
- `create_ml_project` — Fixed request body to match the MLPA API spec: `predictionType` → `targetLevel`, moved `targetVariable` inside `analyticsProjectAttributes`, added required `type`, `pipelineBuildMethod`, and `settings` fields, added `targetEventLevel` for binary/nominal classification
- `get_castable_data` — Rewrote to use the dataTables/rowSets APIs instead of the non-existent `/casManagement/.../rows` endpoint: fetches column metadata via `GET /dataTables/dataSources/.../columns` (with full pagination), then row data via `GET /rowSets/tables/.../rows`; returns structured `{columns, rows}` with named fields; default row limit increased from 20 to 100
- `get_report_image` — Fixed Content-Type to use `application/vnd.sas.report.images.job.request+json` (SAS media type) instead of `application/json`; switched from `json=` to `content=` to prevent httpx from overriding the Content-Type header
- `get_job_log` — Rewrote to fetch log from the Files service (via the job's `results` map) instead of the non-existent `/jobExecution/jobs/{id}/log` endpoint; returns error message when job fails before producing a log
- `run_ml_project` — Rewrote from `POST ?action=start` to correct `GET` (with ETag) → `PUT ?action=retrainProject` pattern with full project body, `If-Match`, `Accept-Language` headers, and `content=` instead of `json=` to preserve Content-Type
- `submit_batch_job` — Added `arguments: {"_contextName": ...}` to route jobs to the correct compute context
- `pyproject.toml` — Corrected `authors` field to PEP 621 format (`[{name = "..."}]`) fixing Docker build error
- `.dockerignore` — Added `!README.md` exception so `uv build` can find the readme during container builds
- `.env.sample` — Corrected `CONTEXT_NAME` to `COMPUTE_CONTEXT_NAME` to match the actual env var read by config
- `examples/configuration.md` — Corrected `CONTEXT_NAME` to `COMPUTE_CONTEXT_NAME` in environment variables table
