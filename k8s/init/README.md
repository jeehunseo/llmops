# k8s init

Cluster bring-up checks that run before anything else is deployed.

Targeted at the local Docker Desktop cluster on Windows, but both files work
against a normal GPU cluster too.

## How the GPU reaches a pod here

Docker Desktop on Windows does not give its Kubernetes pods a GPU by any of the
usual routes. Verified on this machine, in order:

| Attempt | Result |
|---|---|
| Docker Desktop setting for Kubernetes GPU | none exists — no GPU key among the backend Kubernetes settings |
| `"default-runtime": "nvidia"` in Docker Engine | applies to `docker run` only; cri-dockerd pins pod containers to `runc` regardless |
| `RuntimeClass` with handler `nvidia` | rejected: `RuntimeHandler "nvidia" not supported` |
| NVIDIA device plugin | cannot help — with no NVIDIA runtime reachable there is nothing to advertise |

What does work is mounting the WSL2 GPU stack into the pod by hand:

- **`/dev/dxg`** — the WSL2 GPU paravirtualization device. There are no
  `/dev/nvidia*` nodes under WSL.
- **`/usr/lib/wsl`** — `libcuda`, `libnvidia-ml`, `libdxcore` and the
  `nvidia-smi` binary, projected from the Windows driver. Mount the whole
  directory: `libdxcore.so` reads `drivers/`, and mounting only `lib/` gets as
  far as running nvidia-smi before it fails with `Driver Not Loaded`.
- **`privileged: true`** — required. A hostPath mount of `/dev/dxg` does not
  carry the cgroup device permission; without it nvidia-smi reports
  `GPU access blocked by the operating system`.

The trade-off: the GPU is not a scheduled resource. There is no
`nvidia.com/gpu` accounting, so nothing stops two pods from claiming the same
GPU. That is acceptable on a single-node local cluster and should not be
carried to a real one.

## gpu-check.sh

Proves a Kubernetes-scheduled container can actually see the GPU.

```sh
./gpu-check.sh                 # auto-detect, current context
./gpu-check.sh -n llmops       # in another namespace
./gpu-check.sh --keep          # leave the pod up for more exec calls
./gpu-check.sh --help
```

It creates a `gpu-check` pod, waits for Ready, then runs `nvidia-smi`,
`nvidia-smi -L`, and a CSV query of index / name / memory / driver version. The
pod is deleted on exit, including on Ctrl-C, unless `--keep` is passed. On a
readiness timeout it prints the pod state and its scheduling events.

Two modes, picked by `--mode` (default `auto`):

- **`resource`** — the standard path. Requests `nvidia.com/gpu` and lets a
  device plugin plus the NVIDIA runtime do the work. Chosen when some node
  advertises the resource.
- **`wsl`** — the hostPath injection described above. Chosen when no node
  advertises `nvidia.com/gpu`.

Why exec rather than just reading node capacity: allocatable capacity only
proves a resource was advertised. Running `nvidia-smi` in the container also
proves the driver and CUDA libraries are reachable from inside — the part that
actually breaks.

## cuda-ubuntu-pod.yaml

A long-lived Ubuntu 22.04 shell pod on `nvidia/cuda:12.4.1-base-ubuntu22.04`,
with the same GPU wiring baked in.

```sh
kubectl apply -f cuda-ubuntu-pod.yaml
kubectl exec cuda-ubuntu -- nvidia-smi
kubectl exec -it cuda-ubuntu -- bash
kubectl delete -f cuda-ubuntu-pod.yaml
```

`nvidia-smi` is on `PATH` inside the pod via the `PATH` env var, so it can be
called without spelling out `/usr/lib/wsl/lib/nvidia-smi`.

Note that `nvidia-smi` is not part of the image — on a normal cluster the
NVIDIA container runtime injects it, and here it arrives through the
`/usr/lib/wsl` mount.

### Running from Git Bash

Git Bash rewrites arguments that look like absolute POSIX paths, so
`kubectl exec ... -- /usr/lib/wsl/lib/nvidia-smi` turns into a Windows path and
fails with `no such file or directory`. Prefix the command when that happens:

```sh
MSYS_NO_PATHCONV=1 kubectl exec cuda-ubuntu -- /usr/lib/wsl/lib/nvidia-smi
```
