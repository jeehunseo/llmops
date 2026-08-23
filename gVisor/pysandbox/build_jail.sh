#!/bin/sh
# Build a minimal root filesystem holding nothing but CPython and its
# dependencies, for use as `runsc do -root <jail>`.
#
# Why this exists: `runsc do` mounts the HOST root filesystem as the sandbox
# root. Syscalls and writes are isolated, reads are not -- code inside can open
# /etc/passwd and the whole of $HOME. Pointing -root at a directory that
# contains only Python replaces that root, and the host files stop existing
# from the sandbox's point of view.
#
# Run it inside the WSL distro, as your normal user. No root needed.
#
#   ./build_jail.sh              # builds ~/gvjail
#   ./build_jail.sh /path/jail   # elsewhere
#
# Two traps were hit building this, both worth knowing:
#
#   * Hard links do not work.  `cp -al` is the obvious way to assemble a jail
#     for free, but fs.protected_hardlinks=1 forbids linking to files you do
#     not own, and every file under /usr belongs to root. The bytes have to be
#     copied. It comes to roughly 50 MB.
#
#   * /lib64 must be a real directory.  The ELF header of python3 names
#     /lib64/ld-linux-x86-64.so.2 as its interpreter. Making /lib64 a symlink
#     to usr/lib and copying the loader's symlink there leaves a dangling
#     chain, and execve fails with a bare "no such file or directory" that
#     names the python binary rather than the loader -- a confusing error for
#     the actual cause.

set -eu

JAIL="${1:-$HOME/gvjail}"
PY="${PY:-/usr/bin/python3.12}"

[ -x "$PY" ] || { echo "no python at $PY -- set PY=/usr/bin/pythonX.Y" >&2; exit 1; }
PYLIB="/usr/lib/$(basename "$PY")"
[ -d "$PYLIB" ] || { echo "no stdlib at $PYLIB" >&2; exit 1; }

echo "building $JAIL from $PY"
rm -rf "$JAIL"
mkdir -p "$JAIL/usr/bin" "$JAIL/usr/lib" "$JAIL/lib64" \
         "$JAIL/proc" "$JAIL/dev" "$JAIL/tmp" "$JAIL/etc"

# /bin and /lib are symlinks on Ubuntu; mirror that so paths compiled into
# binaries keep resolving.
ln -sfn usr/bin "$JAIL/bin"
ln -sfn usr/lib "$JAIL/lib"

cp -a "$PY" "$JAIL/usr/bin/"
cp -a "$PYLIB" "$JAIL/usr/lib/"

# Copy every shared object the given ELF file pulls in, preserving the
# directory it came from. -L resolves symlinks so the jail gets real files.
copy_deps() {
  ldd "$1" 2>/dev/null \
    | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\//) print $i }' \
    | sort -u \
    | while read -r so; do
        dest="$JAIL$(dirname "$so")"
        mkdir -p "$dest"
        [ -e "$JAIL$so" ] || cp -aL "$so" "$dest/" 2>/dev/null || true
      done
}

copy_deps "$PY"
# The stdlib's C extensions (_ssl, _socket, _hashlib ...) each carry their own
# dependencies, and a missing one only shows up as an ImportError much later.
for ext in "$JAIL/usr/lib/$(basename "$PY")"/lib-dynload/*.so; do
  [ -e "$ext" ] && copy_deps "$ext"
done

# The interpreter named in python3's ELF header. Real file, real directory.
cp -aL /lib64/ld-linux-x86-64.so.2 "$JAIL/lib64/"

echo "size: $(du -sh "$JAIL" | cut -f1)"
echo
echo "smoke test:"
runsc --rootless --network=none do -root "$JAIL" -cwd / \
  "/usr/bin/$(basename "$PY")" -c \
  'import os, json, math; print(" kernel:", os.uname().release); print(" root:  ", sorted(os.listdir("/"))); print(" stdlib:", json.dumps({"sqrt2": round(math.sqrt(2), 5)}))'
echo
echo "done. use it with:"
echo "  python sandbox.py --root $JAIL -c \"print(1)\""
