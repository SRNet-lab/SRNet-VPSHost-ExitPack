#!/usr/bin/env python3
"""Claim endpoint. Buyers POST their signed lease claim here over Session Router.

  claimd.py --host ADDR --port 80 --spool DIR --contract 0x.. --chainid N \
            [--rpc URL --payout-wallet 0x..]

  POST /claim   {"v":1, "hold_id":N, "client_sesh":"<buyer>.sesh", "sig":"0x..."}  (<= 1 KB)

The claim must be well formed and signed for this contract and chain. With --rpc and
--payout-wallet, the lease must also exist on chain, be active, be for this seller, and the
signer must be its buyer (holdInfo, cached briefly). It is then written to the spool, where the
watcher checks it against the chain exactly like a local claim. --host must be loopback or a private address (the router's
tun address); a public address is refused. Requests are rate limited per source address and
overall.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evm import personal_hash, recover  # noqa: E402
from watcher import SESH_RE, claim_message  # noqa: E402

MAX_BODY = 1024
SIG_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")
PER_SOURCE = (10, 60.0)    # requests per window (s) from one address
GLOBAL = (120, 60.0)       # requests per window overall
MAX_SPOOL = 1000           # pending claims before new ones are refused


def bind_ok(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or (ip.is_private and not ip.is_unspecified)


class Limiter:
    def __init__(self, per_source=PER_SOURCE, overall=GLOBAL):
        self.per, self.all = per_source, overall
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _window(self, key: str, span: float, now: float) -> list[float]:
        q = [t for t in self.hits.get(key, []) if now - t < span]
        self.hits[key] = q
        return q

    def allow(self, src: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.lock:
            if len(self.hits) > 10000:
                self.hits.clear()
            g = self._window("*", self.all[1], now)
            p = self._window("src:" + src, self.per[1], now)
            if len(g) >= self.all[0] or len(p) >= self.per[0]:
                return False
            g.append(now)
            p.append(now)
            return True


def claim_signer(claim, contract: str, chainid: int) -> tuple[str | None, str | None]:
    """(signer, None) if the claim is well formed and signed, else (None, reason)."""
    if not isinstance(claim, dict) or set(claim) != {"v", "hold_id", "client_sesh", "sig"}:
        return None, "bad fields"
    if claim["v"] != 1:
        return None, "bad version"
    h = claim["hold_id"]
    if not isinstance(h, int) or isinstance(h, bool) or not 0 < h < 2**64:
        return None, "bad hold_id"
    if not isinstance(claim["client_sesh"], str) or not SESH_RE.match(claim["client_sesh"]):
        return None, "bad client_sesh"
    if not isinstance(claim["sig"], str) or not SIG_RE.match(claim["sig"]):
        return None, "bad sig"
    msg = claim_message(contract, chainid, h, claim["client_sesh"])
    try:
        who = recover(personal_hash(msg), claim["sig"])
    except Exception:
        who = None
    if not who:
        return None, "bad sig"
    return who.lower(), None


def check_claim(claim, contract: str, chainid: int) -> str | None:
    """None if the claim is well formed and signed, else the reason."""
    return claim_signer(claim, contract, chainid)[1]


class LeaseGate:
    """Rejects claims for leases that don't exist, aren't active, aren't for this seller, or
    weren't bought by the signer. lookup(hold_id) -> (buyer, seller_wallet, end, active)."""

    def __init__(self, lookup, payout_wallet: str, ttl: float = 30.0, max_entries: int = 4096,
                 clock=time.monotonic):
        self.lookup, self.wallet = lookup, payout_wallet.lower()
        self.ttl, self.max, self.clock = ttl, max_entries, clock
        self.cache: dict[int, tuple[float, tuple | None]] = {}
        self.lock = threading.Lock()

    def _get(self, hold_id: int):
        now = self.clock()
        with self.lock:
            hit = self.cache.get(hold_id)
            if hit and now - hit[0] < (self.ttl if hit[1] and hit[1][3] else min(self.ttl, 5.0)):
                return hit[1]
        try:
            info = self.lookup(hold_id)
        except Exception:
            return "error"
        with self.lock:
            if len(self.cache) >= self.max:
                self.cache.clear()
            self.cache[hold_id] = (now, info)
        return info

    def check(self, hold_id: int, signer: str) -> str | None:
        info = self._get(hold_id)
        if info == "error":
            return "chain unavailable"
        if not info:
            return "unknown lease"
        buyer, wallet, end, active = info
        if not active or int(end) <= time.time():
            return "lease not active"
        if str(wallet).lower() != self.wallet:
            return "lease not for this seller"
        if str(buyer).lower() != signer:
            return "signer is not the buyer"
        return None


