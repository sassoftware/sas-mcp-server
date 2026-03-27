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

### Changed
- `src/sas_mcp_server/config.py` — Added SSL verification bypass via httpx monkey-patch when `SSL_VERIFY=false`
- `src/sas_mcp_server/viya_utils.py` — Respects `SSL_VERIFY` setting for Viya API calls
- `examples/configuration.md` — Added Python registration script instructions and `SSL_VERIFY` to environment variables table

### Fixed
- `pyproject.toml` — Corrected `authors` field to PEP 621 format (`[{name = "..."}]`) fixing Docker build error
- `.dockerignore` — Added `!README.md` exception so `uv build` can find the readme during container builds
- `.env.sample` — Corrected `CONTEXT_NAME` to `COMPUTE_CONTEXT_NAME` to match the actual env var read by config
- `examples/configuration.md` — Corrected `CONTEXT_NAME` to `COMPUTE_CONTEXT_NAME` in environment variables table
