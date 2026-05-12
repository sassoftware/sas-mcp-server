# Troubleshooting

## OAuth: `client_id ... was not found in the server's client registry`

You restart the server (or wipe its cache) and the next time you connect from your MCP client (VS Code, MCP Inspector, Claude Desktop, …) the browser shows:

> OAuth 2.0 requires clients to register before authorization. This server returned a 400 error because the provided client ID was not found.

…and the server log shows:

```
INFO  Unregistered client_id=<some-uuid>, returned HTML error response
INFO  "GET /authorize?client_id=<some-uuid>..." 400 Bad Request
```

Notice that there's **no `POST /register` immediately before** that `/authorize` line — that is the diagnostic.

### Why this happens

Two independent caches need to agree on the client ID, and they can drift out of sync.

There are also two different "client IDs" involved — they are not the same thing:

| Identity | Where it's defined | What uses it |
|---|---|---|
| **Upstream client** (e.g. `sas-mcp`) | Registered in Viya via `register_mcp_client.py`. Set as `CLIENT_ID` in your `.env`. | Used by *this server* when it redirects to `https://<viya>/SASLogon/oauth/authorize`. Never visible to the MCP client. |
| **Downstream DCR client** (a UUID) | Generated fresh per MCP-client by FastMCP's OAuthProxy, on `POST /register`. Stored encrypted in `cache.db`. | Used by the MCP client (VS Code, etc.) when it talks to *this server*'s `/authorize` and `/token`. |

The MCP spec requires Dynamic Client Registration. Viya doesn't support DCR, so FastMCP's `OAuthProxy` provides DCR locally and translates downstream UUIDs to the single upstream `sas-mcp` credential when calling Viya.

The MCP client caches its UUID after the first `POST /register`. The server caches the same UUID (encrypted) in `cache.db`. As long as both caches stay aligned, everything works.

They go out of sync when **the server's cache is rebuilt** (deleted, or written under a different `MCP_SIGNING_KEY` so its Fernet entries become undecryptable). The MCP client still has the old UUID, sends it to `/authorize`, and the server has no record of it.

The MCP client *should* fall back to a fresh `POST /register` when it sees a 400 — but most clients (VS Code included today) don't auto-recover. They keep sending the cached UUID until you manually clear it.

### How to fix it

**Step 1 — Stop the server.** Don't try to fix this with the server still running; the reloader will rewrite `cache.db` mid-edit.

**Step 2 — Wipe the server-side cache** at:

| OS | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\fastmcp\oauth-proxy\cache.db` (and `cache.db-shm`, `cache.db-wal` if present) |
| macOS | `~/Library/Application Support/fastmcp/oauth-proxy/cache.db` |
| Linux | `~/.local/share/fastmcp/oauth-proxy/cache.db` |

**Step 3 — Pin a stable `MCP_SIGNING_KEY`** in `.env`, so future restarts produce the same Fernet key and `cache.db` stays decryptable across runs:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste the output as `MCP_SIGNING_KEY=...`. An empty value (`MCP_SIGNING_KEY=`) is *not* the same as unset — it overrides the in-code default. Either set a real value or remove the line entirely.

**Step 4 — Wipe the MCP client's cached UUID.** This is the step most people miss. Below are the locations for VS Code; other clients (Claude Desktop, MCP Inspector) have analogous stores.

#### VS Code (Windows)

VS Code's MCP DCR registrations live in `state.vscdb`:

```
%APPDATA%\Code\User\globalStorage\state.vscdb
```

**Close VS Code completely first** (the DB is locked while it runs). On Windows, `Get-Process Code | Stop-Process` is a hammer that works.

Then run this Python snippet (adjust path for macOS/Linux — see below):

```python
import sqlite3
db = r"C:\Users\<you>\AppData\Roaming\Code\User\globalStorage\state.vscdb"
con = sqlite3.connect(db)
cur = con.cursor()
patterns = [
    "secret://dynamicAuthProvider:clientRegistration:%localhost:8134%",
    "secret://dynamicAuthProvider:clientRegistration:%127.0.0.1:8134%",
    'secret://%isDynamicAuthProvider%localhost:8134%',
    'secret://%isDynamicAuthProvider%127.0.0.1:8134%',
    'mcpserver-http://localhost:8134%',
    'mcpserver-http://127.0.0.1:8134%',
    'http://localhost:8134%-mcpserver-usages',
    'http://127.0.0.1:8134%-mcpserver-usages',
    'dynamicAuthProviders',
]
for pat in patterns:
    cur.execute("DELETE FROM ItemTable WHERE key LIKE ?", (pat,))
