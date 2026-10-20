"""Send all system DNS through the exit while connected, and put it back afterwards.

Ubuntu resolves through systemd-resolved, which sends ordinary lookups to the DHCP DNS server
on your normal interface and binds those queries to that interface, so they bypass the tunnel
routes: a DNS leak. While connected we instead:

  1. start srnet_exits.dnsproxy (as you, no root) on 127.3.2.2:5053: .sesh/.snode to Session Router,
     everything else to the exit's own resolver (the listing's "dns" address) on a socket bound
     to the tunnel interface. No public resolver is involved and nothing falls back to your
     normal DNS: if the exit's resolver does not answer, lookups fail;
  2. point resolved's tunnel link at it with routing domain `~.` (every name) and DefaultRoute,
     keeping the link's own ~sesh ~snode domains (one sudo call, same prompt as the kill switch).

The link's previous DNS servers, domains and default-route flag are saved in state.json and
restored exactly on disconnect. If Session Router restarts the tunnel link is recreated with its
stock settings and the forwarder exits."""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import priv

LISTEN = "127.3.2.2:5053"
WHAT = "switching system DNS to the tunnel (systemd-resolved)"


class TunnelDNSError(priv.PrivError):
    pass


def resolvectl() -> str | None:
    return shutil.which("resolvectl") or next((p for p in ("/usr/bin/resolvectl", "/bin/resolvectl")
                                                if os.path.exists(p)), None)


def resolved_active() -> bool:
    rc = resolvectl()
    if not rc:
        return False
    r = subprocess.run([rc, "status"], capture_output=True, text=True)
    return r.returncode == 0


def _value(out: str) -> list[str]:
    # "Link 12 (sr-tun0): ~sesh ~snode"  ->  ["~sesh", "~snode"]
    line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
    return line.split(":", 1)[1].split() if ":" in line else []


def snapshot(tun: str) -> dict:
    rc = resolvectl()
    if not rc:
        raise TunnelDNSError("systemd-resolved (resolvectl) is not available")
    def get(sub):
        r = subprocess.run([rc, sub, tun], capture_output=True, text=True)
        if r.returncode != 0:
            raise TunnelDNSError(f"resolvectl {sub} {tun}: {(r.stderr or r.stdout).strip()}")
        return r.stdout
    # "Link 12 (sr-tun0): 127.3.2.1" -- the value part may itself contain ':' (IPv6, ports)
    dns_line = get("dns").strip().splitlines()
    dns = dns_line[-1].split("): ", 1)[1].split() if dns_line and "): " in dns_line[-1] else []
    return {"tun": tun, "dns": dns, "domains": _value(get("domain")),
            "default_route": (_value(get("default-route")) or ["no"])[0] == "yes"}


def is_ours(snap: dict) -> bool:
    return LISTEN in snap.get("dns", []) or "~." in snap.get("domains", [])


def _resolvectl_script(tun: str, dns: list[str], domains: list[str], default_route: bool) -> str:
    rc = resolvectl() or "resolvectl"
    q = shlex.quote
    parts = [f"{q(rc)} dns {q(tun)} " + (" ".join(q(d) for d in dns) if dns else "''"),
             f"{q(rc)} domain {q(tun)} " + (" ".join(q(d) for d in domains) if domains else "''"),
             f"{q(rc)} default-route {q(tun)} {'true' if default_route else 'false'}",
             f"{q(rc)} flush-caches"]
    return " && ".join(parts)


def _root_sh(script: str) -> None:
    try:
        priv.run(["/bin/sh", "-c", script], what=WHAT)
    except priv.PrivError as e:
        raise TunnelDNSError(str(e)) from None


def apply(saved: dict, listen: str = LISTEN) -> None:
    doms = ["~."] + [d for d in saved["domains"] if d != "~."]
    for d in ("~sesh", "~snode"):
        if d not in doms:
            doms.append(d)
    _root_sh(_resolvectl_script(saved["tun"], [listen], doms, True))


def restore(saved: dict) -> None:
    _root_sh(_resolvectl_script(saved["tun"], saved["dns"], saved["domains"], saved["default_route"]))


def tun_networks(tun: str) -> list[str]:
    """IPv4 networks on the tunnel link (e.g. ["172.16.0.0/16"])."""
    import ipaddress
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", tun], capture_output=True, text=True).stdout
    except OSError:
        return []
    nets = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            nets.append(str(ipaddress.IPv4Interface(parts[parts.index("inet") + 1]).network))
    return nets


def start_proxy(home: Path, tun: str, upstream: str, listen: str = LISTEN, timeout: float = 20) -> int:
    pidfile = home / "dnsproxy.pid"
    stop_proxy(home)
    log = open(home / "dnsproxy.log", "ab")
    cmd = [sys.executable, "-m", "srnet_exits.dnsproxy", "--listen", listen, "--dev", tun, "--upstream", upstream,
           "--pidfile", str(pidfile)]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         cwd=str(Path(__file__).resolve().parents[1]))
    from .dnsproxy import query, rcode
    deadline = time.time() + timeout
    last = "no answer"
    while time.time() < deadline:
        if p.poll() is not None:
            raise TunnelDNSError(f"tunnel DNS forwarder exited (see {home / 'dnsproxy.log'})")
        try:
            r = query(listen, "example.com", 1, timeout=8)
            if rcode(r) == 0:
                return p.pid
            last = f"rcode {rcode(r)}"
        except OSError as e:
            last = str(e)
        time.sleep(0.5)
    stop_proxy(home)
    raise TunnelDNSError(f"the exit's resolver ({upstream}) does not answer through the tunnel ({last})")


def proxy_pid(home: Path) -> int | None:
    try:
        pid = int((home / "dnsproxy.pid").read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if b"srnet_exits.dnsproxy" in cmdline else None


def stop_proxy(home: Path) -> bool:
    pid = proxy_pid(home)
    if not pid:
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    try:
        (home / "dnsproxy.pid").unlink()
    except OSError:
        pass
    return True
