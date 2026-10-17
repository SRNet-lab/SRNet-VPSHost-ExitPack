#!/usr/bin/env python3
"""Seller lease watcher. Leases are prepaid whole hours, final at start.

Turns a buyer's on-chain lease (SRNetBalance.startLease / renewLease to this seller's vendor id) into signed
exit admissions, ONE PAID HOUR AT A TIME, and revokes them as soon as the paid time ends or the seller stops
the lease. Optional auto claimEarned (when --claim-keystore is set) credits payable as each
paid hour begins; otherwise the seller calls payout.py claim|finalize manually.

Claim flow (one JSON file per claim in the spool dir, written locally or by claimd.py when a buyer POSTs it
over Session Router; a renewal is a new lease id with its own claim):
  {"v":1, "hold_id":N, "client_sesh":"<buyer>.sesh", "sig":"0x..."}     (hold_id = lease id)
  sig = EIP-191 personal_sign by the lease's buyer wallet over
        "srnet-lease-claim/v1\n{contract}\n{chainid}\n{hold_id}\n{client_sesh}"
  (contract lowercase 0x hex, chainid decimal).

Admit only when, from holdInfo/leaseTerms/leaseInfo(lease): active (open, before its end), sellerWallet ==
our payout wallet, end in the future, rate per hour >= our listed rate (`min_amount` = minimum rate per hour
in USDC base units), claim signer == buyer, and the lease was never claimed before.

Per paid hour: admission tid = hold-{chainid}-{id}-h{k}, exp = end of paid hour k (start + (k+1) h, capped
at the lease end). A renewal that starts in the future (at the previous lease's end) is admitted from now
until the end of its first hour, so there is no gap between leases. Each poll (reconcile):
  - lease no longer active (seller stopped it, or past its end) -> revoke every admission at once;
  - current admission within `renew_margin` seconds of expiry and the lease still pays the next hour ->
    issue the next hour's admission.
Renewals: each lease can be renewed once (renewedBy[lease] on the contract). When an admitted lease has been
renewed by the same buyer, the watcher follows renewedBy and admits the new lease for the same client without
waiting for a new claim file, so a lost or late renewal claim doesn't cut the buyer off.
Chains: Arbitrum One (42161), plus a local Anvil chain (31337) for tests; anything else is refused.
Chain access is plain JSON-RPC (evm.py, standard library only).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

CLAIM_DOMAIN = "srnet-lease-claim/v1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evm import CHAINS, JsonRpc, decode, encode_call, verify_personal  # noqa: E402,F401
HOUR = 3600
SESH_RE = re.compile(r"^[ybndrfg8ejkmcpqxot1uwisza345h769]{52}\.sesh$")
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HERE = Path(__file__).resolve().parent
# Repo layouts: ../exit-lease or ../contrib/exit-lease; installed layout: admission.py next to this file.
DEFAULT_ADMISSION = next((c for c in (HERE.parent / "exit-lease" / "admission.py",
                                       HERE.parent / "contrib" / "exit-lease" / "admission.py",
                                       HERE / "admission.py") if c.exists()), HERE / "admission.py")


def claim_message(contract: str, chainid: int, hold_id: int, client_sesh: str) -> str:
    return f"{CLAIM_DOMAIN}\n{contract.lower()}\n{int(chainid)}\n{int(hold_id)}\n{client_sesh}"


@dataclass
class Hold:
    user: str
    amount: int
    committed_wallet: str
    deadline: int
    active: bool
    rate_per_hour: int | None = None  # from leaseTerms; None -> fall back to amount (1-hour lease)
    start: int | None = None  # from leaseInfo; None -> deadline - (amount / rate) hours


class Chain(Protocol):
    chainid: int
    contract: str

    def hold_info(self, hold_id: int) -> Hold: ...
    def now(self) -> int: ...
    # optional: def renewed_by(self, hold_id: int) -> int  (0 if not renewed)


class Verifier(Protocol):
    def recover_ok(self, address: str, message: str, sig: str) -> bool: ...


class ClaimEarned(Protocol):
    """Optional: credit seller payable for hours begun on this lease (SRNetBalance.claimEarned)."""

    def claim_earned(self, lease_id: int) -> str: ...


class CastClaimEarned:
    """Send claimEarned(leaseId) via foundry cast using an encrypted keystore + password file."""

    def __init__(self, rpc: str, contract: str, keystore: str, password_file: str):
        self.rpc, self.contract = rpc, contract
        self.keystore, self.password_file = keystore, password_file

    def claim_earned(self, lease_id: int) -> str:
        import shutil
        cast = shutil.which("cast") or str(Path.home() / ".foundry/bin/cast")
        if not Path(cast).exists():
            raise RuntimeError("foundry cast not found for auto claimEarned")
        # Prefer --password-file so the passphrase is never in argv.
        cmd = [cast, "send", self.contract, "claimEarned(uint256)", str(int(lease_id)),
               "--rpc-url", self.rpc, "--keystore", self.keystore,
               "--password-file", self.password_file]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            # already fully claimed this hour is fine
            if "NothingToClaim" in err or "nothing to claim" in err.lower():
                return "nothing_to_claim"
            raise RuntimeError(f"claimEarned({lease_id}) failed: {err[:400]}")
        # return tx hash line if present
        for line in (r.stdout or "").splitlines():
            if line.startswith("0x") and len(line) >= 66:
                return line.strip()
        return (r.stdout or "").strip() or "ok"


# ---------------------------------------------------------------- mock backends
@dataclass
class MockChain:
    chainid: int = 31337
    contract: str = "0x" + "c0" * 20
    holds: dict[int, Hold] = field(default_factory=dict)
    clock: int | None = None
    renewals: dict[int, int] = field(default_factory=dict)

    def renewed_by(self, hold_id: int) -> int:
        return self.renewals.get(hold_id, 0)

    def hold_info(self, hold_id: int) -> Hold:
        return self.holds.get(hold_id, Hold("0x" + "00" * 20, 0, "0x" + "00" * 20, 0, False))

    def now(self) -> int:
        return int(time.time()) if self.clock is None else self.clock


class MockVerifier:
    """Test double: sig must be 'mock:<address lowercase>:<message>'."""

    def recover_ok(self, address: str, message: str, sig: str) -> bool:
        return sig == f"mock:{address.lower()}:{message}"


# ---------------------------------------------------------------- JSON-RPC backends
class RpcVerifier:
    def recover_ok(self, address: str, message: str, sig: str) -> bool:
        return verify_personal(address, message, sig)


class RpcChain:
    def __init__(self, rpc: str, contract: str):
        if not ADDR_RE.match(contract):
            raise ValueError("bad contract address")
        self.rpc, self.contract = JsonRpc(rpc), contract.lower()
        self.chainid = int(self.rpc.call("eth_chainId"), 16)
        if self.chainid not in CHAINS:
            raise SystemExit(f"unsupported chain id {self.chainid}")

    def _call(self, sig: str, out: list[str], *args) -> tuple:
        res = self.rpc.call("eth_call", {"to": self.contract, "data": "0x" + encode_call(sig, *args).hex()}, "latest")
        return decode(out, bytes.fromhex((res or "0x")[2:]))

    def hold_info(self, hold_id: int) -> Hold:
        u, amt, wallet, end, active = self._call("holdInfo(uint256)", ["address", "uint256", "address", "uint256", "bool"],
                                                 hold_id)
        rate, _ = self._call("leaseTerms(uint256)", ["uint256", "uint256"], hold_id)
        info = self._call("leaseInfo(uint256)",
                          ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256", "bool"], hold_id)
        return Hold(u, amt, wallet, end, active, rate_per_hour=rate, start=info[3])

    def renewed_by(self, hold_id: int) -> int:
        return self._call("renewedBy(uint256)", ["uint256"], hold_id)[0]

    def now(self) -> int:
        return int(self.rpc.call("eth_getBlockByNumber", "latest", False)["timestamp"], 16)


# ---------------------------------------------------------------- watcher core
class Watcher:
    def __init__(self, chain: Chain, verifier: Verifier, *, payout_wallet: str, seller_sesh: str,
                 issuer_key: str, store: str, state: str, min_amount: int = 1,
                 provisional_ttl: int = 3600, paid_ttl: int = 3600,
                 admission: str | Path = DEFAULT_ADMISSION, wall_clock=None, renew_margin: int = 120,
                 claim_earned: ClaimEarned | None = None):
        if chain.chainid not in CHAINS:
            raise SystemExit(f"unsupported chain id {chain.chainid}")
        if not ADDR_RE.match(payout_wallet):
            raise ValueError("bad payout wallet")
        if not SESH_RE.match(seller_sesh):
            raise ValueError("bad seller .sesh")
        self.chain, self.verifier = chain, verifier
        self.wallet, self.seller = payout_wallet.lower(), seller_sesh
        self.key, self.store, self.state_path = issuer_key, store, Path(state)
        self.min_amount, self.prov_ttl, self.paid_ttl = min_amount, provisional_ttl, paid_ttl
        self.admission = str(admission)
        self.wall = wall_clock or (lambda: int(time.time()))
        self.renew_margin = int(renew_margin)
        self.claim_earned = claim_earned
        self.state = self._load()

    # state: {"claimed": {lease_id: {"client":..., "tid": current tid, "tids": [...], "exp": current exp,
    #                                 "hour": k, "status": "admitted|ended"}}}
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {"claimed": {}}

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, sort_keys=True, indent=1))
        os.replace(tmp, self.state_path)

    def _adm(self, *args: str) -> str:
        r = subprocess.run([sys.executable, self.admission, *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"admission.py {args[0]} failed: {r.stderr.strip()}")
        return r.stdout

    def tid(self, hold_id: int, suffix: str = "") -> str:
        return f"hold-{self.chain.chainid}-{hold_id}{suffix}"

    def handle_claim(self, claim: dict) -> tuple[bool, str]:
        try:
            if claim.get("v") != 1:
                return False, "bad_version"
            hold_id = claim["hold_id"]
            client = claim["client_sesh"]
            sig = claim["sig"]
            if not isinstance(hold_id, int) or hold_id <= 0:
                return False, "bad_hold_id"
            if not isinstance(client, str) or not SESH_RE.match(client):
                return False, "bad_client"
            if not isinstance(sig, str):
                return False, "bad_sig"
        except KeyError:
            return False, "missing_field"
        prev = self.state["claimed"].get(str(hold_id))
        if prev:
            if prev.get("client") == client:
                # the same claim again (delivered and spooled, or a late claim for a followed renewal)
                return True, prev.get("tid", "already_admitted")
            return False, "hold_already_claimed"
        h = self.chain.hold_info(hold_id)
        chain_now = self.chain.now()
        if not h.active:
            return False, "hold_not_active"
        if h.committed_wallet.lower() != self.wallet:
            return False, "hold_not_for_this_seller"
        if h.deadline <= chain_now:
            return False, "hold_expired"
        rate = h.rate_per_hour if h.rate_per_hour is not None else h.amount
        if rate < self.min_amount:
            return False, "rate_below_price"
        msg = claim_message(self.chain.contract, self.chain.chainid, hold_id, client)
        if not self.verifier.recover_ok(h.user, msg, sig):
            return False, "claim_sig_not_hold_user"
        k, exp = self.paid_hour(h, chain_now)
        c = {"client": client, "tids": [], "status": "admitted", "user": h.user.lower()}
        tid = self._issue_hour(hold_id, c, k, exp, chain_now)
        self.state["claimed"][str(hold_id)] = c
        self._save()
        return True, tid

    @staticmethod
    def _start(h: Hold) -> int:
        if h.start is not None:
            return h.start
        hours = max(1, h.amount // h.rate_per_hour) if h.rate_per_hour else 1
        return h.deadline - hours * HOUR

    @staticmethod
    def paid_hour(h: Hold, now: int) -> tuple[int, int]:
        """(k, exp): the paid hour that covers `now` (hour 0 if the lease starts later) and its end."""
        start = Watcher._start(h)
        k = 0 if now < start else (now - start) // HOUR
        return k, min(start + (k + 1) * HOUR, h.deadline)

    def _issue_hour(self, hold_id: int, c: dict, k: int, exp: int, now: int) -> str:
        tid = self.tid(hold_id, f"-h{k}")
        self._adm("issue", "--key", self.key, "--store", self.store, "--seller", self.seller,
                  "--client", c["client"], "--ttl", str(max(1, exp - now)), "--tid", tid)
        c.update({"tid": tid, "exp": exp, "hour": k})
        c.setdefault("tids", []).append(tid)
        if self.claim_earned is not None:
            try:
                tx = self.claim_earned.claim_earned(hold_id)
                c["last_claim_earned"] = tx
                print(f"claimEarned {hold_id}: {tx}", flush=True)
            except Exception as e:
                # Admission already issued; log and continue so the buyer is not cut off.
                c["last_claim_earned_error"] = f"{e.__class__.__name__}: {e}"
                print(f"claimEarned {hold_id} FAILED: {e}", flush=True)
        return tid

    def _follow_renewals(self, now: int) -> list[str]:
        """Admit leases that renew an admitted lease (same buyer, this seller, rate ok) for the same client."""
        if not hasattr(self.chain, "renewed_by"):
            return []
        events = []
        for hid_s, c in list(self.state["claimed"].items()):
            if c.get("next") or c["status"] not in ("admitted", "ended"):
                continue
            nxt = int(self.chain.renewed_by(int(hid_s)) or 0)
            if not nxt:
                continue
            c["next"] = nxt
            if str(nxt) in self.state["claimed"]:
                continue
            h = self.chain.hold_info(nxt)
            buyer = c.get("user") or self.chain.hold_info(int(hid_s)).user.lower()
            rate = h.rate_per_hour if h.rate_per_hour is not None else h.amount
            if (h.user.lower() != buyer or h.committed_wallet.lower() != self.wallet or not h.active
                    or h.deadline <= now or rate < self.min_amount):
                events.append(f"renewal {nxt} of {hid_s} refused")
                continue
            k, exp = self.paid_hour(h, now)
            nc = {"client": c["client"], "tids": [], "status": "admitted", "user": buyer, "via": "renewal",
                  "prev": int(hid_s)}
            tid = self._issue_hour(nxt, nc, k, exp, now)
            self.state["claimed"][str(nxt)] = nc
            events.append(f"renewal {nxt} of {hid_s} {tid}")
        return events

    def reconcile(self) -> list[str]:
        """Per paid hour: revoke when the lease is no longer active (seller stopped / paid time over);
        otherwise roll the admission to the next paid hour shortly before the current one expires."""
        events = self._follow_renewals(self.chain.now())
        for hid_s, c in list(self.state["claimed"].items()):
            if c["status"] not in ("admitted", "provisional", "paid"):
                continue
            hid = int(hid_s)
            h = self.chain.hold_info(hid)
            now = self.chain.now()
            if not h.active or h.deadline <= now:
                for t in (c.get("tids") or [c["tid"]]):
                    self._adm("revoke", "--store", self.store, "--tid", t)
                c["status"] = "ended"
                events.append(f"ended {hid}")
                continue
            exp = int(c.get("exp") or 0)
            if exp and exp < h.deadline and exp - now <= self.renew_margin:
                k = int(c.get("hour", 0)) + 1
                start = self._start(h)
                nexp = min(start + (k + 1) * HOUR, h.deadline)
                tid = self._issue_hour(hid, c, k, nexp, now)
                events.append(f"hour {hid} {tid}")
        if events:
            self._save()
        return events

    def poll_spool(self, spool: Path) -> list[tuple[str, bool, str]]:
        out = []
        done, bad = spool / "done", spool / "rejected"
        done.mkdir(exist_ok=True); bad.mkdir(exist_ok=True)
        for f in sorted(spool.glob("*.json")):
            try:
                ok, why = self.handle_claim(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError) as e:
                ok, why = False, f"unreadable: {e.__class__.__name__}"
            f.replace((done if ok else bad) / f.name)
            (done if ok else bad).joinpath(f.name + ".result").write_text(why + "\n")
            out.append((f.name, ok, why))
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rpc", required=True, help="JSON-RPC URL")
    ap.add_argument("--contract", required=True)
    ap.add_argument("--payout-wallet", required=True)
    ap.add_argument("--seller", required=True, help="this exit's .sesh")
    ap.add_argument("--issuer-key", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--spool", required=True)
    ap.add_argument("--min-amount", "--min-rate-per-hour", dest="min_amount", type=int, default=1,
                    help="minimum USDC base units per hour (listing price_per_hour_usdc x 1e6)")
    ap.add_argument("--provisional-ttl", type=int, default=3600)
    ap.add_argument("--paid-ttl", type=int, default=3600)
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--renew-margin", type=int, default=120,
                    help="issue the next paid hour's admission this many seconds before the current one expires")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--claim-keystore", default=os.environ.get("CLAIM_KEYSTORE", ""),
                    help="encrypted keystore for sellerWallet; enables auto claimEarned each paid hour")
    ap.add_argument("--claim-keystore-password-file",
                    default=os.environ.get("CLAIM_KEYSTORE_PASSWORD_FILE", ""),
                    help="file whose first line is the keystore password (or set CLAIM_KEYSTORE_PASSWORD_FILE)")
    a = ap.parse_args(argv)
    claimer = None
    if a.claim_keystore:
        if not a.claim_keystore_password_file:
            raise SystemExit("--claim-keystore requires --claim-keystore-password-file (or CLAIM_KEYSTORE_PASSWORD_FILE)")
        claimer = CastClaimEarned(a.rpc, a.contract, a.claim_keystore, a.claim_keystore_password_file)
    w = Watcher(RpcChain(a.rpc, a.contract), RpcVerifier(), payout_wallet=a.payout_wallet,
                seller_sesh=a.seller, issuer_key=a.issuer_key, store=a.store, state=a.state,
                min_amount=a.min_amount, provisional_ttl=a.provisional_ttl, paid_ttl=a.paid_ttl,
                renew_margin=a.renew_margin, claim_earned=claimer)
    spool = Path(a.spool); spool.mkdir(parents=True, exist_ok=True)
    while True:
        for name, ok, why in w.poll_spool(spool):
            print(f"claim {name}: {'ADMIT' if ok else 'REFUSE'} {why}", flush=True)
        for ev in w.reconcile():
            print(ev, flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
