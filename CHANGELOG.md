# Changelog

## [Unreleased]

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
