"""Buy exit time with a direct USDC payment.

  quote()   ask the seller for a quote bound to our .sesh address; check it against the signed listing
  info()    what to show: chain, address, exact amount, EIP-681 link (for a QR code)
  watch()   watch the chain for that exact transfer, then tell the seller "quote X paid" and wait
            for the exit to admit us; record the lease end

Quotes and leases are kept in state.json under "exits". Nothing here holds or moves funds:
the buyer pays from their own wallet and pays the network fee themselves.
"""
from __future__ import annotations

import time
from pathlib import Path

import chains as C  # noqa: E402
import evm  # noqa: E402
import listing as L  # noqa: E402
import quote as Q  # noqa: E402

from . import claim, config, watch as W

PAID_GRACE_S = 900       # this computer watches the chain this long after the quote expires
REMIND_S = 600           # warn this long before a lease ends


class BuyError(RuntimeError):
    pass


def key_path(root: Path) -> Path:
    return Path(root) / "exits" / "buyer.key"


def buyer_key(root: Path):
    p = key_path(root)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        Q.new_key_file(p)
    return Q.load_key(p)


def _ex(st) -> dict:
    e = st.data.setdefault("exits", {})
    e.setdefault("quotes", {})
    e.setdefault("leases", {})
    return e


def no_match_text(q: dict, near: list[int] | None = None, other_chain: int | None = None) -> str:
    amt = C.fmt_usdc(q["amount"])
    msg = (f"No matching payment yet. Send exactly {amt} USDC on {C.label(q['chainid'])} "
           f"to {q['payout']}.")
    if other_chain:
        msg += (f" A payment of that amount arrived on {C.label(other_chain)}, but this quote is for "
                f"{C.label(q['chainid'])}; it can't be matched. Ask the seller directly about it.")
    elif near:
        seen = ", ".join(C.fmt_usdc(v) for v in near[:3])
        msg += (f" The seller received {seen} USDC, but this quote needs exactly {amt}; "
                "a different amount can't be matched to you.")
    return msg


def expired_text(q: dict, near: list[int] | None = None, other_chain: int | None = None) -> str:
    """No payment was seen and the quote has expired: don't invite a payment to it now."""
    amt = C.fmt_usdc(q["amount"])
    msg = (f"No payment of exactly {amt} USDC on {C.label(q['chainid'])} to {q['payout']} arrived "
           "before this quote expired. Don't pay this quote now; a late payment can't be matched. "
           "If you did pay it, run `srnet paid --tx 0x...` with your transaction hash. "
           "Otherwise get a new quote with `srnet buy`.")
    if other_chain:
        msg += (f" A payment of that amount arrived on {C.label(other_chain)}, but this quote is for "
                f"{C.label(q['chainid'])}; it can't be matched. Ask the seller directly about it.")
    elif near:
        seen = ", ".join(C.fmt_usdc(v) for v in near[:3])
        msg += f" The seller received {seen} USDC, but this quote needs exactly {amt}."
    return msg


def unreadable_text(q: dict, err: str = "") -> str:
    """The last chain reads failed: we don't know whether a payment arrived."""
    return (f"Couldn't read {C.label(q['chainid'])} (the RPC didn't answer{': ' + err if err else ''}), so this "
            "computer can't tell whether you paid. Don't pay again. If you paid, run "
            "`srnet paid --tx 0x...` with your transaction hash; the seller checks it on chain.")


def reason_text(q: dict, reason: str) -> str:
    r = str(reason or "")
    if r.startswith("no_matching_payment"):
        detail = r.partition(":")[2].strip()
        return no_match_text(q) + (f" ({detail})" if detail else "")
    return {
        "quote_already_paid": "This quote was already paid with another transaction.",
        "payment_already_used": "That transaction was already used for another purchase.",
        "bad_buyer_signature": "The seller could not check this client's signature.",
        "unknown_quote": "The seller doesn't know this quote; ask for a new one.",
    }.get(r, r or "rejected")


