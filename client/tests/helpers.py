"""Test paths: srnet-exits (client/), and, if it is around, the SRNet client it plugs into.

The tests need no SRNet client. When one is found (an installed package, $SRNET_CLIENT_DIR, or a
sibling SRNet-Desktop checkout at ../SRNet-Desktop/client-cli), the add-on checks run too."""
import importlib.util
import os
import sys
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
REPO = CLIENT.parent
if str(CLIENT) not in sys.path:
    sys.path.insert(0, str(CLIENT))
for d in (os.environ.get("SRNET_CLIENT_DIR"), REPO.parent / "SRNet-Desktop" / "client-cli"):
    if d and (Path(d) / "srnet" / "state.py").exists() and importlib.util.find_spec("srnet") is None:
        sys.path.insert(1, str(d))
HAVE_CORE = importlib.util.find_spec("srnet") is not None
CORE_DIR = Path(importlib.util.find_spec("srnet").origin).parents[1] if HAVE_CORE else None

import srnet_exits  # noqa: E402,F401  (puts the pack modules on sys.path)

SELLER = "y3yi9753qbe4gxr31t1urywd3yd9pk4a157g7jeg9xsz6iecu73y.sesh"
ME = "xykbqrmeoadf48m34nfay44d1esquuz6y1efudifhhm4jc6xbs3o.sesh"


def seller_fixtures():
    """The seller pack's own test helpers (a signed v3 listing, quote key, claim desk state)."""
    p = str(REPO / "seller" / "tests")
    if p not in sys.path:
        sys.path.insert(0, p)
    import fixtures
    return fixtures


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Chain:
    """One fake EVM chain with USDC Transfer logs, receipts and block times (read-only RPC surface)."""

    def __init__(self, chainid, clock):
        import evm
        self.evm, self.chainid, self.clock = evm, chainid, clock
        self.head, self.logs, self.receipts, self.times, self.down = 100, [], {}, {}, False

    def _up(self):
        if self.down:
            raise self.evm.RpcError("connection refused")

    def chain_id(self):
        self._up()
        return self.chainid

    def block_number(self):
        self._up()
        return self.head

    def block_time(self, n):
        self._up()
        return self.times.get(int(n), int(self.clock()))

    def receipt(self, tx):
        self._up()
        return self.receipts.get(tx)

    def transfer_logs(self, token, to, frm, to_block="latest"):
        self._up()
        hi = self.head if to_block == "latest" else int(to_block)
        return [lg for lg in self.logs if lg["address"].lower() == token.lower()
                and lg["topics"][2] == self.evm.addr_topic(to) and frm <= int(lg["blockNumber"], 16) <= hi]

    def pay(self, tx, token, to, value, sender="0x" + "11" * 20, ok=True):
        self.head += 1
        log = {"address": token, "topics": [self.evm.TRANSFER_TOPIC, self.evm.addr_topic(sender), self.evm.addr_topic(to)],
               "data": hex(int(value)), "transactionHash": tx, "blockNumber": hex(self.head), "logIndex": "0x0"}
        if ok:
            self.logs.append(log)
        self.receipts[tx] = {"status": "0x1" if ok else "0x0", "blockNumber": hex(self.head), "logs": [log]}
        self.times[self.head] = int(self.clock())
        self.head += 3  # a few confirmations
        return tx


def desk_transport(desk):
    """Call a seller's claim desk (claimd.Desk) in-process, like the HTTP handler does."""
    import json as _json

    def transport(url, body, timeout=30, dns=None):
        path = url.split("/claim", 1)[1] if "/claim" in url else "/" + url.split("/", 3)[-1]
        data = _json.loads(body) if body else None
        if path == "/quote":
            return desk.quote(data)
        if path == "/paid":
            return desk.paid(data)
        if path.startswith("/status/"):
            return desk.status(path.rsplit("/", 1)[1])
        return 404, {"error": "not found"}
    return transport
