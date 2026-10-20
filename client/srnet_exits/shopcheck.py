"""Check a shop payment on chain (read-only): did transaction X send exactly N USDC to address Y?

The SRNet core only shows a payment's address, amount and QR code; checking that a payment really
arrived is done here, for shop owners who want it."""
from __future__ import annotations

import chains as C  # noqa: E402
import evm  # noqa: E402
import quote as Q  # noqa: E402


def check(rpc, chainid: int, tx: str, to: str, amount: int, testnets: bool = False) -> dict:
    tx, to = str(tx).lower(), str(to).lower()
    if not Q.TX_RE.match(tx):
        return {"paid": False, "why": "bad transaction hash"}
    if not C.ADDR_RE.match(to):
        return {"paid": False, "why": "bad address"}
    if not C.supported(chainid, testnets):
        return {"paid": False, "why": "unsupported chain"}
    if rpc.chain_id() != chainid:
        return {"paid": False, "why": "the RPC is on another chain"}
    r = rpc.receipt(tx)
    if not r:
        return {"paid": False, "why": "not mined yet (or not on " + C.label(chainid) + ")"}
    if not evm.receipt_ok(r):
        return {"paid": False, "why": "the transaction failed"}
    token = C.usdc(chainid, testnets)
    hits = [t for t in evm.receipt_transfers(r) if t["token"] == token and t["to"] == to]
    if not hits:
        return {"paid": False, "why": f"no USDC transfer to {to} in that transaction"}
    exact = [t for t in hits if t["value"] == int(amount)]
    if not exact:
        return {"paid": False, "why": "amount mismatch: got " + ", ".join(C.fmt_usdc(t["value"]) for t in hits)
                + f" USDC, expected {C.fmt_usdc(amount)}"}
    t = exact[0]
    out = {"paid": True, "from": t["from"], "amount": C.fmt_usdc(t["value"]), "block": t["block"],
           "chain": C.label(chainid), "link": C.tx_link(chainid, tx)}
    try:
        out["time"] = rpc.block_time(t["block"])
    except (evm.RpcError, TypeError):
        pass
    return out
