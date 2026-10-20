#!/usr/bin/env python3
"""Seller payment watcher: turns paid quotes into exit admissions.

Buyers pay a quote (claimd.py / quote.py) straight to the seller's payout address in USDC, then
report the tx hash. claimd.py drops each report into STATE/spool/. For each one the watcher reads
the transaction receipt from that chain and admits the buyer only if all of these hold:

  - the quote is ours and was issued for this buyer (buyer key signature, bound .sesh address);
  - the quote agrees with our signed listing (quote key signature, payout address, chain, USDC,
    price, max hours, lifetime). The watcher reads the listing itself, from a file the claim
    desk can't write, and keeps the terms of earlier listings it has seen (and the installer's
    listing/previous.json), so a quote made just before a price change still counts. So even a
    claim desk that has been taken over can't make the watcher admit anyone on its own terms;
  - the tx succeeded and has enough confirmations (per chain: Arbitrum 20 blocks, Base 5);
  - it contains a Transfer of the official USDC (chains.py) to our payout address of exactly
    the quoted amount;
  - its block time is inside the quote window (issued - 5 min .. expires + 2 min);
  - neither the tx nor the quote has been used before.

The buyer is then admitted for the quoted hours, added on top of any time they still have
(admission tid q-<quote id>). Admissions carry their own expiry, so nothing has to be revoked
when time runs out.

A report is only ever refused on a definite answer from the chain (failed tx, wrong token,
address or amount, block time outside the window) or when its quote is older than
quote.KEEP_QUOTES_S. If the RPC is down, the watcher keeps retrying, backing off per report
(10 s, 1 min, then every 5 min). A tx the chain doesn't know yet is retried for a day after
the report arrived. Results go to STATE/status/<quote id>.json, which the buyer polls through
claimd; while the chain can't be read, the status says so ("waiting", "chain_unavailable").

  watcher.py --dir STATE --seller <exit>.sesh --listing listing.json --issuer-key issuer.key \
             --store admissions.json [--rpc 42161=https://... --rpc 8453=https://...]
             [--confirmations N] [--testnets]
             [--state watcher-state.json]
  watcher.py stop --dir STATE --store admissions.json --client <buyer>.sesh [--state F]  # end a buyer's time now
              (stop the service first: the running watcher keeps its own copy of the state)

Chain access is plain read-only JSON-RPC (evm.py). The watcher never holds a wallet key.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chains as C  # noqa: E402
import evm  # noqa: E402
import listing as L  # noqa: E402
import quote as Q  # noqa: E402

HOUR = 3600
NOT_MINED_S = 86400              # retry a tx the chain doesn't know for this long after it was reported
BACKOFF_S = (10, 60, 300)        # per-report retry delays while waiting
LISTING_SKEW_S = 300             # a quote may be issued this long after its listing was replaced
HERE = Path(__file__).resolve().parent
# Repo layouts: ../contrib/exit-lease; installed layout: admission.py next to this file.
DEFAULT_ADMISSION = next((c for c in (HERE.parent / "contrib" / "exit-lease" / "admission.py",
                                       HERE / "admission.py") if c.exists()), HERE / "admission.py")


def load_admission(path: str | Path = DEFAULT_ADMISSION):
    spec = importlib.util.spec_from_file_location("srnet_admission", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RpcChains:
    """chainid -> JsonRpc, checking once that each endpoint is really that chain."""

    def __init__(self, overrides: dict | None = None, testnets: bool = False):
        self.overrides, self.testnets = overrides or {}, testnets
        self.rpcs: dict[int, evm.JsonRpc] = {}

    def get(self, chainid: int) -> evm.JsonRpc:
        if chainid not in self.rpcs:
            r = evm.JsonRpc(C.rpc_url(chainid, self.overrides, self.testnets), agent="srnet-seller")
            got = r.chain_id()
            if got != chainid:
                raise evm.RpcError(f"rpc for chain {chainid} reports chain {got}")
            self.rpcs[chainid] = r
        return self.rpcs[chainid]


class Watcher:
    def __init__(self, chains, *, state_dir: str | Path, seller_sesh: str, listing: str | Path, issuer_key: str,
                 store: str, confirmations: int | None = None, testnets: bool = False, admission=None,
                 clock=time.time, state_file: str | Path | None = None):
        if not Q.SESH_RE.match(seller_sesh):
            raise ValueError("bad seller .sesh")
        self.chains, self.dir, self.seller = chains, Path(state_dir), seller_sesh
        for sub in ("quotes", "spool", "status", "spool/done", "spool/rejected"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.key, self.store = issuer_key, store
        self.conf = None if confirmations is None else max(1, int(confirmations))
        self.testnets, self.clock = testnets, clock
        self.adm = admission or load_admission()
        # Used payments live outside the claims dir when the installer says so, where the
        # network-facing claim desk can't reach them.
        self.state_path = Path(state_file) if state_file else self.dir / "watcher-state.json"
        self.state = self._load()
        self.listing_path = Path(listing)
        self._listing_seen: tuple | None = None
        self._note_listing()

    def _note_listing(self) -> None:
        """Look at the listing now, so a later price change keeps the old terms in the history."""
        try:
            self.listings()
        except (OSError, ValueError, KeyError, TypeError, C.ChainError) as e:
            print(f"watcher: can't use the listing yet: {e}", file=sys.stderr, flush=True)

    # state: {"used_tx": {tx: quote_id}, "done": {quote_id: tx}, "clients": {sesh: {"end", "tids"}},
    #         "retry": {spool file name: {"first", "next", "n"}},
    #         "listings": [{"terms": quote.listing_terms(), "until": time it was replaced | None}]}
    def _load(self) -> dict:
        try:
            st = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            st = {}
        for k in ("used_tx", "done", "clients", "retry"):
            st.setdefault(k, {})
        st.setdefault("listings", [])
        return st

    def _read_listing(self, path: Path) -> dict:
        doc = json.loads(path.read_text())
        if not L.verify(doc):
            raise ValueError("listing signature is invalid")
        body = doc["listing"]
        why = L.listing_error(body, self.testnets)
        if why:
            raise ValueError(f"listing can't take payments: {why}")
        if body["seller"] != self.seller:
            raise ValueError("listing is for another seller")
        return Q.listing_terms(body)

    def listings(self, now: int | None = None) -> list[dict]:
        """Terms of the current listing and of earlier ones (with the time they were replaced).
        Raises ValueError if the current listing can't be read or doesn't verify."""
        now = int(self.clock()) if now is None else int(now)
        st = self.listing_path.stat()
        seen = (st.st_mtime_ns, st.st_size)
        hist = self.state.setdefault("listings", [])
        if seen != self._listing_seen or not hist:
            terms = self._read_listing(self.listing_path)
            dirty = False
            prev = self.listing_path.with_name("previous.json")
            if prev.exists():  # the installer keeps the listing it replaced here
                try:
                    pt = self._read_listing(prev)
                    if pt != terms and not any(h["terms"] == pt for h in hist):
                        hist.insert(0, {"terms": pt, "until": int(prev.stat().st_mtime)})
                        dirty = True
                except (OSError, ValueError, KeyError, TypeError, C.ChainError):
                    pass
            cur = next((h for h in hist if h["until"] is None), None)
            if cur is None or cur["terms"] != terms:
                if cur is not None:
                    cur["until"] = now
                hist[:] = [h for h in hist if h["terms"] != terms]
                hist.append({"terms": terms, "until": None})
                dirty = True
            keep = [h for h in hist if h["until"] is None or h["until"] >= now - Q.KEEP_QUOTES_S]
            if len(keep) != len(hist):
                hist[:] = keep
                dirty = True
            if dirty:
                self._save()
            self._listing_seen = seen
        return hist

    def terms_error(self, doc, now: int) -> str | None:
        """None if some listing that was current when the quote was issued could have made it."""
        q = doc["quote"]
        if not isinstance(q.get("issued"), int) or isinstance(q.get("issued"), bool):
            return "bad quote times"
        hist = self.listings(now)
        why = "no listing"
        for h in reversed(hist):
            if h["until"] is not None and q["issued"] > h["until"] + LISTING_SKEW_S:
                continue
            why = Q.terms_error(doc, h["terms"], self.testnets)
            if not why:
                return None
        return why

    def _save(self) -> None:
        write_durable(self.state_path, json.dumps(self.state, sort_keys=True, indent=1))

    def _status(self, qid: str, **st) -> None:
        write_durable(self.dir / "status" / f"{qid}.json", json.dumps({**st, "t": int(self.clock())}, sort_keys=True))

    def confirmations(self, chainid: int) -> int:
        return self.conf if self.conf is not None else C.confirmations(chainid, self.testnets)

    def _quote_doc(self, qid: str) -> dict | None:
        if not Q.ID_RE.match(qid or ""):
            return None
        try:
            doc = json.loads((self.dir / "quotes" / f"{qid}.json").read_text())["doc"]
            return doc if isinstance(doc, dict) and isinstance(doc.get("quote"), dict) else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _quote(self, qid: str) -> dict | None:
        doc = self._quote_doc(qid)
        return doc["quote"] if doc else None

    def handle(self, notice: dict, first_seen: int | None = None) -> tuple[str, str]:
        """('admit'|'reject'|'wait', detail) for one paid notice. first_seen: when it was reported."""
        now = int(self.clock())
        first_seen = now if first_seen is None else int(first_seen)
        qid = notice.get("quote_id") if isinstance(notice, dict) else None
        doc = self._quote_doc(qid) if isinstance(qid, str) else None
        if not doc:
            return "reject", "unknown_quote"
        q = doc["quote"]
        if q.get("seller") != self.seller:
            return "reject", "quote_for_other_seller"
        try:
            why = self.terms_error(doc, now)
        except (OSError, ValueError, KeyError, TypeError, C.ChainError) as e:
            return "wait", f"listing_unavailable: {e}"
        if why:
            return "reject", f"quote_does_not_match_listing: {why}"
        why = Q.paid_error(notice, q)
        if why:
            return "reject", "bad_buyer_signature"
        tx = notice["tx"]
        if qid in self.state["done"]:
            if self.state["done"][qid] == tx:
                return "admit", "already_admitted"
            return "reject", "quote_already_paid"
        if tx in self.state["used_tx"]:
            return "reject", "payment_already_used"
        if now - q["issued"] > Q.KEEP_QUOTES_S:
            return "reject", "quote_too_old"
        try:
            rpc = self.chains.get(q["chainid"])
            rc = rpc.receipt(tx)
            if rc:
                head = rpc.block_number()
                bn = int(rc["blockNumber"], 16)
                btime = rpc.block_time(bn)
        except (evm.RpcError, KeyError, TypeError, ValueError) as e:
            return "wait", f"chain_unavailable: {e}"
        if not rc:
            if now - first_seen > NOT_MINED_S:
                return "reject", "no_matching_payment: the chain does not know that transaction"
            return "wait", "not_mined_yet"
        if head - bn + 1 < self.confirmations(q["chainid"]):
            return "wait", "waiting_for_confirmations"
        why = Q.payment_error(q, evm.receipt_transfers(rc), btime, evm.receipt_ok(rc), self.testnets)
        if why:
            return "reject", f"no_matching_payment: {why}"
        return "admit", self._admit(q, tx, now)

    def _admit(self, q: dict, tx: str, now: int) -> str:
        c = self.state["clients"].setdefault(q["client_sesh"], {"end": 0, "tids": []})
        end = max(now, int(c["end"])) + int(q["hours"]) * HOUR
        tid = f"q-{q['id']}"
        try:
            self.adm.issue(self.key, self.store, self.seller, q["client_sesh"], end - now, tid=tid, now=now)
        except KeyError:  # issued before a crash, state not saved: replace it
            self.adm.Store(self.store).revoke(tid)
            self.adm.issue(self.key, self.store, self.seller, q["client_sesh"], end - now, tid=tid, now=now)
        c["end"], c["tids"] = end, [t for t in c["tids"] if t != tid][-50:] + [tid]
        self.state["used_tx"][tx] = q["id"]
        self.state["done"][q["id"]] = tx
        self._save()
        self._status(q["id"], status="admitted", tx=tx, chainid=q["chainid"], hours=q["hours"], lease_end=end)
        return tid

    def poll_spool(self) -> list[tuple[str, str, str]]:
        out = []
        spool = self.dir / "spool"
        now = int(self.clock())
        retry = self.state["retry"]
        dirty = False
        self._note_listing()
        for f in sorted(spool.glob("*.json")):
            r = retry.get(f.name)
            if r and now < r["next"]:
                continue
            try:
                notice = json.loads(f.read_text())
                verdict, why = self.handle(notice, first_seen=r["first"] if r else now)
            except (OSError, ValueError) as e:
                notice, verdict, why = {}, "reject", f"unreadable: {e.__class__.__name__}"
            out.append((f.name, verdict, why))
            qid = notice.get("quote_id") if isinstance(notice, dict) else None
            if verdict == "wait":
                r = r or {"first": now, "n": 0}
                r["next"] = now + BACKOFF_S[min(r["n"], len(BACKOFF_S) - 1)]
                r["n"] += 1
                retry[f.name] = r
                dirty = True
                if isinstance(qid, str) and qid not in self.state["done"]:
                    self._status(qid, status="waiting", note=why.partition(":")[0], tx=notice.get("tx"))
                continue
            if retry.pop(f.name, None) is not None:
                dirty = True
            dest = spool / ("done" if verdict == "admit" else "rejected")
            f.replace(dest / f.name)
            (dest / (f.name + ".result")).write_text(why + "\n")
            if verdict == "reject" and isinstance(qid, str) and self._quote(qid) and qid not in self.state["done"]:
                self._status(qid, status="rejected", reason=why, tx=notice.get("tx"))
        if dirty:
            self._save()
        return out

    def tidy(self) -> int:
        """Drop expired admissions and buyers whose time is over."""
        now = int(self.clock())
        n = self.adm.Store(self.store).prune(now)
        gone = [s for s, c in self.state["clients"].items() if int(c["end"]) <= now]
        for s in gone:
            del self.state["clients"][s]
        stale = [k for k in self.state["retry"] if not (self.dir / "spool" / k).exists()]
        for k in stale:
            del self.state["retry"][k]
        if gone or stale:
            self._save()
        return n

    def stop(self, client: str) -> int:
        c = self.state["clients"].pop(client, None)
        if not c:
            return 0
        st = self.adm.Store(self.store)
        n = sum(1 for t in c["tids"] if st.revoke(t))
        self._save()
        return n


def write_durable(path: Path, text: str) -> None:
    """Write via a temp file that is fsynced before it replaces path."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def parse_rpcs(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep or not v.startswith(("http://", "https://")):
            raise SystemExit(f"--rpc wants CHAINID=URL, got {it!r}")
        out[str(int(k))] = v
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["stop"]:
        ap = argparse.ArgumentParser(prog="watcher.py stop")
        ap.add_argument("--dir", required=True)
        ap.add_argument("--store", required=True)
        ap.add_argument("--client", required=True)
        ap.add_argument("--state", help="watcher state file (default STATE/watcher-state.json)")
        a = ap.parse_args(argv[1:])
        sp = Path(a.state) if a.state else Path(a.dir) / "watcher-state.json"
        st = json.loads(sp.read_text())
        c = st["clients"].pop(a.client, None)
        adm = load_admission()
        n = sum(1 for t in (c or {}).get("tids", []) if adm.Store(a.store).revoke(t))
        write_durable(sp, json.dumps(st, sort_keys=True, indent=1))
        print(f"revoked {n} admission(s) for {a.client}")
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="state directory shared with claimd.py")
    ap.add_argument("--seller", required=True, help="this exit's .sesh")
    ap.add_argument("--listing", required=True, help="this exit's signed listing.json (not writable by claimd)")
    ap.add_argument("--issuer-key", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--rpc", action="append", default=[], metavar="CHAINID=URL",
                    help="RPC per chain (default: the public endpoints in chains.py)")
    ap.add_argument("--confirmations", type=int, default=None,
                    help="blocks to wait for on every chain (default per chain: Arbitrum 20, Base 5)")
    ap.add_argument("--testnets", action="store_true")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--state", help="watcher state file: used payments (default STATE/watcher-state.json)")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    w = Watcher(RpcChains(parse_rpcs(a.rpc), a.testnets), state_dir=a.dir, seller_sesh=a.seller,
                listing=a.listing, issuer_key=a.issuer_key, store=a.store, confirmations=a.confirmations, testnets=a.testnets,
                state_file=a.state)
    n = 0
    while True:
        for name, verdict, why in w.poll_spool():
            if verdict != "wait":
                print(f"paid {name}: {verdict.upper()} {why}", flush=True)
        if n % 30 == 0:
            w.tidy()
        n += 1
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
