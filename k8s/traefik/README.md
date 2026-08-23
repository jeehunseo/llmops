# Traefik

Ingress controller for the local Docker Desktop cluster, replacing ingress-nginx
(archived 2026-03-24, no further security patches).

Traefik runs in its own `traefik` namespace. That is where the controller pod
lives, not the limit of what it serves — its RBAC is cluster-scoped and no
namespace filter is set, so one install handles Ingresses in **every**
namespace. Apps and their Ingress objects stay in their own namespaces.

| Path | What it is |
|---|---|
| `my_values.yaml` | Helm values, keys ordered to match the chart's own `values.yaml` |
| `traefik/` | The chart itself, pulled locally (chart 41.3.0, appVersion v3.7.11) |
| `nginx-ingress.yaml` | Ingress routing to the nginx Deployment in `../nginx/nginx.yaml` |

## Install

The chart is vendored in `traefik/`, so no repo needs to be registered. Render
the manifests first and read them — nothing is applied by this:

```bash
helm template traefik ./traefik/ -f my_values.yaml -n traefik
```

Then install:

```bash
helm upgrade --install traefik ./traefik/ -f my_values.yaml --namespace traefik --create-namespace
```

Wait for it:

```bash
kubectl -n traefik rollout status deploy/traefik --timeout=180s
```

The Ingress goes in the **app's** namespace, not Traefik's — `nginx-ingress.yaml`
targets the nginx Service in `default`:

```bash
kubectl apply -f nginx-ingress.yaml
```

## Verify

```bash
kubectl -n traefik get svc traefik
```

`EXTERNAL-IP` should read `localhost` — Docker Desktop assigns that to
LoadBalancer Services, the same way it did for the nginx Service.

```bash
curl http://nginx.localtest.me/
```

`localtest.me` and all its subdomains resolve to 127.0.0.1 publicly, so
host-based routing works with no hosts file edit.

Dashboard, via port-forward — it is not exposed externally because it is
unauthenticated:

```bash
kubectl -n traefik port-forward deploy/traefik 8080:8080
```

Then open `http://localhost:8080/dashboard/` (the trailing slash matters).

## Chart versions

The repo carries 346 published versions of this chart. Adding the repo pins
nothing — **omitting `--version` resolves to whatever is newest at that
moment**:

```
helm show chart traefik --repo https://traefik.github.io/charts
  -> version: 41.3.0   appVersion: v3.7.11      (today's newest)

helm show chart traefik --repo ... --version 40.3.0
  -> version: 40.3.0   appVersion: v3.7.4
```

So always pass `--version` on `pull`, `install` and `upgrade`. Two versions are
in play and they move independently:

- **chart version** (`41.3.0`) — the packaging: templates, CRDs, values schema
- **appVersion** (`v3.7.11`) — the Traefik binary the chart deploys

A chart bump can change the values schema without changing Traefik at all,
which is exactly why an unpinned `helm upgrade` can break a working install.

There is a second reason to pin: `helm repo add` writes a snapshot of the repo
index to `%LOCALAPPDATA%\Temp\helm\repository`, and resolution runs against
that cache until `helm repo update` refreshes it. Unpinned means "newest in
whatever the local cache happens to hold" — which differs between machines.

## Refreshing the vendored chart

```bash
helm pull traefik --repo https://traefik.github.io/charts --version 41.3.0 --untar
```

Note the chart name is `traefik`, not `traefik/traefik` — the two-part form
only works after `helm repo add` registers the alias. `--repo` takes the URL
directly and needs no registration.

## Alternative: install from the remote repo

Dropping `traefik/` keeps this directory to just the values file, at the cost
of needing the repo registered on every machine. The version must be pinned
explicitly here, since nothing local fixes it:

```bash
helm repo add traefik https://traefik.github.io/charts && helm repo update
```

```bash
helm upgrade --install traefik traefik/traefik --version 41.3.0 -f my_values.yaml --namespace traefik --create-namespace
```

To see what versions exist before choosing one:

```bash
helm search repo traefik/traefik --versions
```

That subcommand needs the repo registered — Helm 4 removed the `--repo` flag
from `helm search repo`, though `helm show` and `helm pull` still accept it.

## What the values set, and why

The chart's schema changed between v2, v3 and v4x. Values copied from older
guides frequently target keys that no longer exist and are silently ignored,
so every key was checked against the chart's 41.3.0 `values.yaml`.

Against the chart defaults, only four things actually change:

| Key | Default | Set to | Effect |
|---|---|---|---|
| `ingressRoute.dashboard.enabled` | `false` | `true` | dashboard IngressRoute |
| `ingressRoute.healthcheck.enabled` | `false` | `true` | `/ping` route |
| `accessLog.enabled` | `false` | `true` | access logs |
| `resources` | `{}` | requests/limits | resource bounds |

Everything else matches the chart default and is stated explicitly because it
matters here:

- **`ingressClass`** — creates the IngressClass named `traefik` and makes it the
  default. `isDefaultClass` is safe while this is the only controller.
- **`providers.kubernetesIngress`** — the plain Ingress API. This is what makes
  existing Ingress manifests work. `publishedService.enabled` writes the
  LoadBalancer address into each Ingress's status, so `kubectl get ingress`
  shows an ADDRESS instead of a blank column.
- **`providers.kubernetesCRD`** — Traefik's own IngressRoute/Middleware CRDs.
  Not needed for the Ingress API, but the CRDs ship with the chart anyway and
  middlewares are the idiomatic way to express rewrites and auth.
- **`providers.kubernetesGateway`** — off. The Gateway API CRDs are not
  installed here; enabling it without them makes Traefik log errors on every
  reconcile.
- **`service.spec.type: LoadBalancer`** — works on Docker Desktop. On a cluster
  with no LoadBalancer provider this stays `<pending>`; use NodePort there.
- **`ports.web` / `ports.websecure`** — host ports 80 and 443, so URLs need no
  port suffix. If something on Windows already holds port 80 the Service fails
  to bind; change `exposedPort` to 8000 and append `:8000` to URLs.
- **`ports.traefik.expose.default: false`** — keeps the dashboard off the
  LoadBalancer. Port-forward instead.

## Migrating manifests written for ingress-nginx

For a plain Ingress with no annotations, the whole migration is one line:
`ingressClassName: nginx` becomes `traefik`.

For manifests that do use annotations, the chart has a drop-in provider:

```yaml
providers:
  kubernetesIngressNGINX:
    enabled: true
    controllerClass: "k8s.io/ingress-nginx"
    ingressClass: "nginx"
```

With that on, Traefik serves Ingresses that still say
`ingressClassName: nginx` and understands ingress-nginx annotations, so an
existing set of manifests migrates with no edits at all. Traefik v3.7 added
support for 85+ of those annotations specifically for this migration. It is
commented out in `my_values.yaml` because the Ingress here already says
`traefik`.

## Uninstall

```bash
helm uninstall traefik --namespace traefik
```

CRDs installed by the chart are not removed by `helm uninstall` and have to be
deleted separately if a clean slate is wanted.
