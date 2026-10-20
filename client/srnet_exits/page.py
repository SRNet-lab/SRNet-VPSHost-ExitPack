"""`srnet-exits ui`: the Exits tab on its own small page, for use without the SRNet client.

Serves page.html, a small shell (page.js) and the same tab.js the SRNet app uses, on 127.0.0.1 only.
API calls go to /api/addon/exits/<call> like in the app. Each one needs the X-Core header (which plain
links, forms and other sites can't add), a loopback Host with this port, no foreign Origin, and the
X-Token of this run.

Each run makes a new token, writes {"port", "token", "pid"} to ~/.srnet/ui.json (mode 0600) and opens
http://127.0.0.1:PORT/#t=TOKEN. page.js moves the token from the address into sessionStorage. A second
`srnet-exits ui` reuses the running page only if ui.json is this user's and the page there answers
`hello` with that token (and proves it knows it); otherwise it starts a new page on a free port.
"""
from __future__ import annotations

import hmac
import http.client
import json
import os
import secrets
import signal
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import state as S
from . import ui

PREFIX = "/api/addon/exits/"
STATIC = {"/": ("page.html", "text/html; charset=utf-8"), "/page.js": ("page.js", "text/javascript; charset=utf-8"),
          "/page.css": ("page.css", "text/css; charset=utf-8"), "/tab.js": ("tab.js", "text/javascript; charset=utf-8"),
          "/tab.css": ("tab.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src blob:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def make_handler(be: ui.Backend, port_ref: list, token: str):
    class H(BaseHTTPRequestHandler):
        server_version = "srnet-exits-page"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            ok = {f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}"}
            if self.headers.get("Host") not in ok:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {"http://" + h for h in ok}

        def _api_ok(self) -> bool:
            given = self.headers.get("X-Token") or ""
            return (self._host_ok() and self.headers.get("X-Core") == "1"
                    and hmac.compare_digest(given.encode("utf-8", "replace"), token.encode()))

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if not self._host_ok():
                return self._send(403, b"forbidden\n", "text/plain")
            if u.path in STATIC:
                name, ctype = STATIC[u.path]
                return self._send(200, (ui.WEB / name).read_bytes(), ctype)
            if u.path == PREFIX + "qr.svg" and self._api_ok():
                q = urllib.parse.parse_qs(u.query)
                try:
                    return self._send(200, be.qr((q.get("q") or [""])[0]), "image/svg+xml")
                except Exception as e:  # noqa: BLE001
                    return self._send(404, str(e).encode()[:200], "text/plain")
            return self._send(404, b"not found\n", "text/plain")

        def do_POST(self):
            if not self._api_ok():
                return self._json(403, {"ok": False, "err": "forbidden"})
            if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
                return self._json(415, {"ok": False, "err": "json only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"ok": False, "err": "bad Content-Length"})
            if n < 0 or n > ui.MAX_BODY:
                return self._json(413, {"ok": False, "err": "too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "err": "bad json"})
            if self.path == PREFIX + "hello":
                return self._json(200, {"ok": True, "app": "srnet-exits", "pid": os.getpid(),
                                        "proof": _proof(token, str(req.get("nonce", "")))})
            fn = ui.CALLS.get(self.path[len(PREFIX):]) if self.path.startswith(PREFIX) else None
            if not fn:
                return self._json(404, {"ok": False, "err": "not found"})
            try:
                r = fn(be, req)
            except Exception as e:  # noqa: BLE001  (report, keep serving)
                return self._json(200, {"ok": False, "err": str(e)[:500]})
            r.setdefault("ok", True)
            return self._json(200, r)
    return H


def serve(st, port: int = 0, backend: ui.Backend | None = None, token: str | None = None) -> ThreadingHTTPServer:
    """The page on 127.0.0.1:port (0 = a free one). srv.token is the token its API calls need."""
    port_ref = [port]
    token = token or secrets.token_urlsafe(32)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(backend or ui.Backend(st), port_ref, token))
    port_ref[0] = srv.server_address[1]
    srv.token = token
    return srv


def _info_path(st):
    return st.root / "ui.json"


def _url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/#t={token}"


def _proof(token: str, nonce: str) -> str:
    return hmac.new(token.encode(), b"srnet-exits hello " + nonce.encode("utf-8", "replace"), "sha256").hexdigest()


def _hello(port: int, token: str) -> bool:
    """True only if the page on port knows token: it must send back an HMAC of a fresh nonce, so
    another program that took over the port after our page stopped can't pass as it."""
    nonce = secrets.token_hex(16)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("POST", PREFIX + "hello", body=json.dumps({"nonce": nonce}).encode(),
                  headers={"Host": f"127.0.0.1:{port}", "X-Core": "1", "X-Token": token,
                           "Content-Type": "application/json"})
        r = c.getresponse()
        j = json.loads(r.read(4096) or b"{}") if r.status == 200 else {}
        ok = isinstance(j, dict) and hmac.compare_digest(str(j.get("proof", "")).encode("utf-8", "replace"),
                                                       _proof(token, nonce).encode())
        c.close()
        return ok
    except (OSError, ValueError, http.client.HTTPException):
        return False


def running(st):
    """(port, token) of a page this user already runs, checked with a hello; else None."""
    p = _info_path(st)
    try:
        if p.is_symlink() or p.stat().st_uid != os.getuid():
            return None
        info = json.loads(p.read_text())
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not 0 < port < 65536 or not token:
        return None
    return (port, token) if _hello(port, token) else None


def _write_info(st, port: int, token: str) -> None:
    S.write(_info_path(st), {"port": port, "token": token, "pid": os.getpid()})


def _drop_info(st, token: str) -> None:
    p = _info_path(st)
    try:
        if json.loads(p.read_text()).get("token") == token:
            p.unlink()
    except (OSError, ValueError):
        pass


def run(st, port: int = 0, browser: bool = True) -> int:
    """Port 0 (the default) means a free port picked at start."""
    have = running(st)
    if have:
        url = _url(*have)
        print(f"srnet-exits ui: {url} (already running)", flush=True)
        if browser:
            webbrowser.open(url)
        return 0
    try:
        srv = serve(st, port)
    except OSError as e:
        print(f"srnet-exits ui: can't listen on 127.0.0.1:{port} ({e}); try another --port or leave it out",
              file=sys.stderr)
        return 1
    port = srv.server_address[1]
    _write_info(st, port, srv.token)
    url = _url(port, srv.token)
    print(f"srnet-exits ui: {url}\n(local only; this address works only for this run; Ctrl-C to stop)", flush=True)
    if browser:
        threading.Thread(target=lambda: (time.sleep(0.5), webbrowser.open(url)), daemon=True).start()
    if threading.current_thread() is threading.main_thread():
        def _term(*_):
            raise SystemExit(143)   # so the finally below removes ui.json (uninstall and kill send TERM)
        signal.signal(signal.SIGTERM, _term)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nsrnet-exits ui: stopped", file=sys.stderr)
        return 130
    finally:
        srv.server_close()
        _drop_info(st, srv.token)
    return 0
