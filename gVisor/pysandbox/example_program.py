"""A program to feed the sandbox, so `-f` has something real to point at.

    python sandbox.py -f example_program.py --compare

This file is read by sandbox.py on the calling side (Windows, if that is where
you launched it) and its *contents* are shipped into the sandbox. The file
itself is never mounted, so nothing here can depend on its own path.

It prints things that differ between a sandboxed and an unsandboxed run, which
is what --compare is for.
"""

import os
import platform
import socket


def kernel() -> str:
    return platform.release()


def kernel_surfaces() -> list[str]:
    """Files an exploit would reach for. gVisor implements almost none of them,
    so inside the sandbox they are absent rather than merely unreadable."""
    found = []
    for path in ("/proc/kallsyms", "/dev/kmsg", "/proc/sys/kernel/hostname"):
        if os.path.exists(path):
            found.append(path)
    return found


def egress_works(host: str = "1.1.1.1", port: int = 53) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False


def main() -> None:
    print("kernel:    ", kernel())
    print("cpu count: ", os.cpu_count())
    print("surfaces:  ", kernel_surfaces() or "none visible")
    print("egress:    ", "reachable" if egress_works() else "blocked")

    # Ordinary work, to show the sandbox is a normal Python once you are inside.
    primes = [n for n in range(2, 200) if all(n % d for d in range(2, int(n**0.5) + 1))]
    print("primes<200:", len(primes), "last", primes[-1])


if __name__ == "__main__":
    main()