class Buyer:
    def __init__(self, st, cfg: dict | None = None, transport=claim.http_transport, rpc_factory=None,
                 clock=time.time, sleep=time.sleep):
        self.st = st
        self.cfg = config.load(st.root) if cfg is None else cfg
        self.transport, self.clock, self.sleep = transport, clock, sleep
        self.testnets = config.testnets(self.cfg)
        self.rpc_factory = rpc_factory or (lambda cid: W.rpc_for(cid, config.rpc_overrides(self.cfg), self.testnets))

    # --------------------------------------------------------------- quote
    def desk(self, body: dict) -> claim.Desk:
        return claim.Desk(claim.base_url(body, self.testnets), self.transport, dns=config.sesh_dns(self.cfg))

    def quote(self, doc: dict, hours: int, client_sesh: str, chain=None, max_usdc: str | None = None) -> dict:
        body = (doc or {}).get("listing") or {}
        if not L.verify(doc):
            raise BuyError("the listing's signature does not check out")
        why = L.listing_error(body, self.testnets)
        if why:
            raise BuyError(why)
        if not isinstance(hours, int) or not (1 <= hours <= int(body["max_hours"])):
            raise BuyError(f"hours must be 1..{body['max_hours']} for this seller")
        try:
            cid = config.pick_chain(body, chain, self.cfg)
            cap = C.parse_usdc(max_usdc) if max_usdc not in (None, "") else config.max_payment(self.cfg)
        except C.ChainError as e:
            raise BuyError(str(e)) from e
        price = C.parse_usdc(body["price_per_hour_usdc"]) * hours
        if price >= cap:  # every quote adds a few micro-USDC to make the amount unique
            raise BuyError(f"{C.fmt_usdc(price)} USDC is at or over your limit of {C.fmt_usdc(cap)} per payment "
                           "(raise it with --max-usdc or \"max_payment_usdc\" in config.json)")
        key = buyer_key(self.st.root)
        req = Q.make_request(body["seller"], client_sesh, hours, cid, Q.pub_hex(key))
        try:
            qdoc = self.desk(body).quote(req)
        except claim.ClaimError as e:
            raise BuyError(str(e)) from e
        why = Q.quote_error(qdoc, listing=body, req=req, cap=cap, now=int(self.clock()), testnets=self.testnets)
        if why:
            raise BuyError(why)
        q = qdoc["quote"]
        try:
            nxt = W.start_block(self.rpc_factory(cid), cid)
        except evm.RpcError:
            nxt = None
        rec = {"id": q["id"], "seller": body["seller"], "doc": qdoc, "state": "open", "scan": {},
               "listing": {k: body.get(k) for k in ("seller", "payout", "chains", "price_per_hour_usdc",
                                                     "quote_key", "claim_url", "dns", "max_hours")}}
        if nxt is not None:
            rec["scan"]["next"] = nxt
        _ex(self.st)["quotes"][q["id"]] = rec
        self.st.save()
        return rec

    def info(self, rec: dict) -> dict:
        q = rec["doc"]["quote"]
        return {"id": q["id"], "seller": q["seller"], "chainid": q["chainid"], "chain": C.label(q["chainid"]),
                "token": "USDC", "token_address": q["token_address"], "address": q["payout"],
                "amount": q["amount"], "amount_text": C.fmt_usdc(q["amount"]), "hours": q["hours"],
                "expires": q["expires"],
                "uri": C.eip681(q["chainid"], q["payout"], q["amount"], self.testnets),
                "tx_link": C.tx_link(q["chainid"], "")}

    def find(self, qid: str | None = None, seller: str | None = None, with_expired: bool = False) -> dict:
        """A quote by id, else the newest open one (with_expired: also expired ones, for --tx)."""
        qs = _ex(self.st)["quotes"]
        if qid:
            rec = qs.get(qid)
            if not rec:
                raise BuyError(f"no quote {qid} on this computer")
            return rec
        ok = ("open", "reported", "expired") if with_expired else ("open", "reported")
        live = [r for r in qs.values() if r["state"] in ok
                and (seller is None or r["seller"] == seller)]
        if not live:
            raise BuyError("no open quote; run `srnet buy --seller <id> --hours N` first")
        return max(live, key=lambda r: r["doc"]["quote"]["issued"])

    # --------------------------------------------------------------- pay
    def scan_once(self, rec: dict) -> dict | None:
        q = rec["doc"]["quote"]
        rpc = self.rpc_factory(q["chainid"])
        if "next" not in rec["scan"]:
            rec["scan"]["next"] = W.start_block(rpc, q["chainid"], back_s=int(self.clock()) - q["issued"] + 120)
        s = W.Scan(rpc, q, rec["scan"], self.testnets)
        hit = s.step()
        near = rec.setdefault("near", [])
        for v in s.near:
            if v not in near:
                near.append(v)
        return hit

    def report(self, rec: dict, tx: str) -> dict:
        q = rec["doc"]["quote"]
        try:
            notice = Q.make_paid(buyer_key(self.st.root), q, tx)
        except Q.QuoteError as e:
            raise BuyError(str(e)) from e
        try:
            code, out = self.desk(rec["listing"]).paid(notice)
        except claim.ClaimError as e:
            raise BuyError(str(e)) from e
        rec["tx"], rec["state"] = notice["tx"], "reported"
        self.st.save()
        if code == 200 and out.get("status") in ("admitted", "rejected"):
            return self._settle(rec, out)
        return out

    def status(self, rec: dict) -> dict:
        try:
            out = self.desk(rec["listing"]).status(rec["id"])
        except claim.ClaimError as e:
            raise BuyError(str(e)) from e
        if out.get("status") in ("admitted", "rejected", "expired"):
            return self._settle(rec, out)
        return out

    def _settle(self, rec: dict, out: dict) -> dict:
        q = rec["doc"]["quote"]
        st = out.get("status")
        if st == "admitted":
            rec["state"] = "admitted"
            lease = _ex(self.st)["leases"].setdefault(q["seller"], {"quotes": []})
            lease.update({"seller": q["seller"], "client_sesh": q["client_sesh"], "dns": rec["listing"].get("dns"),
                          "chainid": q["chainid"]})
            lease["end"] = max(int(lease.get("end") or 0), int(out.get("lease_end") or 0))
            if q["id"] not in lease["quotes"]:
                lease["quotes"].append(q["id"])
            lease.pop("reminded", None)
        elif st == "rejected":
            rec["state"] = "rejected"
            rec["reason"] = reason_text(q, out.get("reason"))
            out = dict(out, message=rec["reason"])
        elif st == "expired":
            rec["state"] = "expired"
        self.st.save()
        return out

    def watch(self, rec: dict, on_event=lambda kind, data: None, poll_s: float = 6, tx: str | None = None) -> dict:
        """Wait for the payment (or use tx), report it, wait for admission. Returns the final status."""
        q = rec["doc"]["quote"]
        deadline = q["expires"] + PAID_GRACE_S
        if tx is None and rec.get("tx"):
            tx = rec["tx"]
        while tx is None:
            if self.clock() > deadline and rec.get("scan_error"):
                rec["state"] = "expired"
                rec["reason"] = unreadable_text(q, rec["scan_error"])
                self.st.save()
                return {"status": "unknown", "message": rec["reason"]}
            if self.clock() > deadline:
                other = None
                try:
                    rpcs = {}
                    for cid in rec["listing"].get("chains") or []:
                        if cid != q["chainid"] and C.supported(cid, self.testnets):
                            rpcs[cid] = self.rpc_factory(cid)
                    other = W.other_chain_hit(rpcs, q, list(rpcs), {}, self.testnets)
                except evm.RpcError:
                    pass
                rec["state"] = "expired"
                rec["reason"] = expired_text(q, rec.get("near"), other)
                self.st.save()
                return {"status": "expired", "message": rec["reason"]}
            try:
                hit = self.scan_once(rec)
                rec.pop("scan_error", None)
            except evm.RpcError as e:
                on_event("rpc_error", str(e))
                rec["scan_error"] = str(e)[:200] or "no answer"
                hit = None
            self.st.save()
            if hit:
                tx = hit["tx"]
                on_event("seen", hit)
                break
            on_event("waiting", {"near": rec.get("near") or [], "left": int(q["expires"] - self.clock())})
            self.sleep(poll_s)
        confirm_by = max(deadline, self.clock()) + 600   # a late report still gets its full wait
        if rec["state"] == "open":
            out = self.report(rec, tx)
            on_event("reported", tx)
            if out.get("status") in ("admitted", "rejected"):
                return out
        while True:
            try:
                out = self.status(rec)
            except BuyError as e:
                on_event("desk_error", str(e))
                out = {"status": "waiting"}
            if out.get("status") in ("admitted", "rejected", "expired"):
                return out
            if self.clock() > confirm_by:
                msg = "the seller has not confirmed yet; check again with `srnet paid` in a few minutes"
                if out.get("note") == "chain_unavailable":
                    msg = ("the seller can't read the chain right now; your report is saved there and is "
                           "checked again by itself. Don't pay again; check later with `srnet paid`")
                return {"status": "waiting", "message": msg}
            on_event("confirming", out)
            self.sleep(poll_s)


def leases(st, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    out = []
    for s, l in sorted(_ex(st)["leases"].items()):
        out.append(dict(l, seller=s, left_s=max(0, int(l.get("end", 0) - now)),
                        ending=0 < l.get("end", 0) - now <= REMIND_S))
    return out


def active_lease(st, seller: str, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    l = _ex(st)["leases"].get(seller)
    return l if l and l.get("end", 0) > now else None
