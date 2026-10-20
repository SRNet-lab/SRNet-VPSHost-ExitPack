#!/usr/bin/env python3
"""Claim endpoint. Buyers ask for payment quotes and report payments here, over Session Router.

  claimd.py --host ADDR --port 80 --dir STATE --quote-key quote.key --listing listing.json \
            [--cap-usdc 20] [--quotes-per-hour 150] [--testnets] [--debug-sources]

  POST /claim/quote        quote request (quote.py)          -> 200 signed quote
  POST /claim/paid         paid notice {"v":1,"quote_id","tx","sig"} -> 202, checked by the watcher
  GET  /claim/status/<id>  {"status": "waiting"|"admitted"|"rejected", ...}
  POST /claim              old contract claims                -> 410

Price, max hours, payout address and chains come from the signed listing, so a quote can never
offer anything the listing doesn't. Each quote gets an exact amount that no other quote on that
chain has used in the last 7 days, paid or not (quote.py). Bodies are at most 2 KB.

Limits, so one requester can't wear the desk down or lock other buyers out:
  - requests per source address and overall (per minute), and quotes per source and overall
    (per hour; the overall rate, 150 by default, is set so the 7-day amount pool can't run out);
  - open quotes per buyer key and per source address. Neither is the .sesh address a request
    names, so naming someone else's address can't use up their limit. Requests are signed by
    the buyer key with the time, and a signature is taken once, so nobody can ask in another
    buyer's name either (not even a seller who saw that buyer's earlier requests). The source
    address is the Session Router session the request came in on;
  - at most 3 different tx hashes per quote, a bounded spool, and 10 s socket timeouts with a
    cap on connections per source and overall.

STATE layout (shared with watcher.py):
  quotes/<id>.json   issued quotes          spool/<id>-<tx>.json  paid notices for the watcher
  status/<id>.json   watcher results        index.sqlite          amounts, limits, notices
  Quote files and their notices and results are kept for quote.KEEP_QUOTES_S (8+ days); a paid
  notice is taken any time until then. The index can always be rebuilt from the files: if it is
  missing or damaged, it is moved aside (index.sqlite.bad-<time>) and rebuilt on start.

--debug-sources logs the salted source tag of each quote (never the address), to check that
different buyers come in on different sessions. Off by default.

--host must be loopback or a private address (the router's tun address); a public address is
refused.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
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
PER_SOURCE = (20, 60.0)          # requests per window (s) from one address
GLOBAL = (240, 60.0)             # requests per window overall
MAX_SOURCES = 10000              # tracked sources in the request limiter
QUOTES_PER_SOURCE = (10, 3600)   # new quotes per hour from one address
QUOTES_GLOBAL = (150, 3600)      # new quotes per hour overall (default; --quotes-per-hour)
MAX_OPEN_PER_BUYER = 5           # unexpired quotes per buyer key
MAX_OPEN_PER_SOURCE = 5          # unexpired quotes per source address
MAX_OPEN = 2000                  # unexpired quotes overall
MAX_TX_PER_QUOTE = 3             # different tx hashes one quote can report
MAX_SPOOL = 1000                 # pending paid notices before new ones are refused
PAID_GRACE_S = 900               # after this long past expiry, an unreported quote shows as "expired"
RESERVE_S = Q.RESERVE_S          # an amount is not quoted again within this long
KEEP_QUOTES_S = Q.KEEP_QUOTES_S  # quote files older than this are removed
PRUNE_EVERY_S = 600
SOCKET_TIMEOUT_S = 10
MAX_CONN = 48                    # open connections overall (the unit allows 64 tasks)
MAX_CONN_PER_SOURCE = 4
DEFAULT_CAP_USDC = "20"
REPLAY_KEEP_S = 2 * Q.REQUEST_SKEW_S + 60  # request signatures are remembered this long
# Pool size: each (chain, purchase price) has at least quote.EXTRA_MIN amounts. At the default
# global rate, even if every quote in the 7-day hold were for one and the same purchase, that
# pool can't fill. A higher --quotes-per-hour than SAFE_QUOTES_PER_HOUR can make the desk answer
# "busy" for a popular cheap purchase until amounts free up (no payment is at risk).
SAFE_QUOTES_PER_HOUR = int(Q.POOL_BUSY * Q.EXTRA_MIN * 3600 // (RESERVE_S + Q.MAX_QUOTE_TTL_S))
assert QUOTES_GLOBAL[0] <= SAFE_QUOTES_PER_HOUR


def bind_ok(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or (ip.is_private and not ip.is_unspecified)


class Limiter:
    def __init__(self, per_source=PER_SOURCE, overall=GLOBAL, max_sources=MAX_SOURCES):
        self.per, self.all, self.max_sources = per_source, overall, max_sources
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _window(self, key: str, span: float, now: float) -> list[float]:
        q = [t for t in self.hits.get(key, []) if now - t < span]
        self.hits[key] = q
        return q

    def _evict(self, now: float) -> None:
        """Forget idle sources; if that isn't enough, the least recently seen ones. Never "*"."""
        for k in [k for k, v in self.hits.items() if k != "*" and (not v or now - v[-1] >= self.per[1])]:
            del self.hits[k]
        if len(self.hits) > self.max_sources:
            old = sorted((v[-1], k) for k, v in self.hits.items() if k != "*")
            for _, k in old[: len(self.hits) - self.max_sources // 2]:
                del self.hits[k]

    def allow(self, src: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.lock:
            if len(self.hits) > self.max_sources:
                self._evict(now)
            g = self._window("*", self.all[1], now)
            p = self._window("src:" + src, self.per[1], now)
            if len(g) >= self.all[0] or len(p) >= self.per[0]:
                return False
            g.append(now)
            p.append(now)
            return True


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, sort_keys=True))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, chainid INTEGER, amount INTEGER, issued INTEGER,
                                   expires INTEGER, buyer_key TEXT, src TEXT);
