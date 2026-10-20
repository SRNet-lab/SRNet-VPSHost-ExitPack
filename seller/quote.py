"""Exit quotes: how a buyer pays a seller directly, with no contract in between.

1. The buyer asks the seller's claim endpoint for a quote (request below), over Session Router.
   The request is signed by the buyer key it names and carries the time, so nobody else can ask
   for quotes in that buyer's name (that would use up the buyer's open-quote limit).
2. The seller answers with a signed quote: payout address, chain, the official USDC address, an
   exact amount and an expiry (about 15 minutes). The amount is the price for the hours asked
   plus a few millionths of a dollar that no other quote for this seller on that chain has used
   in the last 7 days, so the last digits work as a payment ID. Normally that is under one cent
   (1..9999 base units). When many quotes are out it grows in steps, but never past
   extra_cap(): 0.05 USDC or 5% of the price, whichever is larger, and never more than half
   the price.
3. The buyer pays exactly that amount from any wallet, then sends a "paid" notice with the tx
   hash, signed by the per-install buyer key named in the quote request.
4. The seller's watcher checks the quote against the signed listing, checks the payment on
   chain and opens the lease for the quoted hours.

The quote is bound to the buyer's .sesh address: the exit only ever admits that address for a
paid quote, so a quote or a tx hash is useless to anyone else.

Messages (all JSON):
  request  {"v":1, "seller", "client_sesh", "hours", "chainid", "token":"USDC", "buyer_key":<hex32>,
            "ts":<unix time>, "nonce":<hex16>, "sig": <hex, Ed25519 by buyer_key over request_message()>}
  quote    {"quote": {"v":1, "id", "seller", "client_sesh", "buyer_key", "hours", "chainid", "token",
                      "token_address", "payout", "amount", "price_per_hour", "issued", "expires"},
            "sig": <hex, Ed25519 by the listing's quote_key>}
  paid     {"v":1, "quote_id", "tx", "sig": <hex, Ed25519 by buyer_key over paid_message()>}
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chains as C  # noqa: E402

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey  # noqa: E402

QUOTE_DOMAIN = "srnet-exit-quote/v1"
REQUEST_DOMAIN = "srnet-exit-request/v1"
PAID_DOMAIN = "srnet-exit-paid/v1"
QUOTE_TTL_S = 900
MAX_QUOTE_TTL_S = 1800
PAY_ID_SMALL = 9999         # unique part of the amount, in USDC base units: normally < 1 cent
PAY_ID_STEPS = (9_999, 19_999, 49_999, 99_999)  # ranges it grows through when many quotes are out
PAY_ID_MAX = 99_999         # ... and never more than 0.099999 USDC
EXTRA_MIN = 50_000          # the extra may always reach 0.05 USDC ...
EXTRA_SHARE = 20            # ... or 1/20 (5%) of the price, whichever is larger,
EXTRA_MAX_SHARE = 2         # ... but never more than half the price
POOL_BUSY = 0.9             # refuse new quotes when this share of the amounts for a price is taken
RESERVE_S = 7 * 86400       # an amount is never quoted again within this long (paid or not)
KEEP_QUOTES_S = RESERVE_S + 1800 + 86400  # quote files (and paid notices for them) are kept this long
EARLY_SKEW_S = 300          # block time may sit this far before the quote was issued (seller clock fast)
LATE_SKEW_S = 120           # ... and this far after it expired
CLOCK_SKEW_S = 120          # buyer side: allowed difference between our clock and the seller's
REQUEST_SKEW_S = 120        # seller side: a request's ts may be this far from the seller's clock
SESH_RE = re.compile(r"^[ybndrfg8ejkmcpqxot1uwisza345h769]{52}\.sesh$")
HEX32_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")
TX_RE = re.compile(r"^0x[0-9a-f]{64}$")
REQUEST_FIELDS = {"v", "seller", "client_sesh", "hours", "chainid", "token", "buyer_key", "ts", "nonce", "sig"}
QUOTE_FIELDS = {"v", "id", "seller", "client_sesh", "buyer_key", "hours", "chainid", "token", "token_address",
                "payout", "amount", "price_per_hour", "issued", "expires"}


class QuoteError(ValueError):
    pass


def _canon(domain: str, body: dict) -> bytes:
    return (domain + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()


def pub_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------- buyer side: request
def request_message(req: dict) -> bytes:
    return _canon(REQUEST_DOMAIN, {k: v for k, v in req.items() if k != "sig"})


def make_request(seller: str, client_sesh: str, hours: int, chainid: int, buyer_key: Ed25519PrivateKey,
                 now: int | None = None) -> dict:
    """A quote request signed by buyer_key (its public half is the request's buyer_key)."""
    req = {"v": 1, "seller": seller, "client_sesh": client_sesh, "hours": int(hours), "chainid": int(chainid),
           "token": C.TOKEN, "buyer_key": pub_hex(buyer_key), "ts": int(time.time()) if now is None else int(now),
           "nonce": secrets.token_hex(8)}
    return sign_request(req, buyer_key)


def sign_request(req: dict, buyer_key: Ed25519PrivateKey) -> dict:
    out = {k: v for k, v in req.items() if k != "sig"}
    out["sig"] = buyer_key.sign(request_message(out)).hex()
    return out


def request_error(req, seller: str, accept: list[int], max_hours: int, testnets: bool = False,
                  now: int | None = None) -> str | None:
    """Seller side: None if the request is acceptable, else the reason. Replays of one signed
    request inside the time window are the caller's to refuse (claimd keeps the signatures)."""
    if not isinstance(req, dict) or set(req) != REQUEST_FIELDS or req.get("v") != 1:
        return "bad request"
    if req["seller"] != seller:
        return "wrong seller"
    if not isinstance(req["buyer_key"], str) or not HEX32_RE.match(req["buyer_key"]):
        return "bad buyer_key"
    if not _is_int(req["ts"]) or not isinstance(req["nonce"], str) or not ID_RE.match(req["nonce"]) \
            or not isinstance(req["sig"], str) or not re.fullmatch(r"[0-9a-f]{128}", req["sig"]):
        return "bad request"
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(req["buyer_key"])).verify(
            bytes.fromhex(req["sig"]), request_message(req))
    except (ValueError, InvalidSignature):
        return "the request is not signed by its buyer key"
    now = int(time.time()) if now is None else int(now)
    if abs(req["ts"] - now) > REQUEST_SKEW_S:
        return "the request's time is more than 2 minutes off the seller's clock (check your computer's clock)"
    if not isinstance(req["client_sesh"], str) or not SESH_RE.match(req["client_sesh"]):
        return "bad client_sesh"
    if not _is_int(req["hours"]) or req["hours"] < 1:
        return "hours must be a whole number, at least 1"
    if req["hours"] > max_hours:
        return f"at most {max_hours} hours per purchase"
    if req["token"] != C.TOKEN:
        return "only USDC is accepted"
    if not C.supported(req["chainid"], testnets) or req["chainid"] not in accept:
        return "this seller does not take payments on that chain"
    return None


