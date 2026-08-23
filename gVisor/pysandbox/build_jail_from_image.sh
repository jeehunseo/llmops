#!/bin/sh
# Turn a container image into a root filesystem for `runsc do -root <dir>`.
#
# The alternative, build_jail.sh, assembles a jail by copying CPython and
# chasing its shared libraries with ldd. That works and stays at 48 MB, but it
# scales badly: every third-party package with a C extension drags in its own
# libraries, and a missed one surfaces later as an ImportError rather than as a
# build failure. An image has already solved that problem -- pip resolved the
# dependencies, and the image records the result.
#
# What this does NOT do is run the container. The image is only a convenient way
# to produce a directory tree; gVisor then uses that tree directly, with no
# daemon, no image store and no root.
#
#   ./build_jail_from_image.sh python:3.12-slim
#   ./build_jail_from_image.sh --tar /mnt/c/tmp/rootfs.tar ~/gvjail-custom
#
# For your own package set, build an image first:
#
#   FROM python:3.12-slim
#   RUN pip install --no-cache-dir numpy pandas
#
# Note the interpreter path differs from build_jail.sh. Official python images
# put it at /usr/local/bin/python3, so runs need:
#
#   python sandbox.py --root <dir> --python /usr/local/bin/python3 ...

set -eu

TAR=""
if [ "${1:-}" = "--tar" ]; then
  TAR="${2:?path to a rootfs tar required}"
  shift 2
  IMAGE=""
else
  IMAGE="${1:?image name, or --tar <path>}"
  shift
fi
JAIL="${1:-$HOME/gvjail-img}"

if [ -z "$TAR" ]; then
  command -v docker >/dev/null 2>&1 || {
    echo "docker not found in this distro." >&2
    echo "Either enable Docker Desktop's WSL integration for it, or export" >&2
    echo "from Windows and pass the tar:" >&2
    echo >&2
    echo "  docker create --name gvsrc $IMAGE /bin/true" >&2
    echo "  docker export gvsrc -o C:\\tmp\\rootfs.tar" >&2
    echo "  docker rm -f gvsrc" >&2
    echo "  ./build_jail_from_image.sh --tar /mnt/c/tmp/rootfs.tar" >&2
    exit 1
  }
  TAR="$(mktemp -d)/rootfs.tar"
  CID="gvsandbox-export-$$"
  echo "exporting $IMAGE"
  docker pull -q "$IMAGE"
  # `create` without `start`: the image only has to be materialised as a
  # filesystem, never executed.
  docker create --name "$CID" "$IMAGE" /bin/true >/dev/null
  docker export "$CID" -o "$TAR"
  docker rm -f "$CID" >/dev/null
fi

echo "extracting into $JAIL"
rm -rf "$JAIL"
mkdir -p "$JAIL"
# Extracting as a normal user is fine: tar drops the archived root ownership
# rather than failing, and gVisor does not care who owns the tree. Verified on
# python:3.12-slim -- zero warnings.
tar -C "$JAIL" -xf "$TAR"

echo "size: $(du -sh "$JAIL" | cut -f1), $(find "$JAIL" -type f | wc -l) files"

PY=""
for candidate in /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$JAIL$candidate" ] && { PY="$candidate"; break; }
done
[ -n "$PY" ] || { echo "no python found in the image" >&2; exit 1; }

echo
echo "smoke test:"
runsc --rootless --network=none do -root "$JAIL" -cwd / "$PY" -c \
  'import os, sys; print(" kernel:", os.uname().release); print(" python:", sys.version.split()[0]); print(" home:  ", os.listdir("/home"), "(the image own, not the host)")'
echo
echo "done. use it with:"
echo "  python sandbox.py --root $JAIL --python $PY -c \"print(1)\""
