#!/usr/bin/env python3
"""Signed exit listing (nothing here publishes anything).

A listing is a small JSON offer signed with the exit's own identity key, so a buyer can
check that the offer really comes from the .sesh it names (the .sesh IS the Ed25519 pubkey,
z-base32 encoded).

  listing.py make   --identity exit.key --price-per-hour-usdc 1.00 [--max-hours 168] \
                    --payout 0x... --vendor-id 0x... --chainid 42161 --contract 0x... \
                    --country US [--region "US-Central"] [--policy tcp/443 --policy tcp/80] \
                    [--speed-cap-mbps 10] [--claim-url http://<claim>.sesh/claim] > listing.json
  listing.py verify listing.json        # exit 0 if the signature matches the named .sesh
  listing.py refresh --identity exit.key --listing listing.json [--submit-url URL]
                                        # re-sign with a new 'issued' time (same offer) and,
                                        # if a board submit URL is set, resubmit it

Liveness: the seller pack refreshes the listing every hour (srnet-seller-listing.timer). Boards
hide a listing that has not been refreshed for 3 hours and show it again on the next valid
refresh, as long as the payout fields are unchanged.

Listing body ("v":2, everything priced by the hour):
  {"v":2, "seller":"<.sesh>", "issued":<unix>, "price_per_hour_usdc":"1.00", "max_hours":168,
   "payout":"0x..", "vendor_id":"0x..", "chainid":N, "contract":"0x..", "country":"US", "region":"..", "policy":[..],
   "claim_url":"http://<claim>.sesh/claim", "exit_version":"0.1.0",
   "speed_cap_mbps":10}
country is the ISO 3166-1 alpha-2 code (uppercase) of the exit's IP; required by `make`, but older
listings without it stay valid (shown as unknown). region is optional finer detail.
speed_cap_mbps (optional) is the per-buyer speed cap in Mbit/s each way; 0 = unlimited. Listings
without it are valid (cap unknown).
exit_version (optional) is the seller pack version, read from VERSION next to this file and
updated on every refresh. It is self-reported and not checked. Listings without it are valid.
dns (optional) is the exit's own resolver, an IPv4 address inside the tunnel (private range) that
buyers send all their DNS to while connected. Listings without it offer no exit resolver.
claim_url (optional) is where buyers POST their signed lease claim (claimd.py). It is a separate
.sesh from the seller's, because the exit identity does not accept inbound sessions from buyers
that are not admitted yet.
Buyers pick whole hours (>= 1); lease amount = price_per_hour x hours, capped at $20 by the contract.

identity file: the session-router keyfile (128 hex chars = 32-byte seed || 32-byte pubkey).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ZB32 = "ybndrfg8ejkmcpqxot1uwisza345h769"
DOMAIN = "srnet-exit-listing/v1"
CLAIM_URL_RE = re.compile(r"^http://[ybndrfg8ejkmcpqxot1uwisza345h769]{52}\.sesh/claim$")
EXIT_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"


def pack_version() -> str:
    """Seller pack version from VERSION next to this file; "" if missing or malformed."""
    try:
        v = VERSION_FILE.read_text().strip()
    except OSError:
        return ""
    return v if EXIT_VERSION_RE.match(v) else ""


COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def valid_dns(v) -> bool:
    """dns: the exit resolver's in-tunnel IPv4 address (private ranges only, never a public resolver)."""
    import ipaddress
    if not isinstance(v, str):
        return False
    try:
        ip = ipaddress.IPv4Address(v)
    except ValueError:
        return False
    return any(ip in ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                          "100.64.0.0/10"))


def country_text(body: dict) -> str:
    v = body.get("country")
    return v if isinstance(v, str) and COUNTRY_RE.match(v) else "unknown"


