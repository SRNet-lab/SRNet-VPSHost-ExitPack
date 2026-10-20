"""`srnet-exits ui`: the Exits tab on its own small page, for use without the SRNet client.

Serves page.html, a small shell (page.js) and the same tab.js the SRNet app uses, on 127.0.0.1 only.
API calls go to /api/addon/exits/<call> like in the app and need the X-Core header (which plain
links, forms and other sites can't add), a loopback Host with this port, and no foreign Origin.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import ui

PREFIX = "/api/addon/exits/"
STATIC = {"/": ("page.html", "text/html; charset=utf-8"), "/page.js": ("page.js", "text/javascript; charset=utf-8"),
          "/page.css": ("page.css", "text/css; charset=utf-8"), "/tab.js": ("tab.js", "text/javascript; charset=utf-8"),
          "/tab.css": ("tab.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src blob:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def make_handler(be: ui.Backend, port_ref: list):
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
            return self._host_ok() and self.headers.get("X-Core") == "1"

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
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > ui.MAX_BODY:
                return self._json(413, {"ok": False, "err": "too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "err": "bad json"})
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


def serve(st, port: int = 0, backend: ui.Backend | None = None) -> ThreadingHTTPServer:
    port_ref = [port]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(backend or ui.Backend(st), port_ref))
    port_ref[0] = srv.server_address[1]
    return srv


def _answers(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def run(st, port: int = 48921, browser: bool = True) -> int:
    url = f"http://127.0.0.1:{port}/"
    if _answers(port):
        print(f"srnet-exits ui: {url} (already running)", flush=True)
        if browser:
            webbrowser.open(url)
        return 0
    try:
        srv = serve(st, port)
    except OSError as e:
        print(f"srnet-exits ui: can't listen on 127.0.0.1:{port} ({e}); try --port", file=sys.stderr)
        return 1
    print(f"srnet-exits ui: {url}\n(local only; Ctrl-C to stop)", flush=True)
    if browser:
        threading.Thread(target=lambda: (time.sleep(0.5), webbrowser.open(url)), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nsrnet-exits ui: stopped", file=sys.stderr)
        return 130
    finally:
        srv.server_close()
    return 0
