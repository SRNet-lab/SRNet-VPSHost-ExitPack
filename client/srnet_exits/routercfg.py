"""Two Session Router client settings that exits need (read from client.ini, never written here).

- keyfile=...: a persistent client key. Without it the router picks a new random .sesh address at
  every restart (or reboot), and paid exit time is bound to the address it was bought for, so it
  would stop working after the next restart.
- blackhole-routes=false: with the default (true) the router adds "blackhole default" to the
  kernel's *local* routing table while an exit is mapped. The local table is looked up before
  everything else, so every non-local packet fails (EINVAL): replies to SSH/RDP sessions, the
  router's own relay traffic and the tunnel itself. The kill switch (nftables) is the leak
  protection instead.
"""
from __future__ import annotations

import configparser
from pathlib import Path

DEFAULT_INI = "/etc/session-router/client.ini"
KEY = "/var/lib/session-router/client/client.key"
FIX = ("sudo session-router-config -k " + KEY + " && sudo chown _session:_session " + KEY + " \\\n"
       "  && sudo sed -i '/^\\[network\\]/a keyfile=" + KEY + "\\nblackhole-routes=false' "
       "/etc/session-router/client.ini \\\n"
       "  && sudo systemctl restart session-router-client")


def read(path: str | Path = DEFAULT_INI) -> dict | None:
    """{'keyfile': str|None, 'blackhole_routes': bool} or None when the file can't be read."""
    p = Path(path)
    cp = configparser.ConfigParser(strict=False, interpolation=None, inline_comment_prefixes=("#", ";"))
    try:
        cp.read_string(p.read_text())
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    net = cp["network"] if cp.has_section("network") else {}
    bh = str(net.get("blackhole-routes", "true")).strip().lower()
    return {"keyfile": (net.get("keyfile") or "").strip() or None,
            "blackhole_routes": bh not in ("false", "0", "no", "off")}


def problems(path: str | Path = DEFAULT_INI) -> list[str]:
    r = read(path)
    if r is None:
        return []
    out = []
    if not r["keyfile"]:
        out.append("Session Router has no persistent key (keyfile= in [network]): it gets a new .sesh "
                   "address at every restart, and paid exit time only works for the address it was bought for")
    if r["blackhole_routes"]:
        out.append("Session Router's blackhole-routes is on: while connected it blackholes all traffic, "
                   "including your SSH/RDP sessions and the tunnel itself")
    return out
