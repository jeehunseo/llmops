# pysandbox

Running Python inside gVisor from a Python process on Windows, through WSL. (NOT Pod/container, Kubernetes env.)

```
Windows python  ──wsl.exe──>  Ubuntu-24.04  ──runsc do──>  Sentry  ──>  python3
```

No daemon, no container image, no root. `runsc do` is gVisor's bundle-less
mode: it boots a Sentry, runs one command inside it, and tears it down.
`--rootless` gets the privileges from a user namespace instead of from sudo.

| File | What it is |
|---|---|
| `sandbox.py` | `GVisorSandbox` class plus a CLI |
| `demo.py` | Six probes, each run sandboxed and unsandboxed side by side |
| `build_jail_from_image.sh` | Root filesystem from a container image — use this when packages are involved |
| `build_jail.sh` | Root filesystem from the host's own Python, 48 MB, stdlib only |
| `example_program.py` | Something for `-f` to point at |

For anything beyond a demo, use it with both gaps closed:

```bash
python sandbox.py --root ~/gvjail-img --python /usr/local/bin/python3 --memory 1G --cpu 200% -f program.py
```

## Use

```bash
python demo.py
```

```bash
python sandbox.py -c "import platform; print(platform.release())"
```

```bash
python sandbox.py -f example_program.py --compare
```

`-f` takes a path on **the machine running `sandbox.py`** — Windows, if that is
where you launched it. The file is read here and its contents travel into the
sandbox as base64; the file itself is never mounted. So a program passed this
way cannot open its own source, and relative paths inside it resolve against
the sandbox's working directory, not against where the file lives.

With no `-c` and no `-f`, the program is read from stdin:

```bash
type program.py | python sandbox.py
```

As a library:

```python
from sandbox import GVisorSandbox

box = GVisorSandbox(timeout=10)
box.preflight()                     # fails early and specifically
result = box.run("print(2 ** 100)")
print(result.stdout, result.exit_code, result.duration_s)
```

`run_native()` runs the same program with no sandbox, which is what every
claim below is measured against.

## What is isolated

Bare `runsc do` covers less than it looks like it does. Two gaps have to be
closed explicitly, and both are closed by flags this repo already passes.

| Property | Bare `do` | `--root` + `--memory`/`--cpu` | Evidence |
|---|---|---|---|
| Syscall interface | **yes** | yes | `uname` reports `4.19.0-gvisor`, not `6.18.33.2-microsoft-standard-WSL2` |
| Host kernel surfaces | **yes** | yes | `/proc/kallsyms`, `/dev/kmsg`, `/sys/module` — readable natively, absent inside |
| Network egress | **yes** | yes | `--network=none` leaves the Sentry no route; `connect()` raises `OSError` |
| Writes to host | **yes** | yes | writes land in a tmpfs overlay that dies with the sandbox |
| Reads of host files | **NO** | **yes** | with `--root`, `/etc/passwd` and `/home` do not exist inside |
| Memory ceiling | **NO** | **yes** | 800 MB allocation under `--memory 400M` dies with SIGKILL |
| CPU ceiling | **NO** | **yes** | busy loop: 18.6M iterations unrestricted, 4.9M under `--cpu 25%` |
| Process count | **NO** | **NO** | see below — the host cannot count guest processes |

### Reads: `--root`

`runsc do` mounts **the host root filesystem** as the sandbox root. Syscalls and
writes are isolated; reads are not. Point `-root` at a filesystem of your own
and the host stops existing from inside. There are two ways to build one.

**From a container image** — the better option once any third-party package is
involved.

```bash
./build_jail_from_image.sh python:3.12-slim
```

```bash
python sandbox.py --root ~/gvjail-img --python /usr/local/bin/python3 -c "import ssl; print(ssl.OPENSSL_VERSION)"
```

