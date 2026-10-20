#!/usr/bin/env python3
"""Minimal read-only EVM access, standard library only.

JSON-RPC over HTTP(S), static ABI encode/decode, and USDC Transfer log decoding. Nothing here
holds a key or sends a transaction: the seller pack and the buyer add-on only ever read the chain.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from keccak256 import keccak256  # noqa: E402

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class RpcError(Exception):
    pass


class JsonRpc:
    def __init__(self, url: str, timeout: float = 20, agent: str = "srnet-exits"):
        self.url, self.timeout, self.agent, self._id = url, timeout, agent, 0

    def call(self, method: str, *params):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                   "User-Agent": self.agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise RpcError(f"rpc {self.url}: {getattr(e, 'reason', e)}") from e
        if not isinstance(resp, dict):
            raise RpcError(f"rpc {method}: bad response")
        if resp.get("error"):
            err = resp["error"]
            raise RpcError(f"rpc {method}: {err.get('message', err) if isinstance(err, dict) else err}")
        return resp.get("result")

    # convenience reads
    def chain_id(self) -> int:
        return int(self.call("eth_chainId"), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def block_time(self, number: int | str) -> int:
        tag = hex(number) if isinstance(number, int) else number
        b = self.call("eth_getBlockByNumber", tag, False)
        if not b:
            raise RpcError(f"block {number} not found")
        return int(b["timestamp"], 16)

    def receipt(self, tx: str) -> dict | None:
        return self.call("eth_getTransactionReceipt", tx)

    def transfer_logs(self, token: str, to: str, from_block: int, to_block: int | str = "latest") -> list[dict]:
        tb = hex(to_block) if isinstance(to_block, int) else to_block
        return self.call("eth_getLogs", {"address": token, "fromBlock": hex(from_block), "toBlock": tb,
                                         "topics": [TRANSFER_TOPIC, None, addr_topic(to)]}) or []

    def erc20_balance(self, token: str, who: str) -> int:
        data = "0x" + encode_call("balanceOf(address)", who).hex()
        return int(self.call("eth_call", {"to": token, "data": data}, "latest") or "0x0", 16)


def addr_topic(addr: str) -> str:
    return "0x" + "00" * 12 + addr.lower()[2:]


def decode_transfer(log: dict) -> dict | None:
    """{"token", "from", "to", "value", "tx", "block", "index"} for an ERC-20 Transfer log, else None."""
    try:
        topics = log["topics"]
        if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
            return None
        return {"token": log["address"].lower(), "from": "0x" + topics[1][-40:].lower(),
                "to": "0x" + topics[2][-40:].lower(), "value": int(log["data"], 16),
                "tx": str(log.get("transactionHash", "")).lower(),
                "block": int(log["blockNumber"], 16) if log.get("blockNumber") else None,
                "index": int(log["logIndex"], 16) if log.get("logIndex") else None}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def receipt_transfers(receipt: dict) -> list[dict]:
    return [t for t in (decode_transfer(lg) for lg in (receipt or {}).get("logs") or []) if t]


def receipt_ok(receipt: dict) -> bool:
    return str((receipt or {}).get("status", "")).lower() in ("0x1", "1")


# ---------------------------------------------------------------- ABI (static types only)
def selector(sig: str) -> bytes:
    return keccak256(sig.encode())[:4]


def _word(t: str, v) -> bytes:
    if t.startswith("uint"):
        return int(v).to_bytes(32, "big")
    if t == "address":
        return bytes.fromhex(str(v)[2:].rjust(40, "0")).rjust(32, b"\0")
    if t == "bytes32":
        b = bytes.fromhex(str(v).removeprefix("0x")) if isinstance(v, str) else bytes(v)
        if len(b) != 32:
            raise ValueError("bytes32 needs 32 bytes")
        return b
    if t == "bool":
        return int(bool(v)).to_bytes(32, "big")
    raise ValueError(f"unsupported ABI type {t}")


def encode_call(sig: str, *args) -> bytes:
    inner = sig[sig.index("(") + 1: sig.rindex(")")]
    types = [t for t in inner.split(",") if t]
    if len(types) != len(args):
        raise ValueError("argument count")
    return selector(sig) + b"".join(_word(t, a) for t, a in zip(types, args))


def decode(types: list[str], data: bytes) -> tuple:
    if len(data) < 32 * len(types):
        raise ValueError("short ABI result")
    out = []
    for i, t in enumerate(types):
        w = data[32 * i: 32 * (i + 1)]
        if t.startswith("uint"):
            out.append(int.from_bytes(w, "big"))
        elif t == "address":
            out.append("0x" + w[12:].hex())
        elif t == "bytes32":
            out.append("0x" + w.hex())
        elif t == "bool":
            out.append(w[-1] == 1)
        else:
            raise ValueError(f"unsupported ABI type {t}")
    return tuple(out)
