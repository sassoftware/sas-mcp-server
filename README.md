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

Add to your MCP client configuration (e.g., `.vscode/mcp.json`):

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
