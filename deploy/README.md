# Deploying the SAS Viya MCP Server

Everything about running the server somewhere other than your laptop. For
configuring Viya auth and connecting MCP clients, see
[`../examples/configuration.md`](../examples/configuration.md).

| Target | Start here |
|---|---|
| **Container** (Docker / Podman) | [`docker.md`](docker.md) — build or pull the image, run it |
| **Kubernetes** | this file, plus the artifacts below |

### Kubernetes artifacts

| | Path | Use when |
|---|---|---|
| **Sample manifest (nginx)** | `k8s/sas-mcp-server.yaml` | One environment, values edited in place, host root. Read this first — it is the clearest statement of what gets deployed. |
| **Sample manifest (Contour)** | `k8s/sas-mcp-server-contour.yaml` | Same, but routed by Contour and mounted under a path prefix on the Viya hostname. |
| **Helm chart** | `helm/sas-mcp-server/` | Several environments, or the settings belong in a values file. Renders either router. |

### Which router

The chart renders **one** of two objects, chosen by `ingress.controller`:

| `ingress.controller` | Renders | Notes |
|---|---|---|
| `contour` *(default)* | `projectcontour.io/v1` `HTTPProxy` | Delegated from an existing root proxy (SAS Viya deploys one) or standalone. The only mode that can mount the server under a **path prefix**, reusing the Viya hostname and certificate. See [`CONTOUR-DEPLOYMENT.md`](CONTOUR-DEPLOYMENT.md). |
| `nginx` | `networking.k8s.io/v1` `Ingress` | Exactly what chart 0.1.x rendered, annotations and all. Set `ingress.className: nginx` with it. |

Chart 0.2.0 flipped the default from nginx to Contour. An unchanged 0.1.x values
file **fails the render** with an explanatory message rather than silently
dropping your `Ingress` — set `ingress.controller: nginx` to stay as you were.

The chart's defaults are the safe general-purpose ones. Keep anything specific
to your environment — the Viya endpoint, the OAuth client id, the ingress host,
the TLS secret, and whether `sslVerify` has to be off for a self-signed
certificate — in your **own** values file, passed with `-f`:

```sh
helm upgrade --install sas-mcp deploy/helm/sas-mcp-server -n <namespace> \
  -f my-environment.yaml
```

`deploy/helm/values-*.yaml` is gitignored for exactly that reason: those files
describe a deployment, not the project, and hostnames and client ids should not
travel with the repo.

### Background

Why the manifests look the way they do — worth reading before changing them:

- [`K8S-DEPLOYMENT.md`](K8S-DEPLOYMENT.md) — what had to be true before this
  could go behind an ingress: the signing key, the OAuth state store, the dev
  auto-reloader, and the in-process session cache.
- [`CONTOUR-DEPLOYMENT.md`](CONTOUR-DEPLOYMENT.md) — Contour specifics:
  delegation from the Viya root proxy, mounting under a path prefix, and the
  exact routing every OAuth discovery URL needs.
- [`AKS-CONTOUR.md`](AKS-CONTOUR.md) — what is different on Azure: ACR
  mirroring, the load balancer's 4-minute idle timeout, hairpin DNS to the Viya
  hostname, and zonal storage.
- [`SCALING.md`](SCALING.md) — how resources track user count, measured against
  a live deployment. Short version: the MCP pod is not what you scale.

The chart is linted, rendered and `kubectl apply --dry-run=client`-validated in
both variants, and has been applied to a real cluster — the numbers in
`SCALING.md` come from that deployment rather than from estimates.

---

## The one thing worth understanding first

The MCP endpoint is at `/mcp`, but **the OAuth endpoints are its siblings at the
host root, not its children.** Enumerating the running app's routes gives:

```
/mcp                                         <- the MCP endpoint
/authorize  /token  /register  /consent      <- OAuth 2.0, at the ROOT
/auth/callback
/.well-known/oauth-authorization-server
/.well-known/oauth-protected-resource/mcp
```

An MCP client discovers those URLs automatically: it calls `/mcp`, gets a 401
carrying `resource_metadata="https://<host>/.well-known/oauth-protected-resource/mcp"`,
and follows the chain from there. Confirmed by driving the real app:

```
POST /mcp -> 401
  WWW-Authenticate: Bearer …, resource_metadata="https://your-viya-server.com/.well-known/oauth-protected-resource/mcp"

GET /.well-known/oauth-authorization-server -> 200
  authorization_endpoint: https://your-viya-server.com/authorize
  token_endpoint:         https://your-viya-server.com/token
  registration_endpoint:  https://your-viya-server.com/register
```

So routing only `/mcp` gets you a server that a client can reach and never sign
in to. **All eight paths must reach the service**, which is why the ingress
lists them explicitly.

Those paths are claimed on a host Viya already owns. Viya's own OAuth lives
under `/SASLogon/oauth/*`, so they should be free — but check before applying:

```sh
kubectl get ingress -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.rules[*].http.paths[*].path}{"\n"}{end}'
```

