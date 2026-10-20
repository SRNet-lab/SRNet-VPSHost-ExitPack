"""Settings and state for the exits add-on, kept in the SRNet home ($SRNET_HOME, ~/.srnet).

config.json (shared with the SRNet client) may hold, all optional:
  "exits": {"max_payment_usdc": "20",            largest single payment you allow
            "chain": "base",                     preferred chain when a seller takes several
            "rpc": {"8453": "https://..."},      your own RPC per chain (default: public RPCs)
            "testnets": false}                   allow test chains (testing only)
  board_urls, board_pin, sesh_dns, omq, tun, wan_if, overlay_udp_port, bootstrap_ips, listings_path,
  client_sesh (your own .sesh address, if Session Router can't be asked for it)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import chains as C  # noqa: E402  (pack module, put on sys.path by the package)

DEFAULT_MAX_PAYMENT = "20"
CLIENT_DNS = os.environ.get("SRNET_SESH_DNS", "127.3.2.1:53")  # Session Router client DNS listener
OMQ = "tcp://127.0.0.1:1190"


def load(root: Path) -> dict:
    p = Path(root) / "config.json"
    try:
        cfg = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def save(root: Path, cfg: dict) -> None:
    p = Path(root) / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True))
    tmp.replace(p)


def exits(cfg: dict) -> dict:
    e = cfg.get("exits")
    return e if isinstance(e, dict) else {}


def max_payment(cfg: dict) -> int:
    """Per-payment limit in USDC base units (default 20 USDC)."""
    try:
        return C.parse_usdc(exits(cfg).get("max_payment_usdc") or DEFAULT_MAX_PAYMENT)
    except C.ChainError:
        return C.parse_usdc(DEFAULT_MAX_PAYMENT)


def testnets(cfg: dict) -> bool:
    return bool(exits(cfg).get("testnets")) or os.environ.get("SRNET_EXITS_TESTNETS") == "1"


def rpc_overrides(cfg: dict) -> dict:
    r = exits(cfg).get("rpc")
    return {str(k): v for k, v in r.items() if isinstance(v, str)} if isinstance(r, dict) else {}


def sesh_dns(cfg: dict) -> str:
    return cfg.get("sesh_dns") or CLIENT_DNS


def pick_chain(listing: dict, want: str | int | None, cfg: dict) -> int:
    """The chain to pay on: --chain if given, else the preferred chain, else the first the seller takes
    (Arbitrum before Base)."""
    t = testnets(cfg)
    offered = [c for c in listing.get("chains") or [] if C.supported(c, t)]
    if not offered:
        raise C.ChainError("this seller takes no chain this client supports")
    if want not in (None, ""):
        cid = C.by_name(str(want), t)
        if cid not in offered:
            raise C.ChainError(f"this seller doesn't take {C.label(cid)}; it takes "
                               + ", ".join(C.label(c) for c in offered))
        return cid
    pref = exits(cfg).get("chain")
    if pref:
        try:
            cid = C.by_name(str(pref), t)
            if cid in offered:
                return cid
        except C.ChainError:
            pass
    for cid in (42161, 8453):
        if cid in offered:
            return cid
    return offered[0]
