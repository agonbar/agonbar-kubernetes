# Tenant: `tokkenno` (external dev sandbox)

Limited-access setup for an external developer. They get **full control inside
the `tokkenno` namespace only** and **zero visibility** into the rest of the
cluster.

## What's here (managed by ArgoCD → `tenant-tokkenno` Application)

| File | Purpose |
|------|---------|
| `namespace.yaml` | The `tokkenno` namespace. |
| `rbac.yaml` | RoleBinding to the built-in `admin` ClusterRole **scoped to `tokkenno`**, plus a cluster-scoped `get`-only-my-own-namespace helper. |
| `quota.yaml` | `ResourceQuota` + `LimitRange` so the tenant can't exhaust the cluster (no LB IPs, no NodePorts). |
| `netpol.yaml` | Ingress isolation (reachable only from own ns + traefik) and egress isolation (internet + DNS, but not other cluster pods/services). |

## What the dev **can** do
- Deploy anything in `tokkenno`: Deployments, Services, Ingress, Secrets,
  ConfigMaps, ServiceAccounts, PVCs, Jobs…
- `kubectl get ns tokkenno` (by name).

## What the dev **cannot** do
- See/list other namespaces, or anything in them.
- `get nodes`, PVs, or any cluster-scoped resource (except the one namespace get).
- Create namespaces, LoadBalancer services, or NodePorts.
- Escalate RBAC (blocked by Kubernetes' built-in escalation prevention).

## Authentication

X509 client cert, `CN=tokkenno`, `O=tenant-tokkenno`, signed by the cluster CA.
Regenerate / rotate with:

```bash
./make-kubeconfig.sh          # run with admin access to context `lamg`
# -> writes kubeconfig-tokkenno.yaml (hand this to the dev)
```

Cert validity: 1 year (edit `EXPIRATION_SECONDS` in the script).

### Revoking access
X509 certs have no fine-grained revocation. To cut the dev off:
- Delete the RBAC (remove the `RoleBinding` / `ClusterRoleBinding`), **or**
- Rotate the cluster CA (nuclear).
Deleting the RBAC is the practical kill-switch — the cert still authenticates
but authorizes nothing.

> `kubeconfig-tokkenno.yaml` and any `*.key/*.crt` are secrets — do not commit
> them. See `.gitignore`.
