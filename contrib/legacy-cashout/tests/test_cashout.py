"""Legacy cash-out: read-only report, call data, and the send path against a fake RPC (no network)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cashout as X  # noqa: E402

try:
    from eth_account import Account
except ImportError:  # the script's own dependency; the rest of the repo doesn't need it
    Account = None


class FakeRpc:
    def __init__(self, avail=710_000, payable=0, locked=0, eth=10 ** 15):
        self.v = {"availableOf(address)": avail, "payableOf(address)": payable, "lockedOf(address)": locked}
        self.eth, self.sent = eth, []

    def call(self, method, params):
        if method == "eth_call":
            data = params[0]["data"]
            assert params[0]["to"] == X.CONTRACT
            for sig, v in self.v.items():
                if data.startswith("0x" + X.evm.selector(sig).hex()):
                    return hex(v)
            raise AssertionError(data)
        if method == "eth_getBalance":
            return hex(self.eth)
        if method == "eth_gasPrice":
            return hex(10 ** 7)
        if method == "eth_getTransactionCount":
            return "0x3"
        if method == "eth_sendRawTransaction":
            self.sent.append(params[0])
            return "0x" + f"{len(self.sent):064x}"
        raise AssertionError(method)


WHO = "0x" + "c0" * 20


class Cashout(unittest.TestCase):
    def test_report_and_calls(self):
        b = X.balances(FakeRpc(avail=710_000, payable=170_000), WHO)
        t = X.report(WHO, b)
        self.assertIn("unused balance:  0.71 USDC", t)
        self.assertIn("seller earnings: 0.17 USDC", t)
        c = X.calls(b)
        self.assertEqual([x[0] for x in c], ["unused balance", "seller earnings"])
        self.assertTrue(c[0][1].startswith("0x" + X.evm.selector("cashOut(uint256)").hex()))
        self.assertTrue(c[0][1].endswith(f"{710_000:064x}"))
        self.assertEqual(X.calls(X.balances(FakeRpc(avail=0), WHO)), [])
        self.assertIn("Nothing to take out", X.report(WHO, X.balances(FakeRpc(avail=0), WHO)))

    @unittest.skipUnless(Account, "eth-account not installed")
    def test_send_asks_and_needs_gas(self):
        acct = Account.create()
        out = []
        rpc = FakeRpc(eth=0)
        self.assertEqual(X.send(rpc, acct, X.balances(rpc, acct.address), ask=lambda p: "y", out=out.append), [])
        self.assertIn("needs about", out[-1])
        rpc = FakeRpc(avail=710_000, payable=3_000_000)
        self.assertEqual(X.send(rpc, acct, X.balances(rpc, acct.address), ask=lambda p: "n", out=out.append), [])
        self.assertEqual(rpc.sent, [])
        sent = X.send(rpc, acct, X.balances(rpc, acct.address), ask=lambda p: "y", out=out.append)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(rpc.sent), 2)

    @unittest.skipUnless(Account, "eth-account not installed")
    def test_keystore(self):
        acct = Account.create()
        p = Path(tempfile.mkdtemp()) / "keystore.json"
        p.write_text(json.dumps(Account.encrypt(acct.key, "pw", kdf="scrypt", iterations=2 ** 4)))
        self.assertEqual(X.open_keystore(p, "pw").address, acct.address)


if __name__ == "__main__":
    unittest.main()
