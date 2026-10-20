import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

import helpers  # noqa: E402,F401
from srnet_exits import dnsproxy as P  # noqa: E402


def q(name, qtype=1, qid=0x1234):
    return (struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
            + b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", qtype, 1))


class Routing(unittest.TestCase):
    def setUp(self):
        self.f = P.Forwarder("10.195.0.53", dev="")
        self.local, self.udp, self.tcp = [], [], []
        self.f._local = lambda m: self.local.append(P.qname(m)) or b"L" + m[1:]
        self.f._exit_udp = lambda m: self.udp.append(P.qname(m)) or m[:2] + b"\x81\x80" + m[4:]
        self.f._exit_tcp = lambda m: self.tcp.append(P.qname(m)) or m[:2] + b"\x81\x80" + m[4:]

    def test_sesh_stays_local_everything_else_to_the_exit_resolver(self):
        self.f.answer(q("xyz.sesh"))
        self.f.answer(q("a.b.snode"))
        self.f.answer(q("Example.COM"))
        self.assertEqual(self.local, ["xyz.sesh", "a.b.snode"])
        self.assertEqual(self.udp, ["example.com"])
        self.assertEqual(self.f.upstream, ("10.195.0.53", 53))

    def test_no_public_resolver_anywhere(self):
        import inspect
        src = inspect.getsource(P)
        for third in ("1.1.1.1", "9.9.9.9", "8.8.8.8", "dns-query", "https"):
            self.assertNotIn(third, src)

    def test_exit_resolver_down_is_servfail_no_fallback(self):
        def dead(m):
            raise OSError("no route")
        self.f._exit_udp = dead
        self.f._exit_tcp = dead
        r = self.f.answer(q("example.com"))
        self.assertEqual(P.rcode(r), 2)
        self.assertEqual(r[:2], b"\x12\x34")
        self.assertTrue(r[2] & 0x80)
        self.assertEqual(self.local, [])

    def test_truncated_answer_retries_over_tcp(self):
        self.f._exit_udp = lambda m: m[:2] + b"\x83\x80" + m[4:]
        self.f.answer(q("big.example"))
        self.assertEqual(self.tcp, ["big.example"])

    def test_tcp_query_goes_tcp(self):
        self.f.answer(q("example.org"), tcp=True)
        self.assertEqual((self.udp, self.tcp), ([], ["example.org"]))

    def test_needs_an_upstream(self):
        with self.assertRaises(ValueError):
            P.Forwarder("", dev="")

    def test_truncated_keeps_question(self):
        m = q("example.com")
        t = P.truncated(m)
        self.assertTrue(t[2] & 0x02)
        self.assertEqual(t[12:], m[12:])


class RealUpstream(unittest.TestCase):
    """The exit side over real sockets: a fake resolver on 127.0.0.1, UDP and TCP."""

    def test_udp_then_tcp_to_upstream(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("127.0.0.1", 0)); port = u.getsockname()[1]
        t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); t.bind(("127.0.0.1", port)); t.listen(1)
        def udp_srv():
            m, peer = u.recvfrom(512)
            u.sendto(m[:2] + b"\x83\x80" + m[4:], peer)  # truncated: client must retry over TCP
        def tcp_srv():
            c, _ = t.accept()
            n = struct.unpack(">H", c.recv(2))[0]; m = c.recv(n)
            r = m[:2] + b"\x81\x80" + m[4:]
            c.sendall(struct.pack(">H", len(r)) + r); c.close()
        threading.Thread(target=udp_srv, daemon=True).start()
        threading.Thread(target=tcp_srv, daemon=True).start()
        f = P.Forwarder(f"127.0.0.1:{port}", dev="")
        r = f.answer(q("example.com"))
        self.assertEqual(P.rcode(r), 0)
        self.assertFalse(r[2] & 0x02)
        u.close(); t.close()


class Resend(unittest.TestCase):
    def test_udp_query_is_resent_until_answered(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("127.0.0.1", 0)); port = u.getsockname()[1]
        seen = []
        def srv():
            while len(seen) < 2:
                m, peer = u.recvfrom(512)
                seen.append(m)
                if len(seen) == 2:  # the first one is "lost"
                    u.sendto(m[:2] + b"\x81\x80" + m[4:], peer)
        threading.Thread(target=srv, daemon=True).start()
        f = P.Forwarder(f"127.0.0.1:{port}", dev="", timeout=3)
        old, P.RESEND = P.RESEND, 0.2
        try:
            r = f.answer(q("example.com"))
        finally:
            P.RESEND = old
        self.assertEqual(P.rcode(r), 0)
        self.assertEqual(len(seen), 2)
        u.close()

    def test_gives_up_at_the_timeout_with_servfail(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(("127.0.0.1", 0)); port = u.getsockname()[1]
        f = P.Forwarder(f"127.0.0.1:{port}", dev="", timeout=0.5)
        t0 = time.monotonic()
        r = f.answer(q("example.com"))
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(P.rcode(r), 2)
        u.close()


class Serve(unittest.TestCase):
    def test_udp_and_tcp(self):
        f = P.Forwarder("10.195.0.53", dev="")
        f.answer = lambda m, tcp=False: m[:2] + b"\x81\x80" + m[4:]
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        stop, ready = threading.Event(), threading.Event()
        th = threading.Thread(target=P.serve, args=(f"127.0.0.1:{port}", f, ready, stop), daemon=True)
        th.start(); ready.wait(5)
        r = P.query(f"127.0.0.1:{port}", "example.com")
        self.assertEqual(P.rcode(r), 0)
        c = socket.create_connection(("127.0.0.1", port), 5)
        m = q("example.org")
        c.sendall(struct.pack(">H", len(m)) + m)
        n = struct.unpack(">H", c.recv(2))[0]
        self.assertEqual(len(c.recv(n)), len(m))
        c.close()
        stop.set(); th.join(5)


if __name__ == "__main__":
    unittest.main()
