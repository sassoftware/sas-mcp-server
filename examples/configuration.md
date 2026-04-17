## Configuration details for the SAS MCP Server

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

Following best practies defined in this [SAS blog post](https://blogs.sas.com/content/sgf/2023/02/07/authentication-to-sas-viya)

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
      "redirect_uri": "http://localhost:8134/auth/callback", "autoapprove":true, "allowpublic":true}'
```
Replace the endpoint with your own value.
Note the client_id and the redirect_uri -- these are important for the environment file

**Alternative: Python script**

If you prefer, you can use the provided registration script instead of curl. It reads your `.env` file for the endpoint, client ID, and port, and handles self-signed certificates automatically.

```sh
uv run python examples/register_mcp_client.py
```

The script will prompt for your Viya admin credentials, delete any existing client with the same ID, register a new one, and verify the registration.

Congratulations! Your Viya is now configured and ready to connect with the MCP server.

---

### Environment file options
The .env file used by the MCP Server allows for customizable options that the user can set themselves.
| Variable            | Required | Default       | Description                                                 |
|---------------------|---------|--------------|---------------------------------------------------------------|
| `VIYA_ENDPOINT`     | Yes     | —            | Viya instance to use                                          |
| `CLIENT_ID`         | No      | `sas-mcp`    | OAuth2 Client ID registered in Viya                           |
| `HOST_PORT`         | No      |  `8134`      | Host Port the local MCP Server listens on                    |
| `MCP_SIGNING_KEY`   | No      | `default`    | Secret key used to sign [FastMCP Proxy JWTs](https://gofastmcp.com/servers/auth/oauth-proxy#param-jwt-signing-key)                                                           |
| `MCP_BASE_URL`         | No   | `http://localhost:{HOST_PORT}`             | External URL of the MCP server (set for k8s/reverse proxy deployments) |
| `COMPUTE_CONTEXT_NAME` | No   | `SAS Job Execution compute context`       | Viya compute context to use for code execution                |
| `SSL_VERIFY`        | No      | `true`       | Set to `false` to disable SSL certificate verification (e.g. for self-signed Viya certificates)  |
| `VIYA_USERNAME`     | Stdio only | —         | Viya username for stdio mode (password grant authentication)  |
| `VIYA_PASSWORD`     | Stdio only | —         | Viya password for stdio mode (password grant authentication)  |

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

| Variable | Value | Why |
|----------|-------|-----|
| `VIYA_ENDPOINT` | `https://your-viya-server.com` | The Viya instance to connect to |
| `MCP_BASE_URL` | `https://sas-mcp.company.com` | The external URL users reach the MCP server at (must match the OAuth redirect URI registered in Viya) |
| `MCP_SIGNING_KEY` | A strong random string (24+ chars) | Signs proxy JWTs — use a Kubernetes Secret |
| `SSL_CERT_FILE` | `/etc/ssl/certs/viya-ca.pem` | Path to Viya CA certificate (mount via Secret or ConfigMap) |

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
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
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

Gemini CLI connects to MCP servers via stdio only — it does not support HTTP mode. Because it cannot participate in browser-based OAuth redirects, stdio mode with password grant credentials is required.

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

#### Required `.env` variables

Stdio mode authenticates via password grant, so you must set these in your `.env` file:
```
VIYA_ENDPOINT=https://your-viya-server.com
VIYA_USERNAME=your-username
VIYA_PASSWORD=your-password
SSL_VERIFY=false  # if using self-signed certificates
```

The `CLIENT_ID` can remain at the default (`sas-mcp`) or be changed to match an existing OAuth client registered on your Viya instance (e.g., `sas.cli`).

---

### Further MCP setup options
For examples on how to run with docker, refer to the **docker** folder.