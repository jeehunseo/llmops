#!/bin/sh
# Verifies that a container scheduled by Kubernetes actually sees the GPU, by
# running `nvidia-smi` inside it via `kubectl exec`.
#
# There are two ways a pod can get a GPU, and this script handles both:
#
#   resource  The standard path. A device plugin advertises nvidia.com/gpu, the
#             pod requests it, and the NVIDIA container runtime injects the
#             driver. Works on any normal GPU cluster.
#
#   wsl       Docker Desktop on Windows. Its kubeadm cluster runs kubelet
#             against cri-dockerd, which pins every pod container to the `runc`
#             runtime -- so the NVIDIA runtime is never reached. Setting
#             "default-runtime": "nvidia" only affects `docker run`, and a
#             RuntimeClass is rejected outright (RuntimeHandler "nvidia" not
#             supported). With no runtime to inject anything, no device plugin
#             can advertise nvidia.com/gpu either. What is left is mounting the
#             WSL GPU stack by hand: /dev/dxg plus /usr/lib/wsl, privileged.
#
# `auto` (the default) picks resource mode when some node advertises
# nvidia.com/gpu, and wsl mode otherwise.
#
# The check is deliberately end-to-end. Node capacity alone only proves a
# resource was advertised; running nvidia-smi in the container also proves the
# driver and CUDA libraries are actually reachable from inside.
#
# Usage:
#   ./gpu-check.sh [options]
#
# Options:
#   -m, --mode MODE        auto | resource | wsl        (default: auto)
#   -n, --namespace NAME   namespace to run in          (default: default)
#   -c, --context NAME     kubectl context              (default: current)
#   -i, --image REF        CUDA image providing nvidia-smi
#                          (default: nvidia/cuda:12.4.1-base-ubuntu22.04)
#   -g, --gpus N           GPUs to request, resource mode only  (default: 1)
#   -t, --timeout SECONDS  how long to wait for Ready    (default: 300)
#       --keep             leave the pod running for further exec calls
#   -h, --help             show this help

set -e

MODE="auto"
NAMESPACE="default"
CONTEXT=""
IMAGE="nvidia/cuda:12.4.1-base-ubuntu22.04"
GPUS="1"
TIMEOUT="300"
KEEP="false"
POD_NAME="gpu-check"

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--mode)      MODE="${2:?mode required}"; shift 2 ;;
    -n|--namespace) NAMESPACE="${2:?namespace required}"; shift 2 ;;
    -c|--context)   CONTEXT="${2:?context required}"; shift 2 ;;
    -i|--image)     IMAGE="${2:?image required}"; shift 2 ;;
    -g|--gpus)      GPUS="${2:?gpu count required}"; shift 2 ;;
    -t|--timeout)   TIMEOUT="${2:?timeout required}"; shift 2 ;;
    --keep)         KEEP="true"; shift ;;
    -h|--help)      sed -n '2,39p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

case "$MODE" in
  auto|resource|wsl) ;;
  *) echo "--mode must be auto, resource or wsl; got: $MODE" >&2; exit 2 ;;
esac
case "$GPUS" in ''|*[!0-9]*|0) echo "--gpus must be a positive integer, got: $GPUS" >&2; exit 2 ;; esac
case "$TIMEOUT" in ''|*[!0-9]*) echo "--timeout must be an integer, got: $TIMEOUT" >&2; exit 2 ;; esac

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found in PATH" >&2; exit 1; }

# Every kubectl call goes through this so --context/--namespace are applied
# consistently and cannot drift between commands.
k() {
  if [ -n "$CONTEXT" ]; then
    kubectl --context "$CONTEXT" -n "$NAMESPACE" "$@"
  else
    kubectl -n "$NAMESPACE" "$@"
  fi
}

CURRENT_CONTEXT="${CONTEXT:-$(kubectl config current-context 2>/dev/null || echo '<none>')}"

