"""srnet-exits: buy exit time and connect through the exit, without the SRNet client.

  srnet-exits doctor                       check Session Router, .sesh names, the clock and what exits need
  srnet-exits setup-router                 once: persistent router key, no blackhole routes (asks sudo)
  srnet-exits exits                        list exits from the signed board
  srnet-exits buy --seller ID --hours N    get a quote, pay it from any wallet, wait for the seller
  srnet-exits paid [--quote ID] [--tx 0x]  carry on after paying (or give the transaction hash)
  srnet-exits connect ID                   route traffic and DNS through the exit (Linux, asks sudo)
  srnet-exits status [--json]              paid time left, connection, kill switch, DNS
  srnet-exits disconnect                   DNS and routes back, kill switch off
  srnet-exits verify --chain base --tx 0x.. --to 0x.. --amount 3.50   check a shop payment on chain
  srnet-exits ui                           the same in a small page in your browser (127.0.0.1 only)

You pay the seller directly in USDC on Arbitrum or Base, from your own wallet, and pay the network
fee yourself. Payments are final. Nothing renews by itself.

If the SRNet client is installed too, `srnet exits`, `srnet buy` and the rest do the same thing:
both use ~/.srnet, the same buyer key and the same connection.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import email.utils
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROG = "srnet-exits"
API = "tcp://127.0.0.1:1190"
SERVICE = "session-router-client"
ROUTER_HELP = ("Session Router is a separate program: install and start it first "
               "(SRNet-Desktop README, Install, steps 1 and 2: "
               "https://github.com/SRNet-lab/SRNet-Desktop#1-session-router)")


# ------------------------------------------------------------------ doctor
def _router_installed() -> bool:
    if shutil.which("session-router") or os.path.exists("/usr/bin/session-router"):
        return True
    try:
        r = subprocess.run(["systemctl", "list-unit-files", f"{SERVICE}.service"], capture_output=True,
                           text=True, timeout=5)
        return SERVICE in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _router_running() -> bool | None:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def c_router():
    if not _router_installed():
        return False, "Session Router", "not installed. " + ROUTER_HELP
    run = _router_running()
    if run is False:
        return False, "Session Router", f"installed but not running: sudo systemctl start {SERVICE}"
    try:
        from .rpc import Rpc
        Rpc(API, timeout_ms=4000).call("llarp.status")
        return True, "Session Router", f"running; API {API[6:]} answers"
    except Exception as e:  # noqa: BLE001
        return None, "Session Router", (f"API {API[6:]} no answer ({str(e)[:80]}); give it a minute after "
                                        f"start, or: sudo systemctl restart {SERVICE}")


def _dns_hint() -> str:
    try:
        rc = os.path.realpath("/etc/resolv.conf")
        txt = Path("/etc/resolv.conf").read_text(errors="replace")
    except OSError:
        rc, txt = "", ""
    if "127.0.0.53" not in txt and "/run/systemd/resolve/" not in rc:
        return ("/etc/resolv.conf does not use systemd-resolved, so .sesh never reaches Session Router: "
                "sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf "
                "(SRNet-Desktop README, Install, step 2)")
    return ("sr-tun0 has no .sesh DNS: add the session-router-client dns.conf drop-in "
            "(SRNet-Desktop README, Install, step 2), then: sudo systemctl daemon-reload && "
            f"sudo systemctl restart {SERVICE}")


def c_sesh():
    try:
        infos = socket.getaddrinfo("localhost.sesh", 80, proto=socket.IPPROTO_TCP)
        return True, ".sesh names", "localhost.sesh -> " + infos[0][4][0]
    except OSError as e:
        return False, ".sesh names", f"no answer ({e}). {_dns_hint()}"


def c_clock(urls=("https://www.cloudflare.com/", "https://www.google.com/")):
    last = None
    for url in urls:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=6) as r:
                remote = email.utils.parsedate_to_datetime(r.headers["Date"])
            break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        return None, "clock", f"could not compare it ({str(last)[:60]})"
    skew = abs((_dt.datetime.now(_dt.timezone.utc) - remote).total_seconds())
    fix = "turn on time sync: sudo timedatectl set-ntp true"
    if skew > 120:
        return False, "clock", f"off by {skew:.0f} s; quote requests need it within 2 minutes. {fix}"
    if skew > 60:
        return None, "clock", f"off by {skew:.0f} s (quotes need it within 2 minutes). {fix}"
    return True, "clock", f"within {skew:.0f} s"


CHECKS = (("router", c_router), ("sesh", c_sesh), ("clock", c_clock))


def cmd_doctor(args, st) -> int:
    from .addon import ADDON
    bad = 0

    def line(ok, what, detail=""):
        nonlocal bad
        tag = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        bad += ok is False
        print(f"[{tag}] {what}{': ' + detail if detail else ''}", flush=True)

    line(sys.version_info >= (3, 10), "python", sys.version.split()[0])
    skip = set(args.skip or ())
    for name, fn in CHECKS:
        if name not in skip:
            line(*fn())
    try:
        ADDON.doctor(st, line)
    except Exception as e:  # noqa: BLE001
        line(False, "srnet-exits", str(e)[:200])
    print(f"{bad} problem(s) above" if bad else "all good")
    return 1 if bad else 0


# ------------------------------------------------------------------ ui
def cmd_ui(args, st) -> int:
    from . import page
    return page.run(st, port=args.port, browser=not args.no_browser)


# ------------------------------------------------------------------ wiring
def _under_sudo() -> bool:
    """`sudo srnet-exits ...` by a normal user (root's own shell and SRNET_ALLOW_ROOT=1 are left alone)."""
    if os.environ.get("SRNET_ALLOW_ROOT") == "1":
        return False
    return os.geteuid() == 0 and os.environ.get("SUDO_USER") not in (None, "", "root")


def parser():
    from . import __version__
    from .addon import ADDON
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    ap.add_argument("--home", help="state dir (default ~/.srnet or $SRNET_HOME)")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", required=True)
    dr = sub.add_parser("doctor", help="check Session Router, .sesh names, the clock and what exits need")
    dr.add_argument("--skip", action="append", choices=[n for n, _ in CHECKS], help="skip a check (repeatable)")
    cmds = {"doctor": cmd_doctor}
    cmds.update(ADDON.register(sub))
    u = sub.add_parser("ui", help="the exits page in your browser (127.0.0.1 only)")
    u.add_argument("--port", type=int, default=0, help="listen on this port (default: a free one)")
    u.add_argument("--no-browser", action="store_true", help="print the address only")
    cmds["ui"] = cmd_ui
    return ap, cmds


def main(argv=None) -> int:
    """Also what `python -m srnet_exits.cli` runs. The hints name $SRNET_EXITS_PROG, which entry() and
    the Exits page set (the SRNet client's Exits tab leaves it at srnet)."""
    from .state import State
    if _under_sudo():
        print(f"{PROG} runs as your own user, not with sudo: your paid time and settings live in your "
              f"~/.srnet, and the steps that need root (connect, disconnect, setup-router) ask sudo for your "
              f"password themselves. Run it again without sudo, e.g.: {PROG} "
              + " ".join(argv if argv is not None else sys.argv[1:]), file=sys.stderr)
        return 2
    ap, cmds = parser()
    args = ap.parse_args(argv)
    st = State(Path(args.home) if args.home else None)
    try:
        return cmds[args.cmd](args, st)
    except KeyboardInterrupt:
        return 130


def entry() -> int:
    """The `srnet-exits` command."""
    os.environ["SRNET_EXITS_PROG"] = PROG
    return main()


if __name__ == "__main__":
    sys.exit(entry())
