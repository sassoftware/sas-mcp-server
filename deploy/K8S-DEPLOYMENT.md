# Kubernetes deployment — investigation

**Date:** 2026-08-10 · **Against:** `ghcr.io/sassoftware/sas-mcp-server:latest` (v1.8.0, 237 MB)
**Status:** investigation only — no manifests or chart are shipped in this repo yet.

---

## Verdict

The image is **close to k8s-ready and nothing here is hard to fix**, but it should not
go behind an ingress as-is. Four issues need a decision before a first deployment, and
one of them is a security exposure rather than an operational wrinkle.

| | Finding | Severity |
|---|---|---|
| 1 | `MCP_SIGNING_KEY` defaults to the literal `"default"` — a publicly known key | **High** (security) |
| 2 | OAuth client registrations live on the pod's ephemeral filesystem | **High** (breaks restarts + replicas) |
| 3 | The production image runs uvicorn's **dev auto-reloader** as PID 1 | Medium |
| 4 | Warm compute-session cache is in-process, so replicas diverge | Medium |

Everything else — health endpoint, non-root user, `0.0.0.0` bind, graceful SIGTERM,
multi-arch images with provenance — is already in good shape.

**Recommendation: start at `replicas: 1`.** Findings 2 and 4 both break at >1 replica for
reasons that need code changes, not configuration. Scaling out is a follow-up, not a
day-one option.

---

## How this was tested

Pulled the published image and ran it against a dummy endpoint, then probed the running
container — no source changes:

```sh
podman run -d --name sasmcp-probe -p 18134:8134 \
  -e VIYA_ENDPOINT=https://viya.example.invalid \
  ghcr.io/sassoftware/sas-mcp-server:latest
```

Every claim below is from that container's logs, filesystem, or observed signal
behaviour, except where explicitly marked as read from source.

---

## 1. `MCP_SIGNING_KEY` defaults to a publicly known value

**Severity: high. Silent.**

`config.py:119` reads `MCP_SIGNING_KEY = os.getenv("MCP_SIGNING_KEY", "default")` and
passes it to `PermissiveOAuthProxy(jwt_signing_key=...)`. FastMCP derives the actual
signing key deterministically:

```python
# fastmcp/server/auth/oauth_proxy — for a str key
jwt_signing_key = derive_jwt_key(
    low_entropy_material=jwt_signing_key, salt="fastmcp-jwt-signing-key",
)
```

The derivation is a pure function of the input string, and the input string in an
unconfigured deployment is the literal `"default"` — published in this open-source
repo. **Anyone can derive the signing key and forge MCP access tokens** against any
deployment that did not override it.

The container says so on startup, though it undersells it:

```
WARNING  jwt_signing_key is less than 12 characters; it is recommended to use a
         longer. string for the key derivation.
```

That reads as a strength complaint. The real problem is that the value is *known*, not
that it is short.

On `localhost:8134` the blast radius is small. Behind an ingress it is not.

**Fix:** generate a high-entropy value, store it in a `Secret`, and inject it. Because
the derivation is deterministic, every replica that shares the env var produces
compatible tokens — no shared keystore needed for this particular field.

```yaml
env:
  - name: MCP_SIGNING_KEY
    valueFrom:
      secretKeyRef: { name: sas-mcp-server, key: signing-key }
```

Worth considering separately: making `config.py` refuse to start with the default value
when `MCP_BASE_URL` is not localhost, so this cannot be deployed by accident.

---

## 2. OAuth client registrations are on the pod's ephemeral filesystem

**Severity: high.**

FastMCP's `OAuthProxy` defaults to a Fernet-encrypted file tree (`FileTreeStore`) for dynamically
registered OAuth clients, at `settings.home / "oauth-proxy"`. In the container that
resolves to a real, writable path:

```
$ ls -la /app/.local/share/fastmcp/oauth-proxy/
drwxr-xr-x 3 sas sas 4096 .
drwxr-xr-x 2 sas sas 4096 247befbf0d8c        <- keyed by the derived signing key
$ id
uid=100(sas) gid=101(sas)
```

It works today because `adduser --home /app` makes `/app` writable by `sas`. Three
consequences in k8s, none of them visible until they bite:

- **Every restart and every rollout wipes it.** Registered clients must re-register.
- **It is per-pod.** With `replicas > 1`, a client registered against pod A is unknown to
  pod B, so the OAuth dance fails depending on which pod the request lands on.
- **It breaks under `readOnlyRootFilesystem: true`** — a standard hardening baseline, and
  part of the Pod Security "restricted" profile. `OAuthProxy.__init__` mkdirs its storage
  directory and `config.py` builds the proxy at module scope, so the failure lands at
  *import*: the container crash-loops with `OSError: [Errno 30] Read-only file system`.
  (An earlier revision of this document said it failed later at runtime with a healthy
  pod. That was wrong — it never starts.)

