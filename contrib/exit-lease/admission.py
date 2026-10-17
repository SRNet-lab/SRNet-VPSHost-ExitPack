#!/usr/bin/env python3
"""Seller-side issuer for signed exit lease admissions (exit lease admission).

The exit (session-router, src/auth/exit_lease_admission.hpp) admits an inbound
client only when the store file named by SR_EXIT_LEASE_ADMISSIONS holds a v2
record for (this exit's .sesh, that client's .sesh), unexpired, signed by the
Ed25519 key whose public half is SR_EXIT_LEASE_ISSUER_PUBKEY.

This tool is what the seller pack's payment watcher calls once a buyer's
payment hold for this seller is seen. It does not check payment itself.

  admission.py keygen  --out issuer.key           # prints pubkey hex
  admission.py issue   --key issuer.key --store admissions.json \
                       --seller <exit>.sesh --client <buyer>.sesh --ttl 3600 [--tid ID]
  admission.py revoke  --store admissions.json --tid ID
  admission.py prune   --store admissions.json
  admission.py verify  --store admissions.json --pubkey HEX --seller S --client C
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

DOMAIN = "srnet-exit-admission/v2"


def message(seller: str, client: str, exp: int, tid: str) -> bytes:
    for f in (seller, client, tid):
        if "\n" in f:
            raise ValueError("newline in field")
    return f"{DOMAIN}\n{seller}\n{client}\n{int(exp)}\n{tid}".encode()


def load_key(path: str | Path) -> Ed25519PrivateKey:
    raw = bytes.fromhex(Path(path).read_text().strip())
    if len(raw) != 32:
        raise ValueError("issuer key file must hold a 32-byte hex seed")
    return Ed25519PrivateKey.from_private_bytes(raw)


def pubkey_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def keygen(out: str | Path) -> str:
    p = Path(out)
    if p.exists():
        raise FileExistsError(str(p))
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(seed.hex() + "\n")
    return pubkey_hex(key)


def make_record(key: Ed25519PrivateKey, seller: str, client: str, exp: int, tid: str) -> dict:
    sig = key.sign(message(seller, client, exp, tid)).hex()
    return {
        "kind": "exit_lease_admission",
        "scope": "exit_lease",
        "v": 2,
        "tid": tid,
        "lock_seller_id": seller,
        "client_sesh": client,
        "exp": int(exp),
        "sig": sig,
    }


def verify_record(rec: dict, pub_hex: str, seller: str, client: str, now: int | None = None) -> str:
    """Python mirror of the C++ check. Returns 'admission' or a reason."""
    now = int(time.time()) if now is None else now
    if rec.get("kind") != "exit_lease_admission":
        return "no_match"
    if rec.get("scope") != "exit_lease" or rec.get("v") != 2 or not isinstance(rec.get("exp"), int):
        return "bad_record"
    if rec.get("lock_seller_id") != seller:
        return "wrong_seller"
    if rec.get("client_sesh") != client:
        return "wrong_client"
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(
            bytes.fromhex(rec.get("sig", "")),
            message(rec["lock_seller_id"], rec["client_sesh"], rec["exp"], rec.get("tid", "")),
        )
    except (InvalidSignature, ValueError):
        return "bad_sig"
    if not rec["exp"] > now:
        return "expired"
    return "admission"


class Store:
    """admissions.json = {"admissions": {tid: record}}; atomic replace under flock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"admissions": {}}
        if not isinstance(data.get("admissions"), dict):
            data["admissions"] = {}
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".adm-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, separators=(",", ":"), sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _mutate(self, fn):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                data = self._read()
                out = fn(data)
                self._write(data)
                return out
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def add(self, rec: dict) -> None:
        def f(d):
            if rec["tid"] in d["admissions"]:
                raise KeyError(f"tid already admitted: {rec['tid']}")
            d["admissions"][rec["tid"]] = rec
        self._mutate(f)

    def revoke(self, tid: str) -> bool:
        return self._mutate(lambda d: d["admissions"].pop(tid, None) is not None)

    def prune(self, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now

        def f(d):
            dead = [t for t, r in d["admissions"].items() if not (isinstance(r.get("exp"), int) and r["exp"] > now)]
            for t in dead:
                del d["admissions"][t]
            return len(dead)
        return self._mutate(f)

    def records(self) -> list[dict]:
        return list(self._read()["admissions"].values())


def issue(key_path, store_path, seller, client, ttl, tid=None, now=None) -> dict:
    now = int(time.time()) if now is None else now
    rec = make_record(load_key(key_path), seller, client, now + int(ttl), tid or secrets.token_hex(8))
    Store(store_path).add(rec)
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--out", required=True)
    i = sub.add_parser("issue")
    for a in ("--key", "--store", "--seller", "--client"):
        i.add_argument(a, required=True)
    i.add_argument("--ttl", type=int, default=3600); i.add_argument("--tid")
    r = sub.add_parser("revoke"); r.add_argument("--store", required=True); r.add_argument("--tid", required=True)
    p = sub.add_parser("prune"); p.add_argument("--store", required=True)
    v = sub.add_parser("verify")
    for a in ("--store", "--pubkey", "--seller", "--client"):
        v.add_argument(a, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        print(keygen(a.out))
    elif a.cmd == "issue":
        print(json.dumps(issue(a.key, a.store, a.seller, a.client, a.ttl, a.tid)))
    elif a.cmd == "revoke":
        print("revoked" if Store(a.store).revoke(a.tid) else "absent")
    elif a.cmd == "prune":
        print(f"pruned {Store(a.store).prune()}")
    elif a.cmd == "verify":
        reasons = [verify_record(r, a.pubkey, a.seller, a.client) for r in Store(a.store).records()]
        res = "admission" if "admission" in reasons else (reasons[0] if reasons else "no_match")
        print(res)
        return 0 if res == "admission" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
