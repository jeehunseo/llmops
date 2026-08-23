"""Six probes that show what the gVisor sandbox does and does not take away.

Each probe runs the same program twice -- once bare in the distro, once inside
gVisor -- because a claim about isolation only means something next to the
unisolated result. Probes 5 and 6 are the ones that fail to differ, and they
are here on purpose: they mark the edge of what this sandbox covers.

    python demo.py
    python demo.py --platform kvm
"""

from __future__ import annotations

import argparse
import textwrap

from sandbox import GVisorSandbox

# (title, why, program, after) -- `after` runs unsandboxed once both columns are
# done, for probes whose point is only visible from outside the sandbox.
PROBES: list[tuple[str, str, str, str | None]] = [
    (
        "kernel identity",
        "Which kernel answers uname(2). The clearest single sign that syscalls "
        "are being served by the Sentry and not by the host.",
        """
        import platform, os
        print("release:", platform.release())
        print("version:", open("/proc/version").read().strip()[:70])
        print("cpus:   ", os.cpu_count())
        """,
        None,
    ),
    (
        "network egress",
        "Untrusted code should not phone home. --network=none gives the Sentry "
        "no route out at all, rather than relying on a firewall rule.",
        """
        import socket
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=3)
            print("RESULT: reachable")
        except OSError as exc:
            print("RESULT: blocked ->", type(exc).__name__)
        """,
        None,
    ),
    (
        "host kernel surfaces",
        "Interfaces an exploit would read first. gVisor does not implement most "
        "of them, so they are absent rather than merely unreadable.",
        """
        import os
        for path in ("/proc/kallsyms", "/sys/kernel/security",
                     "/proc/sys/kernel/core_pattern", "/dev/kmsg"):
            try:
                with open(path, "rb") as fh:
                    fh.read(16)
                print("readable:", path)
            except OSError as exc:
                print("denied:  ", path, "->", type(exc).__name__)
        print("modules:", len(os.listdir("/sys/module")) if
              os.path.isdir("/sys/module") else "absent")
        """,
        None,
    ),
    (
        "write containment",
        "Writes land in an overlay that dies with the sandbox. The file appears "
        "to exist inside and never reaches the host.",
        """
        import os
        target = os.path.expanduser("~/gvisor-demo-write.txt")
        with open(target, "w") as fh:
            fh.write("written from inside\\n")
        print("wrote:", target)
        print("visible inside:", os.path.exists(target))
        """,
        """
        import os
        target = os.path.expanduser("~/gvisor-demo-write.txt")
        if os.path.exists(target):
            print("on host: PRESENT  <- the unsandboxed run wrote through")
            os.remove(target)
            print("on host: removed")
        else:
            print("on host: ABSENT   <- nothing escaped the sandbox overlay")
        """,
    ),
    (
        "host file reads  (NOT isolated)",
        "The limitation of `runsc do`: the host root filesystem is the sandbox "
        "root filesystem. Confidentiality is not provided here.",
        """
        import os
        for path in ("/etc/hostname", "/etc/passwd"):
            try:
                head = open(path).read().split(chr(10))[0][:40]
                print("READ", path, "->", repr(head))
            except OSError as exc:
                print("blocked", path, "->", type(exc).__name__)
        home = os.path.expanduser("~")
        print("entries in home:", len(os.listdir(home)))
        """,
        None,
    ),
    (
        "cpu exhaustion  (NOT limited)",
        "No cgroup is attached, so a busy loop burns a core until the timeout "
        "fires. The wall clock is the only bound.",
        """
        import time
        end = time.time() + 3
        n = 0
        while time.time() < end:
            n += 1
        print("iterations in 3s:", n)
        """,
        None,
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--distro", default="Ubuntu-24.04")
    ap.add_argument("--platform", default="systrap",
                    choices=["systrap", "ptrace", "kvm"])
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--only", type=int, help="run just probe N (1-based)")
    args = ap.parse_args()

    box = GVisorSandbox(
        distro=args.distro, platform=args.platform, timeout=args.timeout
    )
    print("checking runsc in %s ..." % args.distro)
    box.preflight()
    print("ok\n")

    probes = PROBES
    if args.only:
        probes = [PROBES[args.only - 1]]

    for index, (title, why, code, after) in enumerate(probes, start=1):
        number = args.only or index
        print("=" * 72)
        print("%d. %s" % (number, title))
        print(textwrap.fill(why, 72, initial_indent="   ", subsequent_indent="   "))
        print("=" * 72)
        program = textwrap.dedent(code).strip()
        if after:
            # Order is reversed and the check interleaved, because the point is
            # what each run leaves behind. Sandboxed first against a clean host,
            # checked immediately; then the same program unsandboxed, checked
            # again. Running native first would plant the file and make the
            # sandboxed result unreadable.
            check = textwrap.dedent(after).strip()
            print(box.run(program).report())
            print(box.run_native(check).report())
            print(box.run_native(program).report())
            print(box.run_native(check).report())
        else:
            print(box.run_native(program).report())
            print(box.run(program).report())
        print()

    print("Probes 5 and 6 are expected to look identical in both columns.")
    print("See README.md -- they are the reason `runsc do` is a syscall")
    print("sandbox and not yet a code-execution sandbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
