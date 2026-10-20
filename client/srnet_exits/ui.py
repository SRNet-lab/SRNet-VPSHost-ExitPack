"""Backend for the Exits tab in the SRNet app.

`srnet ui` starts this on 127.0.0.1 (random port, per-run token) and hands the URL to the SNApps
core, which serves the tab script from its own origin and forwards the tab's calls here through its
X-Core-gated /api/addon/exits/* bridge. Every request needs the token; API calls send it as
X-CSRF-Token with a Host of 127.0.0.1:<port>.

Watching for a payment runs on a thread, so the tab only polls; nothing here signs or sends a
transaction.
"""
from __future__ import annotations

import hmac
import io
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import chains as C  # noqa: E402
import quote as Q  # noqa: E402

from . import board as boardmod, cli, config, pay, warning
from .prog import P as _P

WEB = Path(__file__).resolve().parent / "web"
MAX_BODY = 8 * 1024
SLOW_CALLS = ("connect", "disconnect")


def _have_qr() -> bool:
    try:
        import qrcode  # noqa: F401
        return True
    except ImportError:
        return False


def qr_svg(data: str) -> bytes:
    import qrcode
    import qrcode.image.svg
    buf = io.BytesIO()
    qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2).save(buf)
    return buf.getvalue()


class Backend:
    def __init__(self, st, buyer_factory=None):
        self.st = st
        self.lock = threading.Lock()
        self.watches: dict[str, dict] = {}
        self.buyer_factory = buyer_factory or (lambda: pay.Buyer(self.st))

    def cfg(self) -> dict:
        return config.load(self.st.root)

    def _reload(self):
        from .state import State
        self.st.data = State(self.st.root).data  # state.json is replaced atomically, never half-written

    # ------------------------------------------------------------ board / status
    def board(self) -> dict:
        cfg = self.cfg()
        good, info = boardmod.fetch(boardmod.sources(cfg), boardmod.pin_of(cfg), self.st.root, dns=config.sesh_dns(cfg))
        rows = boardmod.rows(good, time.time(), config.testnets(cfg))
        return {"listings": rows, "seq": info["seq"], "source": info["source"], "dropped": len(info["dropped"]),
                "needs_update": sum(1 for r in rows if not r["buyable"])}

    def status(self) -> dict:
        with self.lock:
            self._reload()
            cfg = self.cfg()
            info = cli.status_info(self.st, cfg, rpc_probe=False)
        info["client_sesh"] = cfg.get("client_sesh") or cli.own_sesh()
        info["max_payment_usdc"] = C.fmt_usdc(config.max_payment(cfg))
        info["acked"] = warning.acked(self.st.root)
        return info

    def settings(self, req: dict) -> dict:
        cfg = self.cfg()
        ex = dict(config.exits(cfg))
        if "max_payment_usdc" in req:
            try:
                ex["max_payment_usdc"] = C.fmt_usdc(C.parse_usdc(str(req["max_payment_usdc"])))
            except C.ChainError as e:
                return {"ok": False, "err": f"limit: {e}"}
        if "chain" in req:
            if req["chain"]:
                try:
                    ex["chain"] = C.info(C.by_name(str(req["chain"]), config.testnets(cfg)),
                                         config.testnets(cfg))["name"]
                except C.ChainError as e:
                    return {"ok": False, "err": str(e)}
            else:
                ex.pop("chain", None)
        cfg["exits"] = ex
        config.save(self.st.root, cfg)
        return {"ok": True, "max_payment_usdc": ex.get("max_payment_usdc", config.DEFAULT_MAX_PAYMENT),
                "chain": ex.get("chain", "")}

    # ------------------------------------------------------------ buying
    def quote(self, req: dict) -> dict:
        seller, hours = req.get("seller"), req.get("hours", 1)
        if not (isinstance(seller, str) and Q.SESH_RE.match(seller)):
            return {"ok": False, "err": "pick a seller"}
        if not isinstance(hours, int) or hours < 1:
            return {"ok": False, "err": "hours must be a whole number, 1 or more"}
        cs = req.get("client_sesh") or self.cfg().get("client_sesh") or cli.own_sesh()
        if not (isinstance(cs, str) and Q.SESH_RE.match(cs)):
            return {"ok": False, "err": "could not read your .sesh address from Session Router (is it running?)"}
        if not warning.acked(self.st.root):
            return {"ok": False, "need_ack": True, "warning": warning.WARNING}
        cfg = self.cfg()
        good, _ = boardmod.fetch(boardmod.sources(cfg), boardmod.pin_of(cfg), self.st.root, dns=config.sesh_dns(cfg))
        from .listings import find_seller
        doc = find_seller(good, seller)
        if not doc:
            return {"ok": False, "err": "that seller is no longer on the board"}
        with self.lock:
            b = self.buyer_factory()
            try:
                rec = b.quote(doc, hours, cs, chain=req.get("chain") or None, max_usdc=req.get("max_usdc") or None)
            except pay.BuyError as e:
                return {"ok": False, "err": str(e)}
            out = b.info(rec)
        out["qr"] = _have_qr()
        self._start_watch(rec["id"])
        return out

    def _start_watch(self, qid: str, tx: str | None = None) -> None:
        w = self.watches.get(qid)
        if w and w["thread"].is_alive() and not tx:
            return
        w = {"state": "watching", "message": "", "near": [], "stop": threading.Event()}

        def ev(kind, data):
            if kind == "waiting":
                w["near"] = [C.fmt_usdc(v) for v in data.get("near") or []]
                w["left"] = data.get("left")
            elif kind == "seen":
                w["state"], w["tx"] = "seen", data["tx"]
            elif kind == "reported":
                w["state"] = "confirming"
            elif kind in ("rpc_error", "desk_error"):
                w["message"] = str(data)[:300]

        def run():
            try:
                b = self.buyer_factory()
                with self.lock:
                    self._reload()
                    rec = b.find(qid)
                out = b.watch(rec, ev, tx=tx, poll_s=5)
                w["state"] = out.get("status")
                w["message"] = out.get("message") or ""
                if out.get("status") == "admitted":
                    from . import reminder
                    try:
                        reminder.start(self.st.root, rec["seller"])
                    except OSError:
                        pass
            except pay.BuyError as e:
                w["state"], w["message"] = "error", str(e)
        w["thread"] = threading.Thread(target=run, daemon=True)
        self.watches[qid] = w
        w["thread"].start()

    def watch(self, req: dict) -> dict:
        qid = req.get("id")
        if not (isinstance(qid, str) and Q.ID_RE.match(qid)):
            return {"ok": False, "err": "bad quote id"}
        w = self.watches.get(qid)
        if not w:
            with self.lock:
                self._reload()
                rec = (self.st.data.get("exits") or {}).get("quotes", {}).get(qid)
            if not rec:
                return {"ok": False, "err": "unknown quote"}
            if rec["state"] in ("open", "reported"):
                self._start_watch(qid)
                return {"id": qid, "state": "watching"}
            return {"id": qid, "state": rec["state"], "message": rec.get("reason", "")}
        out = {k: w.get(k) for k in ("state", "message", "near", "tx", "left")}
        if w.get("state") == "admitted":
            with self.lock:
                self._reload()
                rec = self.st.data["exits"]["quotes"].get(qid) or {}
                lease = pay.active_lease(self.st, rec.get("seller", "")) or {}
            out["lease_end"] = lease.get("end")
        out["id"] = qid
        return out

    def paid(self, req: dict) -> dict:
        qid, tx = req.get("id"), str(req.get("tx") or "").strip().lower()
        if not (isinstance(qid, str) and Q.ID_RE.match(qid)) or not Q.TX_RE.match(tx):
            return {"ok": False, "err": "paste the transaction hash (0x followed by 64 hex characters)"}
        with self.lock:
            self._reload()
            rec = (self.st.data.get("exits") or {}).get("quotes", {}).get(qid)
            if not rec:
                return {"ok": False, "err": "unknown quote"}
            if rec.get("tx") != tx:
                def reopen(d):
                    r = ((d.get("exits") or {}).get("quotes") or {}).get(qid)
                    if r:
                        r["state"] = "open"
                from . import state as S
                S.update(self.st, reopen)
        self._start_watch(qid, tx=tx)
        return {"id": qid, "state": "confirming"}

    def qr(self, qid: str) -> bytes:
        rec = (self.st.data.get("exits") or {}).get("quotes", {}).get(qid)
        if not rec:
            self._reload()
            rec = (self.st.data.get("exits") or {}).get("quotes", {}).get(qid)
        if not rec:
            raise ValueError("unknown quote")
        return qr_svg(pay.Buyer(self.st, cfg=self.cfg()).info(rec)["uri"])

    # ------------------------------------------------------------ connect
    def _run(self, args: list[str], timeout: float = 600) -> dict:
        cmd = [sys.executable, "-m", "srnet_exits.cli", "--home", str(self.st.root), *args]
        env = dict(os.environ, SRNET_EXITS_PROG=_P())  # hints name the command this page belongs to
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           cwd=str(Path(__file__).resolve().parents[1]), env=env)
        return {"ok": p.returncode == 0, "out": p.stdout[-2000:], "err": p.stderr[-2000:]}

    def verify(self, req: dict) -> dict:
        """Shop owners: check that a tx paid exactly `amount` USDC to `to` (read-only)."""
        from . import shopcheck, watch
        cfg = self.cfg()
        t = config.testnets(cfg)
        try:
            cid = int(req.get("chain_id"))
            amount = C.parse_usdc(str(req.get("amount")))
        except (TypeError, ValueError, C.ChainError):
            return {"ok": False, "err": "need chain_id, tx, to and amount"}
        if not C.supported(cid, t):
            return {"ok": False, "err": "unsupported chain"}
        r = shopcheck.check(watch.rpc_for(cid, config.rpc_overrides(cfg), t), cid, req.get("tx", ""),
                            req.get("to", ""), amount, t)
        return dict(r, ok=True)

    def ack(self, req: dict) -> dict:
        if req.get("text") != "I UNDERSTAND":
            return {"ok": False, "need_ack": True, "warning": warning.WARNING}
        warning.write_ack(self.st.root)
        return {"ok": True}

    def connect(self, req: dict) -> dict:
        if not warning.acked(self.st.root):
            return {"ok": False, "need_ack": True, "warning": warning.WARNING}
        seller = req.get("seller")
        if not (isinstance(seller, str) and Q.SESH_RE.match(seller)):
            return {"ok": False, "err": "pick a seller"}
        return self._run(["connect", seller])

    def disconnect(self, req: dict) -> dict:
        return self._run(["disconnect"])