For your own package set, build an image first and export that instead:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir numpy pandas
```

The image is never run. `docker create` + `docker export` only materialise it
as a tar; gVisor uses the extracted tree directly, with no daemon and no root.
Extracting as a normal user produced zero warnings here. Measured:
`python:3.12-slim` 124 MB / 4409 files; the same plus numpy and pandas
276 MB / 9209 files, extracting in about 4 seconds.

If Docker Desktop's WSL integration is off for the distro, `docker` inside it
just prints a documentation URL. Export from Windows and hand over the tar:

```bash
docker create --name gvsrc gvsandbox:numpy /bin/true && docker export gvsrc -o C:\tmp\rootfs.tar && docker rm -f gvsrc
```

```bash
./build_jail_from_image.sh --tar /mnt/c/tmp/rootfs.tar ~/gvjail-numpy
```

**From the host's own Python** — no Docker, 48 MB, but only the standard
library.

```bash
./build_jail.sh                       # ~/gvjail
```

```bash
python sandbox.py --root /home/you/gvjail --python /usr/bin/python3.12 -c "import os; print(os.path.exists('/home'))"
```

It copies CPython, the stdlib, and every shared object `ldd` reports for the
interpreter and for each `lib-dynload/*.so`. That last part is what makes
`import ssl` work; miss it and the jail builds cleanly and fails much later at
import time. Adding a package means repeating the `ldd` chase for each of its
`.so` files, which is exactly where the image approach earns its size.

`--python` is needed in both cases and differs between them: `/usr/bin/python3.12`
for the ldd jail (a versioned binary, no `python3` symlink),
`/usr/local/bin/python3` for the official images.

### Resources: a transient systemd scope

`do` mode has nowhere to put `linux.resources`, so the limits are applied from
outside instead — the whole invocation runs inside a `systemd-run --user
--scope` with cgroup properties attached. No sudo: systemd delegates the `cpu`,
`memory` and `pids` controllers to the user slice on cgroup v2.

```bash
python sandbox.py --root ~/gvjail --python /usr/bin/python3.12 --memory 400M --cpu 25% -f program.py
```

An out-of-memory kill surfaces as exit 137 with no output at all — the program
gets no exception to catch, so `report()` labels that code specially.

`MemorySwapMax=0` is set alongside `MemoryMax`. Without it the cgroup spills to
swap and the ceiling becomes a slowdown rather than a kill.

**`--cpu` is per core, not per machine.** systemd's `CPUQuota` counts one core
as 100%, so on this 16-core host `--cpu 50%` is half a core — about 3% of the
machine. Measured with a single-threaded loop, 3 seconds:

```
no limit   21.2M iterations
--cpu 200% 18.0M          (a single thread cannot use two cores)
--cpu 100% 17.4M
--cpu 50%   9.9M
```

**`--memory` bounds resident pages, not allocations.** A test that only reserves
address space will not trip it and can look like the limit is broken:

```
--memory 200M   np.zeros((10000, 10000))            -> ok, 762 MB "allocated"
--memory 200M   np.zeros(...) then a[:] = 1.0       -> killed, exit 137
--memory 2G     same code                            -> ok
```

`np.zeros` calls `calloc`, and untouched zero pages cost nothing until written.
Any check of a memory ceiling has to write to the memory it claims.

### The gap that stays open: process count

`TasksMax` does not bound processes inside the sandbox. Measured: under
`TasksMax=100`, code inside forked 500 times successfully. This follows from
the isolation shown elsewhere in this README — guest processes are not host
processes, so the host cgroup has nothing to count. Lowering it far enough to
matter (`TasksMax=20`) instead starves the Sentry's own thread pool and the
sandbox fails to boot.

A fork bomb inside is therefore bounded by memory, not by process count. If
that matters, it needs a limit the Sentry itself enforces, not a host cgroup.

## Why the code looks the way it does

**base64 for the program.** A Python program passed as an argument crosses
Windows argument quoting, then `wsl.exe`, then `execve`. Quotes, newlines and
backslashes each break somewhere along that path. base64 is ASCII-safe and
arrives byte-identical. `compile(src, 'sandbox_code.py', 'exec')` restores a
usable filename in tracebacks.

**Two timeouts.** `timeout -k 5` inside the distro kills the sandbox. The
`subprocess` timeout on the Windows side is 15s longer and only catches
`wsl.exe` itself wedging — killing `wsl.exe` alone would orphan a live Sentry.

**`--cd ~`.** Without it the distro starts in the Windows working directory,
which is a `/mnt/...` 9p mount. Any timing taken there measures the filesystem
bridge instead of gVisor.

**Running from Git Bash.** Git Bash rewrites arguments that look like absolute
POSIX paths, so `--root /home/you/gvjail` arrives as
`C:/Program Files/Git/home/you/gvjail` and runsc reports the mangled path back.
Prefix the command when that happens — the same fix the `k8s/init` notes use
for `kubectl exec`:

```bash
MSYS_NO_PATHCONV=1 python sandbox.py --root /home/you/gvjail -c "print(1)"
```

PowerShell and cmd do not have this problem.

## Numbers seen here

A pure-Python counting loop, 3 seconds wall clock:

```
native            28,804,728 iterations
gVisor/systrap    19,652,910 iterations      ~68% of native
```

The loop calls `time.time()` every pass, so it is syscall-sensitive by
construction — which is exactly where gVisor is expected to cost the most.
Do not read this as a general slowdown figure. A CPU-bound loop with no
syscalls in it should come out near parity, and that comparison belongs in a
proper benchmark rather than a demo probe.

`--platform kvm` is worth trying: `/dev/kvm` exists on this machine (WSL2
nested virtualisation is on), though the user must be in the `kvm` group first.
