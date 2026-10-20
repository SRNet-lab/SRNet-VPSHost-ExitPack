#!/usr/bin/env python3
"""Claim endpoint. Buyers ask for payment quotes and report payments here, over Session Router.

  claimd.py --host ADDR --port 80 --dir STATE --quote-key quote.key --listing listing.json \
            [--cap-usdc 20] [--testnets]

  POST /claim/quote        quote request (quote.py)          -> 200 signed quote
  POST /claim/paid         paid notice {"v":1,"quote_id","tx","sig"} -> 202, checked by the watcher
  GET  /claim/status/<id>  {"status": "waiting"|"admitted"|"rejected", ...}
  POST /claim              old contract claims                -> 410

Price, max hours, payout address and chains come from the signed listing, so a quote can never
offer anything the listing doesn't. Each quote gets an exact amount whose last digits no other
open quote on that chain uses. Bodies are at most 2 KB; requests are rate limited per source
address and overall, and each buyer address can hold only a few open quotes at a time.

STATE layout (shared with watcher.py):
  quotes/<id>.json   issued quotes          spool/<id>-<tx>.json  paid notices for the watcher
  status/<id>.json   watcher results        quotes.lock           serialises quote creation

--host must be loopback or a private address (the router's tun address); a public address is
refused.
"""
from __future__ import annotations

import argparse
import fcntl
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
import chains as C  # noqa: E402
import listing as L  # noqa: E402
import quote as Q  # noqa: E402

MAX_BODY = 2048
PER_SOURCE = (20, 60.0)    # requests per window (s) from one address
GLOBAL = (240, 60.0)       # requests per window overall
MAX_SPOOL = 1000           # pending paid notices before new ones are refused
MAX_OPEN_PER_CLIENT = 3    # open (unexpired, unpaid) quotes per buyer address
PAID_GRACE_S = 900         # paid notices accepted until this long after a quote expires
KEEP_QUOTES_S = 2 * 86400  # quote files older than this are removed
RESERVE_S = 3600           # a quote's amount is not reused until this long after it expires
DEFAULT_CAP_USDC = "20"


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


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True))
    os.replace(tmp, path)