CREATE INDEX IF NOT EXISTS q_amount ON quotes (chainid, amount);
CREATE INDEX IF NOT EXISTS q_issued ON quotes (issued);
CREATE INDEX IF NOT EXISTS q_expires ON quotes (expires);
CREATE INDEX IF NOT EXISTS q_buyer ON quotes (buyer_key, expires);
CREATE INDEX IF NOT EXISTS q_src ON quotes (src, issued);
CREATE TABLE IF NOT EXISTS notices (qid TEXT, tx TEXT, PRIMARY KEY (qid, tx));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS requests (sig TEXT PRIMARY KEY, ts INTEGER);
"""


class Desk:
    """Issues quotes and takes paid notices. File state plus an sqlite index, so tests drive it without HTTP."""

    def __init__(self, state_dir: str | Path, quote_key: str | Path, listing_path: str | Path,
                 cap: int | None = None, testnets: bool = False, clock=time.time, debug_sources: bool = False,
                 quotes_per_hour: int | None = None):
        self.dir = Path(state_dir)
        for sub in ("quotes", "spool", "status"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.key = Q.load_key(quote_key)
        self.listing_path = Path(listing_path)
        self.cap = C.parse_usdc(DEFAULT_CAP_USDC) if cap is None else int(cap)
        self.testnets, self.clock = testnets, clock
        self.debug_sources = debug_sources
        self.quotes_per_hour = quotes_per_hour   # None: QUOTES_GLOBAL
        self.rng = secrets.randbelow
        self._listing: tuple[float, dict] | None = None
        self.lock = threading.Lock()
        try:
            self._open_index()
        except sqlite3.DatabaseError as e:
            self._set_aside(e)
            self._open_index()
        self.last_prune = 0.0

    def _open_index(self) -> None:
        self.db = sqlite3.connect(str(self.dir / "index.sqlite"), check_same_thread=False, isolation_level=None)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            ok = self.db.execute("PRAGMA quick_check").fetchone()
            if not ok or ok[0] != "ok":
                raise sqlite3.DatabaseError(f"quick_check: {ok[0] if ok else 'no answer'}")
            self.db.executescript(SCHEMA)
            row = self.db.execute("SELECT v FROM meta WHERE k='salt'").fetchone()
            if row:
                self.salt = row[0]
            else:
                self.salt = secrets.token_hex(16)
                self.db.execute("INSERT INTO meta VALUES ('salt', ?)", (self.salt,))
                self._backfill()
        except sqlite3.DatabaseError:
            self.db.close()
            raise

    def _set_aside(self, err: Exception) -> None:
        """Move a damaged index (and its WAL files) out of the way; it is rebuilt from the files."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        for suffix in ("", "-wal", "-shm"):
            p = self.dir / f"index.sqlite{suffix}"
            if p.exists():
                os.replace(p, p.with_name(f"{p.name}.bad-{stamp}"))
        sys.stderr.write(f"claimd: index.sqlite was damaged ({err}); moved aside and rebuilt from the files\n")

    def _backfill(self) -> None:
        """New index (first start, or after a damaged one): rebuild it from the quote files and the
        paid notices in the spool (pending, done and rejected), so reported quotes stay reported
        and keep their tx count."""
        for f in (self.dir / "quotes").glob("*.json"):
            try:
                q = json.loads(f.read_text())["doc"]["quote"]
                self.db.execute("INSERT OR IGNORE INTO quotes VALUES (?,?,?,?,?,?,?)",
                                (q["id"], q["chainid"], int(q["amount"]), q["issued"], q["expires"], q["buyer_key"], ""))
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                continue
        for sub in ("spool", "spool/done", "spool/rejected"):
            for f in (self.dir / sub).glob("*.json"):
                try:
                    n = json.loads(f.read_text())
                    qid, tx = n["quote_id"], n["tx"]
                    if Q.ID_RE.match(qid) and Q.TX_RE.match(tx):
                        self.db.execute("INSERT OR IGNORE INTO notices VALUES (?, ?)", (qid, tx))
                except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                    continue

    def source_tag(self, src: str) -> str:
        """Source addresses are kept only as a salted hash."""
        return hashlib.sha256(f"{self.salt}|{src}".encode()).hexdigest()[:24]

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

    def _count(self, sql: str, *args) -> int:
        return self.db.execute(sql, args).fetchone()[0]

    def prune(self, now: int | None = None) -> int:
        """Remove quotes (files, notices, results, index rows) older than KEEP_QUOTES_S."""
        now = int(self.clock()) if now is None else int(now)
        old = [r[0] for r in self.db.execute("SELECT id FROM quotes WHERE issued < ?", (now - KEEP_QUOTES_S,))]
        for qid in old:
            for p in (self.dir / "quotes" / f"{qid}.json", self.dir / "status" / f"{qid}.json"):
                p.unlink(missing_ok=True)
        if old:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.executemany("DELETE FROM quotes WHERE id = ?", [(q,) for q in old])
            self.db.executemany("DELETE FROM notices WHERE qid = ?", [(q,) for q in old])
            self.db.execute("COMMIT")
        cut = time.time() - KEEP_QUOTES_S  # file times are wall-clock times
        for sub in ("spool/done", "spool/rejected"):
            d = self.dir / sub
            if d.is_dir():
                for f in d.iterdir():
                    try:
                        if f.stat().st_mtime < cut:
                            f.unlink()
                    except OSError:
                        continue
        return len(old)

    def quote(self, req, src: str = "local") -> tuple[int, dict]:
        body = self.listing()
        now = int(self.clock())
        why = Q.request_error(req, body["seller"], body["chains"], body["max_hours"], self.testnets, now=now)
        if why:
            return 400, {"error": why}
        tag = self.source_tag(src)
        with self.lock:
            if now - self.last_prune >= PRUNE_EVERY_S:
                self.prune(now)
                self.last_prune = now
            self.db.execute("DELETE FROM requests WHERE ts < ?", (now - REPLAY_KEEP_S,))
            try:
                self.db.execute("INSERT INTO requests VALUES (?, ?)", (req["sig"], now))
            except sqlite3.IntegrityError:
                return 409, {"error": "this request was already used; ask for a new quote"}
            if self._count("SELECT COUNT(*) FROM quotes WHERE buyer_key = ? AND expires > ?",
                           req["buyer_key"], now) >= MAX_OPEN_PER_BUYER:
                return 429, {"error": "too many open quotes for this client; pay or wait for one to expire"}
            if self._count("SELECT COUNT(*) FROM quotes WHERE src = ? AND expires > ?", tag, now) >= MAX_OPEN_PER_SOURCE:
                return 429, {"error": "too many open quotes from this connection; wait for one to expire"}
            if self._count("SELECT COUNT(*) FROM quotes WHERE src = ? AND issued > ?",
                           tag, now - QUOTES_PER_SOURCE[1]) >= QUOTES_PER_SOURCE[0]:
                return 429, {"error": "too many quotes from this connection; try again later"}
            per_hour = QUOTES_GLOBAL[0] if self.quotes_per_hour is None else self.quotes_per_hour
            if self._count("SELECT COUNT(*) FROM quotes WHERE issued > ?", now - QUOTES_GLOBAL[1]) >= per_hour \
                    or self._count("SELECT COUNT(*) FROM quotes WHERE expires > ?", now) >= MAX_OPEN:
                return 503, {"error": "busy: too many quotes right now; try again later"}
            price = C.parse_usdc(body["price_per_hour_usdc"])
            base = price * int(req["hours"])
            taken = {r[0] for r in self.db.execute(
                "SELECT amount FROM quotes WHERE chainid = ? AND amount > ? AND amount <= ? AND expires >= ?",
                (req["chainid"], base, base + Q.extra_cap(base), now - RESERVE_S))}
            try:
                doc, pid = Q.make_quote(self.key, req, payout=body["payout"], price_per_hour=price, taken=taken,
                                        cap=self.cap, now=now, testnets=self.testnets, rng=self.rng)
            except Q.QuoteError as e:
                busy = str(e).startswith("busy")
                return (503 if busy else 403), {"error": str(e)}
            q = doc["quote"]
            _write_json(self.dir / "quotes" / f"{q['id']}.json", {"doc": doc, "pay_id": pid})
            self.db.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?)",
                            (q["id"], q["chainid"], q["amount"], q["issued"], q["expires"], q["buyer_key"], tag))
        if self.debug_sources:
            sys.stderr.write(f"claimd: quote {q['id']} source {tag}\n")
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
        if self.clock() - q["issued"] > KEEP_QUOTES_S:
            return 410, {"error": "this quote is too old to check; contact the seller"}
        if self._paid(qid):
            return 200, self.status(qid)[1]
        tx = notice["tx"]
        with self.lock:
            known = self.db.execute("SELECT 1 FROM notices WHERE qid = ? AND tx = ?", (qid, tx)).fetchone()
            if not known:
                if self._count("SELECT COUNT(*) FROM notices WHERE qid = ?", qid) >= MAX_TX_PER_QUOTE:
                    return 429, {"error": f"at most {MAX_TX_PER_QUOTE} transactions per quote"}
                if sum(1 for _ in (self.dir / "spool").glob("*.json")) >= MAX_SPOOL:
                    return 503, {"error": "busy"}
                _write_json(self.dir / "spool" / f"{qid}-{tx[2:18]}.json", notice)
                self.db.execute("INSERT INTO notices VALUES (?, ?)", (qid, tx))
        return 202, {"status": "received"}

    def status(self, qid: str) -> tuple[int, dict]:
        rec = self.load_quote(qid)
        if not rec:
            return 404, {"error": "unknown quote"}
        try:
            return 200, json.loads((self.dir / "status" / f"{qid}.json").read_text())
        except (OSError, ValueError):
            q = rec["doc"]["quote"]
            reported = self.db.execute("SELECT 1 FROM notices WHERE qid = ?", (qid,)).fetchone()
            st = "expired" if not reported and self.clock() > q["expires"] + PAID_GRACE_S else "waiting"
            return 200, {"status": st}


