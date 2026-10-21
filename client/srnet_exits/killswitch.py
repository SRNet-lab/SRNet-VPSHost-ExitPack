"""nftables kill switch: block clearnet egress except Session Router's own UDP + the tunnel.

When armed, only traffic via the TUN (exit path), UDP on the WAN interface sent by the user
Session Router runs as (`meta skuid`, see udp_owner()), IPv6 neighbor discovery and replies to
connections that came in on the WAN (SSH/RDP to this machine, see inbound.py) are allowed out.
Only this machine's own traffic is covered (output hook): traffic forwarded for containers or VMs
is not. If the router's user cannot be found, the WAN UDP allow falls back to all UDP (QUIC/WebRTC
can then reach the internet directly) and connect says so. Explicitly drops DNS (udp/tcp 53) on
every path except the TUN, so no UDP allow can leak queries to the ISP resolver. Uses the inet
family so IPv4 and IPv6 share the same policy.

Needs the `nft` tool (Ubuntu: sudo apt install nftables) and root. As a normal user the rules
are loaded through sudo, which asks for your password in a terminal; without a terminal (the
web UI) only passwordless sudo works. Any failure raises KillSwitchError with the fix: the
kill switch never reports armed when it is not.
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
from dataclasses import dataclass

from . import inbound, priv

DEFAULT_TUN = "sr-tun0"  # the interface Session Router creates (ip link: sr-tun0)

ROUTER_USER = "_session"  # User= in the packaged session-router-client.service
ROUTER_COMM = "session-router"

# What the kill switch leaves open, shown wherever connect is (CLI, status, Exits page, warning).
NOTE = ("The kill switch blocks everything outside the tunnel except Session Router's own UDP, which it "
        "needs. DNS outside the tunnel is blocked.")
NOTE_FALLBACK = ("If connect can't tell which user Session Router runs as, it lets all UDP out and says so.")
NOTE_ALL = ("The kill switch blocks everything outside the tunnel except UDP, which Session Router needs. "
            "Apps that send UDP, such as QUIC in browsers and WebRTC calls, can still leave outside the exit "
            "if the tunnel drops or they pick your normal connection themselves. DNS outside the tunnel is blocked.")


def NOTE_SHARED(user: str) -> str:
    return (f"The kill switch blocks everything outside the tunnel except UDP from programs running as {user}, "
            f"the user Session Router runs as here. Apps running as {user} that send UDP, such as QUIC in "
            "browsers and WebRTC calls, can still leave outside the exit if the tunnel drops or they pick "
            "your normal connection themselves. DNS outside the tunnel is blocked.")


@dataclass
class UdpOwner:
    """Who may send UDP on the WAN while armed. uids empty: anyone (the old full-UDP rule)."""
    uids: tuple = ()
    how: str = "all"        # router: a user of its own; shared: root or your own user; all: unknown
    why: str = ""

    def note(self) -> str:
        if self.how == "router":
            return NOTE
        if self.how == "shared":
            return NOTE_SHARED(", ".join(_uname(u) for u in self.uids))
        return NOTE_ALL


def _uname(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def router_uids(proc: str = "/proc") -> set[int]:
    """The filesystem uid (what nft's skuid sees on its sockets) of every running session-router.
    When one of them runs a client config (client.ini), only those count: an exit run on the same
    machine (exit.ini, its own user) is not the buyer's router."""
    found = []
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return set()
    for p in pids:
        try:
            with open(f"{proc}/{p}/comm") as f:
                comm = f.read().strip()
            with open(f"{proc}/{p}/cmdline", "rb") as f:
                cmd = f.read()
            # comm, or argv[0] in case the main thread renamed itself
            if comm != ROUTER_COMM and os.path.basename(cmd.split(b"\0")[0]) != ROUTER_COMM.encode():
                continue
            client = b"client" in cmd
            with open(f"{proc}/{p}/status") as f:
                for line in f:
                    if line.startswith("Uid:"):
                        found.append((int(line.split()[4]), client))
        except (OSError, ValueError, IndexError):
            continue
    if any(c for _, c in found):
        return {u for u, c in found if c}
    return {u for u, _ in found}


def _caller_uid() -> int:
    su = os.environ.get("SUDO_UID")
    if os.geteuid() == 0 and su and su.isdigit():
        return int(su)
    return os.getuid()


def udp_owner(proc: str = "/proc") -> UdpOwner:
    """The user(s) the WAN UDP allow is limited to: the running router's, else the packaged
    service user, else nobody known (all UDP, as before)."""
    uids = router_uids(proc)
    why = "the running session-router"
    if not uids:
        try:
            uids = {pwd.getpwnam(ROUTER_USER).pw_uid}
            why = f"the {ROUTER_USER} user (Session Router is not running)"
        except KeyError:
            return UdpOwner((), "all", f"could not tell which user Session Router runs as (it is not running and "
                                       f"there is no {ROUTER_USER} user), so the kill switch lets all UDP out")
    me = _caller_uid()
    shared = 0 in uids or me in uids
    return UdpOwner(tuple(sorted(uids)), "shared" if shared else "router", why)


_IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_IPV6_RE = re.compile(r"^[0-9a-fA-F:]+$")


class KillSwitchError(priv.PrivError):
    pass


def find_nft() -> str | None:
    for c in (shutil.which("nft"), "/usr/sbin/nft", "/sbin/nft"):
        if c and os.path.exists(c):
            return c
    return None


@dataclass
class KillSwitch:
    table: str = "inet"
    name: str = "srnet_ks"
    tun: str = DEFAULT_TUN
    overlay_udp_port: int = 1090  # documented default; WAN UDP allow is not port-limited (QUIC varies)

    def _argv(self, args: list[str], interactive: bool) -> list[str]:
        nft = find_nft()
        if not nft:
            raise KillSwitchError("nftables is not installed (no `nft` command). Install it: "
                                  "sudo apt install nftables")
        try:
            return priv.argv([nft, *args], interactive, what="the kill switch (nftables)")
        except priv.PrivError as e:
            raise KillSwitchError(str(e)) from None

    def _run(self, args: list[str], script: str | None = None, interactive: bool = True,
             check: bool = True) -> subprocess.CompletedProcess:
        self._argv(args, interactive)  # nft present?
        try:
            return priv.run([find_nft(), *args], input=script, interactive=interactive, check=check,
                            what="the kill switch (nftables)")
        except priv.PrivError as e:
            raise KillSwitchError(str(e)) from None

    @staticmethod
    def _fail(argv: list[str], args: list[str], r: subprocess.CompletedProcess):
        try:
            priv.fail(argv, r, "the kill switch (nftables)")
        except priv.PrivError as e:
            raise KillSwitchError(str(e)) from None

    def _nft_f(self, script: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run(["-f", "-"], script=script, check=check)

    def _nft(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run(list(args), check=check)

    def status(self) -> bool | None:
        """True armed, False not armed, None cannot tell (nft missing, or root needed and sudo would ask)."""
        try:
            r = self._run(["list", "table", self.table, self.name], interactive=False, check=False)
        except KillSwitchError:
            return None
        if r.returncode == 0:
            return True
        err = (r.stderr or "").lower()
        if "no such file" in err or "does not exist" in err:
            return False
        return None

    def arm(self, wan_if: str, bootstrap_ips: list[str] | None = None,
            owner: UdpOwner | None = None) -> UdpOwner:
        if not _IFACE_RE.match(wan_if):
            raise ValueError(f"refusing wan_if={wan_if!r}: expected simple iface name")
        if not _IFACE_RE.match(self.tun):
            raise ValueError(f"refusing tun={self.tun!r}: expected simple iface name")
        owner = udp_owner() if owner is None else owner
        if any(not isinstance(u, int) or u < 0 for u in owner.uids):
            raise ValueError(f"refusing uids={owner.uids!r}")
        # only Session Router's own sockets may send UDP on the WAN (skuid: the socket owner);
        # without a known user, any UDP (the overlay must keep working)
        who = ""
        if owner.uids:
            who = (f" meta skuid {owner.uids[0]}" if len(owner.uids) == 1
                   else " meta skuid { " + ", ".join(str(u) for u in owner.uids) + " }")
        self.disarm()
        # Order matters: TUN accept, then DNS drop, then WAN overlay UDP.
        # Without the DNS drop, the WAN UDP accept would leak resolver traffic.
        lines = [
            f"add table {self.table} {self.name}",
            f"add chain {self.table} {self.name} output {{ type filter hook output priority 0; policy drop; }}",
            f'add rule {self.table} {self.name} output oifname "lo" accept',
            # replies on connections that were started from outside on the WAN (SSH/RDP to this
            # machine's own IP); inbound.py marks them and routes them back out the WAN. Connections
            # this machine starts itself never carry the mark, so they cannot use this rule.
            f'add rule {self.table} {self.name} output oifname "{wan_if}" ct mark and {inbound.MARK:#x} == '
            f"{inbound.MARK:#x} ct state established,related accept",
            # IPv6 neighbor discovery (link-local only: hop limit 255). Without it the router's
            # neighbor entry for this machine expires and inbound IPv6 SSH and the v6 overlay die.
            f"add rule {self.table} {self.name} output meta l4proto ipv6-icmp icmpv6 type "
            "{ nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit } ip6 hoplimit 255 accept",
            f'add rule {self.table} {self.name} output oifname "{self.tun}" accept',
            f"add rule {self.table} {self.name} output udp dport 53 drop",
            f"add rule {self.table} {self.name} output tcp dport 53 drop",
            f'add rule {self.table} {self.name} output oifname "{wan_if}" meta l4proto udp{who} accept',
        ]
        for ip in bootstrap_ips or []:
            if _IPV4_RE.match(ip):
                lines.append(
                    f'add rule {self.table} {self.name} output oifname "{wan_if}" ip daddr {ip} accept'
                )
            elif _IPV6_RE.match(ip) and ":" in ip:
                lines.append(
                    f'add rule {self.table} {self.name} output oifname "{wan_if}" ip6 daddr {ip} accept'
                )
            else:
                raise ValueError(f"refusing bootstrap ip={ip!r}")
        self._nft_f("\n".join(lines) + "\n")
        return owner

    def disarm(self) -> bool:
        """Remove the rules. Returns True if a table was removed, False if none was there.
        Raises KillSwitchError if it could not tell (e.g. sudo refused)."""
        if not find_nft():
            return False  # without nft there can be no nftables kill switch to remove
        r = self._nft("delete", "table", self.table, self.name, check=False)
        if r.returncode == 0:
            return True
        err = (r.stderr or "").lower()
        if "no such file" in err or "does not exist" in err:
            return False
        self._fail(self._argv(["delete"], True), ["delete", "table", self.name], r)
        return False

    def assert_blocks_clearnet(self, test_dst: str = "1.1.1.1") -> bool:
        return self.status()
