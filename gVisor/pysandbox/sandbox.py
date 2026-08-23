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
        state = "TIMEOUT" if self.timed_out else "exit=%d" % self.exit_code
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
    ) -> None:
        self.distro = distro
        self.runsc = runsc
        self.platform = platform
        self.network = network
        self.timeout = timeout
        self.python = python

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

    def _argv(self, code: str, sandboxed: bool) -> list[str]:
        # -k 5: SIGTERM first, SIGKILL five seconds later, so a program with a
        # cleanup handler gets a chance to run it.
        inner = ["timeout", "-k", "5", str(self.timeout)]
        if sandboxed:
            inner += [
                self.runsc,
                "--rootless",
                "--network=" + ("host" if self.network else "none"),
                "--platform=" + self.platform,
                "do",
            ]
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
