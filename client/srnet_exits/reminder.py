"""Warn about 10 minutes before an exit lease ends (nothing renews by itself; buy again to add time).

`srnet buy` starts this in the background:  python -m srnet_exits.reminder --home H --seller S
It sleeps until the warning time, re-reads the lease (a new purchase moves the end), shows a desktop
notification (notify-send) if it can, and exits once the lease has ended.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .prog import P as _P

REMIND_S = 600


def _lease(home: Path, seller: str) -> dict | None:
    try:
        st = json.loads((home / "state.json").read_text())
    except (OSError, ValueError):
        return None
    return ((st.get("exits") or {}).get("leases") or {}).get(seller)


def message(seller: str, end: int, now: float) -> str:
    mins = max(0, int(end - now) // 60)
    return (f"Your SRNet exit time with {seller[:12]}... ends in about {mins} min "
            f"({time.strftime('%H:%M %Z', time.localtime(end)).strip()}). To keep going, buy more: "
            f"{_P()} buy --seller {seller} --hours 1")


def notify(text: str) -> None:
    print(text, flush=True)
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "SRNet", "SRNet exit ending soon", text], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def run(home: Path, seller: str, clock=time.time, sleep=time.sleep, notify=notify, poll_s: int = 60) -> int:
    told = None
    while True:
        lease = _lease(home, seller)
        now = clock()
        end = int((lease or {}).get("end") or 0)
        if end <= now:
            return 0
        if end - now <= REMIND_S and told != end:
            notify(message(seller, end, now))
            told = end
        wait = end - REMIND_S - now if end - now > REMIND_S else end - now
        sleep(max(1, min(poll_s, wait)))


def pidfile(home: Path, seller: str) -> Path:
    return home / "exits" / f"reminder-{seller[:16]}.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return b"srnet_exits.reminder" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True


def start(home: Path, seller: str) -> int | None:
    """Start the background reminder unless one already runs for this seller."""
    pf = pidfile(home, seller)
    try:
        if _alive(int(pf.read_text())):
            return None
    except (OSError, ValueError):
        pass
    pf.parent.mkdir(parents=True, exist_ok=True)
    log = open(pf.with_suffix(".log"), "ab")
    p = subprocess.Popen([sys.executable, "-m", "srnet_exits.reminder", "--home", str(home), "--seller", seller],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         cwd=str(Path(__file__).resolve().parents[1]))
    pf.write_text(str(p.pid))
    return p.pid


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="srnet_exits.reminder")
    ap.add_argument("--home", required=True)
    ap.add_argument("--seller", required=True)
    a = ap.parse_args(argv)
    return run(Path(a.home), a.seller)


if __name__ == "__main__":
    sys.exit(main())