# --- 1. Decide the mode ------------------------------------------------------
echo "[1/4] Inspecting nodes"
NODE_GPUS=$(k get nodes \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' \
  2>/dev/null) || { echo "  cannot reach the cluster -- is it running?" >&2; exit 1; }

SCHEDULABLE=$(printf '%s\n' "$NODE_GPUS" | awk -F'\t' '$2 != "" && $2 != "0" { print $1 " (" $2 ")" }')

if [ -n "$SCHEDULABLE" ]; then
  echo "  nodes advertising nvidia.com/gpu:"
  printf '%s\n' "$SCHEDULABLE" | sed 's/^/    /'
  if [ "$MODE" = "auto" ]; then MODE="resource"; fi
else
  printf '%s\n' "$NODE_GPUS" | awk -F'\t' 'NF { print "    " $1 ": no nvidia.com/gpu" }'
  if [ "$MODE" = "auto" ]; then
    MODE="wsl"
    echo "  -> falling back to wsl mode (hostPath injection)"
  elif [ "$MODE" = "resource" ]; then
    echo >&2
    echo "  --mode resource was requested but nothing advertises nvidia.com/gpu." >&2
    echo "  Install the NVIDIA device plugin, or use --mode wsl on Docker Desktop." >&2
    exit 1
  fi
fi

echo
echo "context:   $CURRENT_CONTEXT"
echo "namespace: $NAMESPACE"
echo "image:     $IMAGE"
echo "mode:      $MODE"
echo

# --- 2. Create the pod -------------------------------------------------------
cleanup() {
  if [ "$KEEP" = "true" ]; then
    echo
    echo "Pod '$POD_NAME' left running (--keep). Remove it with:"
    echo "  kubectl -n $NAMESPACE delete pod $POD_NAME"
  else
    k delete pod "$POD_NAME" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "[2/4] Creating pod '$POD_NAME'"
# --wait so the apply below cannot race a pod of the same name still
# terminating; an update to a running pod would be rejected outright.
k delete pod "$POD_NAME" --ignore-not-found --wait=true >/dev/null 2>&1 || true

if [ "$MODE" = "resource" ]; then
  k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $POD_NAME
  labels:
    app: gpu-check
spec:
  restartPolicy: Never
  # GPU node pools are commonly tainted so that only GPU workloads land on
  # them; without this the pod is rejected on exactly the nodes it needs.
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
  containers:
    - name: cuda
      image: $IMAGE
      imagePullPolicy: IfNotPresent
      command: ["sleep", "infinity"]
      resources:
        limits:
          nvidia.com/gpu: "$GPUS"
EOF
else
  # Both /usr/lib/wsl subtrees are needed: lib holds libcuda / libnvidia-ml /
  # nvidia-smi, and libdxcore.so reads drivers. Mounting only lib gets as far
  # as running nvidia-smi, which then fails with "Driver Not Loaded".
  # privileged is not optional either -- a hostPath mount of /dev/dxg does not
  # carry the cgroup device permission, and nvidia-smi reports "GPU access
  # blocked by the operating system" without it.
  k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $POD_NAME
  labels:
    app: gpu-check
spec:
  restartPolicy: Never
  containers:
    - name: cuda
      image: $IMAGE
      imagePullPolicy: IfNotPresent
      command: ["sleep", "infinity"]
      securityContext:
        privileged: true
      env:
        - name: LD_LIBRARY_PATH
          value: "/usr/lib/wsl/lib"
        - name: PATH
          value: "/usr/lib/wsl/lib:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      volumeMounts:
        - name: wsl
          mountPath: /usr/lib/wsl
          readOnly: true
        - name: dxg
          mountPath: /dev/dxg
  volumes:
    - name: wsl
      hostPath:
        path: /usr/lib/wsl
        type: Directory
    - name: dxg
      hostPath:
        path: /dev/dxg
        type: CharDevice
EOF
fi
echo "  applied"
echo

# --- 3. Wait for it to be ready ---------------------------------------------
echo "[3/4] Waiting up to ${TIMEOUT}s for the pod to become Ready"
echo "      (the CUDA base image is a few hundred MB, so a first run pulls)"
if ! k wait --for=condition=Ready "pod/$POD_NAME" --timeout="${TIMEOUT}s" >/dev/null 2>&1; then
  echo "  pod did not become Ready. Current state:" >&2
  echo >&2
  k get pod "$POD_NAME" -o wide >&2 || true
  echo >&2
  k describe pod "$POD_NAME" 2>/dev/null | sed -n '/^Events:/,$p' | sed 's/^/  /' >&2 || true
  exit 1
fi
echo "  ready"
echo

# --- 4. Run nvidia-smi inside the container ----------------------------------
echo "[4/4] nvidia-smi inside the container"
echo
if ! k exec "$POD_NAME" -c cuda -- nvidia-smi; then
  echo >&2
  echo "nvidia-smi failed inside the container." >&2
  if [ "$MODE" = "resource" ]; then
    echo "The pod was scheduled, so the resource was granted, but the driver was" >&2
    echo "not injected -- check that the node container runtime is the NVIDIA" >&2
    echo "runtime and that the image ships nvidia-smi." >&2
  else
    echo "The mounts landed but the driver is unreachable. Check that the node" >&2
    echo "really is a WSL2 host: /dev/dxg and /usr/lib/wsl/{lib,drivers} must" >&2
    echo "exist on it." >&2
  fi
  exit 1
fi

echo
echo "--- Devices visible to the container ---"
k exec "$POD_NAME" -c cuda -- nvidia-smi -L

echo
echo "--- Driver / CUDA versions ---"
k exec "$POD_NAME" -c cuda -- \
  nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv

echo
echo "GPU is visible from inside the container."
