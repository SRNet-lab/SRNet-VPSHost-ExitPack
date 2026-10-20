#!/usr/bin/env python3
"""Seller payment watcher: turns paid quotes into exit admissions.

Buyers pay a quote (claimd.py / quote.py) straight to the seller's payout address in USDC, then
report the tx hash. claimd.py drops each report into STATE/spool/. For each one the watcher reads
the transaction receipt from that chain and admits the buyer only if all of these hold:

  - the quote is ours and was issued for this buyer (buyer key signature, bound .sesh address);
  - the tx succeeded and has enough confirmations;
  - it contains a Transfer of the official USDC (chains.py) to our payout address of exactly
    the quoted amount;
  - its block time is inside the quote window (issued .. expires, with a little clock slack);
  - neither the tx nor the quote has been used before.

The buyer is then admitted for the quoted hours, added on top of any time they still have
(admission tid q-<quote id>). Admissions carry their own expiry, so nothing has to be revoked
when time runs out. A report whose tx isn't mined yet is retried until 15 minutes after the
quote expires, then refused as no_matching_payment. Results go to STATE/status/<quote id>.json,
which the buyer polls through claimd.

  watcher.py --dir STATE --seller <exit>.sesh --issuer-key issuer.key --store admissions.json \
             [--rpc 42161=https://... --rpc 8453=https://...] [--confirmations 2] [--testnets]
  watcher.py stop --dir STATE --store admissions.json --client <buyer>.sesh   # end a buyer's time now

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
import quote as Q  # noqa: E402

HOUR = 3600
PAID_GRACE_S = 900
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
    def __init__(self, chains, *, state_dir: str | Path, seller_sesh: str, issuer_key: str, store: str,
                 confirmations: int = 2, testnets: bool = False, admission=None, clock=time.time):
        if not Q.SESH_RE.match(seller_sesh):
            raise ValueError("bad seller .sesh")
        self.chains, self.dir, self.seller = chains, Path(state_dir), seller_sesh
        for sub in ("quotes", "spool", "status", "spool/done", "spool/rejected"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.key, self.store = issuer_key, store
        self.conf, self.testnets, self.clock = max(1, int(confirmations)), testnets, clock
        self.adm = admission or load_admission()
        self.state_path = self.dir / "watcher-state.json"
        self.state = self._load()

    # state: {"used_tx": {tx: quote_id}, "done": {quote_id: tx}, "clients": {sesh: {"end", "tids"}}}
    def _load(self) -> dict:
        try:
            st = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            st = {}
        for k in ("used_tx", "done", "clients"):
            st.setdefault(k, {})
        return st

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, sort_keys=True, indent=1))
        os.replace(tmp, self.state_path)

    def _status(self, qid: str, **st) -> None:
        p = self.dir / "status" / f"{qid}.json"
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(json.dumps({**st, "t": int(self.clock())}, sort_keys=True))
        os.replace(tmp, p)

    def _quote(self, qid: str) -> dict | None:
        if not Q.ID_RE.match(qid or ""):
            return None
        try:
            return json.loads((self.dir / "quotes" / f"{qid}.json").read_text())["doc"]["quote"]
        except (OSError, ValueError, KeyError):
            return None

    def handle(self, notice: dict) -> tuple[str, str]:
        """('admit'|'reject'|'wait', detail) for one paid notice."""
        now = int(self.clock())
        qid = notice.get("quote_id") if isinstance(notice, dict) else None
        q = self._quote(qid) if isinstance(qid, str) else None
        if not q:
            return "reject", "unknown_quote"
        if q["seller"] != self.seller:
            return "reject", "quote_for_other_seller"
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
        late = now > q["expires"] + PAID_GRACE_S
        try:
            rpc = self.chains.get(q["chainid"])
            rc = rpc.receipt(tx)
            head = rpc.block_number() if rc else None
            btime = rpc.block_time(int(rc["blockNumber"], 16)) if rc else None
        except (evm.RpcError, KeyError, TypeError, ValueError) as e:
            return ("reject", "no_matching_payment") if late else ("wait", f"chain_unavailable: {e}")
        if not rc:
            return ("reject", "no_matching_payment") if late else ("wait", "not_mined_yet")
        if head - int(rc["blockNumber"], 16) + 1 < self.conf:
            return ("reject", "no_matching_payment") if late else ("wait", "waiting_for_confirmations")
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
        for f in sorted(spool.glob("*.json")):
            try:
                notice = json.loads(f.read_text())
                verdict, why = self.handle(notice)
            except (OSError, ValueError) as e:
                notice, verdict, why = {}, "reject", f"unreadable: {e.__class__.__name__}"
            out.append((f.name, verdict, why))
            if verdict == "wait":
                continue
            dest = spool / ("done" if verdict == "admit" else "rejected")
            f.replace(dest / f.name)
            (dest / (f.name + ".result")).write_text(why + "\n")
            qid = notice.get("quote_id") if isinstance(notice, dict) else None
            if verdict == "reject" and isinstance(qid, str) and self._quote(qid) and qid not in self.state["done"]:
                self._status(qid, status="rejected", reason=why, tx=notice.get("tx"))
        return out

    def tidy(self) -> int:
        """Drop expired admissions and buyers whose time is over."""
        now = int(self.clock())
        n = self.adm.Store(self.store).prune(now)
        gone = [s for s, c in self.state["clients"].items() if int(c["end"]) <= now]
        for s in gone:
            del self.state["clients"][s]
        if gone:
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
        a = ap.parse_args(argv[1:])
        st = json.loads((Path(a.dir) / "watcher-state.json").read_text())
        c = st["clients"].pop(a.client, None)
        adm = load_admission()
        n = sum(1 for t in (c or {}).get("tids", []) if adm.Store(a.store).revoke(t))
        (Path(a.dir) / "watcher-state.json").write_text(json.dumps(st, sort_keys=True, indent=1))
        print(f"revoked {n} admission(s) for {a.client}")
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="state directory shared with claimd.py")
    ap.add_argument("--seller", required=True, help="this exit's .sesh")
    ap.add_argument("--issuer-key", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--rpc", action="append", default=[], metavar="CHAINID=URL",
                    help="RPC per chain (default: the public endpoints in chains.py)")
    ap.add_argument("--confirmations", type=int, default=2)
    ap.add_argument("--testnets", action="store_true")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    w = Watcher(RpcChains(parse_rpcs(a.rpc), a.testnets), state_dir=a.dir, seller_sesh=a.seller,
                issuer_key=a.issuer_key, store=a.store, confirmations=a.confirmations, testnets=a.testnets)
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
