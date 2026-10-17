#!/usr/bin/env python3
"""Minimal EVM access for the seller pack, standard library only.

JSON-RPC over HTTP(S), static ABI encode/decode (uint256, address, bytes32, bool),
and EIP-191 personal_sign recovery on secp256k1. Read-only: nothing here holds a key
or sends a transaction.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from keccak256 import keccak256  # noqa: E402

LOCAL_CHAINS = {31337: "anvil"}  # local test chain only
MAINNET_CHAINS = {42161: "arbitrum-one"}
CHAINS = {**MAINNET_CHAINS, **LOCAL_CHAINS}


class RpcError(Exception):
    pass


class JsonRpc:
    def __init__(self, url: str, timeout: float = 20):
        self.url, self.timeout, self._id = url, timeout, 0

    def call(self, method: str, *params):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                   "User-Agent": "srnet-seller"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise RpcError(f"rpc {self.url}: {getattr(e, 'reason', e)}") from e
        if resp.get("error"):
            raise RpcError(f"rpc {method}: {resp['error'].get('message', resp['error'])}")
        return resp.get("result")


# ---------------------------------------------------------------- ABI (static types only)
def selector(sig: str) -> bytes:
    return keccak256(sig.encode())[:4]


def _word(t: str, v) -> bytes:
    if t.startswith("uint"):
        return int(v).to_bytes(32, "big")
    if t == "address":
        return bytes.fromhex(str(v)[2:].rjust(40, "0")).rjust(32, b"\0")
    if t == "bytes32":
        b = bytes.fromhex(str(v).removeprefix("0x")) if isinstance(v, str) else bytes(v)
        if len(b) != 32:
            raise ValueError("bytes32 needs 32 bytes")
        return b
    if t == "bool":
        return int(bool(v)).to_bytes(32, "big")
    raise ValueError(f"unsupported ABI type {t}")


def encode_call(sig: str, *args) -> bytes:
    inner = sig[sig.index("(") + 1: sig.rindex(")")]
    types = [t for t in inner.split(",") if t]
    if len(types) != len(args):
        raise ValueError("argument count")
    return selector(sig) + b"".join(_word(t, a) for t, a in zip(types, args))


def decode(types: list[str], data: bytes) -> tuple:
    if len(data) < 32 * len(types):
        raise ValueError("short ABI result")
    out = []
    for i, t in enumerate(types):
        w = data[32 * i: 32 * (i + 1)]
        if t.startswith("uint"):
            out.append(int.from_bytes(w, "big"))
        elif t == "address":
            out.append("0x" + w[12:].hex())
        elif t == "bytes32":
            out.append("0x" + w.hex())
        elif t == "bool":
            out.append(w[-1] == 1)
        else:
            raise ValueError(f"unsupported ABI type {t}")
    return tuple(out)


# ---------------------------------------------------------------- secp256k1 recovery
_P = 2**256 - 2**32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        m = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P)
    else:
        m = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P)
    x = (m * m - a[0] - b[0]) % _P
    return x, (m * (a[0] - x) - a[1]) % _P


def _mul(k, pt):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def personal_hash(message: str | bytes) -> bytes:
    m = message.encode() if isinstance(message, str) else message
    return keccak256(b"\x19Ethereum Signed Message:\n" + str(len(m)).encode() + m)


def recover(msg_hash: bytes, sig: str | bytes) -> str | None:
    """Address (lowercase 0x) that produced a 65-byte r||s||v signature over msg_hash, or None."""
    try:
        b = bytes.fromhex(sig[2:] if isinstance(sig, str) and sig.startswith("0x") else sig) \
            if isinstance(sig, str) else bytes(sig)
    except ValueError:
        return None
    if len(b) != 65:
        return None
    r, s, v = int.from_bytes(b[:32], "big"), int.from_bytes(b[32:64], "big"), b[64]
    v = v - 27 if v >= 27 else v
    if v not in (0, 1) or not (0 < r < _N) or not (0 < s < _N):
        return None
    y2 = (pow(r, 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if y * y % _P != y2:
        return None
    if y & 1 != v:
        y = _P - y
    e = int.from_bytes(msg_hash, "big")
    rinv = pow(r, -1, _N)
    q = _add(_mul(s * rinv % _N, (r, y)), _mul((-e * rinv) % _N, _G))
    if q is None:
        return None
    pub = q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return "0x" + keccak256(pub)[12:].hex()


def verify_personal(address: str, message: str, sig: str) -> bool:
    got = recover(personal_hash(message), sig)
    return got is not None and got == address.lower()