CALLS = {"board": lambda be, r: be.board(), "status": lambda be, r: be.status(),
         "quote": Backend.quote, "watch": Backend.watch, "paid": Backend.paid,
         "settings": Backend.settings, "ack": Backend.ack, "verify": Backend.verify, "connect": Backend.connect, "disconnect": Backend.disconnect}


def make_handler(be: Backend, token: str, port_ref: list):
    class H(BaseHTTPRequestHandler):
        server_version = "srnet-exits-ui"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode())

        def _host_ok(self) -> bool:
            if self.headers.get("Host") != f"127.0.0.1:{port_ref[0]}":
                return False
            return self.headers.get("Origin") is None

        def _tok(self, given) -> bool:
            return isinstance(given, str) and hmac.compare_digest(given, token)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._host_ok() or not self._tok((q.get("k") or [None])[0]):
                return self._send(403, b"forbidden\n", "text/plain")
            if u.path in ("/tab.js", "/tab.css"):
                ctype = "text/javascript; charset=utf-8" if u.path.endswith(".js") else "text/css; charset=utf-8"
                return self._send(200, (WEB / u.path[1:]).read_bytes(), ctype)
            if u.path == "/qr.svg":
                try:
                    return self._send(200, be.qr((q.get("q") or [""])[0]), "image/svg+xml")
                except Exception as e:
                    return self._send(404, str(e).encode()[:200], "text/plain")
            return self._send(404, b"not found\n", "text/plain")

        def do_POST(self):
            if not self._host_ok() or not self._tok(self.headers.get("X-CSRF-Token")):
                return self._json(403, {"ok": False, "err": "forbidden"})
            if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
                return self._json(415, {"ok": False, "err": "json only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"ok": False, "err": "bad Content-Length"})
            if n < 0 or n > MAX_BODY:
                return self._json(413, {"ok": False, "err": "too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "err": "bad json"})
            path = self.path[len("/api/"):] if self.path.startswith("/api/") else None
            fn = CALLS.get(path)
            if not fn:
                return self._json(404, {"ok": False, "err": "not found"})
            try:
                r = fn(be, req)
            except Exception as e:  # report, keep serving
                return self._json(200, {"ok": False, "err": str(e)[:500]})
            r.setdefault("ok", True)
            return self._json(200, r)
    return H


def serve(be: Backend, port: int = 0) -> tuple[ThreadingHTTPServer, str, str]:
    token = secrets.token_urlsafe(24)
    port_ref = [0]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(be, token, port_ref))
    port_ref[0] = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port_ref[0]}", token


def start_background(st) -> str:
    """Serve on a thread; returns http://127.0.0.1:<port>/#<token> for the core's add-on bridge."""
    srv, base, tok = serve(Backend(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"{base}/#{tok}"
