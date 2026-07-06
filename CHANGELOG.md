# Changelog

## [Unreleased]

## [1.4.0] - 2026-07-02

### Added
- **AutoML champion model register/publish tools (3)** (#22) — `register_ml_champion_model` registers an AutoML pipeline automation project's champion model to the Model Repository; `publish_ml_champion_model` publishes it to a scoring destination; `list_publishing_destinations` lists the available destinations to publish to. All three drive the `mlPipelineAutomation` champion-model action endpoint (`register`/`publish`), shared via `helpers/auto_ml_helpers.py`. Brings the tool count to 45.
- **`export_report` tool** (#18) — one tool covering every synchronous Visual Analytics report export: `package` (zip), `pdf`, `png`, `svg`, `csv`, `tsv`, `xlsx`, and `summary`, for a whole report or selected report objects. Results are returned with native MCP content types — text inline for text formats, image content for `png`, and an embedded binary file (carrying the correct MIME type, e.g. `application/zip` for packages) for `package`/`pdf`/`xlsx` — instead of a hand-rolled base64 JSON blob. Per-format input validation enforces the API's object-count and `image_size` rules; binary exports larger than `MAX_EXPORT_INLINE_BYTES` (default 25 MiB) are refused with guidance rather than streamed through the model context; and a Viya HTTP error is surfaced as a structured `export_failed` result instead of a raised exception. The format registry, validation, and request execution live in `helpers/report_export_helpers.py` so the tool stays a thin wrapper.
- **SAS Business Rules & SAS Intelligent Decisioning tools (23)** — build and manage business rules and decision flows end to end: `create_business_ruleset`, `update_business_ruleset`, `get_business_ruleset`, `list_business_rulesets`, `delete_business_ruleset`, `lock_business_ruleset_revision`, `list_business_ruleset_revisions`, `create_business_rule`, `update_business_rule`, `get_business_rule`, `list_business_rules`, `delete_business_rule`, `create_decision_flow`, `update_decision_flow`, `get_decision_flow`, `list_decision_flows`, `delete_decision_flow`, `get_decision_flow_code`, `lock_decision_flow_revision`, `list_decision_flow_revisions`, `get_decision_flow_revision`, `publish_decision_flow`, and `get_mas_module_step_signature`. `publish_decision_flow` polls the asynchronous Model Publish job to completion and returns the MAS `moduleId` it produces (which is server-generated, not the `publish_name` passed in), so the result is directly usable with `score_data`/`get_mas_module_step_signature` without a separate `list_models_and_decisions` lookup. Brings the tool count to 65.

### Changed — BREAKING
- **`export_report_package` has been replaced by `export_report`.** The old tool hit the wrong route (`/visualAnalytics/getExportedReportPackage/{id}/package`, the operationId rather than the path) and sent `reportObjects` as a JSON body on a GET. Use `export_report` with `export_format="package"`; report objects are passed via `report_objects`.

### Removed — BREAKING
- **`get_report_image` has been removed**, superseded by `export_report`. It used the `reportImages` service to return an async *job descriptor* (not the image itself) for a report section thumbnail. `export_report` with `export_format="png"` or `"svg"` renders the whole report or a single object and returns the actual image bytes as native MCP content.

## [1.3.0] - 2026-06-22

### Added
- **SAS Information Catalog tools (9)** (#19) — `catalog_search`, `catalog_search_helper`, `catalog_find_instance`, `catalog_list_agents`, `catalog_run_agent`, `catalog_get_agent_history`, `catalog_run_adhoc_analysis`, `catalog_get_adhoc_analysis`, and `catalog_download_table_profile`. Metadata discovery and profiling across the whole Viya environment: search assets with the catalog grammar (free text + facets), resolve a search hit's catalog instance, submit and poll ad-hoc profiling jobs (with NLP enrichment for privacy/semantic tags), download a table's data dictionary + column profile as CSV, and list/run/inspect the discovery agents that populate the catalog. Brings the tool count to 41.
- **`upload_inline_data` tool** — creates a *small* CAS table from inline csv/tsv text passed as a string (a lookup/mapping table the model builds on the fly, or a quick test table). This is the deliberate "data travels through the model context" path, split out from `upload_data` so the cost is explicit in the tool you pick. Brings the tool count to 42.

### Changed — BREAKING
- **`upload_data` is now reference-only and no longer takes `csv_data`.** It accepts the data by reference through exactly one of `file_path` (the server reads it off its own disk) or `url` (the server fetches it), so the payload is read **server-side** and never passes through the calling model's context. Inline text moved to the new `upload_inline_data` tool. `file_path` reads from the host the server runs on (in stdio mode, the user's machine) and can be disabled by operators with `ALLOW_LOCAL_FILE_UPLOAD=false`. Callers that passed `csv_data` should switch to `upload_inline_data` (for genuinely small tables) or write the data to a file/URL and use `upload_data`.

### Changed
- **`upload_data` accepts more formats.** Beyond CSV it ingests every format the casManagement `uploadTable` API accepts — **tsv (csv + tab delimiter), xls, xlsx (single sheet), sas7bdat, and sashdat**. The format is auto-detected from the `file_path`/`url` extension and can be overridden with `data_format`; `sheet_name` (Excel) and `contains_header_row` arguments tune the import. **parquet** is not accepted by that endpoint (confirmed against the API spec and a live `HTTP 400`) and is rejected up front with guidance to load it via a path-based caslib + `promote_table_to_memory` or convert it first.
- `catalog_search` and `catalog_search_helper` use the shared `return_items` field projection for their result shaping (#19).

### Fixed
- **stdio token resolution is now expiry-aware (#20).** The resolver returned the first cached access token it found without checking expiry, so an expired SAS Viya CLI cache (`~/.sas`) could shadow a valid helper cache (`~/.sas-mcp-server`) and make every Viya call fail with 401. A token at or past its expiry (minus a 60s skew) is now skipped so resolution falls through to the next source; when a cache's access token is expired but it still holds a refresh token, it is exchanged for a fresh one using the client that minted it (`sas.cli` for the CLI cache, `vscode` for the helper cache), written back to that cache, and used. Refresh is best-effort — a wrong client, revoked token, or network error falls through cleanly to the next source or the device-code flow — and a missing/unparseable expiry is treated as not-expired, preserving prior behaviour.

## [1.2.0] - 2026-06-15

### Added
- **Compute service discovery tools** — `list_compute_contexts`, `list_compute_libraries`, `list_compute_tables`, `list_compute_columns` — browse compute contexts and the SAS libraries/tables/columns visible inside a compute session. Brings the tool count to 32.
- **`reset_compute_session` tool** — deletes the cached compute session for the caller and compute context, discarding its SAS state (WORK tables, macro variables, assigned librefs) so the next call starts from a fresh session. Defaults to the configured execution context.
- **Reusable, per-user compute session cache** (`_ComputeSessionCache` in `viya_utils.py`) — one warm compute session is kept per authenticated user and compute context, so repeat calls skip the slow session spin-up. Keyed by the JWT `sub` claim (falling back to other identity claims, then a token hash) so multi-user HTTP deployments never share a session. Cached sessions are validated before reuse and transparently recreated if Viya has reaped them for inactivity.
- **Compute session shutdown cleanup** — both server entry points register a FastMCP `lifespan` that deletes all cached compute sessions on shutdown (best effort), so warm sessions don't linger until Viya's idle reaper collects them.
- **Integration coverage for every new tool** — `test_compute_discovery_workflow` and `test_compute_session_reuse_and_reset` (which proves reuse, deletion, and recreation end to end against a live Viya), wired into the `test_every_tool_has_integration_coverage` guard.
- **`return_items` helper** (`viya_client.py`) — shared field-projection used by the compute discovery tools.

### Changed — BREAKING
- **`execute_sas_code` is now stateful across calls.** It runs in the reusable per-user compute session, so SAS WORK tables, macro variables, and assigned librefs persist between successive calls instead of being discarded with a per-call session. Call `reset_compute_session` to start from a clean session. (Previously every call created and tore down its own session.)

### Changed
- `get_context_id` and `create_session` now call `raise_for_status()`, so an auth/permission failure surfaces as a real `HTTPStatusError` instead of a misleading "compute context not found" or a `KeyError`.
- `get_context_id` builds its query with httpx `params=` instead of string interpolation, so compute context names containing reserved characters (`&`, `+`, `#`) are encoded correctly.
- The compute session lifecycle (resolve context → create → reuse/reset/teardown) is centralised in `_ComputeSessionCache`; `run_one_snippet` and the compute tools now share it, and `delete_session` was extracted as a reusable helper.

## [1.1.0] - 2026-05-31

### Changed — BREAKING
- **`execute_sas_code` now returns a structured object instead of a 4-element array.** The tool returns a JSON object `{"snippet_id", "state", "log", "listing"}` (all strings) so MCP clients can address fields by name and the declared return type matches reality. Callers that parsed the previous positional `[snippet_id, state, log, listing]` array must switch to keyed access.
- **`create_ml_project` arguments changed.** The single `data_table_uri` argument is replaced by `caslib_name` + `table_name` (plus optional `server_id`, default `cas-shared-default`); the tool now builds the data-table URI itself and pre-checks that the table is loaded in global scope, returning an actionable error when it isn't (instead of an opaque `mlPipelineAutomation` failure later).

### Fixed
- **`promote_table_to_memory` no longer always returns 404 (issue #10).** It previously wrapped a `casManagement` call that only acts on an already-in-memory table, so the common "load a table via `execute_sas_code`, then promote it in a later call" flow failed — the session-scoped table was already gone. It now loads the table from its caslib **source** and promotes it to global scope via the `updateTableState` API, and is idempotent (a no-op when the table is already loaded in global scope).

### Added
- **`list_source_tables` tool** — lists source tables not yet loaded into CAS memory (`state=unloaded`), so callers can discover what `promote_table_to_memory` can load. Brings the tool count to 27.
- **Code-quality tooling and a CI gate.** `[tool.ruff]` and `[tool.pyright]` config in `pyproject.toml`; `pytest-cov` with a 90% coverage floor; a new `.github/workflows/ci.yml` runs ruff → pyright → unit tests on every PR and on `main` (previously CI only built Docker images — tests and lint were never gated). `run_tests.sh` now runs the ruff + pyright gates before pytest (skip with `--no-lint`).
- **`src/sas_mcp_server/viya_client.py`** — generic Viya REST helpers (`get_json`, `get_paged_items`, `post_json`, `delete_resource`, `make_client`) plus the shared `logger`, extracted from `viya_utils.py` with public names.
- **`src/sas_mcp_server/exceptions.py`** — shared `AuthenticationError` (previously defined identically in `mcp_server.py` and `stdio_server.py`) and a `ConfigError`.
- **`src/sas_mcp_server/env.py`** — side-effect-free `env_bool` helper used by both `config.py` and `auth_login.py`, removing duplicated `SSL_VERIFY`/`ALLOW_RAW_BEARER` parsing.
- **Tests for previously uncovered modules** — `tests/test_auth_login.py`, `tests/test_stdio_server.py`, `tests/test_env.py`, `tests/test_config_oauth.py`, real HTTP auth-middleware/health-route tests in `tests/test_mcp_server.py`, and tool error-path tests. Package coverage rose from ~58% to ~95%.
- **Comprehensive live integration coverage** — every one of the 26 tools and 8 prompt templates is now exercised against a real SAS Viya, with `test_every_tool_has_integration_coverage` / `test_every_prompt_has_integration_coverage` guards that fail if a registered tool/prompt is ever added without an integration test. Added `cancel_job` and `run_ml_project` workflows and per-prompt rendering through the live-connected server.
- **`.github/workflows/integration.yml`** — opt-in job (manual `workflow_dispatch` or the `run-integration` PR label) that runs the integration suite against Viya using repo secrets and publishes results onto the PR as a Check, a sticky comment, and a JUnit artifact — **without committing any result files** (`reports/` is git-ignored).
- Full type annotations across the package, including parameterized return types on every tool.

### Changed
- All 26 tools now share a single `viya_session` async context manager for the log + token + client preamble that was previously copy-pasted into each tool.
- `viya_utils.py` is now compute session/job orchestration only; `run_one_snippet` returns the structured dict described above.
- `config.py` raises `ConfigError` (not a bare `Exception`) when `VIYA_ENDPOINT` is unset; logging is now lazy `%`-style throughout.

### Removed
- Dead helpers in `viya_utils.py` reachable only from tests: `_put_data`, `_get_text`, `_get_paged_lines`, `fetch_full_job_log`, `fetch_full_job_listing`, `fetch_full_session_log`.

## [1.0.0] - 2026-05-12

### Added
- **GitHub Container Registry publishing** — `.github/workflows/publish-ghcr.yml` builds and pushes multi-arch (`linux/amd64`, `linux/arm64`) images to `ghcr.io/sassoftware/sas-mcp-server` on push to `main` (`:edge`, `:sha-<short>`), on `v*` tags (`:latest`, semver tags), and on `workflow_dispatch`. Images carry build provenance and SBOM attestations.
- **PR-time Dockerfile build check** — `.github/workflows/docker-build.yml` builds the image (no push, single arch) on every PR that touches Dockerfile-relevant paths.
- **OCI image labels** on the `Dockerfile` runner stage per SAS OSPO publishing guidelines: `maintainer`, `org.opencontainers.image.source`, `org.opencontainers.image.description`, `org.opencontainers.image.licenses`. Closes upstream issue #1.
- **`sas-mcp-login`** — Built-in OAuth 2.0 Authorization Code + PKCE login helper for stdio mode, exposed as a `uv run sas-mcp-login` console-script. Uses the built-in `vscode` Viya OAuth client (available on Viya 2022.11+) so no admin client registration and no external CLI install are needed; writes a cached access token to `~/.sas-mcp-server/credentials.json`. Supports a two-step `--code <CODE>` variant for non-TTY shells.
- **`ALLOW_RAW_BEARER`** env var — Additive HTTP auth mode. When `true`, the server accepts raw upstream Viya JWTs in the `Authorization: Bearer` header alongside the default OAuth2 PKCE flow. PKCE clients are unaffected; the new path only fires when the standard MCP JWT swap returns `None`. Useful for automation/CI clients that already hold a Viya token.
- **`SAS_CLI_CONFIG`** env var — Override the parent directory for the `sas-viya` CLI credential cache used by stdio mode (default: `$HOME`).
- Native OAuth 2.0 Device Authorization Grant (RFC 8628) as the last-resort fallback in stdio mode for Viyas whose admin has not enabled CSRF protection on `/SASLogon/oauth/device_authorization`.
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
- **Stdio auth model overhauled.** Replaced password-grant authentication with a chain of OAuth 2.0 paths, tried in order: (1) token cached by `sas-viya auth loginCode` at `~/.sas/credentials.json` (or `$SAS_CLI_CONFIG/.sas/credentials.json`); (2) token cached by `sas-mcp-login` at `~/.sas-mcp-server/credentials.json`; (3) native device-code flow. The first hit wins. Password grant was deprecated by OAuth 2.1 and failed with `invalid_client` for OAuth clients registered as confidential.
- `src/sas_mcp_server/config.py` — `viya_auth` is now a `PermissiveOAuthProxy` (subclass of `fastmcp.server.auth.OAuthProxy`) so that, when `ALLOW_RAW_BEARER=true`, raw upstream JWTs fall through to the configured `token_verifier` after the standard MCP JWT swap fails. Behaviour is unchanged when the flag is `false`.
- `src/sas_mcp_server/stdio_server.py` — Rewritten around the new auth chain; removed `VIYA_USERNAME`/`VIYA_PASSWORD` reads.
- `examples/configuration.md` — New "Authentication modes — at a glance" overview comparing all five paths; reworked "Authenticating for stdio mode" to cover both `sas-viya` CLI and `sas-mcp-login` paths; updated Gemini CLI section to drop password-grant references; added `ALLOW_RAW_BEARER` and `SAS_CLI_CONFIG` to the environment variables table.
- `README.md` — Added a "Pull pre-built image" snippet with the published tag table; updated the deployment-mode comparison and "Option B: Stdio mode" instructions; new "Programmatic clients with a pre-existing Viya token" section documenting `ALLOW_RAW_BEARER`.
- `examples/docker/setup.md` — Added a "Pulling the pre-built image" section with the tag-to-image-version mapping and a note about signed build provenance.
- `src/sas_mcp_server/config.py` — Added SSL verification bypass via httpx monkey-patch when `SSL_VERIFY=false`
- `src/sas_mcp_server/viya_utils.py` — Respects `SSL_VERIFY` setting for Viya API calls
- `examples/configuration.md` — Added Python registration script instructions and `SSL_VERIFY` to environment variables table
- **Bumped `fastmcp` from `>=2.13.0.2` to `>=3.0.0,<4.0.0`** (major version). v3 made several APIs async and renamed some module paths. The migration in this repo touches:
  - `OAuthProxy(upstream_client_secret=None, ...)` — `""` is no longer accepted.
  - `Context.set_state` / `Context.get_state` are now `async def` (carried from PR #9).
  - `from fastmcp.utilities.logging import get_logger` — top-level `fastmcp.utilities` is no longer re-exported.
  - `from fastmcp.prompts import Message` — module path flattened.
  - `from fastmcp.tools import ToolResult` — module path flattened.
- **`OAuthProxy(valid_scopes=["openid"])`** — required so containerized deployments accept tokens issued only with `openid` (carried from PR #9; fixes OAuth2 under Podman/Docker).
- **SSL monkey-patch in `config.py` is now idempotent** — guarded by `_sas_mcp_ssl_patched` so reloading the module doesn't stack wrappers around `httpx.AsyncClient.__init__`.

### Removed
- **Password-grant stdio authentication.** `VIYA_USERNAME`/`VIYA_PASSWORD` are no longer read by the stdio server. They remain in `.env.sample` (with clearer wording) only because the integration test suite uses the legacy `sas.cli` password grant to acquire test tokens.

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
- **HTTP auth middleware** correctly `await`s `Context.set_state` / `get_state` under v3's async state API (PR #9).

### Tests
- Updated `test_prompts.py` to use the public `mcp.get_prompt()` / `mcp.list_prompts()` API; v3 removed the private `_prompt_manager` attribute the prior tests reached into.
- Updated `test_mcp_server.py` to `await` mocked `Context.get_state`, since v3 makes the method async.
- Switched `test_config.py` from `del sys.modules + import` to `importlib.reload()`. The old pattern created orphan config modules that polluted `httpx.AsyncClient.__init__` and broke later integration tests with empty-message `ConnectError`s.
- `conftest.py` now calls `load_dotenv()` at module top so `SSL_VERIFY` (and friends) are read from `.env` before any `sas_mcp_server` module is first imported; the `viya_token` fixture also gets an explicit `timeout=60.0` on the password-grant request.
- `test_integration.py` pinned to a session-scoped asyncio loop (`@pytest.mark.asyncio(loop_scope="session")`) so the session-scoped `integration_mcp_server` fixture and per-test `Client` share one loop.
- `test_cas_discovery_workflow` now targets `Public.HMEQ` directly (skips if not loaded) instead of picking `caslibs[0]`/`tables[0]`, which was brittle on Viyas with many tables or unloaded source tables.
