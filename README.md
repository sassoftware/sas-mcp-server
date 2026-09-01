# SAS Viya MCP Server

A Model Context Protocol (MCP) server for executing SAS code, training AutoML projects, scoring models and so much more for SAS Viya environments.

## Features

- 89 tools across 11 selectable tiers, spanning the Analytics Life Cycle on SAS Viya
- Prompt Templates for improving your SAS Code
- OAuth2 authentication with PKCE flow
- HTTP-based MCP server compatible with MCP clients

## Articles & Videos

Here you can find getting articles on how to use and integrate the SAS MCP Server in different tools and what to build with it:

- [From REST APIs to AI Agents: Why the SAS Viya MCP Server Matters](https://communities.sas.com/t5/SAS-Communities-Library/From-REST-APIs-to-AI-Agents-Why-the-SAS-Viya-MCP-Server-Matters/ta-p/992010)
- [Connecting GitHub Copilot to SAS Viya with the SAS Viya MCP Server](https://communities.sas.com/t5/SAS-Communities-Library/Connecting-GitHub-Copilot-to-SAS-Viya-with-the-SAS-Viya-MCP/ta-p/987191)
- [Bring Your Own Key: SAS Viya MCP Server with GitHub Copilot CLI](https://communities.sas.com/t5/SAS-Communities-Library/Bring-Your-Own-Key-SAS-Viya-MCP-with-GitHub-Copilot-CLI/ta-p/991530)
- [Putting the SAS Viya MCP Server to Work in GitHub Copilot](https://communities.sas.com/t5/SAS-Communities-Library/Putting-the-SAS-Viya-MCP-Server-to-Work-in-GitHub-Copilot/ta-p/987193)
- [Connecting Claude Code CLI to SAS Viya with the SAS Viya MCP Server](https://communities.sas.com/t5/SAS-Communities-Library/Connecting-Claude-Code-CLI-to-SAS-Viya-with-the-SAS-Viya-MCP/ta-p/988775)
- [Putting the SAS Viya MCP Server to Work in Claude Code CLI](https://communities.sas.com/t5/SAS-Communities-Library/Putting-the-SAS-Viya-MCP-Server-to-Work-in-Claude-Code-CLI/ta-p/988922)
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
    - [Docker](https://docs.docker.com/engine/install): refer to [container setup](/deploy/docker.md)
    - Kubernetes: sample manifests (Contour or nginx) and a Helm chart in [deploy/](/deploy/README.md)

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

If your Viya APIs are intentionally exposed without auth (for example, a local/dev Compute API endpoint), set `VIYA_AUTH=false` to bypass all SASLogon/OAuth flows in both HTTP and stdio modes. In this mode the server sends upstream requests without an `Authorization` header.

If your compute deployment does not expose `/compute/contexts` and only supports a fixed session, set `COMPUTE_SESSION_ID=<session_id>`. The compute tools will use that session directly instead of creating context-backed sessions.

### Choosing a deployment mode

| | **HTTP** | **Stdio** | **Docker** | **Kubernetes** |
|---|---|---|---|---|
| **How it runs** | Long-running server you start separately | MCP client spawns it on demand | Containerized HTTP server | Containerized, behind an ingress |
| **Authentication** | OAuth2 PKCE flow (browser popup) | Cached token via `sas-viya` CLI or `sas-mcp-login` | OAuth2 PKCE flow (browser popup) | PKCE and/or raw Viya bearer token |
| **Best for** | Multi-user or shared setups; production-like environments | Single-user local development; quick experimentation | Team deployments; CI/CD; environments without Python installed | Shared/organisational deployments alongside Viya |
| **Requires** | Python + uv | Python + uv (+ optional `sas-viya` CLI) | Docker or Podman only | A cluster, an ingress controller, a TLS secret |
| **Credentials stored?** | No — user authenticates interactively | No — only an access token (not a password) is cached | No — user authenticates interactively | No — a signing key in a `Secret`; users authenticate themselves |
| **MCP client config** | Point client to `http://localhost:8134/mcp` | Client runs `uv run app-stdio` | Point client to `http://host:8134/mcp` | Point client to `https://<viya-host>/mcp` |

**Quick guidance:**
- **Starting out or exploring?** Use **stdio** — one `sas-viya auth loginCode` or `uv run sas-mcp-login`, then your MCP client manages the server lifecycle.
- **Need secure, interactive auth?** Use **HTTP** — no stored passwords, each user authenticates via browser.
- **Deploying for a team or on a server?** Use **Docker** — portable, no Python dependency on the host, easy to integrate with orchestrators.
- **Running it for a whole organisation?** Use **Kubernetes** — sample manifests and a Helm chart are in [deploy/](/deploy/README.md), including the routing the OAuth flow needs for either **Contour** (the chart's default, and the only one that can mount the server under a path prefix on an existing hostname) or **ingress-nginx**.
- **Using Gemini CLI?** Use **stdio** — Gemini CLI does not support HTTP mode or browser-based OAuth. See [Gemini CLI configuration](examples/configuration.md#gemini-cli).
- **Installing from a client's server catalogue?** That path runs the published container in **stdio** mode (`app-stdio`), not as an HTTP server, so it authenticates from your `~/.sas` token cache — which has to be mounted into the container at `/app/.sas`.

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
| 8 | Workbench (Execute Code Only) |
| 9 | Business Glossary (SAS Data Governance) |
| 10 | Code Assistance & Documentation (SAS Code Assistant) |

```sh
# Example: expose only compute/discovery/data-ops and reporting
MCP_TIERS=0-3 uv run app
```

### Read-only mode

Set `MCP_READ_ONLY=true` to expose only tools that neither change server-side state nor cause server-side work — 52 of the 89 tools. Withheld tools are never registered, so they are absent from the client's tool list entirely: the model cannot see them, so it cannot attempt them.

This is a filter over the tiers, not a tier of its own — the read/write split cuts across every tier (Tier 3 has both `get_report` and `delete_report`). The two settings compose:

```sh
# Every read tool, all tiers
MCP_READ_ONLY=true uv run app

# Read tools of the reporting and decisioning tiers only
MCP_TIERS=3,7 MCP_READ_ONLY=true uv run app
```

The definition is strict: a tool qualifies only if it can neither write nor start work. Beyond the obvious create/update/delete tools, that withholds:

| Withheld | Why |
|---|---|
| `execute_sas_code`, `submit_batch_job` | Run arbitrary code — can perform any operation, including deletes |
| `score_data`, `catalog_run_agent`, `catalog_run_adhoc_analysis` | Start server-side jobs and leave run records, though they return data |
| `promote_table_to_memory` | Mutates CAS in-memory state |
| `cancel_job`, `reset_compute_session` | Destroy something the caller owns |

Classification is fail-closed: a tool that is not explicitly classified as read-only is withheld. The list lives in [`src/sas_mcp_server/tools/_access.py`](src/sas_mcp_server/tools/_access.py), and a test asserts it covers every registered tool, so a newly added tool cannot silently land in read-only mode.

### Tool annotations (what clients are told)

The same classification is **advertised** to every client as [MCP tool annotations](https://modelcontextprotocol.io/specification/2025-03-26/server/tools#tool-annotations) on each `tools/list` entry — whether or not read-only mode is on:

| Hint | Derived from |
|---|---|
| `readOnlyHint` | exactly the read-only set above — one table, so what a client is told and what `MCP_READ_ONLY` enforces cannot drift |
| `destructiveHint` | tools that can remove or overwrite existing state: arbitrary code (`execute_sas_code`, `submit_batch_job`), `delete_*`, `cancel_job`, `reset_compute_session`, the `update_*` PUTs, `apply_report_operations`, `create_report`/`copy_report` (their `replace` conflict policy), `publish_ml_champion_model` |
| `idempotentHint` | reads, the `update_*` PUTs, deletes, `cancel_job`, `reset_compute_session`, `promote_table_to_memory` |
| `openWorldHint` | only tools that can reach beyond Viya: arbitrary code and the upload tools' `url` source |

Clients use these to shape their approval UX — e.g. Claude groups read-only tools for one-click approval and warns before destructive ones — and to decide when to interrupt the user. They are hints, not enforcement: the spec tells clients to treat them as untrusted unless the server is trusted, and `MCP_READ_ONLY` remains the server-side control. Without annotations a client must assume the spec's pessimistic defaults (writable, destructive, open-world) for every tool, so this only ever reduces friction. The browser landing page marks each tool `read-only` / `write` / `destructive` from the same hints.

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
- **query_data**: Run a FedSQL `SELECT` against CAS or compute data and get the rows back — one SQL surface over both storage tiers. Pick the tier with `target` (`cas` for `caslib.table`, `compute` for `libref.table`); joins, subqueries, aggregation, and `UNION` all work, and the row cap is applied server-side by the tool (a `LIMIT` you write is ignored, since a malformed one is silently discarded by CAS). Optionally returns the query as `CREATE VIEW` text for you to run yourself. Reads only: writes are refused pre-flight, and SAS macro triggers (`%`/`&`) are rejected because the macro processor would expand them outside SQL. Note the two tiers cannot be joined in one statement.

*Compute libraries (SAS/Compute, within a compute context):*
- **list_compute_libraries**: List the SAS libraries (librefs) assigned in a compute context
- **list_compute_tables**: List the tables in a SAS library within a compute context
- **list_compute_columns**: List the columns of a table in a SAS library

#### Tier 2 — Data Operations & Files
- **upload_data**: Upload a data file into a CAS table — read **server-side** so the data never passes through the model's context — from `file_path` (the server reads it off disk) or `url` (the server fetches it and converts it to the multipart upload the endpoint requires). Ingests the formats the casManagement `uploadTable` API accepts — csv, tsv (csv + tab delimiter), xls, xlsx (single sheet), sas7bdat, sashdat — auto-detected from the extension or set with `data_format`. parquet is not accepted by that endpoint and is rejected up front with guidance (load via a path-based caslib + `promote_table_to_memory`, or convert to csv/sas7bdat).
- **upload_inline_data**: Create a *small* CAS table from inline csv/tsv text passed as a string (a lookup/mapping table the model builds on the fly, or a quick test table). The payload travels through the model's context, so it's for tiny tables only — use **upload_data** for files or anything larger.
- **promote_table_to_memory**: Load a source table into memory at global scope (idempotent)
- **list_files**: List files in the Viya Files Service
- **upload_file**: Upload a file to the Viya Files Service, optionally into a Content folder (`parent_folder_uri`). Content comes from exactly one of `content` (inline text), `file_path` (read **server-side**, binary-safe — xlsx, zip, images — gated by `ALLOW_LOCAL_FILE_UPLOAD`), or `url` (server-side fetch)
- **download_file**: Download file content

#### Tier 3 — Reports & Visualization
- **list_reports**: List Visual Analytics reports
- **get_report**: Get report metadata and definition
- **export_report**: export a report (or specific report objects) in any format the VA service supports — `package` (zip), `pdf`, `png`, `svg`, `csv`, `tsv`, `xlsx`, or `summary`. Text formats come back inline, `png` as image content, and binary formats (`package`/`pdf`/`xlsx`) as an embedded file with the right MIME type.
- **describe_report_objects**: Discover what a report can contain — the eight report operations and every addable object (bar chart, list table, geo map, key value, …) with a one-line purpose, its data roles, common options, and an example payload. Call with no arguments for the catalog (including an intent→object map, placement guide, layout recipes, and the API's hard limits), `object_type=` for one object's contract (colloquial aliases like `kpi` resolve), `category=` to filter, or `operation=` for one operation's full shape — `operation="addData"` documents `dataItems` (column renames, SAS formats, aggregations, geography classification). Backs the `apply_report_operations` loop.
- **create_report**: Create a Visual Analytics report and return its id. Optionally pass an `operations` array to build the whole report in one atomic call; the result carries the created page/object names+labels and a verify hint.
- **apply_report_operations**: The authoring workhorse — apply an ordered batch of native VA operations (`addData`, `addPage`, `addObject`, `updateObject`, `setParameterValue`, `updateData`, `changeData`, `applyDataView`) to a report. Give a page a **title** with `addPage`'s `title` field (a text band at the top of the page body — VA headers are controls-only); title every chart at add time via `options.object.title`; arrange objects with **placement** — `page`, `relativeToObject` (left/right/top/bottom for columns, rows, and grids), `container` (group into a `standardContainer`), or `report` (`new_page` creates-and-names a page inline for one-batch multi-page reports). The batch is atomic. Validates every operation, object key, and placement against the catalog first (reporting all errors at once), supports `dry_run`, handles the ETag concurrency handshake, and — with `result_report_name`/`result_folder` — applies the batch **save-as** to a new report, leaving the source untouched. Typical loop: `describe_report_objects` → `get_castable_columns` → `apply_report_operations` → `get_report_outline` / `export_report` (png, page-by-page) to verify.
- **get_report_outline**: Read a report's structure back — pages → objects with the handles the other tools need (object `name` for placement/`updateObject` targets, `label` for `export_report`, page `label` for page placement).
- **copy_report**: Copy a report to a new one (optionally renaming/refoldering). Pairs with a `changeData` operation for the copy-and-replace pattern.
- **delete_report**: Delete a report and its content.

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

#### Tier 8 — Workbench (Execute Code Only)
- **execute_sas_code**: Execute SAS code snippets and retrieve execution results (log and listing output). Runs in a reusable compute session that is kept warm across calls, so SAS state (WORK tables, macro variables, assigned librefs) persists between successive calls

#### Tier 9 — Business Glossary (SAS Data Governance)

Read and author the SAS Business Glossary, and link its terms to the columns they describe. Tier 1 tells you a column is called `CD_NAC_RSK`; this tier tells you what that means and who says so.

Two things about the glossary are worth knowing before you start, because both are invisible in the raw API and both are handled for you here:

- **A term has two ids.** It exists as a Glossary object *and* as a Catalog entity, with different identifiers. Every tool returns both — `term_id` (glossary) and `catalog_entity_id` (catalog) — so you never have to work out which one you are holding.
- **Custom attributes are stored under UUID keys.** These tools read and write them by the **label** the glossary UI shows (`{"Scope": "Group"}`), validating required attributes and single-select values before the call is made.

*Dictionary:*
- **search_glossary_terms**: Free-text, ranked search over term names and definitions — the way in when you know a word rather than an id. Reports `assigned_asset_count`, so you can see whether a term is actually in use
- **list_glossary_terms**: Exact structural listing — by term type, by parent (the authoritative hierarchy), or by name fragment
- **get_glossary_term**: One term in full, with its custom attributes named rather than hashed
- **list_glossary_term_types** / **get_glossary_term_type**: The term types available, and the attribute contract a term of that type must satisfy — call the latter before authoring

*Where terms meet data:*
- **list_term_assets**: The columns a term is attached to, with their tables. The authoritative answer to "where is this term used?"
- **list_table_terms**: The reverse — every column of a table and the term assigned to it, with the term's definition inline. The fastest read on whether a table is governed

*Authoring:*
- **create_glossary_term**: Create a term. **Publishes by default** — the underlying API creates an invisible draft unless told otherwise
- **update_glossary_term**: Change a term's text or attributes. Merges onto the current term, so omitted fields are left alone rather than blanked
- **delete_glossary_term**: Permanently delete a term and every assignment that referenced it
- **assign_glossary_term** / **unassign_glossary_term**: Attach a term to a table column, or detach it. This is the step that makes a term govern data — a term with no assigned assets governs nothing

Terms assigned this way also become searchable through Tier 1's **catalog_search** using the `Column.term:"<term name>"` facet on the `datasets` index, which returns the tables carrying a term without resolving individual columns.

#### Tier 10 — Code Assistance & Documentation (SAS Code Assistant)
Tier 10 calls the SAS Code Assistant copilot through Viya's own REST API, using
the authenticated user's Viya bearer token — no separate GenAI/LLM API key or
RAG URL is required. The server calls
`<VIYA_ENDPOINT>/genAiGateway/v1/copilotRequest`; Viya owns model selection and
routes code or documentation requests internally, so the tier needs the GenAI
Gateway provisioned on the instance.

Both tools are read-only — they return text and change nothing on the server —
so both survive `MCP_READ_ONLY=true`. Tier 10 intentionally does **not** add a
code-execution tool; use Tier 0 or Tier 8 `execute_sas_code` when execution is
required.

- **get_doc_answer**: Answer a SAS documentation question from the Code Assistant knowledge base. Optionally narrow the search with `product`
- **generate_sas_code**: Generate code from natural-language requirements. `language` defaults to `sas` (`python` and `r` are also accepted); `use_rag_for_sas` grounds SAS generation in the documentation

### Prompt Templates

- **debug_sas_log**: Analyze SAS log for errors with root-cause explanations
- **explore_dataset**: Generate data-profiling SAS code
- **data_quality_check**: Generate DQ assessment code
- **statistical_analysis**: Set up a statistical workflow with diagnostics
- **optimize_sas_code**: Review and optimize SAS code
- **explain_sas_code**: Block-by-block code explanation
- **sas_macro_builder**: Build production-quality SAS macros
- **generate_report**: Generate ODS/PROC REPORT code
- **build_va_dashboard**: Guide a polished multi-page Visual Analytics dashboard build from a CAS table — a discover → shape → structure → polish → verify method over the report-authoring tools

## MCP Client Configuration

Example configurations are provided in the `examples/` folder. Below are quick-start snippets for common clients.

> **Tip — open the endpoint in a browser.** In HTTP mode, pointing a browser at the MCP URL (e.g. `http://localhost:8134/mcp`, or `https://<host>/mcp` for a deployed server) shows a landing page instead of a bare `401`: what the server is, which SAS Viya it talks to, the tool tiers this deployment exposes with a one-line summary per tool, and ready-to-copy configuration for Claude Code, VS Code, Cursor, Claude connectors and generic `mcp.json` clients — with the deployment's real URL already filled in. Only a plain browser `GET` (`Accept: text/html`) is answered this way; MCP clients and `curl` see exactly what they saw before. The page is unauthenticated and shows deployment shape only (never user data); administrators can turn it off with `MCP_LANDING_PAGE=false`.

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
2. **Appends one JSON line per tool call** (JSON Lines / NDJSON, schema v3) to a local log file: timestamp, run id, per-run sequence number, tool
   name, goal, arguments (plus a stable `args_hash` for retry analysis), result, status, error, latency, and the calling client's
   `client_name` / `client_version`. When a tool *declares* a failure as data
   (e.g. `{"status": "apply_failed"}`, which the MCP layer sees as success), the record also carries `tool_status` / `is_tool_error` /
   `tool_message` / `failed_operation_index` — so tool-level failure rates are analyzable in every mode. A `run_start` header record
   (transport, pid, server version, result mode, and an optional `COLLECTION_RUN_TAG` label for tagging A/B runs) opens the log and is
   **re-emitted every 1000 records**, so rotation cannot leave a stretch of the log with no header to resolve; every emission is
   byte-identical, so any one of them will do. Secret-shaped keys and inline Bearer/JWT tokens are redacted, the Viya hostname is masked in
   error/result text, and every field is size-capped.

   **Records group by `run_id` — one per server process — not by MCP session.** The protocol is moving to a *sessionless* model (FastMCP 4 makes
   it the default) in which `session_id` is absent or minted per request, so grouping on it would shatter every trace into single-call fragments.
   Under stdio, one process serves one client, so a run *is* that client's trace. Under HTTP a run spans every client the process served, and
   `client_name`/`client_version` are the only thing separating them — **two users on the same client software share one `run_id` and one `seq`
   counter**, which is an accepted limitation of dropping the session key, not something a per-process `COLLECTION_LOG_PATH` can fix (that splits
   by process, the axis `run_id` already covers).

### Enabling it

Set the toggle in `.env` (all options are documented in `.env.sample`):

```sh
COLLECTION_MODE=true
# optional overrides (defaults shown):
# COLLECTION_LOG_PATH=~/.sas-mcp-server/tool-usage.log
# COLLECTION_LOG_RESULTS=failures  # never | failures | always (see below)
# COLLECTION_RUN_TAG=            # free-text label stamped into run_start
```

Tool **results** are recorded per `COLLECTION_LOG_RESULTS` — a tri-state dial: `never` records only a content-free shape summary (type + key
names, e.g. `{"_type":"object","_keys":["status","report_id"]}`); `failures` (**the default**) records full (capped + redacted) result contents **only** for calls that
errored or whose tool declared a failure — the middle ground, since failure diagnostics are the highest-value trace data and rarely carry
table rows, and because under `never` a success and a tool-declared failure are indistinguishable in the log; `always` records result contents on every call. Arguments, goal, status, error text, and the tool-declared outcome fields are captured in
every mode. (`true`/`false` still work as aliases for `always`/`never`.)

> ⚠️ **Privacy:** when enabled, the log captures your tool inputs (e.g. the SAS code and queries you submit) and — in `failures`/`always` modes — real
> result data that may include table rows, SAS listings, and PII. Redaction is heuristic (credential-shaped keys + Bearer/JWT + the Viya hostname) and
> does **not** detect PII in data values. **Review the log before sharing it.** The file is locked to your user (chmod 0600 on POSIX; icacls on
> Windows, best-effort).

### Performance impact

Collection mode is designed to be cheap enough to leave on. Measured on this repo (45 registered tools, FastMCP 3.4.2):

- **Prompt tokens.** The injected `goal` field grows the `tools/list` schema the model sees by roughly **+2,400 input tokens (~29%) per turn**. Because the tool list is stable within a session it is served from the prompt cache after the first turn (steady-state ≈ +240 tokens/turn), plus ~15–30 output tokens per call for the model to write the `goal` sentence. This is the only client-visible cost and it applies only while collection mode is enabled. 
- **Per-call latency.** Middleware + logging adds **≈1.4 ms per call** at the shape-only default (**≈5.3 ms** with `COLLECTION_LOG_RESULTS=always`). The JSONL
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
summary as e.g. `28 deselected`. That is expected — those tests are *not* meant to run in a
unit-only pass. They only execute in the integration modes below, because they need a live
Viya instance; there is no flag that "activates" them in a `not integration` run.

### Running Integration Tests

Integration tests call every tool against a live Viya environment. They require credentials, provided via `.env` or CLI arguments.

`uv sync` installs everything the integration suite needs, including `openpyxl` (used to
build the Excel `upload_data` fixture). It lives in the `test-formats` dependency group,
which `[tool.uv] default-groups` syncs by default — so no extra install step is required.

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
fixture with `openpyxl`, from the `test-formats` group that `uv sync` installs by default
(see above). If you deliberately sync without it (e.g. `uv sync --no-default-groups`), the
test `importorskip`s — you'll see it as *skipped*, not failed. csv,
tsv, and `file_path`/`data_format` coverage needs no extra deps. Generating a
`sas7bdat`/`sashdat` fixture requires SAS itself, so those two formats are covered by
unit-level payload tests only, not live.

Every one of the 89 tools and 9 prompt templates has an integration test, enforced by the
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
| `tests/test_tool_payloads.py` | Payload assertions for all 75 Tier 0-8 tools (URL paths, JSON body, query params, headers) plus error-path coverage |
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
