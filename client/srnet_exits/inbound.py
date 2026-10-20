"""Keep the machine reachable on its own public IP while connected to an exit.

`srnet connect` sends all of this machine's traffic into the tunnel (sr-tun0). Without more, that
includes the replies to connections that came IN on the normal link: an SSH or RDP session to a
remote machine's public IP would see its replies leave through the exit and die there, and new
SSH/RDP connections could not be answered either.

So while connected:
- nftables marks every connection that was started from outside on the WAN interface (the first
  packet arrived there in the "original" direction; this also covers sessions that were already
  open before connect, from their next packet on), and gives that connection's replies a fwmark;
- an ip rule sends packets with that fwmark to a small routing table holding only the WAN routes
  (a copy of the main table's routes on the WAN interface, taken before the tunnel routes appear),
  so those replies go back out the way they came in;
- the kill switch lets such replies out on the WAN (ct mark + established/related) and nothing
  else that was not there before (overlay UDP, bootstrap IPs).

Connections this machine starts itself never carry the mark, so they (and DNS) still only go
through the tunnel. Nothing here survives a reboot (nft tables, ip rules and routes are runtime only).
"""
from __future__ import annotations

import json
import re
import subprocess

from . import priv

MARK = 0x5352          # "SR"
TABLE_ID = 5352        # routing table with the WAN routes
RULE_PRIO = 5352       # ip rule priority (before main at 32766)
NFT_TABLE = "srnet_inbound"
_IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")
WHAT = "keeping inbound sessions (SSH/RDP) working"


class InboundError(priv.PrivError):
    pass


def nft_script(wan: str) -> str:
    if not _IFACE_RE.match(wan):
        raise ValueError(f"refusing wan_if={wan!r}: expected simple iface name")
    t = f"inet {NFT_TABLE}"
    return "\n".join([
        f"add table {t}",
        f"add chain {t} pre {{ type filter hook prerouting priority mangle; policy accept; }}",
        # a packet arriving on the WAN in the original direction: this connection was started from outside
        f'add rule {t} pre iifname "{wan}" ct direction original ct mark set {MARK:#x}',
        f"add chain {t} out {{ type route hook output priority mangle; policy accept; }}",
        # our replies on such a connection: fwmark, so the ip rule routes them back out the WAN
        f"add rule {t} out ct mark {MARK:#x} ct direction reply meta mark set {MARK:#x}",
    ]) + "\n"


def _routes(fam: str, wan: str) -> list[dict]:
    try:
        out = subprocess.check_output(["ip", "-j", fam, "route", "show", "table", "main", "dev", wan],
                                      text=True, stderr=subprocess.DEVNULL)
        return json.loads(out or "[]")
    except (OSError, subprocess.CalledProcessError, ValueError):
        return []


def route_cmds(wan: str, v4: list[dict], v6: list[dict]) -> list[str]:
    """Lines for `ip -4|-6 -batch` (the family flag leads each line here; _ip_batch splits them):
    flush our table, copy the WAN routes into it, add the fwmark rules."""
    if not _IFACE_RE.match(wan):
        raise ValueError(f"refusing wan_if={wan!r}: expected simple iface name")
    cmds = []
    for fam, routes in (("-4", v4), ("-6", v6)):
        cmds.append(f"{fam} route flush table {TABLE_ID}")
        cmds.append(f"{fam} rule del priority {RULE_PRIO}")
        for r in routes:
            dst = r.get("dst")
            if not dst or not re.match(r"^[0-9A-Fa-f:./]+$|^default$", dst):
                continue
            line = f"{fam} route replace {dst}"
            gw = r.get("gateway")
            if gw and re.match(r"^[0-9A-Fa-f:.]+$", gw):
                line += f" via {gw}"
            line += f" dev {wan}"
            src = r.get("prefsrc")
            if src and re.match(r"^[0-9A-Fa-f:.]+$", src):
                line += f" src {src}"
            cmds.append(line + f" table {TABLE_ID}")
        cmds.append(f"{fam} rule add fwmark {MARK:#x} lookup {TABLE_ID} priority {RULE_PRIO}")
    return cmds


def _ip_batch(lines: list[str]) -> None:
    """Run "-4 ..." / "-6 ..." lines, one `ip -4|-6 -force -batch -` per family (ip's batch mode
    takes no per-line family flag). -force keeps going past "no such rule" from the cleanup lines."""
    for fam in ("-4", "-6"):
        batch = [l[len(fam) + 1:] for l in lines if l.startswith(fam + " ")]
        if not batch:
            continue
        r = priv.run(["ip", fam, "-force", "-batch", "-"], input="\n".join(batch) + "\n", check=False, what=WHAT)
        errs = [l for l in (r.stderr or "").splitlines()
                if l.strip() and "No such" not in l and "Command failed" not in l and "Nothing to flush" not in l]
        if r.returncode != 0 and errs:
            try:
                priv.fail(priv.argv(["ip"], True, WHAT), r, WHAT)
            except priv.PrivError as e:
                raise InboundError(str(e)) from None


def up(wan: str) -> None:
    """Call before the tunnel takes the default route (the WAN routes are copied from main)."""
    v4, v6 = _routes("-4", wan), _routes("-6", wan)
    if not any(r.get("dst") == "default" for r in v4 + v6):
        raise InboundError(f"no default route on {wan}; pass --wan-if or set wan_if in ~/.srnet/config.json")
    from .killswitch import find_nft
    nft = find_nft()
    if not nft:
        raise InboundError("nftables is not installed (no `nft` command). Install it: sudo apt install nftables")
    priv.run([nft, "delete", "table", "inet", NFT_TABLE], check=False, what=WHAT)
    try:
        priv.run([nft, "-f", "-"], input=nft_script(wan), what=WHAT)
        _ip_batch(route_cmds(wan, v4, v6))
    except priv.PrivError as e:
        try:
            down()
        except priv.PrivError:
            pass
        raise InboundError(str(e)) from None


def down() -> None:
    from .killswitch import find_nft
    nft = find_nft()
    if nft:
        priv.run([nft, "delete", "table", "inet", NFT_TABLE], check=False, what=WHAT)
    _ip_batch([f"{fam} {c}" for fam in ("-4", "-6")
               for c in (f"rule del priority {RULE_PRIO}", f"route flush table {TABLE_ID}")])