class Desk:
    """Issues quotes and takes paid notices. Pure file state, so tests drive it without HTTP."""

    def __init__(self, state_dir: str | Path, quote_key: str | Path, listing_path: str | Path,
                 cap: int | None = None, testnets: bool = False, clock=time.time):
        self.dir = Path(state_dir)
        for sub in ("quotes", "spool", "status"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.key = Q.load_key(quote_key)
        self.listing_path = Path(listing_path)
        self.cap = C.parse_usdc(DEFAULT_CAP_USDC) if cap is None else int(cap)
        self.testnets, self.clock = testnets, clock
        self._listing: tuple[float, dict] | None = None

    def listing(self) -> dict:
        m = self.listing_path.stat().st_mtime
        if not self._listing or self._listing[0] != m:
            doc = json.loads(self.listing_path.read_text())
            if not L.verify(doc):
                raise RuntimeError("listing signature is invalid")
            body = doc["listing"]
            why = L.listing_error(body, self.testnets)
            if why:
                raise RuntimeError(f"listing can't take payments: {why}")
            if body["quote_key"] != Q.pub_hex(self.key):
                raise RuntimeError("listing quote_key does not match --quote-key")
            self._listing = (m, body)
        return self._listing[1]

    def _quotes(self):
        for f in (self.dir / "quotes").glob("*.json"):
            try:
                yield f, json.loads(f.read_text())
            except (OSError, ValueError):
                continue

    def load_quote(self, qid: str) -> dict | None:
        if not Q.ID_RE.match(qid or ""):
            return None
        try:
            return json.loads((self.dir / "quotes" / f"{qid}.json").read_text())
        except (OSError, ValueError):
            return None

    def _paid(self, qid: str) -> bool:
        try:
            return json.loads((self.dir / "status" / f"{qid}.json").read_text()).get("status") == "admitted"
        except (OSError, ValueError):
            return False

    def quote(self, req) -> tuple[int, dict]:
        body = self.listing()
        why = Q.request_error(req, body["seller"], body["chains"], body["max_hours"], self.testnets)
        if why:
            return 400, {"error": why}
        now = int(self.clock())
        with open(self.dir / "quotes.lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            taken: set[int] = set()
            mine = 0
            for f, rec in self._quotes():
                q = rec["doc"]["quote"]
                if now - q["issued"] > KEEP_QUOTES_S:
                    f.unlink(missing_ok=True)
                    continue
                if q["expires"] + RESERVE_S < now or self._paid(q["id"]):
                    continue
                if q["chainid"] == req["chainid"]:
                    taken.add(rec["pay_id"])
                if q["client_sesh"] == req["client_sesh"] and q["expires"] > now:
                    mine += 1
            if mine >= MAX_OPEN_PER_CLIENT:
                return 429, {"error": "too many open quotes for this address; pay or wait for one to expire"}
            try:
                doc, pid = Q.make_quote(self.key, req, payout=body["payout"],
                                        price_per_hour=C.parse_usdc(body["price_per_hour_usdc"]), taken=taken,
                                        cap=self.cap, now=now, testnets=self.testnets)
            except Q.QuoteError as e:
                return 403, {"error": str(e)}
            _write_json(self.dir / "quotes" / f"{doc['quote']['id']}.json", {"doc": doc, "pay_id": pid})
        return 200, doc

    def paid(self, notice) -> tuple[int, dict]:
        qid = notice.get("quote_id") if isinstance(notice, dict) else None
        rec = self.load_quote(qid) if isinstance(qid, str) else None
        if not rec:
            return 404, {"error": "unknown quote"}
        q = rec["doc"]["quote"]
        why = Q.paid_error(notice, q)
        if why:
            return 403, {"error": why}
        if self.clock() > q["expires"] + PAID_GRACE_S:
            return 410, {"error": "quote expired; ask for a new one"}
        if self._paid(qid):
            return 200, self.status(qid)[1]
        if sum(1 for _ in (self.dir / "spool").glob("*.json")) >= MAX_SPOOL:
            return 503, {"error": "busy"}
        name = f"{qid}-{notice['tx'][2:18]}.json"
        if not (self.dir / "spool" / name).exists():
            _write_json(self.dir / "spool" / name, notice)
        return 202, {"status": "received"}

    def status(self, qid: str) -> tuple[int, dict]:
        rec = self.load_quote(qid)
        if not rec:
            return 404, {"error": "unknown quote"}
        try:
            return 200, json.loads((self.dir / "status" / f"{qid}.json").read_text())
        except (OSError, ValueError):
            q = rec["doc"]["quote"]
            st = "expired" if self.clock() > q["expires"] + PAID_GRACE_S else "waiting"
            return 200, {"status": st}


def make_handler(desk: Desk, limiter: Limiter):
    class H(BaseHTTPRequestHandler):
        server_version = "srnet-claim/2"
        sys_version = ""

        def log_message(self, fmt, *args):  # no client addresses
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def address_string(self):
            return "-"

        def _send(self, code: int, obj):
            b = (json.dumps(obj) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            m = re.fullmatch(r"/claim/status/([0-9a-f]{16})", self.path)
            if not m:
                return self._send(404, {"error": "not found"})
            if not limiter.allow(self.client_address[0]):
                return self._send(429, {"error": "slow down"})
            self._send(*desk.status(m.group(1)))

        def do_POST(self):
            if self.path == "/claim":
                return self._send(410, {"error": "this exit takes direct USDC payments now; update SRNet Exits"})
            if self.path not in ("/claim/quote", "/claim/paid"):
                return self._send(404, {"error": "not found"})
            if not limiter.allow(self.client_address[0]):
                return self._send(429, {"error": "slow down"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n <= 0 or n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                msg = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"error": "bad json"})
            try:
                if self.path == "/claim/quote":
                    return self._send(*desk.quote(msg))
                return self._send(*desk.paid(msg))
            except RuntimeError as e:
                sys.stderr.write(f"claimd: {e}\n")
                return self._send(503, {"error": "seller is not set up to take payments right now"})
    return H


def serve(host: str, port: int, desk: Desk, limiter: Limiter | None = None) -> ThreadingHTTPServer:
    if not bind_ok(host):
        raise SystemExit("refusing to bind a public address; use the router's tun address")
    cls = ThreadingHTTPServer
    if ":" in host:
        import socket

        class V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        cls = V6
    return cls((host, port), make_handler(desk, limiter or Limiter()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--dir", required=True, help="state directory shared with watcher.py")
    ap.add_argument("--quote-key", required=True)
    ap.add_argument("--listing", required=True, help="this exit's signed listing.json")
    ap.add_argument("--cap-usdc", default=DEFAULT_CAP_USDC, help="largest single payment to quote (default 20)")
    ap.add_argument("--testnets", action="store_true")
    a = ap.parse_args(argv)
    desk = Desk(a.dir, a.quote_key, a.listing, cap=C.parse_usdc(a.cap_usdc), testnets=a.testnets)
    desk.listing()  # fail early on a bad setup
    srv = serve(a.host, a.port, desk)
    print(f"claims on http://[{a.host}]:{srv.server_address[1]}/claim/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
