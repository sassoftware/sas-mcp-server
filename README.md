# SAS Viya MCP Server

A Model Context Protocol (MCP) server for executing SAS code, training AutoML projects, scoring models and so much more for SAS Viya environments.

## Features

- 68 tools across 8 selectable tiers, spanning the Analytics Life Cycle on SAS Viya
- Prompt Templates for improving your SAS Code
- OAuth2 authentication with PKCE flow
- HTTP-based MCP server compatible with MCP clients

## Articles & Videos

Here you can find getting articles on how to use and integrate the SAS MCP Server in different tools and what to build with it:

- [Connecting GitHub Copilot to SAS Viya with the SAS MCP Server](https://communities.sas.com/t5/SAS-Communities-Library/Connecting-GitHub-Copilot-to-SAS-Viya-with-the-SAS-MCP-Server/ta-p/987191)
- [Putting the SAS MCP Server to Work in GitHub Copilot](https://communities.sas.com/t5/SAS-Communities-Library/Putting-the-SAS-MCP-Server-to-Work-in-GitHub-Copilot/ta-p/987193)
- [Connecting Claude Code CLI to SAS Viya with the SAS MCP Server](https://communities.sas.com/t5/SAS-Communities-Library/Connecting-Claude-Code-CLI-to-SAS-Viya-with-the-SAS-MCP-Server/ta-p/988775)
- [Putting the SAS MCP Server to Work in Claude Code CLI](https://communities.sas.com/t5/SAS-Communities-Library/Putting-the-SAS-MCP-Server-to-Work-in-Claude-Code-CLI/ta-p/988922)
- [Integration with SAS Retrieval Agent Manager (RAM)](https://github.com/sassoftware/sas-retrieval-agent-manager-examples/tree/main/examples/container_mcp_servers/sas_mcp_server)

## Getting Started
### Prerequisites
- Required
    - [Python 3.12+](https://www.python.org/downloads)
    - [uv 0.8+](https://github.com/astral-sh/uv)
    - [SAS Viya environment](https://www.sas.com/en_us/software/viya.html) with compute service
    - Setup the Viya environment for MCP
        - See [configuration.md](/examples/configuration.md)

- Optional
    - [Docker](https://docs.docker.com/engine/install): refer to [docker setup](/examples/docker/setup.md)

### Installation

1. Clone the repository:
```sh
git clone <repository-url>
cd sas-mcp-server
```

2. Install dependencies
```sh
uv sync
```

NOTE: This will by default create a virtual environment called .venv in the project's root directory.

If for some reason the virtual environment is not created, please run `uv venv` and then re-run `uv sync`.

### Usage

1. Configure environment variables:
```sh
cp .env.sample .env
```

Edit `.env` and set
```sh
VIYA_ENDPOINT=https://your-viya-server.com
```

2. Start the MCP server (see [Choosing a deployment mode](#choosing-a-deployment-mode) below):

**Option A: HTTP mode** (pre-run the server, connect from MCP client)
```sh
uv run app
```
The server will be available at `http://localhost:8134/mcp` by default. Authentication is handled via OAuth2 PKCE flow in the browser.

**Option B: Stdio mode** (MCP client starts the server on demand)

Authenticate once. Two equivalent options:

```sh
# Option B1 — if you have the SAS Viya CLI installed:
sas-viya auth loginCode

# Option B2 — built-in helper, no external CLI needed (Viya 2022.11+):
uv run sas-mcp-login
```

Both flows write an access token to a local cache (`~/.sas/credentials.json` and `~/.sas-mcp-server/credentials.json` respectively); the stdio server reads whichever it finds. When the token expires, re-run the same command.

Then configure your MCP client to launch the server directly (see below).

**Option C: Docker / Podman** (containerized deployment)

Pull the pre-built image from GitHub Container Registry:
```sh
docker pull ghcr.io/sassoftware/sas-mcp-server:latest
docker run -e VIYA_ENDPOINT=https://your-viya-server.com -p 8134:8134 ghcr.io/sassoftware/sas-mcp-server:latest
```

Or build locally from source:
```sh
docker build -t sas-mcp-server .
docker run -e VIYA_ENDPOINT=https://your-viya-server.com -p 8134:8134 sas-mcp-server
```

Available image tags:
- `latest` — most recent tagged release
- `<major>.<minor>.<patch>` (e.g. `1.0.0`) — specific release
- `<major>.<minor>` (e.g. `1.0`) — latest patch of a minor release
- `edge` — tip of `main` (unreleased, for testing)
- `sha-<short>` — pinned to a specific commit

**Programmatic clients with a pre-existing Viya token**

If your caller already holds a Viya access token (e.g. an automation script that obtained one via the SAS Viya CLI), start the HTTP-mode server with `ALLOW_RAW_BEARER=true` and pass the token directly:

```sh
curl -H "Authorization: Bearer $VIYA_TOKEN" http://localhost:8134/mcp ...
```

The server validates the token against Viya's JWKS and uses it upstream as-is, bypassing the MCP JWT swap. The default OAuth2 PKCE flow keeps working alongside — both client types share the same `/mcp` endpoint.

### Choosing a deployment mode

| | **HTTP** | **Stdio** | **Docker** |
|---|---|---|---|
| **How it runs** | Long-running server you start separately | MCP client spawns it on demand | Containerized HTTP server |
| **Authentication** | OAuth2 PKCE flow (browser popup) | Cached token via `sas-viya` CLI or `sas-mcp-login` | OAuth2 PKCE flow (browser popup) |
| **Best for** | Multi-user or shared setups; production-like environments | Single-user local development; quick experimentation | Team deployments; CI/CD; environments without Python installed |
| **Requires** | Python + uv | Python + uv (+ optional `sas-viya` CLI) | Docker or Podman only |
| **Credentials stored?** | No — user authenticates interactively | No — only an access token (not a password) is cached | No — user authenticates interactively |
| **MCP client config** | Point client to `http://localhost:8134/mcp` | Client runs `uv run app-stdio` | Point client to `http://host:8134/mcp` |

**Quick guidance:**
- **Starting out or exploring?** Use **stdio** — one `sas-viya auth loginCode` or `uv run sas-mcp-login`, then your MCP client manages the server lifecycle.
- **Need secure, interactive auth?** Use **HTTP** — no stored passwords, each user authenticates via browser.
- **Deploying for a team or on a server?** Use **Docker** — portable, no Python dependency on the host, easy to integrate with orchestrators.
- **Using Gemini CLI?** Use **stdio** — Gemini CLI does not support HTTP mode or browser-based OAuth. See [Gemini CLI configuration](examples/configuration.md#gemini-cli).

### Limiting exposed tools (tiers)

Tools are grouped into numbered tiers. By default the server exposes all of them; set `MCP_TIERS` to expose only a subset — handy for keeping a client's tool list small and focused, or hiding capabilities a deployment shouldn't offer. Accepts ranges and comma lists (e.g. `MCP_TIERS=0-4` or `MCP_TIERS=0,1,6,7`); unset means all tiers.

| Tier | Group |
|---|---|
| 0 | Compute Contexts & Code Execution |
| 1 | Data Discovery |
| 2 | Data Operations & Files |
| 3 | Reports & Visualization |
| 4 | Batch Jobs & Async Execution |
| 5 | Automated Machine Learning |
| 6 | Model Management & Scoring |
| 7 | Decisioning (SAS Intelligent Decisioning) |

```sh
# Example: expose only compute/discovery/data-ops and reporting
MCP_TIERS=0-3 uv run app
```

### Available Tools

The headings below match the numbered **tiers** above, so `MCP_TIERS` maps directly to the tools you expose (e.g. `MCP_TIERS=0-3` gives Tiers 0–3).

#### Tier 0 — Compute Contexts & Code Execution
- **execute_sas_code**: Execute SAS code snippets and retrieve execution results (log and listing output). Runs in a reusable, per-user compute session that is kept warm across calls, so SAS state (WORK tables, macro variables, assigned librefs) persists between successive calls — use **reset_compute_session** to start fresh.
- **list_compute_contexts**: List available compute contexts
- **reset_compute_session**: Delete the cached compute session for a context, discarding its SAS state and forcing a fresh session on the next call

#### Tier 1 — Data Discovery
*Information Catalog (metadata discovery & profiling):*
- **catalog_search**: Search the catalog for assets (tables, columns, reports, …) using the SAS catalog search grammar (free text, facets like `AssetType:Report`, ranges). Each hit carries a `resource_uri` you can hand to the matching tool (e.g. `get_report`, `get_castable_data`).
- **catalog_search_helper**: Discover how to query the catalog — list the available facets, or the valid values for one facet — so you can build precise `catalog_search` queries.
- **catalog_find_instance**: Resolve the catalog *instance* for a source-asset `resource_uri`, bridging a search hit to the profiling and download tools without handling an instance id by hand.
- **catalog_run_adhoc_analysis**: Submit an ad-hoc profiling job for a table. NLP enrichment (language, sentiment, semantic IDs) is on by default, populating `informationPrivacy`, `nlpTerms`, `nlpTags`, and `mostImportantFields`.
- **catalog_get_adhoc_analysis**: Poll a profiling job and cross-check the target instance, reporting `profile_ready` once results have landed on the asset — so a download isn't fired too early.
- **catalog_download_table_profile**: Download a table's data dictionary and column profile as CSV, identified by either `instance_id` or `resource_uri`.
- **catalog_list_agents**: List the catalog's discovery agents (the crawlers that populate metadata).
- **catalog_run_agent**: Start a discovery agent run (asynchronous) to crawl its data source and refresh catalog metadata.
- **catalog_get_agent_history**: Inspect an agent's run history — status and how much metadata each run enumerated/added/updated/removed.

*CAS data (in-memory):*
- **list_cas_servers**: List available CAS servers
- **list_caslibs**: List CAS libraries on a server
- **list_castables**: List tables in a CAS library
- **list_source_tables**: List source tables not yet loaded into memory (candidates for promotion)
- **get_castable_info**: Get table metadata (row count, columns, size)
- **get_castable_columns**: Get column names, types, labels, formats
- **get_castable_data**: Fetch sample rows from a CAS table

*Compute libraries (SAS/Compute, within a compute context):*
- **list_compute_libraries**: List the SAS libraries (librefs) assigned in a compute context
- **list_compute_tables**: List the tables in a SAS library within a compute context
- **list_compute_columns**: List the columns of a table in a SAS library

#### Tier 2 — Data Operations & Files
- **upload_data**: Upload a data file into a CAS table — read **server-side** so the data never passes through the model's context — from `file_path` (the server reads it off disk) or `url` (the server fetches it and converts it to the multipart upload the endpoint requires). Ingests the formats the casManagement `uploadTable` API accepts — csv, tsv (csv + tab delimiter), xls, xlsx (single sheet), sas7bdat, sashdat — auto-detected from the extension or set with `data_format`. parquet is not accepted by that endpoint and is rejected up front with guidance (load via a path-based caslib + `promote_table_to_memory`, or convert to csv/sas7bdat).
- **upload_inline_data**: Create a *small* CAS table from inline csv/tsv text passed as a string (a lookup/mapping table the model builds on the fly, or a quick test table). The payload travels through the model's context, so it's for tiny tables only — use **upload_data** for files or anything larger.
- **promote_table_to_memory**: Load a source table into memory at global scope (idempotent)
- **list_files**: List files in the Viya Files Service
- **upload_file**: Upload a file to Viya Files Service
- **download_file**: Download file content

#### Tier 3 — Reports & Visualization
- **list_reports**: List Visual Analytics reports
- **get_report**: Get report metadata and definition
- **export_report**: export a report (or specific report objects) in any format the VA service supports — `package` (zip), `pdf`, `png`, `svg`, `csv`, `tsv`, `xlsx`, or `summary`. Text formats come back inline, `png` as image content, and binary formats (`package`/`pdf`/`xlsx`) as an embedded file with the right MIME type.

#### Tier 4 — Batch Jobs & Async Execution
- **submit_batch_job**: Submit a SAS job for async execution
- **get_job_status**: Check job state
- **list_jobs**: List recent/running jobs
- **cancel_job**: Cancel a running job
- **get_job_log**: Retrieve job log

#### Tier 5 — Automated Machine Learning
- **list_ml_projects**: List AutoML projects
- **create_ml_project**: Create a new AutoML project from a loaded, global-scope CAS table (caslib + table + optional CAS server)
- **run_ml_project**: Run pipeline automation
- **register_ml_champion_model**: Register an AutoML project's champion model to the Model Repository
- **publish_ml_champion_model**: Publish an AutoML project's champion model to a scoring destination

#### Tier 6 — Model Management & Scoring
- **list_registered_models**: List models in repository
- **list_publishing_destinations**: List available scoring/publishing destinations, for use with **publish_ml_champion_model**
- **list_mas_modules**: List published MAS modules
- **get_mas_module_step_signature**: Inspect a published MAS module step's input/output variable signature before scoring
- **score_data**: Score data against a published model or decision

#### Tier 7 — Decisioning (SAS Intelligent Decisioning)
Build and manage SAS Intelligent Decisioning rule sets and decision flows end to end, then publish a flow to Micro Analytic Score (MAS) so **score_data** can execute it.

*Business rules — rule sets:*
- **create_business_ruleset** / **update_business_ruleset** / **get_business_ruleset** / **list_business_rulesets** / **delete_business_ruleset**: Manage rule sets (the input/output signature the rules operate on)
- **lock_business_ruleset_revision**: Lock the current rule set state as an immutable revision (what a decision step references)
- **list_business_ruleset_revisions**: List a rule set's locked revisions

*Business rules — rules:*
- **create_business_rule** / **update_business_rule** / **get_business_rule** / **list_business_rules** / **delete_business_rule**: Manage the conditional rules inside a rule set

*Decision flows:*
- **create_decision_flow** / **update_decision_flow** / **get_decision_flow** / **list_decision_flows** / **delete_decision_flow**: Manage decision flows that chain rule set steps
- **get_decision_flow_code**: Retrieve the generated DS2 execution code for a flow
- **lock_decision_flow_revision** / **list_decision_flow_revisions** / **get_decision_flow_revision**: Lock, list, and fetch immutable decision revisions
- **publish_decision_flow**: Publish a locked decision revision to a MAS destination, polling to completion and returning the server-generated MAS `moduleId` (directly usable with **get_mas_module_step_signature** / **score_data**)

### Prompt Templates

- **debug_sas_log**: Analyze SAS log for errors with root-cause explanations
- **explore_dataset**: Generate data-profiling SAS code
- **data_quality_check**: Generate DQ assessment code
- **statistical_analysis**: Set up a statistical workflow with diagnostics
- **optimize_sas_code**: Review and optimize SAS code
- **explain_sas_code**: Block-by-block code explanation
- **sas_macro_builder**: Build production-quality SAS macros
- **generate_report**: Generate ODS/PROC REPORT code

## MCP Client Configuration

Example configurations are provided in the `examples/` folder. Below are quick-start snippets for common clients.

### VS Code / Cursor / Claude Code (`.vscode/mcp.json`)

**HTTP mode** (requires `uv run app` running separately):
```json
{
    "servers": {
        "sas-execution-mcp": {
            "url": "http://localhost:8134/mcp",
            "type": "http"
        }
    }
}
```

**Stdio mode** (starts the server on demand):
```json
{
    "servers": {
        "sas-execution-mcp": {
            "command": "uv",
            "args": ["run", "app-stdio"],
            "cwd": "${workspaceFolder}"
        }
    }
}
```

### Gemini CLI (`.gemini/settings.json`)

Gemini CLI only supports stdio mode. Add to your `~/.gemini/settings.json` or project-level `.gemini/settings.json`:

```json
{
    "mcpServers": {
        "sas-viya-mcp": {
            "command": "uv",
            "args": ["run", "app-stdio"],
            "cwd": "/path/to/sas-mcp-server",
            "timeout": 60000
        }
    }
}
```

> **Note:** The `timeout` field (in milliseconds) is important — SAS Viya API calls can take longer than the Gemini CLI default of 10 seconds. A value of `60000` (60s) is recommended. Set `cwd` to the absolute path of your `sas-mcp-server` checkout.

## Example

Execute SAS code through the MCP tool:
```sas
data work.students;
input Name $ Age Grade $;
datalines;
Alice 20 A
Bob 22 B
;
run;

proc print data=work.students;
run;
```
---

**For more details, configuration options, and deployment options, please refer to the **examples** folder and follow the instructions listed there.**

## Collection Mode (Usage Telemetry)

An **opt-in**, **off-by-default** mode that records how the server is actually used — which tools, for what goals, with what inputs, and where they fall short. It serves two audiences:

- **Contributors giving structured feedback to the maintainers.** Rather than filing prose bug reports, you can turn it on for a while and share the resulting log so maintainers can see which tools are used, which fail, and what goals have no good tool yet — a direct signal for improving existing tools and identifying new ones.
- **Organizations running the server for their own users.** Teams that deploy the MCP server internally can enable it to understand what their users do with it and why, entirely within their own infrastructure.

It is implemented as a FastMCP middleware wrapper (`telemetry.py` + `usage_logger.py`) and requires **no changes to any tool**.

> 🔒 **Nothing is ever sent anywhere automatically.** Collection mode only appends to a local log file on the machine running the server. It is disabled unless you explicitly enable it, and even when enabled the data stays on your disk — sharing it with anyone (including the maintainers) is a deliberate, manual step you take by sending the file yourself. There is no phone-home, no network transmission, and no third party involved.

When enabled it does two things:

1. **Injects a required `goal` parameter** into every tool's schema, asking the model to state in one sentence *why* it chose that tool for the current
   request. The `goal` is stripped from the arguments before the real tool runs, so tools never see it.
2. **Appends one JSON line per tool call** (JSON Lines / NDJSON) to a local log file: timestamp, session id, tool name, goal, arguments, result, status,
   error, and latency. Secret-shaped keys and inline Bearer/JWT tokens are redacted and every field is size-capped.

### Enabling it

Set the toggle in `.env` (all options are documented in `.env.sample`):

```sh
COLLECTION_MODE=true
# optional overrides (defaults shown):
# COLLECTION_LOG_PATH=~/.sas-mcp-server/tool-usage.log
# COLLECTION_LOG_RESULTS=false   # false = record result shape only, not contents
```

By default (`COLLECTION_LOG_RESULTS=false`) tool **results** are recorded only as a content-free shape summary (e.g. `{"_type":"array","_items":500}`) — arguments, goal, status, and error text are still captured. Set it to `true` to capture (capped + redacted) result contents for richer analysis.

> ⚠️ **Privacy:** when enabled, the log captures your tool inputs (e.g. the SAS code and queries you submit) and — if `COLLECTION_LOG_RESULTS=true` — real result data that may include table rows, SAS listings, and PII. Redaction is heuristic (credential-shaped keys + Bearer/JWT only) and does **not** detect PII in data values. **Review the log before sharing it.** The file is locked to your user (chmod 0600 on POSIX; icacls on Windows, best-effort).

### Performance impact

Collection mode is designed to be cheap enough to leave on. Measured on this repo (45 registered tools, FastMCP 3.4.2):

- **Prompt tokens.** The injected `goal` field grows the `tools/list` schema the model sees by roughly **+2,400 input tokens (~29%) per turn**. Because the tool list is stable within a session it is served from the prompt cache after the first turn (steady-state ≈ +240 tokens/turn), plus ~15–30 output tokens per call for the model to write the `goal` sentence. This is the only client-visible cost and it applies only while collection mode is enabled. 
- **Per-call latency.** Middleware + logging adds **≈1.4 ms per call** at the shape-only default (**≈5.3 ms** with `COLLECTION_LOG_RESULTS=true`). The JSONL
  write is offloaded to a worker thread so it never blocks the event loop. Against real Viya calls (typically hundreds of milliseconds to seconds) this is
  negligible — the live integration suite passed identically with collection mode off and on, the overhead lost in normal network variance.
- **Disk.** Roughly **0.5–0.7 KB per tool call** at the shape-only default. The log rotates at `COLLECTION_MAX_LOG_BYTES` (default 10 MiB, ≈16k calls) and keeps `COLLECTION_LOG_BACKUPS` (default 3) rotated files, so on-disk growth is bounded.

## Testing

The project includes two layers of tests: **unit tests** (fast, no credentials required) and **integration tests** (run against a real SAS Viya instance).

> **`run_tests.sh` vs. running `pytest` directly — pick by platform.** `run_tests.sh` is a
> Bash convenience wrapper (it adds the ruff + pyright gates, credential wiring, and JUnit
> reporting). It runs on **Linux/macOS** — and on Windows only under **Git Bash or WSL**. On
> **Windows PowerShell or `cmd`**, use the **`uv run python -m pytest …`** commands shown
> under each mode below. They are cross-platform, do the same test selection, and need no
> setup beyond `uv sync`.

### Running Unit Tests

Unit tests verify tool schemas, request payloads, and internal logic without making any network calls:

```sh
./run_tests.sh                                     # Linux/macOS (also runs ruff + pyright)
uv run python -m pytest -m "not integration" -v    # any platform, incl. Windows PowerShell
```

This runs the unit suite and **deselects the integration tests**, which then show up in the
summary as e.g. `27 deselected`. That is expected — those tests are *not* meant to run in a
unit-only pass. They only execute in the integration modes below, because they need a live
Viya instance; there is no flag that "activates" them in a `not integration` run.

### Running Integration Tests

Integration tests call every tool against a live Viya environment. They require credentials, provided via `.env` or CLI arguments.

For the **full** integration run (including the Excel `upload_data` test), install the
optional format dependency first — plain `uv sync` does **not** include it:

```sh
uv sync --group test-formats
```

**Full suite (unit + integration)** — reads `VIYA_ENDPOINT`, `VIYA_USERNAME`, `VIYA_PASSWORD` from `.env`:
```sh
./run_tests.sh --integration      # Linux/macOS
uv run python -m pytest -v        # any platform
```

**Passing credentials on the command line** (wrapper only):
```sh
./run_tests.sh --integration \
    --endpoint https://your-viya-server.com \
    --username youruser \
    --password yourpassword
```
With the direct `pytest` command, set the same three variables in `.env` (or export them in your shell) instead.

**Integration tests only** (skip unit tests):
```sh
./run_tests.sh --integration-only                    # Linux/macOS
uv run python -m pytest -m integration --no-cov -v   # any platform
```

> **The pytest marker is `integration`, not `integration-only`.** `--integration-only` is a
> flag of the `run_tests.sh` *wrapper*; the underlying pytest marker is just `integration`.
> Running `pytest -m "integration-only"` matches no marker and silently deselects **all**
> tests (`0 selected`). Use `-m integration`.
>
> **Why `--no-cov`?** `pytest.ini` enforces a 90% coverage floor that only the *full* unit
> suite reaches. An integration-only run exercises far less code (~65%), so without
> `--no-cov` pytest exits non-zero with a **coverage** failure even though every selected
> test passed. `run_tests.sh --integration-only` adds `--no-cov` for you; add it yourself
> when calling pytest directly (or use `--cov-fail-under=0`).

**Binary upload formats.** The Excel `upload_data` integration test generates its `.xlsx`
fixture with `openpyxl`, which lives in the optional `test-formats` group installed above.
Without that group the test `importorskip`s (you'll see it as *skipped*, not failed). csv,
tsv, and `file_path`/`data_format` coverage needs no extra deps. Generating a
`sas7bdat`/`sashdat` fixture requires SAS itself, so those two formats are covered by
unit-level payload tests only, not live.

Every one of the 68 tools and 8 prompt templates has an integration test, enforced by the
`test_every_tool_has_integration_coverage` / `test_every_prompt_has_integration_coverage`
guards — adding a new tool or prompt without integration coverage fails the suite. The
resource-dependent tests discover real targets on the instance: `score_data` scores the most
recently modified MAS module (discovering a real step and its inputs), and `run_ml_project`
re-runs the most recently modified `completed` ML project. They `skip` only if the instance
has no such resource at all. Likewise, `test_catalog_agents_workflow` `skip`s with *"No
discovery agent named 'Public'"* on instances where SAS Information Catalog has no discovery
agent named `Public` configured — an expected skip, not a failure; ask a Viya admin to
configure one if you need that test to run.

**In CI:** the `.github/workflows/integration.yml` workflow runs this suite on demand
(manual dispatch, or by adding the `run-integration` label to a PR) using repository
secrets, and publishes the results back to the PR as a status check, a sticky comment, and
a downloadable JUnit artifact. Result files are written to `reports/` (git-ignored) and are
never committed.

**Locally (attach results to a PR yourself):** run with `--report` to write the JUnit XML
and a Markdown summary into `reports/` (git-ignored), then post them to a PR with the GitHub
CLI — no commit, no CI required:

```sh
./run_tests.sh --integration-only --report
gh pr comment <PR> --body-file reports/integration-summary.md   # summary table as a comment
gh gist create reports/integration.xml                          # full XML as a linkable gist
```

> GitHub has no API/CLI to attach a binary file to a PR (drag-and-drop upload is browser-only),
> so the summary is posted as a comment and the raw XML is shared via a gist link or pasted in a
> collapsed `<details>` block. To produce the canonical Actions *artifact* from your machine
> instead, trigger the workflow remotely: `gh workflow run integration.yml`.

### Test Structure

| File | Description |
|---|---|
| `tests/test_tool_payloads.py` | Payload assertions for all 68 tools (URL paths, JSON body, query params, headers) plus error-path coverage |
| `tests/test_integration.py` | End-to-end workflow tests against a real Viya instance |
| `tests/test_tools.py` | Unit tests for the generic Viya REST helpers in `viya_client` (`get_json`, `post_json`, `make_client`, …) |
| `tests/test_viya_utils.py` | Unit tests for Viya compute session and job orchestration |
| `tests/test_mcp_server.py` | Unit tests for the HTTP auth middleware, health route, and token getter |
| `tests/test_config.py` | Unit tests for configuration loading |
| `tests/test_config_oauth.py` | Unit tests for `PermissiveOAuthProxy` raw-bearer handling |
| `tests/test_auth_login.py` | Unit tests for the `sas-mcp-login` OAuth/PKCE helper |
| `tests/test_stdio_server.py` | Unit tests for stdio token resolution and the device-code flow |
| `tests/test_env.py` | Unit tests for the `env_bool` helper |
| `tests/test_prompts.py` | Unit tests for prompt template rendering |

## Contributing
Maintainers are accepting patches and contributions to this project. Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details about submitting contributions to this project.

## License & Attribution

Except for the the contents of the `/static` folder, this project is licensed under the [Apache 2.0 License](LICENSE).
Elements in the `/static` folder are owned by SAS and are not released under an open source license.
SAS and all other SAS Institute Inc. product or service names are registered trademarks or trademarks of SAS Institute Inc. in the USA and other countries. ® indicates USA registration.

Separate commercial licenses for SAS software (e.g., SAS Viya) are not included and are required to use these capabilities with SAS software.

As with any container image, direct and indirect dependencies are governed by their own licenses.
Users of the published container image are responsible for ensuring that their use complies with all applicable licenses.

All third-party trademarks referenced belong to their respective owners and are only used here for identification and reference purposes, and not to imply any affiliation or endorsement by the trademark owners.

## Third-Party Dependencies

This project requires the following dependencies.

| Dependency | License |
| ---------- | ------- |
| Python | [Python Software License](https://docs.python.org/3/license.html) |
| FastMCP | [Apache License 2.0](https://github.com/PrefectHQ/fastmcp/blob/main/LICENSE) |
| uvicorn | [BSD 3-Clause License](https://github.com/Kludex/uvicorn/blob/main/LICENSE.md) |
| starlette | [BSD 3-Clause License](https://github.com/Kludex/starlette/blob/main/LICENSE.md)
| httpx | [MIT License](https://github.com/projectdiscovery/httpx/blob/dev/LICENSE.md) |