def rpc_lookup(rpc: str, contract: str):
    from evm import JsonRpc, decode, encode_call  # noqa: E402

    j = JsonRpc(rpc)
    zero = "0x" + "00" * 20

    def lookup(hold_id: int):
        data = "0x" + encode_call("holdInfo(uint256)", hold_id).hex()
        res = j.call("eth_call", {"to": contract, "data": data}, "latest")
        u, _amt, wallet, end, active = decode(["address", "uint256", "address", "uint256", "bool"],
                                              bytes.fromhex((res or "0x")[2:]))
        if u.lower() == zero:
            return None
        return u, wallet, end, active
    return lookup


def make_handler(spool: Path, contract: str, chainid: int, limiter: Limiter, gate: LeaseGate | None = None):
    class H(BaseHTTPRequestHandler):
        server_version = "srnet-claim/1"
        sys_version = ""

        def log_message(self, fmt, *args):  # no client addresses
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def address_string(self):
            return "-"

        def _send(self, code: int, text: str):
            b = (text + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._send(404, "not found")

        def do_POST(self):
            if self.path != "/claim":
                return self._send(404, "not found")
            if not limiter.allow(self.client_address[0]):
                return self._send(429, "slow down")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_BODY:
                return self._send(413, "too large")
            try:
                claim = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, "bad json")
            who, why = claim_signer(claim, contract, chainid)
            if why:
                return self._send(400, why)
            if gate is not None:
                why = gate.check(claim["hold_id"], who)
                if why == "chain unavailable":
                    return self._send(503, why)
                if why:
                    return self._send(403, why)
            if sum(1 for _ in spool.glob("*.json")) >= MAX_SPOOL:
                return self._send(503, "busy")
            digest = hashlib.sha256(json.dumps(claim, sort_keys=True).encode()).hexdigest()[:16]
            name = f"hold-{claim['hold_id']}-{digest}.json"
            if (spool / name).exists() or (spool / "done" / name).exists():
                return self._send(202, "already received")
            tmp = spool / f".{name}.tmp"
            tmp.write_text(json.dumps(claim))
            os.replace(tmp, spool / name)
            return self._send(202, "received")
    return H


def serve(host: str, port: int, spool: Path, contract: str, chainid: int,
          limiter: Limiter | None = None, gate: LeaseGate | None = None) -> ThreadingHTTPServer:
    if not bind_ok(host):
        raise SystemExit("refusing to bind a public address; use the router's tun address")
    spool.mkdir(parents=True, exist_ok=True)
    cls = ThreadingHTTPServer
    if ":" in host:
        import socket

        class V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        cls = V6
    return cls((host, port), make_handler(spool, contract.lower(), int(chainid), limiter or Limiter(), gate))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--spool", required=True)
    ap.add_argument("--contract", required=True)
    ap.add_argument("--chainid", type=int, required=True)
    ap.add_argument("--rpc", help="check each claim's lease on chain before spooling")
    ap.add_argument("--payout-wallet", help="this seller's payout address (with --rpc)")
    a = ap.parse_args(argv)
    gate = None
    if a.rpc or a.payout_wallet:
        if not (a.rpc and a.payout_wallet and re.fullmatch(r"0x[0-9a-fA-F]{40}", a.payout_wallet)):
            ap.error("--rpc and --payout-wallet go together")
        gate = LeaseGate(rpc_lookup(a.rpc, a.contract.lower()), a.payout_wallet)
    srv = serve(a.host, a.port, Path(a.spool), a.contract, a.chainid, gate=gate)
    print(f"claims on http://[{a.host}]:{srv.server_address[1]}/claim", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
