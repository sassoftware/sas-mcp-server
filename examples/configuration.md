## Configuration details for the SAS Viya MCP Server

### Authentication modes — at a glance

The server supports six authentication paths across HTTP and stdio modes. Pick one per deployment; HTTP mode lets PKCE and raw-bearer clients share the same endpoint.

| Mode                              | Transport | What the client does                                      | What the server does                                                                                        | Admin client registration?                                                                        | External CLI?     | Best for                                                                                                                    |
| --------------------------------- | --------- | --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ----------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **OAuth2 PKCE**                   | HTTP      | Browser-driven login on first tool call                   | Issues an MCP-signed JWT after the upstream PKCE flow; swaps it for the upstream Viya token on each request | ✅ Required (`sas-mcp` client, see [Step 2](#step-2-register-an-oauth-client-for-the-mcp-server)) | No                | Interactive end-users, k8s/multi-user deployments                                                                           |
| **Raw bearer passthrough**        | HTTP      | Sends `Authorization: Bearer <viya-jwt>` on every request | Validates the JWT against Viya's JWKS and uses it upstream as-is                                            | ✅ Required (or token issued elsewhere)                                                           | No                | Automation/CI that already holds a Viya token; co-exists with PKCE on the same `/mcp` endpoint when `ALLOW_RAW_BEARER=true` |
| **`sas-viya` CLI cache**          | stdio     | Runs `sas-viya auth loginCode` once                       | Reads access token from `~/.sas/credentials.json` on each tool call                                         | ❌ Not needed (uses built-in `sas.cli` client)                                                    | ✅ `sas-viya` CLI | Operators who already use the SAS Viya CLI                                                                                  |
| **`sas-mcp-login` cache**         | stdio     | Runs `uv run sas-mcp-login` once                          | Reads access token from `~/.sas-mcp-server/credentials.json` on each tool call                              | ❌ Not needed (uses built-in `vscode` client)                                                     | ❌ None           | Zero-prereq bootstrap; lowest friction on Viya 2022.11+                                                                     |
| **Native device-code (fallback)** | stdio     | None — server prints a URL and code on first tool call    | RFC 8628 device-authorization flow against SAS Logon                                                        | ⚠️ Only if your registered client has the device-code grant type                                  | ❌ None           | Viya deployments that don't CSRF-protect `/SASLogon/oauth/device_authorization`                                             |
| **No-auth mode**                  | HTTP/stdio | Sets `VIYA_AUTH=false`                                      | Skips SASLogon/OAuth entirely; forwards requests to Viya without an `Authorization` header                  | ❌ No                                                                                              | ❌ None           | Compute/API deployments that are already reachable without authentication                                                    |

**Defaults and ordering**

- **HTTP mode** always runs PKCE. The raw-bearer path is additive and opt-in via `ALLOW_RAW_BEARER=true`; when off, only PKCE clients can authenticate.
- **stdio mode** uses the server's own `sas-mcp-login` login when there is one, and only that. Without one it tries the `sas-viya` CLI cache, then an older `sas-mcp-login` login, then the native device-code flow. See [Which login the server uses](#which-login-the-server-uses).
- **No-auth override**: set `VIYA_AUTH=false` to bypass all auth flows in both HTTP and stdio modes.

**Removed**

- Password-grant stdio authentication (was: `VIYA_USERNAME` + `VIYA_PASSWORD`). Deprecated by OAuth 2.1 and incompatible with confidential OAuth clients. See [`stdio_server.py`](../src/sas_mcp_server/stdio_server.py) for details.

---

### Viya Setup

The SAS MCP Server runs locally and expects to communicate with a Viya instance.

The Viya instance serves two important roles:

1. Acts as an authorization server for the MCP Server
2. It provides the SAS execution API for the MCP Server

In order for the local MCP server to function properly, there are a few tweaks that need to be made to the Viya instance.

NOTE: These steps require Administrative access over the Viya instance. If you do not have access, please ask your SAS Administrator for assistance.

#### Step 1: Disable form-action Content Security Policy on SAS Logon Manager

Since the MCP Server is an external client to Viya, after successful authentication, the redirect will fail to trigger due to the form-action directive CSP. For local development and testing, it is most straightforward to **disable the directive**.

1. Log into Viya, assume the Administrator role
2. Go to SAS Environment Manager (left hand screen, Manage Environment)
3. Go to Configuration (left hand screen, under System)
4. View Definitions (Right next to the View:)
5. Filter by 'sas.commons.web.security', select it
6. Search for 'SAS Logon Manager', edit it
7. Go to 'content-security-policy', delete the 'form-action' component entirely.
8. Save the configuration

IMPORTANT: This approach does not follow security best practices. While it is feasible for local development and testing, for production scenarios, we strongly recommend hosting the MCP Server with proper TLS termination and adding its domain to the form-action directive as an allowed domain.

#### Step 2. Register an OAuth Client for the MCP Server

Since Viya does not support Dynamic Client Registration (DCR) pattern. It is required to register the OAuth client ahead of time. The [MCP Authorization spec](https://modelcontextprotocol.io/specification/draft/basic/authorization) states that this must be Authorization Code Flow with PKCE.

Following best practices defined in this [SAS blog post](https://blogs.sas.com/content/sgf/2023/02/07/authentication-to-sas-viya)

If you are not comfortable with curl and the command line. Feel free to use any API client.

1\. Retrieve a Viya access token (user is assumed to be a SAS Administrator)

```sh
export BEARER_TOKEN=`curl -sk -X POST \
    "https://YOUR_VIYA_ENDPOINT/SASLogon/oauth/token" \
    -u "sas.cli:" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d 'grant_type=password&username=user&password=password' | awk -F: '{print $2}'|awk -F\" '{print $2}'`
```

Replace the endpoint, username and password with your own values.

2\. Register the OAuth Client

```sh
curl -k -X POST "https://YOUR_VIYA_ENDPOINT/SASLogon/oauth/clients" \
   -H "Content-Type: application/json" \
   -H "Authorization: Bearer $BEARER_TOKEN" \
   -d '{"client_id": "sas-mcp",
      "scope": ["openid"],
      "authorized_grant_types": ["authorization_code","refresh_token"],
      "redirect_uri": ["http://localhost:8134/auth/callback"], "autoapprove":true, "allowpublic":true}'
```

Replace the endpoint with your own value.
Note the client_id and the redirect_uri -- these are important for the environment file

If the server will be reachable at an **external URL** rather than `localhost` -- a shared
host, reverse proxy, tunnel or Kubernetes -- then that URL's callback has to be registered
too, and it has to match the `MCP_BASE_URL` you set in `.env`. Register both, so the same
client keeps working for local development:

```json
"redirect_uri": ["http://localhost:8134/auth/callback",
                 "https://YOUR_MCP_BASE_URL/auth/callback"],
```

Viya accepts only the callbacks it was registered with, and it is Viya -- not this server --
that rejects a mismatch, after the user has already signed in. Changing `MCP_BASE_URL` later
therefore means re-registering the client; see
[TROUBLESHOOTING.md](../TROUBLESHOOTING.md) if sign-in fails with
`Invalid redirect ... did not match one of the registered values`.

**Alternative: Python script**

If you prefer, you can use the provided registration script instead of curl. It reads your `.env` file for the endpoint, client ID, port and `MCP_BASE_URL` -- registering the external callback alongside `localhost` -- and handles self-signed certificates automatically.

Preview what it would register, without contacting Viya or changing anything:

```sh
uv run python examples/register_mcp_client.py --dry-run
```

Then run it for real:

```sh
uv run python examples/register_mcp_client.py
```

It prompts for your Viya admin credentials, prints and backs up the current registration,
deletes it, registers the new one, and then reads the client back to confirm Viya stored every
redirect URI it was asked for. It exits non-zero if anything failed -- a verification mismatch
included -- and if the registration fails *after* the old client was deleted, it prints the
exact `curl` that puts the previous definition back.

| Option | Purpose |
|---|---|
| `--dry-run` | Print the redirect URIs and the request body that would be sent, then exit. Makes no network calls and needs no credentials. |
| `--redirect-uri URI` | Register one more full redirect URI. Repeatable. |
| `MCP_EXTRA_REDIRECT_URIS` | Comma-separated extra redirect URIs, for a deployment answering on more than one URL (a tunnel beside a shared host, blue/green hostnames). |
| `VIYA_USERNAME` / `VIYA_PASSWORD` | Skip the prompts for an unattended run. Set both. |

Note that if `VIYA_USERNAME` and `VIYA_PASSWORD` are already in your `.env` -- the integration
tests use them -- the script uses those instead of prompting, and prints which user it is
using. Check that user has admin rights, or the client calls will fail with `HTTP 403`.

Congratulations! Your Viya is now configured and ready to connect with the MCP server.

---

### Environment file options

The .env file used by the MCP Server allows for customizable options that the user can set themselves.
| Variable | Required | Default | Description |
|---------------------|---------|--------------|---------------------------------------------------------------|
| `VIYA_ENDPOINT` | Yes | — | Viya instance to use |
| `CLIENT_ID` | No | `sas-mcp` | OAuth2 Client ID registered in Viya |
| `HOST_PORT` | No | `8134` | Host Port the local MCP Server listens on |
| `MCP_SIGNING_KEY` | No | `default` | Secret key used to sign [FastMCP Proxy JWTs](https://gofastmcp.com/servers/auth/oauth-proxy#param-jwt-signing-key) |
| `MCP_BASE_URL` | No | `http://localhost:{HOST_PORT}` | External URL of the MCP server (set for k8s/reverse proxy deployments). Changing it requires re-running `examples/register_mcp_client.py` with Viya admin credentials, so SAS Logon accepts the new `/auth/callback` |
| `COMPUTE_CONTEXT_NAME` | No | `SAS Job Execution compute context` | Viya compute context to use for code execution |
| `MCP_SERVER_NAME` | No | `SAS Viya Execution MCP Server` | Name advertised to MCP clients (`serverInfo.name`); set a distinct name per deployment when running multiple servers |
| `SSL_VERIFY` | No | `true` | Set to `false` to disable SSL certificate verification (e.g. for self-signed Viya certificates) |
| `ALLOW_RAW_BEARER` | No | `false` | When `true`, the HTTP-mode server also accepts a raw Viya access token in the `Authorization` header alongside the default OAuth2 PKCE flow. Useful for automation that already holds a Viya token. |
| `MCP_OAUTH_CLIENT_SECRETS` | No | `false` | HTTP mode: issue a client secret to an MCP client that registers asking for one (`client_secret_basic` or `client_secret_post`), list those methods in the authorization-server metadata, and require the secret at the token endpoint. Off, every dynamically registered client is a public client — FastMCP 4's behaviour, and what most MCP clients expect. **Microsoft 365 Copilot needs it on**: its registration service rejects metadata without a secret-based method (*"The authorization server does not support client_secret_basic or client_secret_post…"*) and a registration response without `client_secret`. Clients that register with `none` are unaffected either way. |
| `MCP_STATELESS_HTTP` | No | `false` | HTTP mode: serve every request without an MCP session. The 2026-07-28 protocol revision is sessionless and opens with `server/discover`, which the default stateful transport refuses, so a client that speaks only that revision — claude.ai's custom-connector backend — cannot connect without this. The legacy `initialize` handshake keeps working either way. Costs a fresh transport per request. |
| `MCP_DEFAULT_FOLDER` | No | *(none)* | SAS Content folder for the rule sets and decision flows the tools create when a call names none: a folder URI (`/folders/folders/<id>`), a folder ID, or a path such as `/Public/Content/Decisions`. `/folders/folders/@myFolder` means each person's own My Folder. These assets are always stored in a folder; with no folder in the call and none set here, the tool refuses and the model asks the user. Resolved per call, with the caller's permissions. |
| `MCP_DEFAULT_PUBLISH_DESTINATION` | No | `maslocal` | Publishing destination that `publish_model`, `publish_ml_champion_model` and `publish_decision_flow` use when a call names none. `list_publishing_destinations` shows the names a deployment has. `publish_decision_flow` generates code for MAS, so its destination must be a MAS one. |
| `MCP_DEFAULT_MODEL_REPOSITORY` | No | `Public` | SAS Model Manager repository (name or ID) for new projects and registered models when a call names none. Projects live in a model repository, not in SAS Content, so `MCP_DEFAULT_FOLDER` does not apply to them; `list_model_repositories` shows the names. |
| `MCP_TOOL_RENAME` | No | *(none)* | Rename tools on the wire: comma-separated `old=new` pairs, each replacing a tool-name **prefix** (the longest matching prefix wins; a whole name is a prefix of itself). For hosts that refuse names the protocol allows — Microsoft Copilot Studio's agent runtime fails every turn with `MCS-2011` while a tool whose name starts with `catalog_` is listed, so use `MCP_TOOL_RENAME=catalog_=infocat_` there (see [copilot-studio](copilot-studio/README.md)). Only what the client is shown changes: the listing and the cross-references in tool descriptions carry the new names, calls are translated back before anything else runs, the original names stay callable, and the server log, telemetry, tier and read-only handling keep them. A pair that would give two tools the same name, or a name outside `[A-Za-z0-9_-]{1,64}`, stops the server at startup with the reason. |
| `MCP_LANDING_PAGE` | No | `true` | HTTP mode: serve an explanatory HTML page (server description, exposed tool tiers, client-config snippets) when the `/mcp` URL is opened in a browser (`GET` with `Accept: text/html`), instead of FastMCP's bare 401. MCP clients are unaffected. Unauthenticated; shows deployment shape only. Set `false` to return the 401 to browsers too. |
| `VIYA_AUTH` | No | `true` | Master auth toggle. Set to `false` to disable SASLogon/OAuth handling (HTTP and stdio) and send Viya API calls without an `Authorization` header. |
| `COMPUTE_SESSION_ID` | No | — | Fixed compute session id (for deployments without `/compute/contexts`). When set (e.g. `0001`), compute tools use that session directly. |
| `MAX_UPLOAD_BYTES` | No | `104857600` (100 MiB) | Upper bound for the server-side upload sources (`upload_data`/`upload_file` with `file_path` or `url`). The default matches SAS Viya's 100 MB file-upload limit — if your administrator raises the Viya-side limit, raise this with it (see [deploy/SCALING.md](../deploy/SCALING.md)). |
| `VIYA_CLIENT_TIMEOUT` | No | `300` | Timeout in seconds for every SAS Viya REST call the tools make. Lower it to fail fast behind a proxy that drops idle connections; raise it for compute contexts that are slow to start. Must be a number greater than 0, or the server refuses to start. |
| `MAX_EXPORT_INLINE_BYTES` | No | `26214400` (25 MiB) | Upper bound for binary report exports returned inline by `export_report`; larger exports are refused with guidance. |
| `HTTP_DEBUG` | No | `false` | Trace every SAS Viya API request and response to a separate JSONL file (see `HTTP_DEBUG_LOG_PATH`). Credentials are redacted; data values in bodies are not. For troubleshooting only. |
| `HTTP_DEBUG_LOG_PATH` | No | `~/.sas-mcp-server/http-debug.log` | File the HTTP debug trace is written to; created with owner-only permissions. |
| `HTTP_DEBUG_MAX_BODY_BYTES` | No | `4096` | Cap on each recorded request/response body; `0` records no bodies. |
| `HTTP_DEBUG_MAX_LOG_BYTES` / `HTTP_DEBUG_LOG_BACKUPS` | No | `10485760` / `3` | Rotation size of the trace file and number of rotated files kept. |
| `SAS_CLI_CONFIG` | Stdio (optional) | `$HOME` | Parent directory for the SAS Viya CLI credential cache. The token is read from `$SAS_CLI_CONFIG/.sas/credentials.json`. |
| `VIYA_USERNAME` | Tests only | — | Used by the integration test suite to acquire a token via the legacy `sas.cli` password grant. Not used by the MCP server itself. |
| `VIYA_PASSWORD` | Tests only | — | See `VIYA_USERNAME`. |

The defaults listed here are the variable values used in the Viya setup step. If your SAS Administrator has used a different `CLIENT_ID`, `HOST_PORT` during the OAuth Client registration. Please use those values instead.

---

### SSL Certificate Configuration

If your Viya instance uses custom or internal CA certificates, Python needs to know where to find them. Rather than disabling verification entirely with `SSL_VERIFY=false`, you can point Python to your Viya certificate chain.

**Linux / macOS:**

```sh
export REQUESTS_CA_BUNDLE="/path/to/sas-viya-ca-certificate.pem"
export SSL_CERT_FILE="/path/to/sas-viya-ca-certificate.pem"
```

**Windows (PowerShell):**

```powershell
$env:REQUESTS_CA_BUNDLE = "C:\path\to\sas-viya-ca-certificate.pem"
$env:SSL_CERT_FILE = "C:\path\to\sas-viya-ca-certificate.pem"
```

Set these environment variables before starting the MCP server. The `.pem` file should contain the full certificate chain for your Viya instance (including any intermediate CA certificates).

To obtain the certificate, ask your SAS Administrator or extract it from the Viya ingress:

```sh
openssl s_client -connect your-viya-server.com:443 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -outform PEM > sas-viya-ca-certificate.pem
```

You can also add these variables to your `.env` file so they are loaded automatically:

```
REQUESTS_CA_BUNDLE=/path/to/sas-viya-ca-certificate.pem
SSL_CERT_FILE=/path/to/sas-viya-ca-certificate.pem
```

> **Note:** `SSL_VERIFY=false` should only be used for local development and testing. For production, always configure the proper certificate chain.

---

### Kubernetes Deployment

When deploying the MCP server in Kubernetes for multi-user access, each user authenticates independently via the OAuth2 PKCE flow using their own Viya credentials. No shared service account is needed.

#### Key configuration

Set these environment variables on the container (via ConfigMap, Secret, or Helm values):

| Variable          | Value                              | Why                                                                                                   |
| ----------------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `VIYA_ENDPOINT`   | `https://your-viya-server.com`     | The Viya instance to connect to                                                                       |
| `MCP_BASE_URL`    | `https://sas-mcp.company.com`      | The external URL users reach the MCP server at (must match the OAuth redirect URI registered in Viya) |
| `MCP_SIGNING_KEY` | A strong random string (24+ chars) | Signs proxy JWTs — use a Kubernetes Secret                                                            |
| `SSL_CERT_FILE`   | `/etc/ssl/certs/viya-ca.pem`       | Path to Viya CA certificate (mount via Secret or ConfigMap)                                           |

#### OAuth client registration

The OAuth redirect URI registered in Viya (Step 2 above) must match your ingress URL. For example, if `MCP_BASE_URL=https://sas-mcp.company.com`, register:

```sh
curl -k -X POST "https://YOUR_VIYA_ENDPOINT/SASLogon/oauth/clients" \
   -H "Content-Type: application/json" \
   -H "Authorization: Bearer $BEARER_TOKEN" \
   -d '{"client_id": "sas-mcp",
      "scope": ["openid"],
      "authorized_grant_types": ["authorization_code","refresh_token"],
      "redirect_uri": "https://sas-mcp.company.com/auth/callback",
      "autoapprove":true, "allowpublic":true}'
```

#### Ingress

Expose the server via an Ingress with TLS termination:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
    name: sas-mcp-server
    annotations:
        nginx.ingress.kubernetes.io/proxy-read-timeout: '300'
spec:
    tls:
        - hosts:
              - sas-mcp.company.com
          secretName: sas-mcp-tls
    rules:
        - host: sas-mcp.company.com
          http:
              paths:
                  - path: /
                    pathType: Prefix
                    backend:
                        service:
                            name: sas-mcp-server
                            port:
                                number: 8134
```

#### MCP client configuration

Each user points their MCP client at the ingress URL:

```json
{
    "servers": {
        "sas-execution-mcp": {
            "url": "https://sas-mcp.company.com/mcp",
            "type": "http"
        }
    }
}
```

When a user first invokes a tool, their browser opens for Viya login. After authentication, their session is tied to their own Viya identity and permissions.

---

### Gemini CLI

Gemini CLI connects to MCP servers via stdio only — it does not support HTTP mode. Stdio mode reads an access token cached by the SAS Viya CLI's device-code flow, so no MCP-client browser redirect is involved.

#### Configuration

Add to `~/.gemini/settings.json` or your project's `.gemini/settings.json`:

```json
{
    "mcpServers": {
        "sas-viya-mcp": {
            "command": "uv",
            "args": ["run", "--no-sync", "app-stdio"],
            "cwd": "/path/to/sas-mcp-server",
            "timeout": 60000
        }
    }
}
```

Set `cwd` to the absolute path where `sas-mcp-server` is cloned. `--no-sync` starts the server from `.venv` as it is, so run `uv sync` yourself after each `git pull`, with Gemini CLI closed; on Windows a client-triggered sync can fail on the running server's locked `.exe` (see [TROUBLESHOOTING.md](../TROUBLESHOOTING.md#stdio-server-fails-to-start-with-failed-to-remove-file-venvscriptsapp-stdioexe--os-error-32)).

A pre-built example is available at [`examples/gemini-settings.json`](gemini-settings.json).

#### Timeout

The `timeout` field (in milliseconds) controls how long Gemini CLI waits for a tool call to complete. The default is 10 seconds, which is too short for most SAS Viya API calls. **Set this to at least `60000` (60 seconds).**

Without this setting, tool calls will appear to fail with a timeout error even though the server and authentication are working correctly.

#### Authenticating for stdio mode

Stdio mode reads a cached OAuth access token. Two equivalent ways to obtain one — pick whichever fits your environment.

**Option 1 — SAS Viya CLI** (preferred if it's already installed):

```sh
sas-viya --profile Default profile init --sas-endpoint https://your-viya-server.com
sas-viya auth loginCode
```

This writes `~/.sas/credentials.json`. If you keep the SAS profile somewhere other than `$HOME`, set `SAS_CLI_CONFIG` to its parent directory in your `.env`:

```
SAS_CLI_CONFIG=/path/whose/.sas/credentials.json/lives/here
```

**Option 2 — built-in `sas-mcp-login` helper** (no external CLI, no admin client registration; requires Viya 2022.11+). See the next section.

#### Signing in with `sas-mcp-login`

Run it from the folder you cloned the server into:

```sh
uv run sas-mcp-login --endpoint https://your-viya-server.com --name prod
```

1. The helper prints a SAS Logon address and opens it in your browser.
2. Sign in as usual (including any single sign-on or MFA prompt).
3. SAS Logon then shows a page with an **authorization code**. Copy it.
4. Paste the code at the `Authorization code:` prompt in the terminal and press Enter.
5. The helper prints `Token cached at: ~/.sas-mcp-server/credentials.json (for https://your-viya-server.com)`, and with `--name`, the short command for next time.

The token is stored under the endpoint's URL, so logging in to another deployment never replaces it. Restart nothing: the stdio server reads the file on every tool call.

**When you need to sign in again.** The server refreshes the access token on its own while the refresh token is valid, so you only sign in again when that has expired too (its lifetime is set by your SAS administrator), when it was revoked, or when you switch user. The server then logs `No usable cached credentials for <endpoint>` with the command to run, and falls back to the device-code sign-in, which shows an address and a code, or fails where SAS Logon doesn't allow it. `uv run sas-mcp-login --list` shows each login's state.

**Without a terminal prompt** (the Claude Code `!` prefix, CI, some IDE terminals), the helper can't ask for the code. It saves its state and prints the second command instead:

```sh
uv run sas-mcp-login prod                 # step 1: prints the address, saves the state, exits
uv run sas-mcp-login prod --code <CODE>   # step 2: exchanges the code you copied
```

Only one login can be pending at a time. Naming the endpoint in step 2 makes the helper refuse a code that belongs to a login you started for another deployment in between.

**Options**

| Option | What it does |
| --- | --- |
| `ENDPOINT_OR_NAME`, or `--endpoint <value>` | The deployment to sign in to. A full URL (`https://viya.example.com`), a name saved with `--name`, or one part of the host name of a deployment you have logged in to before (`zeus` for `https://viya4-s2.zeus.example.com`, as long as no other stored deployment has that part). Defaults to `VIYA_ENDPOINT` from the environment or `.env`. |
| `--name <name>` | Saves a short name for the endpoint once the login succeeds, so later commands can say `sas-mcp-login prod`. Letters, digits, `.`, `_` and `-`; stored in lower case. Giving an existing name to another endpoint moves it, and the helper says so. |
| `--list` | Lists the stored logins: name, endpoint, and whether the login is valid, expired but refreshable, or expired. Never prints tokens. |
| `--logout` | Removes the stored login for the endpoint (`sas-mcp-login --logout prod`). Other deployments' logins are kept, and so is the name, so `sas-mcp-login prod` signs you in again. It does not revoke the token in SAS Logon. |
| `--code <code>` | Second step of the login without a terminal prompt (above). |
| `--no-browser` | Prints the SAS Logon address without opening a browser, for example on a remote machine. |
| `--insecure` | Skips TLS certificate checks for this login, for a deployment with a self-signed certificate. `SSL_VERIFY=false` in `.env` does the same. The stdio server also needs `SSL_VERIFY=false` for such a deployment. |
| `--client-id <id>` | The OAuth client to sign in with. Default `vscode`, which Viya 2022.11 and later have built in. Change it only if your administrator registered another public client; the server must refresh with the same one (`AUTH_HELPER_CLIENT_ID`). |
| `--redirect-uri <uri>` | Only if your client has a registered redirect address. Without it, SAS Logon shows the code on a page, which is what you want in almost every case. |

**Files** (all in `~/.sas-mcp-server/`, readable only by you):

- `credentials.json` holds the access and refresh tokens, one block per endpoint (plus `Default` from versions before per-endpoint logins). Keep it out of backups and shared folders.
- `endpoints.json` holds the names you saved, as `{"prod": "https://..."}`. No secrets; you can edit it by hand. The stdio server doesn't read it: `VIYA_ENDPOINT` in a server entry is always the full URL.
- `login-state.json` exists only while a two-step login is pending.
- `credentials.json.lock` exists for a moment while a login or a token refresh updates the file.

#### Which login the server uses

On each tool call a stdio server looks for a token in this order and uses the first that works, refreshing it first if it has expired:

1. **Its own login:** the block in `~/.sas-mcp-server/credentials.json` for its `VIYA_ENDPOINT`. When this block exists, it is the only one used: if it can't be refreshed, the server does not fall back to another deployment's token.
2. **The `sas-viya` CLI cache**, `~/.sas/credentials.json` (or `$SAS_CLI_CONFIG/.sas/credentials.json`), only without a login of its own.
3. **The `Default` block** written by versions before per-endpoint logins: when it records this endpoint, or records none and the file holds no other deployment's login.
4. **The device-code flow**, as a last resort (see [the authentication table](#authentication-modes--at-a-glance)).

#### Several SAS Viya deployments (stdio)

Run one server entry per deployment, each with its own `VIYA_ENDPOINT` and a distinct `MCP_SERVER_NAME`, and log in to each one:

```json
{
    "mcpServers": {
        "sas-viya-dev": {
            "command": "uv",
            "args": ["run", "--no-sync", "app-stdio"],
            "cwd": "/path/to/sas-mcp-server",
            "env": { "VIYA_ENDPOINT": "https://viya-dev.example.com", "MCP_SERVER_NAME": "SAS Viya (dev)" }
        },
        "sas-viya-prod": {
            "command": "uv",
            "args": ["run", "--no-sync", "app-stdio"],
            "cwd": "/path/to/sas-mcp-server",
            "env": { "VIYA_ENDPOINT": "https://viya-prod.example.com", "MCP_SERVER_NAME": "SAS Viya (prod)" }
        }
    }
}
```

```sh
uv run sas-mcp-login --endpoint https://viya-dev.example.com --name dev
uv run sas-mcp-login --endpoint https://viya-prod.example.com --name prod
```

After that, `uv run sas-mcp-login dev`, `uv run sas-mcp-login --logout prod` and `uv run sas-mcp-login --list` are enough.

`sas-mcp-login` adds a block for the endpoint to `~/.sas-mcp-server/credentials.json` and keeps the others; the key is the endpoint URL (lower-cased, without a default port or trailing slash). A server reads the block for its own `VIYA_ENDPOINT`. When that block exists it is the only cached login the server uses: a login that has expired or been revoked is never replaced by another deployment's token. The lookup order is this endpoint's block, then (only without one) the `sas-viya` CLI cache, then the legacy `Default` block.

- **Existing setups keep working.** A `Default` block written by an earlier version is still used when no block exists for the server's endpoint, and the server logs a warning that it is not bound to an endpoint. Once it has refreshed that token, it records the endpoint in the block, and a server for any other endpoint then ignores it.
- **Adding a second deployment:** log in to each deployment with `sas-mcp-login --endpoint <url>`, including the one you used before. A `Default` block that has not recorded its endpoint yet is no longer used once the file holds another endpoint's login, because it could have been issued by either deployment and its refresh token must not be sent to the wrong one. `sas-mcp-login` says so when that happens.
- **Remove a login:** `uv run sas-mcp-login --logout dev` (or `--logout --endpoint https://viya-dev.example.com`) removes that endpoint's block, and a `Default` block recorded for it. A `Default` block that is not bound to an endpoint is kept and the command says so; delete the `Default` key from the file to remove it.
- The `sas-viya` CLI cache (`~/.sas/credentials.json`) holds no endpoint. A server with a login of its own never reads it; one without sends it to whichever endpoint the server is configured for. With several deployments, use `sas-mcp-login` or point each entry at its own cache with `SAS_CLI_CONFIG`.
- Several servers can refresh at the same time: the file is updated under a lock file, `credentials.json.lock`, next to it.
- The file holds access and refresh tokens in plain text. It is created readable only by your user (on Windows by restricting its ACL, best effort). Keep it out of backups and shared folders.

---

### Further MCP setup options

For examples on how to run with docker, refer to the **docker** folder.
