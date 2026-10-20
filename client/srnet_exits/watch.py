"""Watch a chain for the exact USDC transfer a quote asks for (read-only JSON-RPC)."""
from __future__ import annotations

import chains as C  # noqa: E402
import evm  # noqa: E402

CHUNK = 2000          # blocks per eth_getLogs call (public RPCs cap the range)
# rough seconds per block, used only to start scanning a little before the quote was issued
BLOCK_S = {42161: 0.25, 8453: 2.0, 421614: 0.25, 84532: 2.0, 31337: 1.0}


def start_block(rpc, chainid: int, back_s: int = 120) -> int:
    head = rpc.block_number()
    return max(0, head - int(back_s / BLOCK_S.get(chainid, 1.0)) - 1)


def transfers(rpc, q: dict, from_block: int, to_block: int, testnets: bool = False) -> list[dict]:
    token = C.usdc(q["chainid"], testnets)
    out = []
    b = from_block
    while b <= to_block:
        e = min(to_block, b + CHUNK - 1)
        for log in rpc.transfer_logs(token, q["payout"], b, e):
            t = evm.decode_transfer(log)
            if t and t["token"] == token and t["to"] == q["payout"].lower():
                out.append(t)
        b = e + 1
    return out


class Scan:
    """Incremental scan of one quote's chain. state is a dict kept in the quote record:
    {"next": block to scan from}."""

    def __init__(self, rpc, quote: dict, state: dict, testnets: bool = False):
        self.rpc, self.q, self.state, self.testnets = rpc, quote, state, testnets
        self.near: list[int] = []  # other amounts that reached the payout while we watched

    def step(self) -> dict | None:
        """One pass up to the head. Returns the matching transfer, or None."""
        head = self.rpc.block_number()
        frm = int(self.state.get("next", head))
        if frm > head:
            return None
        found = None
        for t in transfers(self.rpc, self.q, frm, head, self.testnets):
            if t["value"] == self.q["amount"]:
                found = found or t
            elif t["value"] not in self.near:
                self.near.append(t["value"])
        self.state["next"] = head + 1
        return found


def other_chain_hit(rpcs: dict, quote: dict, chains: list[int], back_blocks: dict, testnets: bool = False) -> int | None:
    """If the exact amount reached the payout on another chain the seller lists, return that chain."""
    for cid in chains:
        if cid == quote["chainid"] or cid not in rpcs:
            continue
        rpc = rpcs[cid]
        try:
            head = rpc.block_number()
            q = dict(quote, chainid=cid)
            for t in transfers(rpc, q, max(0, head - back_blocks.get(cid, 2000)), head, testnets):
                if t["value"] == quote["amount"]:
                    return cid
        except evm.RpcError:
            continue
    return None


def rpc_for(chainid: int, cfg_overrides: dict, testnets: bool = False):
    return evm.JsonRpc(C.rpc_url(chainid, cfg_overrides, testnets))
