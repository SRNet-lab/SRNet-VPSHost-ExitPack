"""Resolve .sesh names through a local Session Router DNS listener and open HTTP to them.

  open_url(url, data=None, timeout=30, dns="127.0.0.1:5353")

For http://<name>.sesh/... the name is looked up (AAAA, then A) at `dns` and the request goes to
that address with the original Host header. Other URLs use the system resolver.
"""
from __future__ import annotations

import http.client
import os
import random
import socket
import struct
import time
import urllib.parse
import urllib.request

DEFAULT_DNS = os.environ.get("SRNET_SESH_DNS", "127.0.0.1:5353")


def _query(name: str, server: str, timeout: float, qtype: int = 28) -> str:
    host, _, port = server.rpartition(":")
    qid = random.randrange(65536)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    q += struct.pack(">HH", qtype, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q, (host, int(port)))
        r, _ = s.recvfrom(4096)
    rid, flags, qd, an = struct.unpack(">HHHH", r[:8])
    if rid != qid or flags & 0x000F or an == 0:
        raise OSError(f"no address for {name} from {server}")
    i = 12
    for _ in range(qd):
        while r[i]:
            i += r[i] + 1
        i += 5
    for _ in range(an):
        if r[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while r[i]:
                i += r[i] + 1
            i += 1
        typ, _cls, _ttl, ln = struct.unpack(">HHIH", r[i:i + 10])
        i += 10
        if typ == qtype == 1 and ln == 4:
            return socket.inet_ntoa(r[i:i + 4])
        if typ == qtype == 28 and ln == 16:
            return socket.inet_ntop(socket.AF_INET6, r[i:i + 16])
        i += ln
    raise OSError(f"no address for {name} from {server}")


def resolve(name: str, dns: str | None = None, timeout: float = 10, tries: int = 6,
            pause: float = 3) -> str:
    """Session Router answers AAAA for .sesh names; A is tried as a fallback. The first lookup of
    a name can come back empty while the router finds it, so empty answers are retried."""
    err = None
    for n in range(tries):
        if n:
            time.sleep(pause)
        for qtype in (28, 1):
            try:
                return _query(name, dns or DEFAULT_DNS, timeout, qtype)
            except OSError as e:
                err = e
    raise OSError(str(err))


def open_url(url: str, data: bytes | None = None, timeout: float = 30, dns: str | None = None,
             limit: int = 1 << 20, headers: dict | None = None) -> tuple[int, bytes]:
    """Returns (status, body). Raises OSError on network errors and for HTTP status >= 400."""
    u = urllib.parse.urlparse(url)
    if u.scheme == "http" and (u.hostname or "").endswith(".sesh"):
        ip = resolve(u.hostname, dns)
        c = http.client.HTTPConnection(ip, u.port or 80, timeout=timeout)
        try:
            h = {"Host": u.hostname, **(headers or {})}
            if data is not None:
                h.setdefault("Content-Type", "application/json")
            c.request("POST" if data is not None else "GET", (u.path or "/") + (f"?{u.query}" if u.query else ""),
                      body=data, headers=h)
            r = c.getresponse()
            body = r.read(limit + 1)
        finally:
            c.close()
        if r.status >= 400:
            raise OSError(f"HTTP {r.status}: {body[:200].decode(errors='replace').strip()}")
        return r.status, body
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers=headers or ({"Content-Type": "application/json"} if data is not None else {}))
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.status, r.read(limit + 1)