If a collision shows up, see [Alternative: everything under `/mcp`](#alternative-everything-under-mcp).

---

## Prerequisites

**1. Signing key.** The server derives its JWT signing key from
`MCP_SIGNING_KEY`. Unset, it falls back to the literal string `"default"` —
published in this open-source repo, so anyone can derive the key and forge
tokens. Create a real one:

```sh
kubectl -n sas-mcp create secret generic sas-mcp-server \
  --from-literal=signing-key="$(openssl rand -base64 32)"
```

The derivation is deterministic, so replicas sharing the secret issue
compatible tokens. Pass it through the secret rather than a values file, so it
never lands in version control.

**2. SASLogon redirect URI.** Register this on OAuth client `sas-mcp`:

```
https://your-viya-server.com/auth/callback
```

Without it the browser sign-in dead-ends after the Viya login page. This is a
Viya-side change; the deployment cannot do it for you.

**3. Image pull.** The manifests use `ghcr.io/sassoftware/sas-mcp-server:1.14.2`.
If the cluster cannot pull from ghcr.io, mirror it first:

```sh
podman pull ghcr.io/sassoftware/sas-mcp-server:1.14.2
podman tag ghcr.io/sassoftware/sas-mcp-server:1.14.2 \
  registry.example.com/library/sas-mcp-server:1.14.2
podman push registry.example.com/library/sas-mcp-server:1.14.2
```

then set `image.repository` accordingly.

---

## Deploy

### Sample manifest

```sh
kubectl apply -f deploy/k8s/sas-mcp-server.yaml
kubectl -n sas-mcp rollout status deploy/sas-mcp-server
```

### Helm

```sh
helm upgrade --install sas-mcp deploy/helm/sas-mcp-server \
  --namespace sas-mcp \
  --set fullnameOverride=sas-mcp-server
```

Override per environment:

```sh
helm upgrade --install sas-mcp deploy/helm/sas-mcp-server -n sas-mcp \
  --set viya.endpoint=https://other-viya.example.com \
  --set ingress.host=other-viya.example.com \
  --set viya.clientId=sas-mcp-prod \
  --set server.tiers=0-4 \
  --set persistence.type=pvc
```

`MCP_BASE_URL` is derived from `ingress.host` and `ingress.tls.enabled`, so it
cannot drift out of step with the ingress.

### Verify

```sh
kubectl -n sas-mcp port-forward svc/sas-mcp-server 8134:8134
curl -s localhost:8134/health
# {"status":"healthy","service":"sas-viya-execution-mcp"}

# through the ingress — a 401 with a resource_metadata pointer is CORRECT,
# it is how a client discovers the sign-in flow
curl -isk https://your-viya-server.com/mcp -X POST \
  -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | head -20
```

Then point an MCP client at `https://your-viya-server.com/mcp`.

### Both auth paths work here

`ALLOW_RAW_BEARER` is additive, not a replacement — HTTP mode always runs PKCE,
so the browser sign-in and token-bearing clients share the one `/mcp` endpoint.
Verified against the deployment: dynamic client registration returns a
`client_id`, and `/authorize` 302s onward into the consent step.

**This topology also sidesteps the CSP problem** that `examples/configuration.md`
warns about. Viya sends `Content-Security-Policy: form-action 'self'`, which
blocks the post-login redirect to an MCP server on a *different* origin — the
reason local development is told to disable the directive. Because this
deployment serves `/auth/callback` on the **Viya host itself**, the callback is
same-origin and `'self'` permits it. Nothing needs disabling.

That advantage disappears if you move the server to its own hostname.

---

## Deliberate choices

**`replicas: 1`.** Two things break at more than one replica, neither fixable in
a manifest:

- OAuth client registrations live in a per-pod file-tree store, so a client
  registered on pod A is unknown to pod B. `OAuthProxy` accepts a
  `client_storage=` argument for a shared backend, but this repo does not wire
  it yet.
- Warm Viya compute sessions are cached in process, so the same user hitting a
  different pod spins up a second session on Viya, and `reset_compute_session`
  only resets whichever pod serves that call.

The chart warns if you raise it anyway.

**`readOnlyRootFilesystem: true` plus a state volume.** The OAuth store needs a
writable path, and it is needed *at import*: `OAuthProxy.__init__` mkdirs its
storage directory, and `config.py` constructs the proxy at module scope. Without
the volume the container **crash-loops at startup** with
`OSError: [Errno 30] Read-only file system` — it does not start healthy and fail
later. The chart fails the render on that combination rather than let you find
out live. With `emptyDir` (the default) registrations are lost on every restart
and clients re-register; `persistence.type=pvc` keeps them.

**`terminationGracePeriodSeconds: 60`.** Shutdown issues one `DELETE` per warm
compute session. Measured 2.4 s with none cached; with many warm users this
scales with Viya latency, and the 30 s default would leak sessions on rollout.

**Router timeouts, in both flavours.** MCP streamable HTTP holds long-lived
connections and streams responses. nginx's 60 s default read timeout would cut a
long SAS execution mid-call and its default buffering would stall streamed
output until the response completed, so the chart sets
`proxy-read-timeout`/`proxy-send-timeout`/`proxy-buffering: off`. Envoy behind
Contour has the same problem sooner — a 15 s default response timeout — so the
Contour routes carry `timeoutPolicy: {response: infinity, idle: 5m}` instead.
Whatever sits in front of the cluster needs the same treatment; on AKS that is
the load balancer's 4-minute TCP idle timeout.

**No node affinity or tolerations.** The other workloads on this cluster pin to
the `app=llm` node pool because they are GPU model servers. This is a light
Python service with no such need, so it schedules anywhere. Set
`nodeSelector`/`tolerations` if you want it on that pool regardless.

---

## Usage telemetry (optional, off by default)

The server can log every tool call to a JSONL file — tool name, arguments, the
model's stated `goal`, status, error, and latency. The chart wires it up; it is
disabled unless you ask for it:

```sh
helm upgrade sas-mcp deploy/helm/sas-mcp-server -n sas-mcp --reuse-values \
  --set telemetry.enabled=true \
  --set telemetry.persistence.type=pvc      # else the log dies with the pod
```

That sets `COLLECTION_MODE` and friends, and mounts a volume at the log's
directory. Read it back with:

```sh
kubectl -n sas-mcp exec deploy/sas-mcp-server -- cat /var/log/sas-mcp/tool-usage.log
```

**Before enabling this on a shared server**, be clear about what it is: the log
captures the SAS code and queries submitted by *every user of the deployment*,
not just you, along with the model's stated reason for each call. Redaction
covers credential-shaped keys, inline Bearer/JWT tokens and the Viya hostname —
it does **not** detect PII in data values. On a laptop this is self-consent; on
a shared deployment it is collecting other people's work, so tell them first.
Nothing is transmitted anywhere: the log stays on the volume until someone
deliberately copies it off.

`telemetry.logResults` is a tri-state — `never` / `failures` / `always` —
defaulting to **`failures`**: full (capped, redacted) result bodies only for
calls that errored or whose tool declared a failure. That is the highest-value
trace data and the least likely to carry table rows.

**One version trap.** The tri-state needs **schema v3**, i.e. image `1.9.0` or
newer, which is what this chart now deploys by default. If you *pin* `image.tag`
to `1.8.0` or older, that build parses the same variable with `env_bool` —
`true/1/yes/on` and `false/0/no/off` only — and silently falls back to its
default for anything else, so `failures` would quietly mean shape-only.
`"true"` and `"false"` mean the same thing on every build, and the install notes
warn if the combination looks wrong.

---

## Alternative: mount under a path prefix

If claiming seven root paths on the Viya host is unacceptable — or if a second
hostname would mean a new certificate and a new DNS record you cannot get
approved — mount the server under a prefix instead:
`https://<viya-host>/sas-mcp/mcp`. Set `ingress.pathPrefix: /sas-mcp` with
`ingress.controller: contour` (`k8s/sas-mcp-server-contour.yaml` is the same
thing as a plain manifest).

**No code change is needed.** `MCP_BASE_URL` carries the prefix, so the server
advertises `…/sas-mcp/authorize`, `…/sas-mcp/token`, `…/sas-mcp/register`,
`…/sas-mcp/consent` and `…/sas-mcp/auth/callback`, and the router strips the
prefix before the pod — which still serves everything at its own root — sees the
request.

**Two root paths remain**, and no configuration can move them: RFC 9728 places
protected-resource metadata at the host root
(`/.well-known/oauth-protected-resource/sas-mcp/mcp` — the prefix goes *inside*
the path), and RFC 8414 does the same for authorization-server metadata. A third
route is needed because the app serves that second document at the bare
`/.well-known/oauth-authorization-server` while RFC 8414 §3 clients ask for it
with the issuer's path appended.

So this trades seven root paths for two — three routes in total, plus optional
host-root aliases for clients that ignore the issuer path. The chart renders all
of them; [`CONTOUR-DEPLOYMENT.md`](CONTOUR-DEPLOYMENT.md) explains each one.

A third option avoids the OAuth paths entirely: set `ingress.oauthEnabled=false`
and have clients pass a Viya token directly (`ALLOW_RAW_BEARER=true`, already on
by default here). That collapses the ingress to the single `/mcp` rule, but the
browser sign-in flow is then unavailable — every client must bring its own
token.

---

## Not covered

- `SSL_VERIFY=false` is set for the self-signed Viya certificate. It disables
  certificate verification for the whole server process, not just the Viya API
  calls, so a trusted certificate is the better answer wherever you can get one.
- No live-cluster load or failover testing beyond what `SCALING.md` records.
- No NetworkPolicy, PodDisruptionBudget, or HPA — an HPA in particular would be
  actively wrong while replicas are capped at 1.
