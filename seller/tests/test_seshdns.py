import json
import socket
import struct
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seshdns  # noqa: E402

NAME = "exitsrjnc83har7a17mdtjpgi1j3odgiphaffe9ejfq4wnbdxkio.sesh"


def fake_dns(answer_ip: str | None):
    v6 = answer_ip is not None and ":" in answer_ip
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    seen = []

    def loop():
        while True:
            try:
                q, addr = s.recvfrom(512)
            except OSError:
                return
            qid = q[:2]
            qname_end = q.index(b"\0", 12) + 5
            seen.append(q[12:qname_end - 5])
            qtype = struct.unpack(">H", q[qname_end - 4:qname_end - 2])[0]
            if answer_ip is None or (qtype == 28) != v6:
                r = qid + struct.pack(">HHHHH", 0x8183, 1, 0, 0, 0) + q[12:qname_end]
            else:
                r = (qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + q[12:qname_end]
                     + b"\xc0\x0c" + struct.pack(">HHIH", 28 if v6 else 1, 1, 60, 16 if v6 else 4)
                     + (socket.inet_pton(socket.AF_INET6, answer_ip) if v6 else socket.inet_aton(answer_ip)))
            s.sendto(r, addr)
    threading.Thread(target=loop, daemon=True).start()
    return s, f"127.0.0.1:{s.getsockname()[1]}", seen


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = self.rfile.read(n)
        self.server.got.append((self.headers["Host"], json.loads(body)))
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"queued\n")


class SeshDnsTest(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.got = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_resolve_and_post(self):
        s, dns, seen = fake_dns("127.0.0.1")
        try:
            st, body = seshdns.open_url(f"http://{NAME}:{self.port}/submit", data=b'{"a":1}', dns=dns)
        finally:
            s.close()
        self.assertEqual(st, 202)
        self.assertEqual(body, b"queued\n")
        self.assertEqual(self.srv.got[0], (NAME, {"a": 1}))
        self.assertIn(b"sesh", seen[0])

    def test_aaaa(self):
        s, dns, _ = fake_dns("fd2e:7365:7368:210::1")
        try:
            self.assertEqual(seshdns.resolve(NAME, dns, timeout=1, tries=2, pause=0), "fd2e:7365:7368:210::1")
        finally:
            s.close()

    def test_nxdomain(self):
        s, dns, _ = fake_dns(None)
        try:
            with self.assertRaises(OSError):
                seshdns.resolve(NAME, dns, timeout=1, tries=2, pause=0)
        finally:
            s.close()

    def test_no_listener(self):
        with self.assertRaises(OSError):
            seshdns.resolve(NAME, "127.0.0.1:9", timeout=0.5, tries=1)


if __name__ == "__main__":
    unittest.main()
