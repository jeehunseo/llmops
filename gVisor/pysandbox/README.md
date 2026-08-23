# pysandbox

Running Python inside gVisor from a Python process on Windows, through WSL.

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
| `example_program.py` | Something for `-f` to point at |

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

## SECURITY NOTE — read before pointing this at hostile code

`runsc do` gives the sandbox **the host's root filesystem as its own**. That is
a deliberate convenience in gVisor's design, and it means this setup is a
syscall sandbox, not yet a code-execution sandbox.

Measured on this machine, `runsc --rootless --network=none do`:

| Property | Isolated? | Evidence |
|---|---|---|
| Syscall interface | **yes** | `uname` reports `4.19.0-gvisor`, not the host's `6.18.33.2-microsoft-standard-WSL2` |
| Host kernel surfaces | **yes** | `/proc/kallsyms`, `/dev/kmsg`, `/sys/module` — present and readable natively, absent inside |
| Network egress | **yes** | `--network=none` leaves the Sentry no route; `connect()` fails with `OSError` |
| Writes to host | **yes** | writes land in an overlay that dies with the sandbox; the host file never appears |
| **Reads of host files** | **NO** | `/etc/passwd`, `~/*` read straight through — probe 5 |
| **CPU / memory limits** | **NO** | no cgroup is attached; a busy loop runs until the timeout — probe 6 |

So: safe against code trying to **attack the kernel or reach the network**.
Not safe against code trying to **read your files**. For an LLM code-execution
service, the second one is usually the whole point, so treat this as a working
model of the plumbing rather than a finished sandbox.

## Closing the two gaps

Both gaps come from `do` mode, not from gVisor.

**Filesystem** — use a real OCI bundle instead. `runsc spec` generates a
`config.json`; point its `root.path` at a rootfs you control and list only the
mounts the program should see. `runsc run --bundle <dir> <id>` then starts it.
The host filesystem is no longer the sandbox filesystem, so reads are confined
by construction.

**Resources** — an OCI bundle also carries `linux.resources`, which is where
CPU shares and a memory ceiling go. `do` mode has nowhere to put them.

Beyond that, containerd with the `runsc` handler gives image pulls, CNI
networking and per-container cgroups for free. That is the same path
Kubernetes takes, via `RuntimeClass`.

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