con.commit()
con.close()
```

(Replace `8134` with whatever `HOST_PORT` you use.) This removes the cached UUID + token entries but leaves your `mcp.config.ws0.*` workspace config alone, so VS Code still knows about the server.

VS Code paths on the other OSes:
- macOS: `~/Library/Application Support/Code/User/globalStorage/state.vscdb`
- Linux: `~/.config/Code/User/globalStorage/state.vscdb`

#### Quick alternative for VS Code: rename the server in `mcp.json`

VS Code keys cached client IDs by server *name*. If you change the key in `mcp.json` (e.g. `sas-mcp` → `sas-mcp-fresh`) and reload the window, VS Code treats it as a brand-new server and DCR-registers from scratch. Useful for quick recovery; less useful if you accumulate stale entries over time.

**Step 5 — Restart everything.**

```sh
uv run app          # restart server
# then in VS Code: reload window, click start in mcp.json
```

You should now see this in the server log:

```
POST /register HTTP/1.1 200 OK
GET /.well-known/oauth-protected-resource/mcp HTTP/1.1 200 OK
GET /.well-known/oauth-authorization-server HTTP/1.1 200 OK
GET /authorize?client_id=<new-uuid>... HTTP/1.1 302 Found
```

The `POST /register` line is the proof that the MCP client re-registered cleanly.

### Avoiding it next time

- **Pin `MCP_SIGNING_KEY`** to a real value in `.env` and don't change it. This alone prevents the most common cause (silent Fernet key change between runs), so deleting `cache.db` becomes a rare manual operation rather than a side effect of restarts.
- **If you wipe `cache.db`, also wipe the MCP client's cached UUID in the same step.** They are paired; clearing only one creates the mismatch this doc exists to fix.

---

## Stdio: `Could not read sas-viya credentials at ...`

The stdio server logs the warning when the cache files at `~/.sas/credentials.json` and `~/.sas-mcp-server/credentials.json` are both missing or malformed. The server then falls through to the native device-code flow, which either prints a SAS Logon URL on stderr or fails with the CSRF error documented below.

### Fix

Run one of the bootstrap commands:

```sh
sas-viya auth loginCode      # if you have the SAS Viya CLI
uv run sas-mcp-login          # zero-prereq helper using the built-in vscode client
```

If your `sas-viya` profile lives outside `$HOME`, set `SAS_CLI_CONFIG` to its parent directory in `.env`. See [`examples/configuration.md`](examples/configuration.md#authentication-modes--at-a-glance) for the full auth chain.

---

## Stdio: device-code fallback fails with `403 ... CSRF token`

The server log shows:

```
Viya rejected the device-authorization request (CSRF protection on /SASLogon/oauth/device_authorization).
```

SAS Logon Manager protects the device endpoint with CSRF on most deployments, so a pure RFC 8628 call from outside a browser session can't succeed. This isn't a bug — it's a deliberate Viya admin setting.

### Fix

Use one of the two cache-based paths instead (`sas-viya auth loginCode` or `uv run sas-mcp-login`). The device-code path is meant as a last-resort fallback for Viyas without CSRF protection on that endpoint; if yours protects it, ignore the path.

---

## sas-mcp-login: command exits immediately with `Aborted.` in non-TTY shells

You run `uv run sas-mcp-login` inside a wrapper that does not provide an interactive stdin (CI runners, the Claude Code `!` prefix, `nohup ... &`, etc.) and the helper prints the URL then aborts at the `Authorization code:` prompt.

### Fix

Use the two-step variant. The first invocation persists PKCE state to `~/.sas-mcp-server/login-state.json` and exits cleanly; the second exchanges the code:

```sh
uv run sas-mcp-login                            # opens browser, prints URL, exits
# sign in, copy the code from the SAS Logon results page
uv run sas-mcp-login --code <PASTE-CODE-HERE>   # completes the exchange
```

---

## sas-mcp-login: `Invalid redirect <uri> did not match one of the registered values`

SAS Logon rejects the authorization request *after* you sign in because the `redirect_uri` you supplied isn't on the allow-list registered for your OAuth client. The built-in `vscode` client on many Viya deployments has no registered redirect URI at all.

### Fix

Re-run without `--redirect-uri` (this is the default). When `redirect_uri` is omitted, SAS Logon displays the authorization code on a results page for you to copy.

```sh
uv run sas-mcp-login            # no --redirect-uri
```

If your Viya admin has registered a specific redirect URI for the client you want to use, pass it explicitly with `--redirect-uri vscode://your-extension/sso` (or whatever they registered).

---

## HTTP: `ALLOW_RAW_BEARER=true` but raw tokens still return `401 Unauthorized`

The server is configured to accept raw upstream JWTs, but your `Authorization: Bearer <token>` call is still rejected.

### Likely causes

1. **Token expired.** Decode the JWT at [jwt.io](https://jwt.io) and check the `exp` claim. SAS Viya access tokens typically last under an hour.
2. **Token isn't a valid Viya JWT.** The server validates against the JWKS at `${VIYA_ENDPOINT}/SASLogon/token_keys`; tokens from a different issuer will fail.
3. **`VIYA_ENDPOINT` in the server's env points at a different Viya** than the one that minted the token. The JWKS won't have a matching signing key.
4. **`ALLOW_RAW_BEARER` is not actually set on the server.** Check `podman inspect <container> --format '{{.Config.Env}}'` (or the equivalent for your runtime). The env var must be read by the *running* server process, not just present in your `.env` file on the host.

### Fix

Verify each step. To rule out an expired token, mint a fresh one and retry. To rule out a JWKS mismatch, check that `iss` in the token matches `${VIYA_ENDPOINT}/SASLogon/oauth/token`.

---

## Stdio in container: token cache file isn't found

You run the stdio server in a container and see `Loaded access token from ...` is *not* logged before tool calls fail.

### Why

`stdio_server.py` looks under the *container's* `$HOME` (typically `/app` for the `sas` user), not the host's. Without an explicit volume mount, the container has no access to the cache files on your host.

### Fix

Mount the cache directory and (for the `sas-viya` CLI path) set `SAS_CLI_CONFIG` to the mount point's parent:

```sh
# sas-viya CLI cache
podman run --rm -i \
  --env-file .env \
  -v "$HOME/.sas:/app/.sas:ro" \
  -e SAS_CLI_CONFIG=/app \
  ghcr.io/sassoftware/sas-mcp-server:latest app-stdio

# sas-mcp-login cache (default location inside container)
podman run --rm -i \
  --env-file .env \
  -v "$HOME/.sas-mcp-server:/app/.sas-mcp-server:ro" \
  ghcr.io/sassoftware/sas-mcp-server:latest app-stdio
```

The container reads the file on every tool call, so refreshing the host-side cache (`sas-viya auth loginCode` again, or re-running `sas-mcp-login`) is picked up without restarting the container.
