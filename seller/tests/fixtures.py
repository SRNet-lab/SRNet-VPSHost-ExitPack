"""Shared test helpers: a seller with a quote key and listing, and a fake chain."""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "contrib" / "exit-lease")]
import admission as A  # noqa: E402
import chains as C  # noqa: E402
import evm  # noqa: E402
import listing as L  # noqa: E402
import quote as Q  # noqa: E402

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

PAYOUT = "0x" + "aa" * 20
BUYER_WALLET = "0x" + "11" * 20
CLIENT = "ycbogyadycbogyadycbogyadycbogyadycbogyadycbogyadycbo.sesh"  # 0x03 * 32
OTHER = "yryonyebyryonyebyryonyebyryonyebyryonyebyryonyebyryo.sesh"   # 0x01 * 32
CLAIM = "http://" + "y" * 52 + ".sesh/claim"
T0 = 1_800_000_000


class FakeRpc:
    """One chain: receipts by tx hash, a head block and block times."""

    def __init__(self, chainid: int):
        self.chainid, self.receipts, self.times, self.head = chainid, {}, {}, 100
        self.down = False

    def _up(self):
        if self.down:
            raise evm.RpcError("connection refused")

    def receipt(self, tx):
        self._up()
        return self.receipts.get(tx)

    def block_number(self):
        self._up()
        return self.head

    def block_time(self, n):
        self._up()
        return self.times[n]

    def mine(self, tx, transfers, block=None, when=T0, ok=True):
        """transfers: [(token, to, value)]"""
        block = self.head if block is None else block
        logs = [{"address": tok, "topics": [evm.TRANSFER_TOPIC, evm.addr_topic(BUYER_WALLET), evm.addr_topic(to)],
                 "data": hex(int(v)), "transactionHash": tx, "blockNumber": hex(block), "logIndex": hex(i)}
                for i, (tok, to, v) in enumerate(transfers)]
        self.receipts[tx] = {"status": "0x1" if ok else "0x0", "blockNumber": hex(block), "logs": logs}
        self.times[block] = when


class FakeChains:
    def __init__(self, *ids):
        self.by = {c: FakeRpc(c) for c in ids}

    def get(self, chainid):
        if chainid not in self.by:
            raise evm.RpcError("no rpc")
        return self.by[chainid]


def tx(n: int) -> str:
    return "0x" + f"{n:064x}"


class Seller:
    """Files for one seller in a temp dir: identity, quote key, issuer key, signed v3 listing."""

    def __init__(self, price="1.00", max_hours=168, chains=(42161, 8453), testnets=False, **extra):
        self.d = Path(tempfile.mkdtemp())
        self.identity = self.d / "exit.key"
        k = Ed25519PrivateKey.generate()
        self.identity.write_text(k.private_bytes_raw().hex())
        self.quote_key = self.d / "quote.key"
        Q.new_key_file(self.quote_key)
        self.issuer = self.d / "issuer.key"
        self.issuer_pub = A.keygen(self.issuer)
        self.store = self.d / "admissions.json"
        self.state = self.d / "claims"
        doc = L.make(L.load_identity(self.identity), price_per_hour_usdc=price, max_hours=max_hours, payout=PAYOUT,
                     chains=sorted(chains), token="USDC", quote_key=Q.pub_hex(Q.load_key(self.quote_key)),
                     claim_url=CLAIM, country="US", policy=["tcp/443"], **extra)
        self.listing_doc = doc
        self.listing = doc["listing"]
        self.sesh = self.listing["seller"]
        self.listing_path = self.d / "listing.json"
        self.listing_path.write_text(json.dumps(doc))


def buyer_key():
    k = Ed25519PrivateKey.generate()
    return k, Q.pub_hex(k)