# ---------------------------------------------------------------- seller side: quote
def extra_cap(base: int) -> int:
    """Largest pay id (extra over the price) for a purchase of base units: 0.05 USDC or 5% of
    the price, whichever is larger, but never more than half the price or PAY_ID_MAX. With the
    0.10 minimum price (listing.MIN_PRICE_UNITS) the half-price rule never cuts below 0.05."""
    return max(1, min(PAY_ID_MAX, int(base) // EXTRA_MAX_SHARE, max(EXTRA_MIN, int(base) // EXTRA_SHARE)))


def unique_amount(base: int, taken: set[int], rng=secrets.randbelow) -> tuple[int, int]:
    """(amount, pay_id): base + a pay id that makes the full amount differ from every amount in
    taken (all amounts quoted on this chain in the last RESERVE_S seconds).

    The pay id is drawn from 1..PAY_ID_SMALL while that range is at most half used, then from
    the next of PAY_ID_STEPS, never past extra_cap(base)."""
    cap = extra_cap(base)
    used = {a - base for a in taken if base < a <= base + cap}
    if len(used) >= POOL_BUSY * cap:
        raise QuoteError("busy: too many recent quotes; try again later")
    widths = sorted({min(w, cap) for w in PAY_ID_STEPS} | {cap})
    width = widths[-1]
    for w in widths:
        if sum(1 for p in used if p <= w) * 2 <= w:
            width = w
            break
    for _ in range(64):
        pid = 1 + rng(width)
        if pid not in used:
            return base + pid, pid
    pid = next(p for p in range(1, cap + 1) if p not in used)
    return base + pid, pid


def make_quote(key: Ed25519PrivateKey, req: dict, *, payout: str, price_per_hour: int, taken: set[int],
               cap: int | None = None, ttl: int = QUOTE_TTL_S, now: int | None = None, testnets: bool = False,
               rng=secrets.randbelow) -> tuple[dict, int]:
    """Signed quote for an accepted request; returns (doc, pay_id). taken: full amounts in use."""
    if not C.ADDR_RE.match(payout) or int(payout, 16) == 0:
        raise QuoteError("seller has no payout address")
    now = int(time.time()) if now is None else int(now)
    base = int(price_per_hour) * int(req["hours"])
    amount, pid = unique_amount(base, taken, rng)
    if cap is not None and amount > cap:
        raise QuoteError(f"over this seller's limit of {C.fmt_usdc(cap)} USDC per purchase; ask for fewer hours")
    body = {"v": 1, "id": secrets.token_hex(8), "seller": req["seller"], "client_sesh": req["client_sesh"],
            "buyer_key": req["buyer_key"], "hours": int(req["hours"]), "chainid": int(req["chainid"]),
            "token": C.TOKEN, "token_address": C.usdc(req["chainid"], testnets), "payout": payout.lower(),
            "amount": amount, "price_per_hour": int(price_per_hour), "issued": now,
            "expires": now + min(int(ttl), MAX_QUOTE_TTL_S)}
    return {"quote": body, "sig": key.sign(_canon(QUOTE_DOMAIN, body)).hex()}, pid


def verify_quote(doc, quote_key_hex: str) -> bool:
    try:
        body = doc["quote"]
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(quote_key_hex)).verify(
            bytes.fromhex(doc["sig"]), _canon(QUOTE_DOMAIN, body))
        return set(body) == QUOTE_FIELDS
    except (KeyError, TypeError, ValueError, InvalidSignature):
        return False


def quote_error(doc, *, listing: dict, req: dict, cap: int, now: int | None = None,
                testnets: bool = False) -> str | None:
    """Buyer side: None if the seller's quote matches what we asked for and what the listing offers."""
    now = int(time.time()) if now is None else int(now)
    if not verify_quote(doc, str(listing.get("quote_key") or "")):
        return "the quote is not signed by this seller's quote key"
    q = doc["quote"]
    for k in ("seller", "client_sesh", "buyer_key", "hours", "chainid"):
        if q[k] != req[k]:
            return f"the quote's {k} does not match the request"
    if q["token"] != C.TOKEN or q["token_address"] != C.usdc(q["chainid"], testnets):
        return "the quote names a token that is not the official USDC on that chain"
    if str(q["payout"]).lower() != str(listing.get("payout", "")).lower():
        return "the quote's payout address is not the one in the signed listing"
    try:
        listed = C.parse_usdc(listing["price_per_hour_usdc"])
    except (KeyError, C.ChainError):
        return "the listing has no valid price"
    base = listed * q["hours"]
    if q["price_per_hour"] != listed or not (base < q["amount"] <= base + extra_cap(base)):
        return "the quoted amount does not match the listed price"
    if q["amount"] > cap:
        return (f"{C.fmt_usdc(q['amount'])} USDC is over your limit of {C.fmt_usdc(cap)} per payment "
                "(raise it with --max-usdc or \"max_payment_usdc\" in config.json)")
    if not (now - CLOCK_SKEW_S <= q["issued"] <= now + CLOCK_SKEW_S) or q["expires"] <= now \
            or q["expires"] - q["issued"] > MAX_QUOTE_TTL_S:
        return "the quote is expired or has bad times (check your computer's clock)"
    return None


# ---------------------------------------------------------------- seller side: re-check a stored quote
def listing_terms(body: dict) -> dict:
    """What a quote must agree with, from a verified listing body."""
    return {"seller": body["seller"], "payout": str(body["payout"]).lower(),
            "chains": sorted(int(c) for c in body["chains"]), "price": C.parse_usdc(body["price_per_hour_usdc"]),
            "max_hours": int(body["max_hours"]), "quote_key": str(body["quote_key"])}


def terms_error(doc, terms: dict, testnets: bool = False, price: bool = True) -> str | None:
    """None if a stored quote is one the listing with these terms could have issued: signed by
    its quote key, its payout, one of its chains, the official USDC, 1..max_hours, the listed
    price plus at most extra_cap(), and a sane lifetime. The watcher runs this on every quote
    it is asked to pay out, because quote files are written by the network-facing claim desk.
    price=False skips the price and max_hours checks (the quote only has to add up on its own
    price): the watcher uses that to tell a quote from an unknown older price, which the seller
    can admit by hand, from one that pays someone else or uses the wrong key or token."""
    if not verify_quote(doc, terms["quote_key"]):
        return "not signed by the listing's quote key"
    q = doc["quote"]
    if q.get("v") != 1 or q["seller"] != terms["seller"]:
        return "not for this seller"
    if not isinstance(q["client_sesh"], str) or not SESH_RE.match(q["client_sesh"]) \
            or not isinstance(q["buyer_key"], str) or not HEX32_RE.match(q["buyer_key"]):
        return "bad buyer fields"
    if str(q["payout"]).lower() != terms["payout"]:
        return "payout address is not the listing's"
    if not _is_int(q["chainid"]) or q["chainid"] not in terms["chains"] or not C.supported(q["chainid"], testnets):
        return "chain is not one the listing takes"
    if q["token"] != C.TOKEN or str(q["token_address"]).lower() != C.usdc(q["chainid"], testnets):
        return "token is not the official USDC on that chain"
    top = terms["max_hours"] if price else 999
    if not _is_int(q["hours"]) or not (1 <= q["hours"] <= top):
        return "hours outside the listing's 1..max_hours"
    pph = terms["price"] if price else q["price_per_hour"]
    if not _is_int(pph) or pph < 1:
        return "amount does not match the listed price"
    base = pph * q["hours"]
    if not _is_int(q["amount"]) or q["price_per_hour"] != pph \
            or not (base < q["amount"] <= base + extra_cap(base)):
        return "amount does not match the listed price"
    if not _is_int(q["issued"]) or not _is_int(q["expires"]) \
            or not (q["issued"] < q["expires"] <= q["issued"] + MAX_QUOTE_TTL_S):
        return "bad quote times"
    return None


# ---------------------------------------------------------------- paid notice
def paid_message(quote_id: str, chainid: int, tx: str, client_sesh: str) -> bytes:
    return f"{PAID_DOMAIN}\n{quote_id}\n{int(chainid)}\n{tx.lower()}\n{client_sesh}".encode()


def make_paid(buyer_key: Ed25519PrivateKey, quote: dict, tx: str) -> dict:
    tx = tx.lower()
    if not TX_RE.match(tx):
        raise QuoteError("bad transaction hash")
    sig = buyer_key.sign(paid_message(quote["id"], quote["chainid"], tx, quote["client_sesh"])).hex()
    return {"v": 1, "quote_id": quote["id"], "tx": tx, "sig": sig}


def paid_error(paid, quote: dict) -> str | None:
    """None if the paid notice is well formed and signed by the key the quote was issued to."""
    if not isinstance(paid, dict) or set(paid) != {"v", "quote_id", "tx", "sig"} or paid.get("v") != 1:
        return "bad paid notice"
    if paid["quote_id"] != quote["id"]:
        return "wrong quote"
    if not isinstance(paid["tx"], str) or not TX_RE.match(paid["tx"]):
        return "bad transaction hash"
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(quote["buyer_key"])).verify(
            bytes.fromhex(paid["sig"]), paid_message(quote["id"], quote["chainid"], paid["tx"], quote["client_sesh"]))
    except (TypeError, ValueError, InvalidSignature):
        return "paid notice is not signed by the buyer who asked for the quote"
    return None


