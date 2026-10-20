"""Tunnel DNS forwarder used while `srnet connect` is up (runs as you, no root).

systemd-resolved is pointed at this forwarder for every name (routing domain `~.` on the
tunnel link). It answers:

  * .sesh / .snode / .loki  -> Session Router's DNS (127.3.2.1:53), on this machine;
  * everything else         -> the exit's own resolver (the listing's "dns" address, port 53),
                               on a socket bound to the tunnel interface, so the query can only
                               travel inside the tunnel to the exit, which resolves it itself.

There is no other upstream: no public resolver, no fallback to your normal DNS. If the exit's
resolver does not answer (or the tunnel is gone) the query gets SERVFAIL: it fails closed.

  python -m srnet_exits.dnsproxy --listen 127.3.2.2:5053 --dev sr-tun0 --upstream 10.195.0.53 [--pidfile F]
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

LOCAL_SUFFIXES = ("sesh", "snode", "loki")
UDP_MAX = 1232
RESEND = 0.8  # seconds between UDP re-sends to the exit resolver


def qname(msg: bytes) -> str:
    i, parts = 12, []
    while i < len(msg) and msg[i]:
        n = msg[i]
        parts.append(msg[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += n + 1
    return ".".join(parts).lower()


def _question_end(msg: bytes) -> int:
    i = 12
    while i < len(msg) and msg[i]:
        i += msg[i] + 1
    return i + 5


def servfail(q: bytes) -> bytes:
    flags = struct.unpack(">H", q[2:4])[0]
    hdr = struct.pack(">HHHHHH", struct.unpack(">H", q[:2])[0], 0x8000 | (flags & 0x0100) | 0x0080 | 2, 1, 0, 0, 0)
    return hdr + q[12:_question_end(q)]


def truncated(q: bytes) -> bytes:
    flags = struct.unpack(">H", q[2:4])[0]
    hdr = struct.pack(">HHHHHH", struct.unpack(">H", q[:2])[0], 0x8000 | 0x0200 | (flags & 0x0100) | 0x0080, 1, 0, 0, 0)
    return hdr + q[12:_question_end(q)]


def is_truncated(r: bytes) -> bool:
    return len(r) >= 4 and bool(r[2] & 0x02)


def _hostport(s: str, default_port: int = 53) -> tuple[str, int]:
    h, sep, p = s.rpartition(":")
    return (h, int(p)) if sep and h and "." in h else (s, default_port)


def _recv_exact(s: socket.socket, n: int) -> bytes:
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            raise OSError("connection closed")
        b += c
    return b


class Forwarder:
    def __init__(self, upstream: str, dev: str = "sr-tun0", sesh: str = "127.3.2.1:53", timeout: float = 5.0):
        if not upstream:
            raise ValueError("no exit resolver address")
        self.upstream = _hostport(upstream)
        self.dev, self.timeout = dev, timeout
        self.sesh = _hostport(sesh)

    def _local(self, q: bytes) -> bytes:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(min(self.timeout, 4.0))  # answer before the asking resolver gives up
            s.sendto(q, self.sesh)
            r, _ = s.recvfrom(65535)
            return r

    def _bound(self, kind) -> socket.socket:
        s = socket.socket(socket.AF_INET, kind)
        if self.dev:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.dev.encode())
        s.settimeout(self.timeout)
        return s

    def _exit_udp(self, q: bytes) -> bytes:
        """UDP to the exit resolver, re-sent every RESEND seconds until the timeout: the onion path can
        drop single packets, and the exit answers a repeat from its cache at once."""
        deadline = time.monotonic() + self.timeout
        with self._bound(socket.SOCK_DGRAM) as s:
            s.connect(self.upstream)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise OSError("the exit resolver did not answer")
                s.send(q)
                resend = time.monotonic() + min(RESEND, left)
                while True:
                    s.settimeout(max(0.01, resend - time.monotonic()))
                    try:
                        r = s.recv(65535)
                    except socket.timeout:
                        break
                    if r[:2] == q[:2]:
                        return r

    def _exit_tcp(self, q: bytes) -> bytes:
        with self._bound(socket.SOCK_STREAM) as s:
            s.connect(self.upstream)
            s.sendall(struct.pack(">H", len(q)) + q)
            n = struct.unpack(">H", _recv_exact(s, 2))[0]
            return _recv_exact(s, n)

    def answer(self, q: bytes, tcp: bool = False) -> bytes:
        if len(q) < 17:
            raise ValueError("short query")
        name = qname(q)
        try:
            if name.rsplit(".", 1)[-1] in LOCAL_SUFFIXES:
                return self._local(q)
            if tcp:
                return self._exit_tcp(q)
            r = self._exit_udp(q)
            return self._exit_tcp(q) if is_truncated(r) else r
        except (OSError, ValueError, struct.error):
            return servfail(q)


def serve(listen: str, fwd: Forwarder, ready: threading.Event | None = None, stop: threading.Event | None = None):
    addr = _hostport(listen)
    pool = ThreadPoolExecutor(32)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind(addr)
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind(addr)
    tcp.listen(64)
    stop = stop or threading.Event()

    def udp_one(q, peer):
        r = fwd.answer(q)
        if len(r) > UDP_MAX:
            r = truncated(q)
        try:
            udp.sendto(r, peer)
        except OSError:
            pass

    def tcp_conn(c):
        with c:
            c.settimeout(15)
            try:
                while True:
                    h = c.recv(2)
                    if len(h) < 2:
                        return
                    q = _recv_exact(c, struct.unpack(">H", h)[0])
                    r = fwd.answer(q, tcp=True)
                    c.sendall(struct.pack(">H", len(r)) + r)
            except OSError:
                return

    def udp_loop():
        while not stop.is_set():
            try:
                q, peer = udp.recvfrom(65535)
            except OSError:
                return
            pool.submit(udp_one, q, peer)

    def tcp_loop():
        while not stop.is_set():
            try:
                c, _ = tcp.accept()
            except OSError:
                return
            pool.submit(tcp_conn, c)

    threading.Thread(target=udp_loop, daemon=True).start()
    threading.Thread(target=tcp_loop, daemon=True).start()
    if ready:
        ready.set()
    try:
        while not stop.is_set():
            if fwd.dev and not os.path.exists(f"/sys/class/net/{fwd.dev}"):
                print(f"{fwd.dev} is gone; stopping", file=sys.stderr, flush=True)
                break  # the tunnel went away; resolved drops that link's settings with it
            stop.wait(2)
    finally:
        udp.close()
        tcp.close()


def query(server: str, name: str, qtype: int = 1, timeout: float = 5) -> bytes:
    q = struct.pack(">HHHHHH", os.getpid() & 0xFFFF, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    q += struct.pack(">HH", qtype, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q, _hostport(server))
        r, _ = s.recvfrom(65535)
    return r


def rcode(r: bytes) -> int:
    return r[3] & 0x0F


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="srnet_exits.dnsproxy")
    ap.add_argument("--listen", default="127.3.2.2:5053")
    ap.add_argument("--dev", default="sr-tun0")
    ap.add_argument("--sesh", default="127.3.2.1:53")
    ap.add_argument("--upstream", required=True, help="the exit's resolver (listing 'dns'), reached through --dev")
    ap.add_argument("--pidfile")
    a = ap.parse_args(argv)
    if a.pidfile:
        with open(a.pidfile, "w") as f:
            f.write(str(os.getpid()))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        serve(a.listen, Forwarder(a.upstream, a.dev, a.sesh), stop=stop)
    finally:
        if a.pidfile:
            try:
                os.unlink(a.pidfile)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
