# nginx

A minimal externally reachable workload, used to confirm that traffic gets from
the host into the cluster.

```sh
kubectl apply -f nginx.yaml
curl http://localhost:8080
kubectl delete -f nginx.yaml
```

## What it deploys

- **Deployment `nginx`** — 2 replicas of `nginx:1.27-alpine`, with readiness and
  liveness probes on `/`, modest resource bounds, and emptyDir mounts for the
  paths nginx writes to.
- **Service `nginx-lb`** — `LoadBalancer` on port 8080.
- **Service `nginx-np`** — `NodePort` pinned to 30080.

Both Services select the same pods. Delete whichever you do not need.

## Which endpoint to use

| URL | Service | Notes |
|---|---|---|
| `http://localhost:8080` | `nginx-lb` | Preferred. Docker Desktop assigns `EXTERNAL-IP: localhost` to LoadBalancer Services. |
| `http://localhost:30080` | `nginx-np` | Fallback. Works on any cluster, including ones with no LoadBalancer provider. |

Both were verified returning HTTP 200 from the Windows host.

The NodePort variant exists because a `LoadBalancer` needs a provider to assign
an address — on a cluster without one the `EXTERNAL-IP` stays `<pending>`
indefinitely and nothing is reachable. Docker Desktop does provide one, so on
this cluster the LoadBalancer works; the NodePort is there for portability.

Port 8080 rather than 80 keeps the Service off a privileged host port that
something else may already hold. The nodePort must stay inside the cluster
range, 30000-32767 by default.

## No Ingress

This cluster has no ingress controller and no IngressClass, so an `Ingress`
resource would be accepted by the API server and then never programmed. Add a
controller first (ingress-nginx, Traefik) if host- or path-based routing is
needed; until then a Service is the whole story.