# ---------------------------------------------------------------- matching a payment
def payment_error(quote: dict, transfers: list[dict], block_time: int, status_ok: bool,
                  testnets: bool = False) -> str | None:
    """None if one of the tx's USDC transfers pays this quote exactly, inside its window
    (issued - EARLY_SKEW_S .. expires + LATE_SKEW_S, by block time).

    transfers: [{"token", "from", "to", "value"}] decoded from the receipt's Transfer logs."""
    if not status_ok:
        return "the transaction failed on chain"
    token = C.usdc(quote["chainid"], testnets)
    mine = [t for t in transfers if t["token"].lower() == token and t["to"].lower() == quote["payout"].lower()]
    if not mine:
        return "no USDC transfer to the seller's payout address in that transaction"
    if not any(int(t["value"]) == int(quote["amount"]) for t in mine):
        got = ", ".join(C.fmt_usdc(t["value"]) for t in mine)
        return f"amount mismatch: the quote asks for exactly {C.fmt_usdc(quote['amount'])} USDC, the tx paid {got}"
    if block_time < quote["issued"] - EARLY_SKEW_S:
        return "the payment is older than the quote"
    if block_time > quote["expires"] + LATE_SKEW_S:
        return "the payment arrived after the quote expired"
    return None


def load_key(path: str | Path) -> Ed25519PrivateKey:
    raw = bytes.fromhex(Path(path).read_text().strip())
    if len(raw) not in (32, 64):
        raise ValueError("key file must hold a 32-byte seed (hex)")
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


def new_key_file(path: str | Path) -> str:
    """Create a 0600 Ed25519 seed file if missing; returns the public key hex."""
    import os
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (secrets.token_bytes(32).hex() + "\n").encode())
        os.close(fd)
    return pub_hex(load_key(p))
