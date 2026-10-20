"""Run a system command as root: directly when we are root, else through sudo.

sudo asks for the password on the terminal (not stdin, so piping input is fine). Without a
terminal (the web UI) only passwordless sudo works (`sudo -n`). The kill switch and the tunnel
DNS switch share this, so one password prompt covers both while sudo remembers it."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .prog import P


class PrivError(RuntimeError):
    pass


_told = False


def argv(cmd: list[str], interactive: bool = True, what: str = "this") -> list[str]:
    if os.geteuid() == 0:
        return list(cmd)
    if not shutil.which("sudo"):
        raise PrivError(f"{what} needs root and sudo is not available")
    tty = interactive and sys.stdin is not None and sys.stdin.isatty()
    return ["sudo", *([] if tty else ["-n"]), *cmd]


def run(cmd: list[str], *, input: str | None = None, interactive: bool = True, check: bool = True,
        what: str = "this", cwd: str | None = None) -> subprocess.CompletedProcess:
    global _told
    a = argv(cmd, interactive, what)
    if a[0] == "sudo" and "-n" not in a and not _told:
        _told = True
        sys.stdout.flush()
        print(f"{what} needs root; sudo may ask for your password", file=sys.stderr, flush=True)
    if a[0] == "sudo":
        # a password prompt goes straight to the terminal: nothing of ours may be half-written then
        sys.stdout.flush()
        sys.stderr.flush()
    r = subprocess.run(a, input=input, capture_output=True, text=True, cwd=cwd)
    if check and r.returncode != 0:
        fail(a, r, what)
    return r


def fail(a: list[str], r: subprocess.CompletedProcess, what: str):
    err = (r.stderr or r.stdout or "").strip()
    if a and a[0] == "sudo" and "sudo" in err.lower():
        raise PrivError(f"{what} needs root and sudo did not allow it ({err.splitlines()[-1]}). Run {P()} in a "
                        "terminal so sudo can ask for your password")
    raise PrivError(f"{' '.join(os.path.basename(x) for x in a[:3])} failed: {err or r.returncode}")
