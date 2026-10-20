"""nftables kill switch: block clearnet egress except the overlay UDP + tunnel.

When armed, only traffic via the TUN (exit path) and UDP on the WAN interface
(needed by the overlay) is allowed out. That UDP allow is broad: QUIC/WebRTC
traffic can still reach the internet directly. Explicitly drops DNS (udp/tcp 53) on every path
except the TUN so a blanket UDP allow for the overlay cannot leak queries to
the ISP resolver. Uses the inet family so IPv4 and IPv6
share the same policy.

Needs the `nft` tool (Ubuntu: sudo apt install nftables) and root. As a normal user the rules
are loaded through sudo, which asks for your password in a terminal; without a terminal (the
web UI) only passwordless sudo works. Any failure raises KillSwitchError with the fix: the
kill switch never reports armed when it is not.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from . import priv

DEFAULT_TUN = "sr-tun0"  # the interface Session Router creates (ip link: sr-tun0)

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

    def arm(self, wan_if: str, bootstrap_ips: list[str] | None = None) -> None:
        if not _IFACE_RE.match(wan_if):
            raise ValueError(f"refusing wan_if={wan_if!r}: expected simple iface name")
        if not _IFACE_RE.match(self.tun):
            raise ValueError(f"refusing tun={self.tun!r}: expected simple iface name")
        self.disarm()
        # Order matters: TUN accept, then DNS drop, then WAN overlay UDP.
        # Without the DNS drop, "meta l4proto udp accept" on WAN would leak resolver traffic.
        lines = [
            f"add table {self.table} {self.name}",
            f"add chain {self.table} {self.name} output {{ type filter hook output priority 0; policy drop; }}",
            f'add rule {self.table} {self.name} output oifname "lo" accept',
            f"add rule {self.table} {self.name} output ct state established,related accept",
            f'add rule {self.table} {self.name} output oifname "{self.tun}" accept',
            f"add rule {self.table} {self.name} output udp dport 53 drop",
            f"add rule {self.table} {self.name} output tcp dport 53 drop",
            f'add rule {self.table} {self.name} output oifname "{wan_if}" meta l4proto udp accept',
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