**Fix (single replica):** mount a volume at `/app/.local/share/fastmcp` — an `emptyDir`
restores compatibility with a read-only root filesystem; a PVC additionally survives
restarts.

**Fix (multiple replicas):** the volume is not enough — a `ReadWriteOnce` PVC cannot be
shared, and an `emptyDir` is per-pod by definition. `OAuthProxy` accepts a
`client_storage=` argument, so a shared backend (Redis, or any `key-value` store) would
be the real solution. **This repo does not wire that argument today**, so it is a code
change, not a values file.

---

## 3. The production image runs the dev auto-reloader

**Severity: medium.**

`main.py:10-12` — the entry point behind `CMD ["app"]`:

```python
uvicorn.run(
    "sas_mcp_server.mcp_server:app", host="0.0.0.0", port=HOST_PORT, reload=True
)
```

Confirmed live in the published image:

```
INFO:     Will watch for changes in these directories: ['/app']
INFO:     Started reloader process [1] using WatchFiles
INFO:     Started server process [3]
```

So PID 1 is a file watcher, supervising the actual server as a child, watching a tree of
**1,223 directories / 7,404 files** (mostly `.venv`). Costs:

- inotify watches are a **node-wide** resource (`fs.inotify.max_user_watches`), shared by
  every pod on the node. Many replicas each watching a full virtualenv is a noisy
  neighbour, and exhaustion surfaces as unrelated pods failing to start.
- Wasted CPU and memory for a capability that cannot be used — the image layer is
  immutable, so nothing will ever change under `/app`.
- An extra process in the tree for no benefit.

**What it does *not* break, tested rather than assumed:** graceful shutdown still works.
SIGTERM propagates through the reloader and the lifespan runs:

```
$ podman kill --signal TERM sasmcp-probe
INFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.       <- lifespan ran: warm sessions torn down
INFO:     Finished server process [3]
INFO:     Stopping reloader process [1]
exited after 2397 ms, exit code 0
```

**Fix:** drop `reload=True`, or gate it behind a `DEV_RELOAD` env var so local
development keeps it. One line.

---

## 4. Warm compute sessions are cached in-process

**Severity: medium — a scaling limit, not a bug.**

`viya_utils._ComputeSessionCache` keeps one reusable Viya compute session per
`(context, user)` in process memory, with a per-key `asyncio.Lock`. That is exactly right
for one process and does not survive horizontal scaling:

- The same user hitting a different pod creates a **second** Viya compute session, so
  session count multiplies by replica count.
- `reset_compute_session` only resets the cache on whichever pod serves that call, so
  "my session is stuck" is not reliably fixable by the user.
- Warm-session state persistence — the field-feedback gotcha documented in v1.8.0 —
  becomes non-deterministic, because whether state carries over depends on pod routing.

**Fix (day one):** `replicas: 1`, or session affinity at the ingress so a given user
consistently lands on one pod. Note affinity solves *this* and finding 2's read path, but
not registration writes.

**Grace-period nuance:** the lifespan teardown issues a `DELETE` per cached session, so
shutdown time scales with cached sessions × Viya latency. The 2.4 s measured above was
with zero warm sessions. With many warm users, raise
`terminationGracePeriodSeconds` above the default 30 s, or sessions leak on rollout and
wait for Viya to reap them.

---

## What is already right

- **`/health` returns 200 with no auth**, verified against the running container:
  `{"status":"healthy","service":"sas-viya-execution-mcp"}`. It is registered via
  `@mcp.custom_route`, outside the MCP auth chain, so probes need no credentials.
  *Caveat:* it is static — it does not check Viya reachability, so it is a good
  **liveness** probe but tells readiness nothing about upstream health.
- **Runs as non-root** (`uid=100(sas)`) with no capabilities needed, so a restricted
  `securityContext` is straightforward.
- **Binds `0.0.0.0`**, so a `Service` reaches it without changes.
- **Graceful SIGTERM**, verified above.
- **Multi-arch images** (`linux/amd64`, `linux/arm64`) with signed provenance
  attestations, published to GHCR on `v*` tags plus `edge`/`sha-*` — everything needed
  to pin an image digest in a manifest.

---

## Configuration that must change for k8s

