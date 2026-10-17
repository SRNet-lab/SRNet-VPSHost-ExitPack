#!/usr/bin/env python3
"""Board server. Serves the signed board and takes seller submissions.

  GET  /board.json, /board.sig     the current signed board
  POST /submit                     a seller's signed listing (<= 16 KB); checked, then queued
                                   in --inbox for `board.py tick`
  POST /publish                    {"body": "...", "sig": "..."}: a newer board signed by the
                                   --pin key replaces the served one (mirrors)

  board_server.py --www WWW [--inbox STATE/inbox] [--pin BOARD.sesh] [--host ADDR] [--port 8787]

Nothing is signed here. /publish needs no secret: only a valid board signature with a higher
seq is accepted. --host must be loopback or a private address (the Session Router tun address);
a public address is refused.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boardfmt as B  # noqa: E402
from boardfmt import L  # noqa: E402

MAX_SUBMIT = 16 * 1024
FILES = {"/board.json": "application/json", "/board.sig": "text/plain"}
_publish_lock = threading.Lock()


def bind_ok(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or (ip.is_private and not ip.is_unspecified)


def install_board(www: Path, body_b: bytes, sig: str, pin: str) -> int:
    """Verify and install a published board. Returns its seq. Raises BoardError."""
    with _publish_lock:
        seen = None
        cur = www / "board.json"
        if cur.exists():
            try:
                old = json.loads(cur.read_bytes())
                seen = {"seq": int(old.get("seq", 0)), "hash": B.body_hash(old)}
            except ValueError:
                seen = None
        body, _, _ = B.verify_board(body_b, sig, pin, seen=seen)
        if seen and body["seq"] == seen["seq"]:
            return body["seq"]
        www.mkdir(parents=True, exist_ok=True)
        (www / "board.json.tmp").write_bytes(body_b)
        (www / "board.sig.tmp").write_text(sig.strip() + "\n")
        os.replace(www / "board.json.tmp", www / "board.json")
        os.replace(www / "board.sig.tmp", www / "board.sig")
        return body["seq"]


def make_handler(www: Path, inbox: Path | None, pin: str | None):
    class H(BaseHTTPRequestHandler):
        server_version = "srnet-board/1"
        sys_version = ""

        def log_message(self, fmt, *args):  # no client addresses
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def _send(self, code: int, body: bytes, ctype: str = "text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def address_string(self):
            return "-"

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path not in FILES:
                return self._send(404, b"not found\n")
            f = www / path[1:]
            if not f.exists():
                return self._send(503, b"board not signed yet\n")
            return self._send(200, f.read_bytes(), FILES[path])

        do_HEAD = do_GET

        def _body(self, limit: int) -> bytes | None:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > limit:
                self._send(413, b"too large\n")
                return None
            return self.rfile.read(n)

        def do_POST(self):
            if self.path == "/submit" and inbox is not None:
                raw = self._body(MAX_SUBMIT)
                if raw is None:
                    return
                try:
                    doc = json.loads(raw)
                except ValueError:
                    return self._send(400, b"bad json\n")
                if not isinstance(doc, dict) or not L.verify(doc):
                    return self._send(400, b"bad listing signature\n")
                body = doc["listing"]
                name = f"{body['seller'][:52]}-{int(body.get('issued', 0))}.json"
                (inbox / name).write_text(json.dumps(doc))
                return self._send(202, b"queued\n")
            if self.path == "/publish" and pin:
                raw = self._body(B.MAX_BOARD_BYTES * 2 + 1024)
                if raw is None:
                    return
                try:
                    msg = json.loads(raw)
                    seq = install_board(www, msg["body"].encode(), msg["sig"], pin)
                except (ValueError, KeyError, TypeError, AttributeError) as e:
                    return self._send(400, f"bad publish: {e}\n".encode())
                except B.BoardError as e:
                    return self._send(409, f"refused: {e}\n".encode())
                return self._send(200, f"seq {seq}\n".encode())
            return self._send(404, b"not found\n")
    return H


def serve(www: Path, inbox: Path | None, host: str, port: int, pin: str | None = None) -> ThreadingHTTPServer:
    if not bind_ok(host):
        raise SystemExit("refusing to bind a public address; use 127.0.0.1 or the Session Router tun address")
    if inbox:
        inbox.mkdir(parents=True, exist_ok=True)
    fam = ThreadingHTTPServer
    if ":" in host:
        import socket

        class V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        fam = V6
    return fam((host, port), make_handler(www, inbox, pin))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--www", required=True)
    ap.add_argument("--inbox", help="accept POST /submit into this directory (primary only)")
    ap.add_argument("--pin", help="board .sesh; accept POST /publish of boards signed by it (mirrors)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args(argv)
    srv = serve(Path(a.www), Path(a.inbox) if a.inbox else None, a.host, a.port, a.pin)
    print(f"board on http://{a.host}:{srv.server_address[1]}/board.json", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