def valid_speed_cap(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100000


def speed_cap_text(body: dict) -> str:
    """"10 Mbps", "unlimited", or "unknown" (absent or malformed)."""
    v = body.get("speed_cap_mbps")
    if not valid_speed_cap(v):
        return "unknown"
    return "unlimited" if v == 0 else f"{v} Mbps"


def version_key(v) -> tuple[int, ...] | None:
    """(major, minor, patch) from "1.2.3" / "1.2.3+build"; None if absent or unparsable."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v) if isinstance(v, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def meets_min_version(body: dict, min_version: str | None) -> bool:
    """True if no minimum is set, else the listing's exit_version must be >= min_version."""
    if not min_version:
        return True
    want, have = version_key(min_version), version_key(body.get("exit_version"))
    if want is None:
        raise ValueError(f"bad minimum exit version: {min_version!r}")
    return have is not None and have >= want


def zb32_encode(data: bytes) -> str:
    bits = int.from_bytes(data, "big")
    n = len(data) * 8
    pad = (5 - n % 5) % 5
    bits <<= pad
    n += pad
    return "".join(ZB32[(bits >> (n - 5 * (i + 1))) & 31] for i in range(n // 5))


def zb32_decode(s: str, nbytes: int = 32) -> bytes:
    bits = 0
    for ch in s:
        bits = (bits << 5) | ZB32.index(ch)
    extra = len(s) * 5 - nbytes * 8
    if extra < 0:
        raise ValueError("too short")
    if bits & ((1 << extra) - 1):
        raise ValueError("non-canonical z-base32")
    return (bits >> extra).to_bytes(nbytes, "big")


def sesh_of(pub: bytes) -> str:
    return zb32_encode(pub) + ".sesh"


def pub_of_sesh(sesh: str) -> bytes:
    if not sesh.endswith(".sesh") or len(sesh) != 57:
        raise ValueError("bad .sesh")
    return zb32_decode(sesh[:-5])


def load_identity(path: str | Path) -> Ed25519PrivateKey:
    raw = bytes.fromhex(Path(path).read_text().strip())
    if len(raw) not in (32, 64):
        raise ValueError("identity must be 32-byte seed or 64-byte seed||pubkey hex")
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    if len(raw) == 64:
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if pub != raw[32:]:
            raise ValueError("identity file pubkey half does not match seed")
    return key


def canonical(body: dict) -> bytes:
    return (DOMAIN + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()


def make(key: Ed25519PrivateKey, **fields) -> dict:
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"v": 2, "seller": sesh_of(pub), "issued": int(time.time()), **fields}
    return {"listing": body, "sig": key.sign(canonical(body)).hex()}


def verify(doc: dict) -> bool:
    try:
        body = doc["listing"]
        pub = pub_of_sesh(body["seller"])
        Ed25519PublicKey.from_public_bytes(pub).verify(bytes.fromhex(doc["sig"]), canonical(body))
        return True
    except (KeyError, ValueError, InvalidSignature, TypeError):
        return False


def expires_at(body: dict, max_age_s: int) -> int:
    """Effective expiry: the listing's own 'expires' (if any), capped at issued + max_age_s."""
    cap = int(body.get("issued", 0)) + int(max_age_s)
    exp = body.get("expires")
    return min(int(exp), cap) if isinstance(exp, int) else cap


REFRESH_INTERVAL_S = 3600
SUBMIT_TIMEOUT_S = 30


def refresh(key: Ed25519PrivateKey, path: str | Path) -> dict:
    """Re-sign the listing at `path` with a fresh 'issued' time; the offer itself is unchanged."""
    path = Path(path)
    old = json.loads(path.read_text())
    body = dict(old["listing"])
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if body.get("seller") != sesh_of(pub):
        raise ValueError("listing seller does not match this identity key")
    fields = {k: v for k, v in body.items() if k not in ("v", "seller", "issued")}
    ver = pack_version()
    if ver:
        fields["exit_version"] = ver
    doc = make(key, **fields)
    if not verify(doc):
        raise ValueError("refreshed listing failed to verify")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".listing-")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(doc, indent=1) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return doc


def submit(doc: dict, url: str) -> str:
    """POST the listing to a board. .sesh hosts are resolved via the local router (seshdns)."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("submit URL must be http(s)")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import seshdns
    status, body = seshdns.open_url(url, data=json.dumps(doc).encode(), timeout=SUBMIT_TIMEOUT_S, limit=200)
    return f"{status} {body[:200].decode(errors='replace').strip()}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--identity", required=True)
    m.add_argument("--price-per-hour-usdc", "--price-usdc", dest="price_per_hour_usdc", required=True,
                   help="USDC per hour, e.g. 1.00")
    m.add_argument("--max-hours", type=int, default=168)
    m.add_argument("--payout", required=True)
    m.add_argument("--vendor-id", required=True)
    m.add_argument("--chainid", type=int, required=True)
    m.add_argument("--contract", required=True)
    m.add_argument("--country", required=True, help="ISO 3166-1 alpha-2, e.g. US")
    m.add_argument("--region", default="")
    m.add_argument("--policy", action="append", default=[])
    m.add_argument("--claim-url", default="")
    m.add_argument("--dns", default="", help="exit resolver address buyers use through the tunnel, e.g. 10.195.0.53")
    m.add_argument("--speed-cap-mbps", type=int, default=None, help="per-buyer cap, Mbit/s; 0 = unlimited")
    m.add_argument("--exit-version", default=None, help="default: the pack VERSION file")
    v = sub.add_parser("verify")
    v.add_argument("file")
    r = sub.add_parser("refresh")
    r.add_argument("--identity", required=True)
    r.add_argument("--listing", required=True)
    r.add_argument("--submit-url", default=os.environ.get("BOARD_SUBMIT_URL", ""),
                   help="board submit endpoint (env BOARD_SUBMIT_URL); empty = only re-sign locally")
    a = ap.parse_args(argv)
    if a.cmd == "refresh":
        doc = refresh(load_identity(a.identity), a.listing)
        print(f"refreshed {doc['listing']['seller']} issued={doc['listing']['issued']} "
              f"exit_version={doc['listing'].get('exit_version', 'unknown')}")
        if a.submit_url:
            try:
                print("board:", submit(doc, a.submit_url))
            except Exception as e:  # network errors: keep the fresh local listing, report failure
                print(f"board submit failed: {e}", file=sys.stderr)
                return 3
        return 0
    if a.cmd == "make":
        extra = {}
        if a.claim_url:
            if not CLAIM_URL_RE.match(a.claim_url):
                ap.error("--claim-url must be http://<52 z-base32>.sesh/claim")
            extra["claim_url"] = a.claim_url
        if a.dns:
            if not valid_dns(a.dns):
                ap.error("--dns must be a private IPv4 address (the exit resolver inside the tunnel)")
            extra["dns"] = a.dns
        country = a.country.strip().upper()
        if not COUNTRY_RE.match(country):
            ap.error("--country must be an ISO 3166-1 alpha-2 code, e.g. US")
        extra["country"] = country
        if a.speed_cap_mbps is not None:
            if not valid_speed_cap(a.speed_cap_mbps):
                ap.error("--speed-cap-mbps must be 0..100000")
            extra["speed_cap_mbps"] = a.speed_cap_mbps
        ver = pack_version() if a.exit_version is None else a.exit_version
        if ver:
            if not EXIT_VERSION_RE.match(ver):
                ap.error("--exit-version must look like 0.1.0")
            extra["exit_version"] = ver
        doc = make(load_identity(a.identity), price_per_hour_usdc=a.price_per_hour_usdc, max_hours=a.max_hours,
                   payout=a.payout.lower(),
                   vendor_id=a.vendor_id.lower(), chainid=a.chainid, contract=a.contract.lower(),
                   region=a.region, policy=a.policy or ["tcp/443", "tcp/80"], **extra)
        print(json.dumps(doc, indent=1))
        return 0
    ok = verify(json.loads(Path(a.file).read_text()))
    print("valid" if ok else "INVALID")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
