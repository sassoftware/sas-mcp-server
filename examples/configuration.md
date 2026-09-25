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
- **stdio mode** tries the cache files in order: `sas-viya` CLI cache → `sas-mcp-login` cache → native device-code. The first hit wins.
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
| `MCP_STATELESS_HTTP` | No | `false` | HTTP mode: serve every request without an MCP session. The 2026-07-28 protocol revision is sessionless and opens with `server/discover`, which the default stateful transport refuses, so a client that speaks only that revision — claude.ai's custom-connector backend — cannot connect without this. The legacy `initialize` handshake keeps working either way. Costs a fresh transport per request. |
| `MCP_LANDING_PAGE` | No | `true` | HTTP mode: serve an explanatory HTML page (server description, exposed tool tiers, client-config snippets) when the `/mcp` URL is opened in a browser (`GET` with `Accept: text/html`), instead of FastMCP's bare 401. MCP clients are unaffected. Unauthenticated; shows deployment shape only. Set `false` to return the 401 to browsers too. |
| `VIYA_AUTH` | No | `true` | Master auth toggle. Set to `false` to disable SASLogon/OAuth handling (HTTP and stdio) and send Viya API calls without an `Authorization` header. |
| `COMPUTE_SESSION_ID` | No | — | Fixed compute session id (for deployments without `/compute/contexts`). When set (e.g. `0001`), compute tools use that session directly. |
| `MAX_UPLOAD_BYTES` | No | `104857600` (100 MiB) | Upper bound for the server-side upload sources (`upload_data`/`upload_file` with `file_path` or `url`). The default matches SAS Viya's 100 MB file-upload limit — if your administrator raises the Viya-side limit, raise this with it (see [deploy/SCALING.md](../deploy/SCALING.md)). |
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
            "args": ["run", "app-stdio"],
            "cwd": "/path/to/sas-mcp-server",
            "timeout": 60000
        }
    }
}
```

Set `cwd` to the absolute path where `sas-mcp-server` is cloned.

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

**Option 2 — built-in `sas-mcp-login` helper** (no external CLI, no admin client registration; requires Viya 2022.11+):

```sh
uv run sas-mcp-login
```

The helper runs OAuth 2.0 Authorization Code + PKCE against the built-in `vscode` Viya OAuth client, opens your browser, and writes the token to `~/.sas-mcp-server/credentials.json`. After signing in, SAS Logon displays the authorization code on a results page; copy it and paste it back into the terminal.

The stdio server reads whichever cache file exists, in this order:

1. `~/.sas/credentials.json` (or `$SAS_CLI_CONFIG/.sas/credentials.json`)
2. `~/.sas-mcp-server/credentials.json`

When the token expires, re-run the same command you used originally.

---

### Further MCP setup options

For examples on how to run with docker, refer to the **docker** folder.