| Setting | Default | Why it matters |
|---|---|---|
| `MCP_SIGNING_KEY` | `"default"` | Publicly known — see finding 1. Must be a `Secret`. |
| `MCP_BASE_URL` | `http://localhost:8134` | OAuth redirects point here. Must be the **external ingress URL** or the browser flow dead-ends. |
| `VIYA_ENDPOINT` | *(required)* | Startup fails without it — good, it is a `ConfigError`, not a silent default. |
| `SSL_VERIFY` | `true` | Set `false` only for a self-signed Viya certificate. It disables verification for the whole process, including the OAuth token exchange that carries the user's authorization code and issued Viya token — so prefer a trusted certificate where you can. |
| `COLLECTION_LOG_PATH` | `~/.sas-mcp-server/tool-usage.log` | Only if telemetry is enabled: writes to the ephemeral layer and needs a volume. Cross-process appends are not coordinated, so one path per pod. |
| `ALLOW_LOCAL_FILE_UPLOAD` | **`true`** | **Set `false` in any container deployment.** It lets `upload_file`/`upload_data`'s `file_path` source read *the server's own disk* with no allowlist — which in a pod means `/proc/self/environ` (holding `MCP_SIGNING_KEY`), the ServiceAccount token, and the OAuth store, exfiltrated to Viya Files by any authenticated caller. The source exists for stdio mode, where the server's disk *is* the user's machine; in a container it can only read the pod, so nothing legitimate is lost — `content`, `url` and `upload_inline_data` all still work. Absent from `.env.sample`, so it is easy to miss. |
| `ALLOW_RAW_BEARER` | `false` | If enabled, understand the scope: the fallback verifier uses `audience=[]` with no required scopes, so **any** JWT this Viya's SASLogon signed is a full MCP credential — including tokens minted for other clients — with no client registration and no consent screen. |
| `MCP_TIERS` / `MCP_READ_ONLY` | all / `false` | Worth setting deliberately for a shared deployment rather than exposing all 89 tools. |
| `MCP_LANDING_PAGE` | `true` | A browser `GET /mcp` gets an unauthenticated HTML page describing the deployment (server name/version, Viya host, exposed tiers, tool names + summaries, client-config snippets) instead of the 401. Handy for onboarding users who are handed the URL; set `false` if you would rather not advertise even that much to anyone who can reach the ingress. |

FastMCP also warns on startup: `Using non-secure cookies for development; deploy with
HTTPS for production` — so **TLS terminates at the ingress**, and the ingress must be the
`MCP_BASE_URL`.

---

## Reference manifest

> Since this investigation: the shipped artifacts are `deploy/k8s/sas-mcp-server.yaml`
> (nginx, host root), `deploy/k8s/sas-mcp-server-contour.yaml` (Contour, mounted under a
> path prefix) and the Helm chart, which renders either router from
> `ingress.controller`. See [`CONTOUR-DEPLOYMENT.md`](CONTOUR-DEPLOYMENT.md) and
> [`AKS-CONTOUR.md`](AKS-CONTOUR.md).

Illustrative, to make the findings concrete — not a supported artifact. Single replica,
restricted security context, with the volume that finding 2 requires.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: sas-mcp-server }
spec:
  replicas: 1                       # see findings 2 and 4 before raising this
  selector: { matchLabels: { app: sas-mcp-server } }
  template:
    metadata: { labels: { app: sas-mcp-server } }
    spec:
      terminationGracePeriodSeconds: 60   # lifespan DELETEs one session per warm user
      securityContext:
        runAsNonRoot: true
        runAsUser: 100
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: server
          image: ghcr.io/sassoftware/sas-mcp-server:1.13.0
          ports: [{ containerPort: 8134 }]
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true    # needs the volume below
            capabilities: { drop: ["ALL"] }
          env:
            - name: VIYA_ENDPOINT
              value: https://viya.example.com
            - name: MCP_BASE_URL
              value: https://mcp.example.com        # the ingress, NOT localhost
            - name: MCP_SIGNING_KEY
              valueFrom:
                secretKeyRef: { name: sas-mcp-server, key: signing-key }
          volumeMounts:
            - name: fastmcp-state
              mountPath: /app/.local/share/fastmcp  # OAuth client registrations
          livenessProbe:
            httpGet: { path: /health, port: 8134 }
            initialDelaySeconds: 10
          readinessProbe:
            httpGet: { path: /health, port: 8134 }
          resources:
            requests: { cpu: 100m, memory: 256Mi }
            limits:   { memory: 1Gi }   # headroom for transient result buffers, see SCALING.md
      volumes:
        - name: fastmcp-state
          emptyDir: {}                # PVC instead, to survive restarts
```

---

## Suggested order of work

1. **Drop `reload=True`** (finding 3) — one line, no design decision, benefits Docker users too.
2. **Fail fast on the default signing key** when `MCP_BASE_URL` is not localhost (finding 1) — turns a silent exposure into a startup error.
3. **Document the state directory** and ship the volume mount in an example manifest (finding 2).
4. **Then decide on multi-replica**, which needs `client_storage=` wired to a shared backend and a plan for the compute-session cache (findings 2 and 4).

---

## Caveats

- Tested against a **dummy** `VIYA_ENDPOINT`. Startup, health, filesystem layout, and
  signal handling are real; nothing exercised an actual OAuth flow or a live compute
  session, so finding 4's session-multiplication is reasoned from source, not measured.
- Run under **podman on WSL**, not a real cluster. The container-level facts transfer; the
  inotify and grace-period concerns in findings 3 and 4 are extrapolations to a node,
  flagged as such.
- No Helm chart, kustomization, or CI deployment path was written — this is an
  investigation only.