def make_handler(desk: Desk, limiter: Limiter):
    class H(BaseHTTPRequestHandler):
        server_version = "srnet-claim/2"
        sys_version = ""
        timeout = SOCKET_TIMEOUT_S

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
                    return self._send(*desk.quote(msg, src=self.client_address[0]))
                return self._send(*desk.paid(msg))
            except RuntimeError as e:
                sys.stderr.write(f"claimd: {e}\n")
                return self._send(503, {"error": "seller is not set up to take payments right now"})
    return H


class Server(ThreadingHTTPServer):
    """Threaded server that caps open connections, overall and per source address."""
    daemon_threads = True
    max_conn, max_per_source = MAX_CONN, MAX_CONN_PER_SOURCE

    def __init__(self, *a, **kw):
        self.active: dict[str, int] = {}
        self.active_lock = threading.Lock()
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        src = client_address[0]
        with self.active_lock:
            total = sum(self.active.values())
            if total >= self.max_conn or self.active.get(src, 0) >= self.max_per_source:
                self.shutdown_request(request)
                return
            self.active[src] = self.active.get(src, 0) + 1
        try:
            super().process_request(request, client_address)
        except Exception:
            self._done(src)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._done(client_address[0])

    def _done(self, src: str) -> None:
        with self.active_lock:
            n = self.active.get(src, 0) - 1
            if n > 0:
                self.active[src] = n
            else:
                self.active.pop(src, None)


