"""Keep the machine reachable on its own public IP while connected to an exit.

`srnet connect` sends all of this machine's traffic into the tunnel (sr-tun0). Without more, that
includes the replies to connections that came IN on the normal link: an SSH or RDP session to a
remote machine's public IP would see its replies leave through the exit and die there, and new
SSH/RDP connections could not be answered either.

So while connected:
- nftables sets one bit (MARK) in the conntrack mark of every connection that was started from
  outside on the WAN interface (its first packet arrived there in the "original" direction; this
  also covers sessions that were already open before connect, from their next packet on). Only that
  bit is touched: other users of the mark (wg-quick, Tailscale, kube-proxy) keep theirs;
- the replies on such a connection get the same bit in the packet mark: replies this machine sends
  (output hook) and replies forwarded from containers with published ports (prerouting);
- packets arriving on the WAN for this machine on such a connection get the bit too, and
  net.ipv4.conf.all.src_valid_mark=1 (the old value comes back at disconnect), so a strict
  rp_filter checks their source against the WAN routes instead of the tunnel;
- an ip rule sends packets with that bit to a small routing table holding only the WAN routes
  (a copy of the main table's routes on the WAN interface, metrics included, taken before the
  tunnel routes appear), so those replies go back out the way they came in. The rule priority and
  table number are the first free pair from 5352 on; a rule or table someone else uses is never
  touched;
- the kill switch lets such replies out on the WAN (ct mark bit + established/related) and nothing
  else that was not there before (overlay UDP, bootstrap IPs, IPv6 neighbor discovery).

Connections this machine starts itself never carry the bit, so they (and DNS) still only go
through the tunnel. Nothing here survives a reboot (nft tables, ip rules and routes are runtime only).
"""
from __future__ import annotations

import json
import re
import subprocess

from . import priv

MARK = 0x02000000    # one bit of the 32-bit mark, used with this mask everywhere
FIRST_SLOT = 5352    # ip rule priority == routing table id; first candidate
SLOTS = 16           # candidates 5352..5367
NFT_TABLE = "srnet_inbound"
SVM_CHAIN = "svm_was_0"      # marker chain: src_valid_mark was 0 before connect
SVM = "/proc/sys/net/ipv4/conf/all/src_valid_mark"
_IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")
_ADDR_RE = re.compile(r"^[0-9A-Fa-f:.]+$")
WHAT = "keeping inbound sessions (SSH/RDP) working"
M = f"{MARK:#x}"


class InboundError(priv.PrivError):
    pass


def _check_iface(wan: str) -> None:
    if not _IFACE_RE.match(wan):
        raise ValueError(f"refusing wan_if={wan!r}: expected simple iface name")


def nft_script(wan: str, svm_was_0: bool = False) -> str:
    _check_iface(wan)
    t = f"inet {NFT_TABLE}"
    has = f"ct mark and {M} == {M}"
    lines = [
        f"add table {t}",
        # after DNAT (-100), so `fib daddr type local` sees the real destination
        f"add chain {t} pre {{ type filter hook prerouting priority -90; policy accept; }}",
        # a packet arriving on the WAN in the original direction: this connection was started from outside
        f'add rule {t} pre iifname "{wan}" ct direction original ct mark set ct mark or {M}',
        # inbound packets for this machine on such a connection: rp_filter (src_valid_mark) uses the WAN table
        f'add rule {t} pre iifname "{wan}" {has} fib daddr type local meta mark set meta mark or {M}',
        # replies forwarded from containers (published ports) on such a connection: back out the WAN
        f'add rule {t} pre iifname != "{wan}" ct direction reply {has} meta mark set meta mark or {M}',
        f"add chain {t} out {{ type route hook output priority mangle; policy accept; }}",
        # our replies on such a connection: the mark bit, so the ip rule routes them back out the WAN
        f"add rule {t} out {has} ct direction reply meta mark set meta mark or {M}",
    ]
    if svm_was_0:
        lines.append(f"add chain {t} {SVM_CHAIN}")
    return "\n".join(lines) + "\n"


def _ipj(*args: str) -> list[dict]:
    try:
        out = subprocess.check_output(["ip", "-j", *args], text=True, stderr=subprocess.DEVNULL)
        return json.loads(out or "[]")
    except (OSError, subprocess.CalledProcessError, ValueError):
        return []


def _routes(fam: str, wan: str) -> list[dict]:
    return _ipj(fam, "route", "show", "table", "main", "dev", wan)


def _is_ours(rule: dict) -> bool:
    fw, mask = str(rule.get("fwmark", "")), str(rule.get("fwmask", ""))
    try:
        return int(fw, 16) == MARK and mask and int(mask, 16) == MARK
    except ValueError:
        return False


def our_rules(rules4: list[dict], rules6: list[dict]) -> list[tuple[str, int, str]]:
    """(family, priority, table) of the rules this module added (fwmark MARK/MARK)."""
    return [(fam, int(r.get("priority", 0)), str(r.get("table", "")))
            for fam, rules in (("-4", rules4), ("-6", rules6)) for r in rules if _is_ours(r)]


