"""Run untrusted Python code inside a gVisor sandbox living in WSL.

The sandbox is `runsc do`, gVisor's bundle-less mode: it boots a Sentry (the
userspace kernel), runs one command inside it, and tears it down. No daemon, no
container image, no root -- `--rootless` uses a user namespace instead.

Why this shape:

  * Callable from Windows.  On win32 every command is prefixed with
    `wsl.exe -d <distro> --`, so the same class works from a Windows Python and
    from a Python already running inside the distro.

  * Code travels as base64.  Passing a program as a command-line argument
    through Windows argument quoting, wsl.exe, and then execve is a quoting
    minefield. base64 is ASCII-safe and survives all three untouched.

  * Two timeouts.  `timeout(1)` inside the distro kills the sandbox itself;
    the subprocess timeout on this side is a backstop for the case where
    wsl.exe hangs. Killing wsl.exe alone would orphan the Sentry.

READ THE SECURITY NOTE in README.md before pointing this at genuinely hostile
code: `runsc do` isolates syscalls and writes, but the host filesystem stays
READABLE from inside.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import time
from dataclasses import dataclass

DEFAULT_DISTRO = "Ubuntu-24.04"
DEFAULT_RUNSC = "/usr/local/bin/runsc"


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool
    sandboxed: bool
    platform: str | None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def report(self) -> str:
        head = "gVisor/" + (self.platform or "?") if self.sandboxed else "native"
        if self.timed_out:
            state = "TIMEOUT"
        elif self.exit_code == 137:
            # 128 + SIGKILL. With a MemoryMax set, this is all the OOM killer
            # leaves behind -- the program gets no exception and prints nothing.
            state = "KILLED (137, out of memory?)"
        else:
            state = "exit=%d" % self.exit_code
        lines = ["[%s] %s in %.2fs" % (head, state, self.duration_s)]
        if self.stdout:
            lines.append(_indent(self.stdout, "  out| "))
        if self.stderr:
            lines.append(_indent(self.stderr, "  err| "))
        return "\n".join(lines)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.rstrip("\n").split("\n"))


class GVisorSandbox:
    def __init__(
        self,
        distro: str = DEFAULT_DISTRO,
        runsc: str = DEFAULT_RUNSC,
        platform: str = "systrap",
        network: bool = False,
        timeout: int = 30,
        python: str = "python3",
        root: str | None = None,
        memory_max: str | None = None,
        cpu_quota: str | None = None,
    ) -> None:
        self.distro = distro
        self.runsc = runsc
        self.platform = platform
        self.network = network
        self.timeout = timeout
        self.python = python
        # Sandbox root filesystem. Without it `runsc do` uses the host's, which
        # leaves every host file readable from inside. See build_jail.sh.
        self.root = root
        # Host-side cgroup bounds, applied by wrapping the whole invocation in
        # a transient systemd scope. Strings in systemd's units: "400M", "50%".
        self.memory_max = memory_max
        self.cpu_quota = cpu_quota

    # -- command construction -------------------------------------------------

    def _wsl_prefix(self) -> list[str]:
        """Empty when already inside Linux; otherwise hop through wsl.exe.

        `--cd ~` matters: without it the distro starts in the Windows working
        directory, which lands on /mnt/... -- a 9p mount slow enough to distort
        any timing taken here.
        """
        if sys.platform != "win32":
            return []
        return ["wsl.exe", "-d", self.distro, "--cd", "~", "--"]

    def _bootstrap(self, code: str) -> str:
        """A one-liner that decodes and executes the real program.

        compile() gives the program a filename, so a traceback from inside the
        sandbox points at 'sandbox_code.py' rather than the string itself.
        Deliberately free of double quotes and angle brackets, both of which
        acquire meaning somewhere along the Windows -> wsl.exe path.
        """
        blob = base64.b64encode(code.encode("utf-8")).decode("ascii")
        return (
            "import base64;"
            "src=base64.b64decode('" + blob + "').decode('utf-8');"
            "exec(compile(src,'sandbox_code.py','exec'),{'__name__':'__main__'})"
        )

    def _scope(self) -> list[str]:
        """A transient systemd scope carrying the cgroup limits, or nothing.

        --user works without sudo because systemd delegates the cpu, memory and
        pids controllers to the user slice on cgroup v2. Verify with:
            cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@*.service/cgroup.controllers
        """
        if not (self.memory_max or self.cpu_quota):
            return []
        argv = ["systemd-run", "--user", "--scope", "-q"]
        if self.memory_max:
            # MemorySwapMax=0 matters: without it the cgroup spills to swap and
            # the limit turns into a slowdown instead of a kill.
            argv += ["-p", "MemoryMax=" + self.memory_max, "-p", "MemorySwapMax=0"]
        if self.cpu_quota:
            argv += ["-p", "CPUQuota=" + self.cpu_quota]
        return argv

    def _argv(self, code: str, sandboxed: bool) -> list[str]:
        # -k 5: SIGTERM first, SIGKILL five seconds later, so a program with a
        # cleanup handler gets a chance to run it. It sits inside the scope so
        # that it is the direct parent of runsc -- wrapping systemd-run instead
        # would leave the signal to propagate through one more hop.
        inner = self._scope() + ["timeout", "-k", "5", str(self.timeout)]
        if sandboxed:
            inner += [
                self.runsc,
                "--rootless",
                "--network=" + ("host" if self.network else "none"),
                "--platform=" + self.platform,
                "do",
            ]
            if self.root:
                # -cwd / because the default working directory is inherited
                # from the host and will not exist inside the jail.
                inner += ["-root", self.root, "-cwd", "/"]
        inner += [self.python, "-c", self._bootstrap(code)]
        return self._wsl_prefix() + inner

    # -- execution ------------------------------------------------------------

    def _exec(self, code: str, sandboxed: bool) -> SandboxResult:
        argv = self._argv(code, sandboxed)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # Backstop only. The inner `timeout` should always fire first;
                # this catches wsl.exe itself wedging.
                timeout=self.timeout + 15,
            )
            out, err, rc = proc.stdout, proc.stderr, proc.returncode
            # 124 is what timeout(1) exits with when it had to kill the child.
            timed_out = rc == 124
        except subprocess.TimeoutExpired as exc:
            out = _text(exc.stdout)
            err = _text(exc.stderr) + "\n[host] wsl.exe did not return; killed"
            rc, timed_out = -1, True
        except FileNotFoundError as exc:
            raise RuntimeError("cannot launch %r: %s" % (argv[0], exc)) from exc

        return SandboxResult(
            stdout=out,
            stderr=err,
            exit_code=rc,
            duration_s=time.monotonic() - started,
            timed_out=timed_out,
            sandboxed=sandboxed,
            platform=self.platform if sandboxed else None,
        )

    def run(self, code: str) -> SandboxResult:
        """Execute `code` inside gVisor."""
        return self._exec(code, sandboxed=True)

    def run_native(self, code: str) -> SandboxResult:
        """Execute `code` in the distro with no sandbox -- the baseline to
        compare against. Everything the sandboxed run can do, this can do too,
        plus everything gVisor takes away."""
        return self._exec(code, sandboxed=False)

    # -- diagnostics ----------------------------------------------------------

    def preflight(self) -> None:
        """Fail loudly and specifically, rather than letting every later call
        die with the same opaque error."""
        probe = self._wsl_prefix() + [self.runsc, "--version"]
        try:
            proc = subprocess.run(probe, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise RuntimeError(
                "wsl.exe not found -- run this from Windows with WSL installed, "
                "or from inside the distro itself."
            ) from None
        except subprocess.TimeoutExpired:
            raise RuntimeError("distro %r did not respond" % self.distro) from None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                "%s not usable in %r:\n  %s\n  install it, or pass runsc=<path>"
                % (self.runsc, self.distro, detail)
            )


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run Python inside a gVisor sandbox in WSL.",
        epilog="Reads the program from stdin when neither -c nor -f is given.",
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("-c", "--code", help="program text")
    src.add_argument("-f", "--file", help="program file")
    ap.add_argument("--distro", default=DEFAULT_DISTRO)
    ap.add_argument("--runsc", default=DEFAULT_RUNSC)
    ap.add_argument(
        "--platform", default="systrap", choices=["systrap", "ptrace", "kvm"]
    )
    ap.add_argument("--network", action="store_true", help="allow egress (default: none)")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument(
        "--root",
        help="sandbox root filesystem, e.g. ~/gvjail (see build_jail.sh). "
        "Without it the HOST root is used and host files stay readable.",
    )
    ap.add_argument("--python", default="python3",
                    help="interpreter path inside the sandbox (default: python3)")
    ap.add_argument("--memory", help="cgroup memory ceiling, e.g. 400M")
    ap.add_argument("--cpu", help="cgroup cpu ceiling, e.g. 25%%")
    ap.add_argument("--native", action="store_true", help="skip gVisor, run bare")
    ap.add_argument("--compare", action="store_true", help="run both, print both")
    args = ap.parse_args(argv)

    if args.code is not None:
        code = args.code
    elif args.file:
        with open(args.file, encoding="utf-8") as fh:
            code = fh.read()
    else:
        code = sys.stdin.read()
    if not code.strip():
        ap.error("no program given")

    box = GVisorSandbox(
        distro=args.distro,
        runsc=args.runsc,
        platform=args.platform,
        network=args.network,
        timeout=args.timeout,
        python=args.python,
        root=args.root,
        memory_max=args.memory,
        cpu_quota=args.cpu,
    )

    if args.compare:
        print(box.run_native(code).report())
        print(box.run(code).report())
        return 0

    result = box.run_native(code) if args.native else box.run(code)
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
