#!/usr/bin/env python3
"""Seller payout commands (Arbitrum One).

Vendor wallet calls SRNetBalance.withdrawVendor() to pull accrued payable to itself.
Arbitrum One uses real USDC; the contract is unaudited, keep amounts small. Fee settlement is out of scope for this pack.

  payout.py balance --rpc URL --contract 0x... --wallet 0x...
  payout.py withdraw --rpc URL --contract 0x... --keystore PATH [--dry-run]
  payout.py claim    --rpc URL --contract 0x... --keystore PATH --lease N  # credit every hour begun (1-hour lease: all of it, at once)
  payout.py cancel   --rpc URL --contract 0x... --keystore PATH --lease N  # SELLER stops; keeps hours begun, buyer gets unused whole hours
  payout.py finalize --rpc URL --contract 0x... --keystore PATH --lease N  # after the end, pays seller in full (anyone may call)
(leases are prepaid whole hours, final at start (no buyer cancel); each hour is earned the moment
it begins; credited amounts land in payableOf(wallet) and leave via withdraw.)
  payout.py mock-balance --state FILE     # offline mock used by tests

Signing key (claim / cancel / finalize / withdraw): pick ONE of
  --keystore PATH   encrypted foundry/geth keystore (recommended; cast asks for the password)
  --ledger          hardware wallet
  --interactive     cast prompts for the raw key (nothing stored, nothing in argv)
  --private-key HEX plaintext key on the command line: visible in `ps` and shell history (warned)
The default is --interactive. This pack never stores a wallet key; the exit and watcher do not need one.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SUPPORTED_CHAINS = {42161, 31337}  # Arbitrum One; 31337 = local Anvil for tests


def _cast() -> str:
    c = shutil.which("cast") or str(Path.home() / ".foundry/bin/cast")
    if not Path(c).exists():
        raise SystemExit("foundry `cast` not found")
    return c


def chain_id(rpc: str) -> int:
    r = subprocess.run([_cast(), "chain-id", "--rpc-url", rpc], capture_output=True, text=True, check=True)
    return int(r.stdout.strip())


def refuse_unsupported(rpc: str) -> int:
    cid = chain_id(rpc)
    if cid not in SUPPORTED_CHAINS:
        raise SystemExit(f"unsupported chain id {cid}")
    return cid


refuse_mainnet = refuse_unsupported  # old name


def balance(rpc: str, contract: str, wallet: str) -> int:
    refuse_unsupported(rpc)
    r = subprocess.run([_cast(), "call", contract, "payableOf(address)(uint256)", wallet, "--rpc-url", rpc],
                       capture_output=True, text=True, check=True)
    return int(r.stdout.strip().split()[0])


def signer_args(a) -> list[str]:
    if getattr(a, "private_key", None):
        print("payout.py: WARNING: --private-key puts a plaintext wallet key in argv (visible in ps / shell "
              "history). Prefer --keystore, --ledger or --interactive.", file=sys.stderr)
        return ["--private-key", a.private_key]
    if getattr(a, "keystore", None):
        return ["--keystore", a.keystore]
    if getattr(a, "ledger", False):
        return ["--ledger"]
    return ["--interactive"]


def withdraw(rpc: str, contract: str, pk, dry_run: bool) -> str:
    refuse_unsupported(rpc)
    if dry_run:
        return f"DRY_RUN: would send withdrawVendor() to {contract} on chain {chain_id(rpc)}"
    r = subprocess.run([_cast(), "send", contract, "withdrawVendor()", *_signer(pk), "--rpc-url", rpc],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise SystemExit(f"withdraw failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _signer(pk) -> list[str]:
    # pk: list of cast signer args (from signer_args) or, for older callers, a raw key string.
    return list(pk) if isinstance(pk, (list, tuple)) else ["--private-key", pk]


def _send(rpc: str, contract: str, pk, sig: str, *args: str) -> str:
    refuse_unsupported(rpc)
    # stdin stays attached so `cast --interactive` / keystore password prompts work.
    r = subprocess.run([_cast(), "send", contract, sig, *args, *_signer(pk), "--rpc-url", rpc],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{sig} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("balance"); b.add_argument("--rpc", required=True); b.add_argument("--contract", required=True)
    b.add_argument("--wallet", required=True)
    w = sub.add_parser("withdraw"); w.add_argument("--rpc", required=True); w.add_argument("--contract", required=True)
    w.add_argument("--dry-run", action="store_true")
    def signer(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--keystore"); g.add_argument("--ledger", action="store_true")
        g.add_argument("--interactive", action="store_true"); g.add_argument("--private-key")
    signer(w)
    for name in ("claim", "cancel", "finalize"):
        p = sub.add_parser(name); p.add_argument("--rpc", required=True); p.add_argument("--contract", required=True)
        signer(p)
        p.add_argument("--lease", "--hold", dest="lease", required=True, type=int)
    m = sub.add_parser("mock-balance"); m.add_argument("--state", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "balance":
        print(balance(a.rpc, a.contract, a.wallet)); return 0
    if a.cmd == "withdraw":
        print(withdraw(a.rpc, a.contract, signer_args(a), a.dry_run)); return 0
    sigs = {"claim": "claimEarned(uint256)", "cancel": "cancel(uint256)", "finalize": "finalize(uint256)"}
    if a.cmd in sigs:
        print(_send(a.rpc, a.contract, signer_args(a), sigs[a.cmd], str(a.lease))); return 0
    # mock-balance: offline readable mock for local tests
    st = json.loads(Path(a.state).read_text()) if Path(a.state).exists() else {"payable": 0}
    print(int(st.get("payable", 0))); return 0


if __name__ == "__main__":
    sys.exit(main())
