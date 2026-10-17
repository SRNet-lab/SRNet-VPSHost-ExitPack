#!/usr/bin/env python3
"""Checks for the exit resolver (used by install.sh --health).

  dnscheck.py IP NAME        exit 0 if IP answers NAME (A) with NOERROR and at least one record
  dnscheck.py --conf FILE    exit 0 if the unbound config resolves itself and logs no queries:
                             no forward-zone/forward-addr, log-queries/log-replies off, verbosity 0,
                             QNAME minimisation on, a DNSSEC trust anchor, validator not disabled
"""
from __future__ import annotations

import os
import re
import socket
import struct
import sys


def query(ip: str, name: str, qtype: int = 1, timeout: float = 8.0) -> bytes:
    q = struct.pack(">HHHHHH", os.getpid() & 0xFFFF, 0x0100, 1, 0, 0, 1)  # RD, one OPT record (additional)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    q += struct.pack(">HH", qtype, 1)
    q += b"\0" + struct.pack(">HHIH", 41, 1232, 0x8000, 0)  # EDNS0, DO bit
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q, (ip, 53))
        r, _ = s.recvfrom(65535)
    return r


def answers(r: bytes) -> tuple[int, int, bool]:
    """(rcode, answer count, AD flag)"""
    return r[3] & 0x0F, struct.unpack(">H", r[6:8])[0], bool(r[3] & 0x20)


def conf_problems(text: str) -> list[str]:
    lines = [re.sub(r"#.*", "", l).strip() for l in text.splitlines()]
    kv = {}
    for l in lines:
        if ":" in l:
            k, v = l.split(":", 1)
            kv.setdefault(k.strip(), []).append(v.strip().strip('"'))
    bad = []
    if any(l.startswith(("forward-zone", "forward-addr", "forward-host", "stub-zone")) for l in lines):
        bad.append("forwards queries (forward-zone/stub-zone present)")
    for k, want in (("log-queries", "no"), ("log-replies", "no"), ("verbosity", "0"), ("qname-minimisation", "yes")):
        if kv.get(k, [None])[-1] != want:
            bad.append(f"{k} is not {want}")
    if not kv.get("auto-trust-anchor-file") and not kv.get("trust-anchor-file"):
        bad.append("no DNSSEC trust anchor")
    mc = kv.get("module-config", [""])[-1]
    if mc and "validator" not in mc:
        bad.append("validator module disabled")
    if kv.get("include"):
        bad.append("includes other files (cannot check them)")
    return bad


def main(argv=None) -> int:
    a = sys.argv[1:] if argv is None else argv
    if len(a) == 2 and a[0] == "--conf":
        bad = conf_problems(open(a[1]).read())
        for b in bad:
            print("resolver config:", b, file=sys.stderr)
        return 1 if bad else 0
    if len(a) == 2:
        try:
            rc, n, ad = answers(query(a[0], a[1]))
        except OSError as e:
            print(f"no answer from {a[0]}: {e}", file=sys.stderr)
            return 1
        print(f"rcode={rc} answers={n} dnssec_validated={'yes' if ad else 'no'}")
        return 0 if rc == 0 and n > 0 else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
