# AKS notes: SAS Viya MCP Server on Azure Kubernetes Service

Everything in `deploy/CONTOUR-DEPLOYMENT.md` applies unchanged on AKS. This file collects only what
is *different* because the cluster is Azure — the things that cost an afternoon if nobody says them
first.

> The AKS + Contour + path-prefix deployment this is drawn from was contributed by GitHub user
> **sasaom-sdksbe**.

---

## 1. The image usually cannot come from GHCR

Most SAS Viya clusters on AKS have controlled egress (Azure Firewall, a UDR to an appliance, or a
private cluster), and `ghcr.io` is rarely on the allowlist. Rather than argue about the firewall,
copy the image into the Azure Container Registry the cluster already pulls from. `az acr import`
does it server-side — nothing transits your laptop:

```sh
az acr import --name aomdk \
  --source ghcr.io/sassoftware/sas-mcp-server:1.14.2 \
  --image sas-mcp-server:1.14.2
```

Give the cluster pull rights once (this grants `AcrPull` to the kubelet identity):

```sh
az aks update -n <cluster> -g <resource-group> --attach-acr aomdk
```

Then point the chart at it — and pin by digest in production, because a tag in a mirrored registry
is a moving target that nobody outside your org can audit:

```yaml
image:
  repository: aomdk.azurecr.io/sas-mcp-server
  tag: 1.14.2
```

If the registry is not attached to the cluster, use `imagePullSecrets` instead:

```sh
kubectl -n sas-mcp create secret docker-registry acr \
  --docker-server=aomdk.azurecr.io --docker-username=<sp-id> --docker-password=<sp-secret>
```

```yaml
imagePullSecrets:
  - name: acr
```

Re-import on every upgrade. A stale mirror is the most common reason an AKS deployment is running an
older server than its `helm list` output claims.

---

## 2. Reuse the Viya certificate — that is the whole point of the prefix

On AKS the certificate for the Viya hostname is typically issued by the organisation's PKI (or
cert-manager against it) and lives in a Secret in the Viya namespace, owned by the Viya deployment.
Getting a *second* hostname approved means a new DNS record in the corporate zone, a new certificate
request, and often a new public IP — weeks, in a regulated shop.

Mounting the MCP server at `https://<viya-host>/sas-mcp` reuses all three. That is why the path
prefix exists, and why delegation from `sas-httpproxy-root` is the default shape here: the child
proxies inherit the root's FQDN and its TLS secret, and nothing in the Viya namespace changes except
one `includes` list.

If you do go with a dedicated hostname and a standalone proxy, remember Contour needs the TLS Secret
**in the proxy's own namespace**, or a `TLSCertificateDelegation` in the namespace that holds it.

---

## 3. Azure Load Balancer idle timeout will cut long calls

The Standard Load Balancer in front of Contour's Envoy service has a **4-minute TCP idle timeout by
default**. MCP streamable HTTP holds the connection open for the duration of a tool call, and a long
`execute_sas_code` or a batch job poll can be quiet for longer than that — the client then sees a
connection reset with no server-side error to point at.

The annotation belongs on the **Envoy Service** (part of the Contour installation, not this chart);
the maximum is 30 minutes:

```yaml
metadata:
  annotations:
    service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout: "30"
```

Keep the chart's `ingress.contour.timeoutPolicy.idle` at or below whatever you set here, so Envoy
closes idle streams on its own terms rather than having Azure sever them. The chart already disables
the *response* timeout (`response: infinity`), which is the Envoy-side equivalent of this problem.

If Azure Front Door or an Application Gateway sits in front of the cluster, check its timeout too —
Front Door's origin response timeout defaults to well under an hour and applies to the whole
response, streaming included.

---

## 4. Hairpin: the pod calling Viya's public hostname

`VIYA_ENDPOINT` is normally the public Viya URL, so a pod in the same cluster resolves that name to
the ingress load balancer's IP and the traffic leaves the cluster and comes back. On some AKS network
configurations that hairpin fails or is asymmetric, and the symptom is unhelpful: the MCP server
starts fine, serves its landing page, and every Viya call times out.

Two ways out, in order of preference:

1. **Let it hairpin, but confirm it first** — from inside the cluster, before blaming the app:
   ```sh
   kubectl -n sas-mcp exec deploy/sas-mcp -- \
     python -c "import httpx;print(httpx.get('https://lab401.example.com/SASLogon/.well-known/openid-configuration',verify=False).status_code)"
   ```
2. **Resolve the Viya FQDN to the in-cluster ingress** with a CoreDNS rewrite or a `hostAliases`
   entry on the pod. Use the FQDN, never the Service name: the certificate and the SASLogon token
   issuer are both bound to the public hostname, so anything that changes the name in the request
   breaks TLS verification or token validation.

---

## 5. Storage classes

The chart's default `persistence.type: emptyDir` costs nothing and loses OAuth client registrations
on restart. If you switch to a PVC on AKS:

- `managed-csi` (Azure Disk) is `ReadWriteOnce` and **zonal** — the disk pins the pod to the zone it
  was provisioned in. With one replica that is fine; it does mean a zone outage needs manual
  attention.
- `azurefile-csi` is `ReadWriteMany` but needs `mountOptions` with `uid`/`gid` to be writable by a
  non-root container, and this image runs as UID 100 / GID 101.
- A ReadWriteOnce PVC also forces the Deployment off `RollingUpdate` (the chart handles this: see the
  `hasRWO` helper) or the new pod deadlocks waiting for the old one's disk.

The cluster autoscaler evicting the pod is a normal event on AKS. `terminationGracePeriodSeconds: 60`
(the chart default) matters here — the lifespan issues one `DELETE` per warm compute session on
shutdown, and a SIGKILL leaks them until Viya reaps them.

---

## 6. TLS to Viya

`SSL_VERIFY: "false"` (`viya.sslVerify: false`) is convenient in a lab and disables verification for
the **whole process**, including the OAuth token exchange that carries the user's authorization code
and the issued Viya token. On a shared cluster, mount the CA instead — httpx builds a default SSL
context, which honours `SSL_CERT_FILE`:

```yaml
viya:
  sslVerify: true
extraEnv:
  - name: SSL_CERT_FILE
    value: /etc/ssl/viya/ca.crt
```

…with the CA mounted from a ConfigMap or Secret at that path. Verify it took effect with the same
`kubectl exec` probe as above, minus `verify=False`.

---

## 7. What the server does *not* need on Azure

Worth saying because it shortens the security review: the MCP server calls **no Azure API**. It needs
no managed identity, no workload identity federation, no role assignment beyond the kubelet's
`AcrPull`. It talks HTTPS to SAS Viya and nothing else, and the chart mounts no ServiceAccount token
(`automountServiceAccountToken: false`). Its only secret is the JWT signing key.

---

## 8. Checklist

- [ ] Image imported into ACR, cluster attached or `imagePullSecrets` set, tag or digest pinned
- [ ] `MCP_BASE_URL` (chart: `ingress.host` + `ingress.pathPrefix`) is the externally reachable URL
- [ ] SASLogon client has the redirect URI **including the prefix**: `…/sas-mcp/auth/callback`
- [ ] Signing key Secret created out of band; `signingKey.existingSecret` points at it
- [ ] Root `HTTPProxy` patched with the includes the chart printed; nothing reports "orphaned"
- [ ] Envoy Service carries the Azure LB idle-timeout annotation
- [ ] Pod can reach `VIYA_ENDPOINT` from inside the cluster
- [ ] The four discovery `curl`s in `deploy/CONTOUR-DEPLOYMENT.md` §4 all return JSON