def serve(host: str, port: int, desk: Desk, limiter: Limiter | None = None) -> ThreadingHTTPServer:
    if not bind_ok(host):
        raise SystemExit("refusing to bind a public address; use the router's tun address")
    cls = Server
    if ":" in host:
        class V6(Server):
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
    ap.add_argument("--quotes-per-hour", type=int, default=QUOTES_GLOBAL[0],
                    help=f"new quotes per hour overall (default {QUOTES_GLOBAL[0]}; above {SAFE_QUOTES_PER_HOUR} "
                         "a busy desk may refuse some cheap purchases for a while)")
    ap.add_argument("--testnets", action="store_true")
    ap.add_argument("--debug-sources", action="store_true",
                    help="log each quote's salted source tag (not the address), to tell buyers' sessions apart")
    a = ap.parse_args(argv)
    if a.quotes_per_hour < 1:
        raise SystemExit("--quotes-per-hour must be at least 1")
    if a.quotes_per_hour > SAFE_QUOTES_PER_HOUR:
        sys.stderr.write(f"claimd: --quotes-per-hour {a.quotes_per_hour} is over {SAFE_QUOTES_PER_HOUR}; under heavy "
                         "load some cheap purchases may get \"busy\" until amounts free up\n")
    desk = Desk(a.dir, a.quote_key, a.listing, cap=C.parse_usdc(a.cap_usdc), testnets=a.testnets,
                quotes_per_hour=a.quotes_per_hour,
                debug_sources=a.debug_sources or os.environ.get("SRNET_CLAIM_DEBUG_SOURCES") == "1")
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