def pick_slot(rules4: list[dict], rules6: list[dict], table_used) -> int | None:
    """First n in 5352.. with no rule at priority n and table n empty and unreferenced, in both
    families. table_used(n) -> True when table n has routes (in either family)."""
    prios = {int(r.get("priority", -1)) for r in rules4 + rules6}
    tables = {str(r.get("table", "")) for r in rules4 + rules6}
    for n in range(FIRST_SLOT, FIRST_SLOT + SLOTS):
        if n in prios or str(n) in tables or table_used(n):
            continue
        return n
    return None


def _table_used(n: int) -> bool:
    return bool(_ipj("-4", "route", "show", "table", str(n)) or _ipj("-6", "route", "show", "table", str(n)))


def route_cmds(wan: str, v4: list[dict], v6: list[dict], slot: int = FIRST_SLOT) -> list[str]:
    """Lines for `ip -4|-6 -batch` (the family flag leads each line here; _ip_batch splits them):
    copy the WAN routes (with their metrics) into table `slot`, add the mark rule at priority `slot`."""
    _check_iface(wan)
    cmds = []
    for fam, routes in (("-4", v4), ("-6", v6)):
        for r in routes:
            dst = r.get("dst")
            if not dst or not re.match(r"^[0-9A-Fa-f:./]+$|^default$", dst):
                continue
            line = f"{fam} route append {dst}"
            gw = r.get("gateway")
            if gw and _ADDR_RE.match(gw):
                line += f" via {gw}"
            line += f" dev {wan}"
            src = r.get("prefsrc")
            if src and _ADDR_RE.match(src):
                line += f" src {src}"
            metric = r.get("metric")
            if isinstance(metric, int) and metric >= 0:
                line += f" metric {metric}"
            if "onlink" in (r.get("flags") or []):
                line += " onlink"
            cmds.append(line + f" table {slot}")
        cmds.append(f"{fam} rule add fwmark {M}/{M} lookup {slot} priority {slot}")
    return cmds


def _ip_batch(lines: list[str]) -> None:
    """Run "-4 ..." / "-6 ..." lines, one `ip -4|-6 -force -batch -` per family (ip's batch mode
    takes no per-line family flag). -force keeps going past "no such rule" from cleanup lines."""
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


def _svm() -> str | None:
    try:
        with open(SVM) as f:
            return f.read().strip()
    except OSError:
        return None


def up(wan: str) -> None:
    """Call before the tunnel takes the default route (the WAN routes are copied from main)."""
    _check_iface(wan)
    v4, v6 = _routes("-4", wan), _routes("-6", wan)
    if not any(r.get("dst") == "default" for r in v4 + v6):
        raise InboundError(f"no default route on {wan}; pass --wan-if or set wan_if in ~/.srnet/config.json")
    from .killswitch import find_nft
    nft = find_nft()
    if not nft:
        raise InboundError("nftables is not installed (no `nft` command). Install it: sudo apt install nftables")
    down()  # leftovers of an earlier connect (only ours)
    slot = pick_slot(_ipj("-4", "rule", "show"), _ipj("-6", "rule", "show"), _table_used)
    if slot is None:
        raise InboundError(f"ip rule priorities / routing tables {FIRST_SLOT}-{FIRST_SLOT + SLOTS - 1} are all "
                           "in use by other software; free one of them (ip rule; ip route show table N)")
    svm_was_0 = _svm() == "0"
    try:
        priv.run([nft, "-f", "-"], input=nft_script(wan, svm_was_0), what=WHAT)
        if svm_was_0:
            priv.run(["sysctl", "-qw", "net.ipv4.conf.all.src_valid_mark=1"], what=WHAT)
        _ip_batch(route_cmds(wan, v4, v6, slot))
    except priv.PrivError as e:
        try:
            down()
        except priv.PrivError:
            pass
        raise InboundError(str(e)) from None


def down() -> None:
    """Remove only what up() added: our nft table, the fwmark MARK/MARK rules and their tables,
    and put src_valid_mark back if we changed it."""
    from .killswitch import find_nft
    nft = find_nft()
    if nft:
        r = priv.run([nft, "list", "chain", "inet", NFT_TABLE, SVM_CHAIN], check=False, what=WHAT)
        if r.returncode == 0:
            priv.run(["sysctl", "-qw", "net.ipv4.conf.all.src_valid_mark=0"], check=False, what=WHAT)
        priv.run([nft, "delete", "table", "inet", NFT_TABLE], check=False, what=WHAT)
    lines = []
    for fam, prio, table in our_rules(_ipj("-4", "rule", "show"), _ipj("-6", "rule", "show")):
        if not table.isdigit():
            continue
        lines.append(f"{fam} rule del priority {prio} fwmark {M}/{M} lookup {table}")
        lines.append(f"{fam} route flush table {table}")
    if lines:
        _ip_batch(lines)
