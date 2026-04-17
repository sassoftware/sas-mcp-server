# SAS MCP Server

A Model Context Protocol (MCP) server for executing SAS code on SAS Viya environments.

## Features

- Execute SAS code on SAS Viya compute contexts
- OAuth2 authentication with PKCE flow
- HTTP-based MCP server compatible with MCP clients

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

Set `VIYA_USERNAME` and `VIYA_PASSWORD` in your `.env` file, then configure your MCP client to launch the server directly (see below).

**Option C: Docker / Podman** (containerized deployment)
```sh
docker build -t sas-mcp-server .
docker run -e VIYA_ENDPOINT=https://your-viya-server.com -p 8134:8134 sas-mcp-server
```

### Choosing a deployment mode

| | **HTTP** | **Stdio** | **Docker** |
|---|---|---|---|
| **How it runs** | Long-running server you start separately | MCP client spawns it on demand | Containerized HTTP server |
| **Authentication** | OAuth2 PKCE flow (browser popup) | Password grant (credentials in `.env`) | OAuth2 PKCE flow (browser popup) |
| **Best for** | Multi-user or shared setups; production-like environments | Single-user local development; quick experimentation | Team deployments; CI/CD; environments without Python installed |
| **Requires** | Python + uv | Python + uv | Docker or Podman only |
| **Credentials stored?** | No — user authenticates interactively | Yes — username/password in `.env` | No — user authenticates interactively |
| **MCP client config** | Point client to `http://localhost:8134/mcp` | Client runs `uv run app-stdio` | Point client to `http://host:8134/mcp` |

**Quick guidance:**
- **Starting out or exploring?** Use **stdio** — zero setup beyond `.env`, and your MCP client manages the server lifecycle.
- **Need secure, interactive auth?** Use **HTTP** — no stored passwords, each user authenticates via browser.
- **Deploying for a team or on a server?** Use **Docker** — portable, no Python dependency on the host, easy to integrate with orchestrators.
- **Using Gemini CLI?** Use **stdio** — Gemini CLI does not support HTTP mode or browser-based OAuth. See [Gemini CLI configuration](examples/configuration.md#gemini-cli).

### Available Tools

#### Code Execution
- **execute_sas_code**: Execute SAS code snippets and retrieve execution results (log and listing output)

#### Data Discovery (CAS Management)
- **list_cas_servers**: List available CAS servers
- **list_caslibs**: List CAS libraries on a server
- **list_castables**: List tables in a CAS library
- **get_castable_info**: Get table metadata (row count, columns, size)
- **get_castable_columns**: Get column names, types, labels, formats
- **get_castable_data**: Fetch sample rows from a CAS table

#### Data Operations & Files
- **upload_data**: Upload CSV data into a CAS table
- **promote_table_to_memory**: Promote a table to global scope in CAS
- **list_files**: List files in the Viya Files Service
- **upload_file**: Upload a file to Viya Files Service
- **download_file**: Download file content

#### Reports & Visualization
- **list_reports**: List Visual Analytics reports
- **get_report**: Get report metadata and definition
- **get_report_image**: Render a report section as an image

#### Batch Jobs
- **submit_batch_job**: Submit a SAS job for async execution
- **get_job_status**: Check job state
- **list_jobs**: List recent/running jobs
- **cancel_job**: Cancel a running job
- **get_job_log**: Retrieve job log

#### Model Management & Scoring
- **list_ml_projects**: List AutoML projects
- **create_ml_project**: Create a new AutoML project
- **run_ml_project**: Run pipeline automation
- **list_registered_models**: List models in repository
- **list_models_and_decisions**: List published MAS modules
- **score_data**: Score data against a published model

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

## Testing

The project includes two layers of tests: **unit tests** (fast, no credentials required) and **integration tests** (run against a real SAS Viya instance).

### Running Unit Tests

Unit tests verify tool schemas, request payloads, and internal logic without making any network calls:

```sh
./run_tests.sh
```

Or directly via pytest:

```sh
uv run python -m pytest -m "not integration" -v
```

### Running Integration Tests

Integration tests call every tool against a live Viya environment. They require credentials, which can be provided via CLI arguments or `.env`:

**Using `.env`** (set `VIYA_ENDPOINT`, `VIYA_USERNAME`, `VIYA_PASSWORD`):
```sh
./run_tests.sh --integration
```

**Using CLI arguments:**
```sh
./run_tests.sh --integration \
    --endpoint https://your-viya-server.com \
    --username youruser \
    --password yourpassword
```

**Integration tests only** (skip unit tests):
```sh
./run_tests.sh --integration-only
```

### Test Structure

| File | Description |
|---|---|
| `tests/test_tool_payloads.py` | Payload assertions for all 26 tools — verifies URL paths, JSON body structure, query params, and headers |
| `tests/test_integration.py` | End-to-end workflow tests against a real Viya instance |
| `tests/test_tools.py` | Unit tests for HTTP helper functions (`_get_json`, `_post_json`, etc.) |
| `tests/test_viya_utils.py` | Unit tests for Viya compute session and job utilities |
| `tests/test_mcp_server.py` | Unit tests for MCP server and auth middleware |
| `tests/test_prompts.py` | Unit tests for prompt template rendering |
| `tests/test_config.py` | Unit tests for configuration loading |

## Contributing
Maintainers are accepting patches and contributions to this project. Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details about submitting contributions to this project.

## License & Attribution

Except for the the contents of the /static folder, this project is licensed under the [Apache 2.0 License](LICENSE). Elements in the /static folder are owned by SAS and are not released under an open source license. SAS and all other SAS Institute Inc. product or service names are registered trademarks or trademarks of SAS Institute Inc. in the USA and other countries. ® indicates USA registration.

Separate commercial licenses for SAS software (e.g., SAS Viya) are not included and are required to use these capabilities with SAS software.

All third-party trademarks referenced belong to their respective owners and are only used here for identification and reference purposes, and not to imply any affiliation or endorsement by the trademark owners.

This project requires the usage of the following:

- Python, see the Python license [here](https://docs.python.org/3/license.html)
- FastMCP, under the Apache 2.0 License
- uvicorn, under the BSD 3-Clause
- starlette, under the BSD 3-Clause
- httpx, under the MIT license
