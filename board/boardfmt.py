"""Signed exit-seller board format, shared by board.py, board_server.py and the client.

board.json  = the board body (JSON object, below)
board.sig   = hex Ed25519 signature by the board key over
              b"srnet-exit-board/v1\\n" + canonical_json(body)

body = {
  "v": 1,
  "board": "<board .sesh>",          # the board key's own .sesh (= its Ed25519 pubkey)
  "seq": 7,                          # strictly increasing; clients refuse rollback
  "issued": 1760000000,              # unix seconds
  "expires": 1760604800,             # clients refuse a board past this (forces fresh signing)
  "max_listing_age_s": 10800,        # listings not refreshed for this long are hidden (3 h)
  "listings": [ {listing doc}, ... ] # each one still signed by its own exit .sesh key
}

A client checks both layers: the board signature (pinned board key) and each listing's own
exit signature.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

_PACK = Path(__file__).resolve().parent.parent / "seller"
if not (_PACK / "listing.py").exists():  # vendored next to listing.py
    _PACK = Path(__file__).resolve().parent
if str(_PACK) not in sys.path:
    sys.path.insert(0, str(_PACK))
import listing as L  # noqa: E402

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

BOARD_DOMAIN = "srnet-exit-board/v1"
MAX_BOARD_BYTES = 1 << 20
CLOCK_SKEW_S = 300
# Listing liveness: sellers re-sign their listing every LISTING_REFRESH_S; a listing whose newest
# signature is older than LISTING_HIDE_AFTER_S is hidden, and shows again on its next valid refresh.
LISTING_REFRESH_S = 3600
LISTING_HIDE_AFTER_S = 3 * 3600
DEFAULT_MAX_LISTING_AGE_S = LISTING_HIDE_AFTER_S
# Fields whose change on a refresh is recorded in the board audit log (they go live immediately).
AUDIT_FIELDS = ("payout", "chains", "quote_key", "claim_url", "country")
# Old v2 listings (contract payments) stay on the board, marked "seller needs to update", for this
# long after the board first runs this version; after that they are left out.
LEGACY_GRACE_S = 14 * 86400


class BoardError(Exception):
    pass


def canonical(body: dict) -> bytes:
    return (BOARD_DOMAIN + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()


def body_hash(body: dict) -> str:
    return hashlib.sha256(canonical(body)).hexdigest()


def pub_raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign_board(key: Ed25519PrivateKey, body: dict) -> str:
    if body.get("board") != L.sesh_of(pub_raw(key)):
        raise BoardError("body.board does not match the signing key's .sesh")
    return key.sign(canonical(body)).hex()


def listing_live(doc: dict, now: float, max_age_s: int) -> bool:
    if not L.verify(doc):
        return False
    body = doc["listing"]
    if int(body.get("issued", 0)) > now + CLOCK_SKEW_S:
        return False
    return now < L.expires_at(body, max_age_s)


def verify_board(body_bytes: bytes, sig_hex: str, pinned_sesh: str, now: float | None = None,
                 min_seq: int = 0, seen: dict | None = None) -> tuple[dict, list[dict], list[str]]:
    """Verify a fetched board against the pinned board .sesh.

    Returns (body, good_listings, dropped_reasons).  Raises BoardError if the board itself
    must be rejected (bad/missing sig, wrong key, expired, rollback, equivocation).
    `seen` = {"seq": n, "hash": h} from the client's cache, for rollback/equivocation checks.
    """
    now = time.time() if now is None else now
    if len(body_bytes) > MAX_BOARD_BYTES:
        raise BoardError("board too large")
    try:
        body = json.loads(body_bytes)
        pub = L.pub_of_sesh(pinned_sesh)
        sig = bytes.fromhex(sig_hex.strip())
    except (ValueError, TypeError) as e:
        raise BoardError(f"malformed board or pin: {e}") from None
    if not isinstance(body, dict) or body.get("v") != 1:
        raise BoardError("unsupported board version")
    if body.get("board") != pinned_sesh:
        raise BoardError("board key is not the pinned board key")
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, canonical(body))
    except InvalidSignature:
        raise BoardError("board signature invalid") from None
    if int(body.get("issued", 0)) > now + CLOCK_SKEW_S:
        raise BoardError("board issued in the future")
    if now >= int(body.get("expires", 0)):
        raise BoardError("board expired (stale or replayed)")
    seq = int(body.get("seq", -1))
    if seen:
        if seq < int(seen.get("seq", 0)):
            raise BoardError(f"board rollback: seq {seq} < last seen {seen['seq']}")
        if seq == int(seen.get("seq", 0)) and seen.get("hash") and seen["hash"] != body_hash(body):
            raise BoardError("board equivocation: same seq, different content")
    if seq < min_seq:
        raise BoardError("board seq below minimum")
    max_age = int(body.get("max_listing_age_s", DEFAULT_MAX_LISTING_AGE_S))
    good: dict[str, dict] = {}
    dropped: list[str] = []
    for doc in body.get("listings") or []:
        seller = (doc.get("listing") or {}).get("seller", "?") if isinstance(doc, dict) else "?"
        if not isinstance(doc, dict) or not L.verify(doc):
            dropped.append(f"{seller}: bad listing signature")
            continue
        if not listing_live(doc, now, max_age):
            dropped.append(f"{seller}: listing expired")
            continue
        prev = good.get(seller)
        if prev is None or doc["listing"]["issued"] > prev["listing"]["issued"]:
            good[seller] = doc
    return body, list(good.values()), dropped
