# Deploying on Posit Connect

Run the server as MCP content on [Posit Connect](https://docs.posit.co/connect/user/mcp-servers/),
with Connect deciding who may use it and every tool call still running as the
calling user in SAS Viya.

## How sign-in works here

Elsewhere the server signs users in itself, proxying SAS Logon's OAuth flow.
On Connect that cannot work: Connect sits in front of the content and is
itself the authorization server MCP clients talk to (or takes a Connect API key
in `Authorization: Key …`), so a client never reaches the server's own SAS
Logon flow. With `POSIT_CONNECT_OAUTH=true` the server hands sign-in to Connect
instead:

1. The MCP client signs in to **Connect**: Connect's OAuth flow in the browser, or a Connect API key.
2. Connect checks the content's access list and forwards the request with a
   per-request `Posit-Connect-User-Session-Token` header.
3. For each tool call the server exchanges that token at Connect
   (`/__api__/v1/oauth/integrations/credentials`) for the user's **SAS Viya
   access token**. Connect gets that token from a **viewer OAuth integration**
   whose provider is SAS Logon. Each user authorizes it once, and Connect keeps
   and refreshes it.
4. The tool calls Viya with that token, so Viya permissions apply as usual.

The server's own OAuth proxy, `MCP_SIGNING_KEY`, `MCP_BASE_URL` callback and
`ALLOW_RAW_BEARER` play no part in this mode.

## Requirements

- Posit Connect with MCP content support and **custom OAuth integrations**
  (Enhanced or Advanced tier), Python 3.12 available on the server.
- Connect can reach your SAS Viya endpoint over HTTPS.
- The content can reach Connect's API, because the server calls Connect's
  credentials endpoint for every tool call. It uses `CONNECT_SERVER`, which
  Connect sets from
  [`Server.EnvAddress`](https://docs.posit.co/connect/admin/appendix/configuration/#Server.EnvAddress)
  (default: `Server.Address`) unless the content's Vars override it. With
  off-host execution (Kubernetes) that address must work **from inside the
  content pod**:
  - `http://localhost:3939` is the pod itself, not Connect.
  - The public URL may not be reachable from inside the cluster (no hairpin
    through the load balancer, or egress blocked).
  - Connect's in-cluster service address
    (`http://<connect-service>.<namespace>.svc.cluster.local:<port>`, see
    `kubectl get svc -n <namespace>`) is the reliable choice.

  Set it for all content with `Server.EnvAddress`, or for this server only
  with `POSIT_CONNECT_API_URL` in the content's Vars (it takes precedence over
  `CONNECT_SERVER`). Do not leave a stale `CONNECT_SERVER` in the content's
  Vars: it overrides Connect's value.
- A SAS Viya administrator (step 1) and a Connect administrator (step 2).
- Locally: [`uv`](https://docs.astral.sh/uv/) and `rsconnect-python`
  (`uv tool install rsconnect-python`), with `rsconnect add` already run for
  your server.

## 1. SAS Viya: register Connect as an OAuth client (Viya administrator)

Connect, not the MCP server, is the OAuth client of SAS Logon here. Register a
**confidential** client whose redirect URI is Connect's integration callback.
With an administrator token (see [`examples/configuration.md`](../examples/configuration.md#step-2-register-an-oauth-client-for-the-mcp-server)
for getting one):

```sh
curl -X POST "https://viya.example.com/SASLogon/oauth/clients" \
  -H "Authorization: Bearer $BEARER_TOKEN" -H "Content-Type: application/json" \
  -d '{"client_id": "posit-connect",
       "client_secret": "<a long random secret>",
       "scope": ["openid"],
       "authorized_grant_types": ["authorization_code", "refresh_token"],
       "redirect_uri": ["https://connect.example.com/__oauth__/integrations/callback"],
       "autoapprove": true}'
```

Then allow SAS Logon to redirect to Connect after sign-in: in SAS Environment
Manager, *Configuration → Definitions → sas.commons.web.security*, edit
**SAS Logon Manager**'s `content-security-policy` and add the Connect origin
to `form-action`, e.g. `form-action 'self' https://connect.example.com;`. Without
it the browser stops on the SAS Logon page after the password is accepted
(the same problem [`examples/configuration.md`](../examples/configuration.md)
describes for local development).

## 2. Connect: create the SAS Logon integration (Connect administrator)

*System → Integrations → Add integration → Custom*, or with the Connect API:

```sh
curl -H "Authorization: Key ${CONNECT_ADMIN_API_KEY}" \
  -X POST https://connect.example.com/__api__/v1/oauth/integrations \
  --data '{
    "template": "custom",
    "name": "SAS Viya (SAS Logon)",
    "description": "Your SAS Viya identity, for the SAS MCP server",
    "config": {
      "auth_type": "Viewer",
      "auth_mode": "Confidential",
      "client_id": "posit-connect",
      "client_secret": "<the secret from step 1>",
      "authorization_uri": "https://viya.example.com/SASLogon/oauth/authorize",
      "token_uri": "https://viya.example.com/SASLogon/oauth/token",
      "token_endpoint_auth_method": "client_secret_basic",
      "scopes": "openid"
    }
  }'
```

Note the returned `guid`. Make sure MCP OAuth sign-in is enabled on the server
(it is by default). If clients will use API keys instead, enable
`Authentication.APIKeyAuth`.

## 3. Deploy (publisher)

```sh
export VIYA_ENDPOINT=https://viya.example.com
deploy/posit-connect/deploy.sh -n <connect-server-nickname>
```._mcp_server` with a `requirements.txt` pinned from
`uv.lock` and runs `rsconnect deploy fastapi` with entrypoint
`sas_mcp_server.mcp_server:app`. It sets `POSIT_CONNECT_OAUTH=true`,
`MCP_STATELESS_HTTP=true` (Connect requires stateless Streamable HTTP) and
`SAS_MCP_VERSION`, and forwards `VIYA_ENDPOINT` plus any optional settings set
in your shell (`MCP_TIERS`, `MCP_READ_ONLY`, `COMPUTE_CONTEXT_NAME`, … — the
list is at the top of the script). Extra arguments go to `rsconnect`
unchanged. Pass `--app-id <content-guid>` to redeploy over existing content.
`--bundle-only <dir>` only builds the bundle.

Then, in the content's settings on Connect:

| Setting | Value | Why |
|---|---|---|
| **Access** | *Specific users or groups* (or *All users – login required*) | Connect login is the gate. Viya permissions still apply per user. |
| **Integrations** | Add *SAS Viya (SAS Logon)* | Supplies each user's Viya token. If you attach more than one integration, also set `POSIT_CONNECT_INTEGRATION_GUID` to its GUID. |
| **Runtime → Min processes** | 1 | Keeps the server warm. Connect sets this for MCP content on first deploy. |
| **Runtime → Max processes** | 1 | Warm compute sessions are cached per process, as with `replicas: 1` in [`README.md`](README.md#deliberate-choices). |
| **Runtime → Read / idle timeouts** | Above your longest SAS job | Long-running code-execution calls hold the request open. |
| **Vanity URL** (optional) | e.g. `/sas-mcp/` | A stable client URL that survives redeploys. |
| **Vars** (optional) | `MCP_BASE_URL=https://connect.example.com/sas-mcp` | Only used for the client snippets on the browser landing page. |

## 4. Connect an MCP client

The endpoint is the content URL plus `/mcp`, e.g.
`https://connect.example.com/content/<guid>/mcp` or
`https://connect.example.com/sas-mcp/mcp`.

- **OAuth-capable clients** (Claude Code, VS Code, Positron, …): add the URL.
  The client discovers Connect's OAuth server and opens a browser. Connect
  then asks the user to authorize the SAS Logon integration once.

  ```sh
  claude mcp add --transport http sas-viya https://connect.example.com/sas-mcp/mcp
  ```

- **API-key clients**: open the content once in a browser and authorize SAS
  Logon there, then configure the header `Authorization: Key <your Connect API key>`.

Opening the content's root URL in a browser shows Connect's MCP console with
the tool list.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Client sign-in fails before any browser opens, e.g. *expected string, received undefined* for `authorization_endpoint` / `token_endpoint` | Connect is served under a path prefix (`https://host/connect/`). Its OAuth issuer then has a path, and MCP clients look for its metadata at the **host root**: `/.well-known/oauth-authorization-server/connect`, then `/.well-known/openid-configuration/connect`, then `/connect/.well-known/openid-configuration`. Whatever owns the root (Workbench, say) answers the first two, and the last is Connect's OIDC document, which has no `authorization_endpoint`. The fix is in the reverse proxy, not this server: route `/.well-known/oauth-authorization-server/connect` to Connect's `/connect/.well-known/oauth-authorization-server` (see the note below the table). Or serve Connect on its own hostname. |
| Tool result: *Posit Connect could not provide a SAS Viya token (HTTP 4xx): …* | The text after the colon is Connect's own reason. *No OAuth session found…*: this user has not authorized SAS Logon for this content, so open the content URL in a browser and authorize it. *No OAuth integrations have been associated…*: attach the integration to this content item, because attaching it to other content does not count. *Refresh token is invalid* (error 219): the SAS Logon grant expired, so log out and back in to the integration (content settings, *Runtime* tab). Several integrations attached: set `POSIT_CONNECT_INTEGRATION_GUID`. The link in the message is the public content URL Connect reports in `X-RSC-Request`; `CONNECT_SERVER` is Connect's internal address and is only used for the server's own call to Connect. |
| Tool result: *… (HTTP 403): Direct access to this content is not permitted* | The token exchange reached the content itself, not Connect: `CONNECT_SERVER` is `http://localhost:3939` and the content runs off-host, so `localhost` is its own pod. Check the content's Vars first: a `CONNECT_SERVER` set there overrides Connect's value. Authorizing SAS Logon again does not help, and Connect's log shows no `/__api__/v1/oauth/integrations/credentials` request. Use Connect's in-cluster address (see Requirements). |
| Tool result: *Could not reach Posit Connect at https://… to get a SAS Viya token: ConnectTimeout* | The pod cannot open a connection to that address, typically the public URL from inside the cluster. Use Connect's in-cluster service address via `Server.EnvAddress` or `POSIT_CONNECT_API_URL` (see Requirements). |
| Tool result: *No Posit-Connect-User-Session-Token header* | `POSIT_CONNECT_OAUTH=true` on a server that is not running behind Connect. |
| Browser stuck on SAS Logon after the password | `form-action` in SAS Logon's CSP does not include the Connect origin (step 1). |
| SAS Logon: *Invalid redirect* | The client's `redirect_uri` is not exactly `https://<connect>/__oauth__/integrations/callback`. |
| Content fails to start: *ALLOW_RAW_BEARER cannot be combined…* / *needs VIYA_AUTH left on* | Remove `ALLOW_RAW_BEARER` / `VIYA_AUTH=false` from the content's Vars. Neither applies on Connect. |
| TLS errors reaching Viya | Connect does not trust the Viya certificate. Add the CA to the Connect host's trust store, or as a last resort set `SSL_VERIFY=false`. |

**Connect under a path prefix.** With nginx in front, an internal rewrite into
the location that already proxies `/connect/` is enough (adjust the prefix):

```nginx
location = /.well-known/oauth-authorization-server/connect {
    rewrite ^ /connect/.well-known/oauth-authorization-server last;
}
```

Check it with
`curl https://host/.well-known/oauth-authorization-server/connect`, which must
return JSON that includes `authorization_endpoint`.

*Optional: protected-resource metadata.* MCP clients don't need a rewrite for
this lookup. Connect's 401 names the protected-resource metadata URL in
`WWW-Authenticate: Bearer resource_metadata="…"`, and that URL is inside the
prefix (`https://host/connect/.well-known/oauth-protected-resource/<content-path>`),
so it already works. The MCP spec tells clients to use that URL and to build
the RFC 9728 URL themselves only when the header is missing. A client that
builds it anyway asks the host root
(`https://host/.well-known/oauth-protected-resource/connect/<content-path>`)
and gets whatever owns the root. To cover such clients, map that prefix onto
Connect as well. The path varies per content item, so use a prefix match:

```nginx
location ^~ /.well-known/oauth-protected-resource/connect/ {
    rewrite ^/.well-known/oauth-protected-resource/connect/(.*)$
            /connect/.well-known/oauth-protected-resource/$1 last;
}
```

`curl https://host/.well-known/oauth-protected-resource/connect/<content-path>/mcp`
must then return the same JSON as the URL in the `WWW-Authenticate` header,
with `resource` equal to the MCP endpoint URL.
