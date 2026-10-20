#!/usr/bin/env python3
"""Take out USDC left in the old SRNet balance on Arbitrum One (before direct payments).

  python3 cashout.py check [--address 0x...]      read-only: what is left, and the ETH for gas
  python3 cashout.py send [--keystore FILE]        send it back to the same wallet (asks first)

The old client kept a wallet in ~/.srnet/keystore.json; this script opens it with your passphrase,
then calls cashOut (unused balance) and withdrawPayable (seller earnings) from that wallet. The
wallet pays the network fee itself, so it needs a little ETH on Arbitrum One (a few cents).
Nothing is sent without your "y". Needs: pip install "eth-account>=0.11"

Kept until 2027-01-18.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "seller"))
import evm  # noqa: E402

CONTRACT = "0x7E0d01B7dd916BdAB96B0971ab92D160E1664431"
CHAIN_ID = 42161
RPC = os.environ.get("SRNET_ARB_RPC", "https://arb1.arbitrum.io/rpc")
USDC = 10 ** 6
GAS_LIMIT = 120_000


class Rpc:
    def __init__(self, url: str = RPC):
        self.url = url

    def call(self, method: str, params: list):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                 "User-Agent": "srnet-legacy-cashout"})
        out = json.loads(urllib.request.urlopen(req, timeout=30).read())
        if "error" in out:
            raise RuntimeError(out["error"].get("message", out["error"]))
        return out["result"]


def _view(rpc, sig: str, who: str) -> int:
    data = "0x" + evm.selector(sig).hex() + who[2:].lower().rjust(64, "0")
    return int(rpc.call("eth_call", [{"to": CONTRACT, "data": data}, "latest"]), 16)


def balances(rpc, who: str) -> dict:
    return {"available": _view(rpc, "availableOf(address)", who),
            "locked": _view(rpc, "lockedOf(address)", who),
            "payable": _view(rpc, "payableOf(address)", who),
            "eth": int(rpc.call("eth_getBalance", [who, "latest"]), 16)}


def calls(b: dict) -> list[tuple[str, str]]:
    """(label, calldata) for each withdrawal this wallet can make."""
    out = []
    if b["available"]:
        out.append(("unused balance", "0x" + (evm.selector("cashOut(uint256)") + b["available"].to_bytes(32, "big")).hex()))
    if b["payable"]:
        out.append(("seller earnings", "0x" + evm.selector("withdrawPayable()").hex()))
    return out


def fmt(units: int) -> str:
    return f"{units / USDC:.2f}"


def report(who: str, b: dict) -> str:
    lines = [f"wallet {who}",
             f"  unused balance:  {fmt(b['available'])} USDC",
             f"  seller earnings: {fmt(b['payable'])} USDC"]
    if b["locked"]:
        lines.append(f"  in a running lease: {fmt(b['locked'])} USDC (comes back to the unused balance when it ends)")
    lines.append(f"  ETH for gas:     {b['eth'] / 1e18:.6f}")
    if not calls(b):
        lines.append("Nothing to take out.")
    return "\n".join(lines)


def open_keystore(path: Path, password: str | None = None):
    from eth_account import Account
    ks = json.loads(path.read_text())
    if password is None:
        password = os.environ.get("SRNET_KEYSTORE_PASSWORD") or getpass.getpass("keystore passphrase: ")
    return Account.from_key(Account.decrypt(ks, password))


def send(rpc, acct, b: dict, ask=input, out=print) -> list[str]:
    todo = calls(b)
    if not todo:
        out("Nothing to take out.")
        return []
    gas_price = int(rpc.call("eth_gasPrice", []), 16)
    need = gas_price * 2 * GAS_LIMIT * len(todo)
    if b["eth"] < need:
        out(f"This wallet needs about {need / 1e18:.6f} ETH on Arbitrum One for the network fee and has "
            f"{b['eth'] / 1e18:.6f}. Send a little ETH to {acct.address} first, then run this again.")
        return []
    total = sum(b[k] for k in ("available", "payable"))
    out(f"Send {fmt(total)} USDC from the old SRNet balance to {acct.address}?")
    if ask("Continue? [y/N] ").strip().lower() != "y":
        out("cancelled")
        return []
    nonce = int(rpc.call("eth_getTransactionCount", [acct.address, "pending"]), 16)
    sent = []
    for label, data in todo:
        tx = {"to": CONTRACT, "data": data, "value": 0, "gas": GAS_LIMIT, "nonce": nonce,
              "chainId": CHAIN_ID, "maxFeePerGas": gas_price * 2, "maxPriorityFeePerGas": 0}
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = rpc.call("eth_sendRawTransaction", ["0x" + bytes(raw).hex()])
        out(f"{label}: sent {h}  https://arbiscan.io/tx/{h}")
        sent.append(h)
        nonce += 1
    return sent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--address")
    c.add_argument("--keystore", default=str(Path(os.environ.get("SRNET_HOME", "~/.srnet")).expanduser() / "keystore.json"))
    s = sub.add_parser("send")
    s.add_argument("--keystore", default=str(Path(os.environ.get("SRNET_HOME", "~/.srnet")).expanduser() / "keystore.json"))
    a = ap.parse_args(argv)
    rpc = Rpc()
    if a.cmd == "check":
        who = a.address
        if not who:
            try:
                who = "0x" + json.loads(Path(a.keystore).read_text())["address"].lower().removeprefix("0x")
            except (OSError, ValueError, KeyError):
                print(f"no keystore at {a.keystore}; pass --address 0x...", file=sys.stderr)
                return 2
        print(report(who, balances(rpc, who)))
        return 0
    acct = open_keystore(Path(a.keystore))
    send(rpc, acct, balances(rpc, acct.address))
    return 0


if __name__ == "__main__":
    sys.exit(main())
