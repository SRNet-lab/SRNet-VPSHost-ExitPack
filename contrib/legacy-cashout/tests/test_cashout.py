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
    def __init__(self, avail=710_000, payable=0, locked=0, eth=10 ** 15, chain=42161, revert=None, gas=90_000):
        self.v = {"availableOf(address)": avail, "payableOf(address)": payable, "lockedOf(address)": locked}
        self.eth, self.sent, self.chain, self.revert, self.gas, self.log = eth, [], chain, revert, gas, []

    def call(self, method, params):
        self.log.append(method)
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "eth_estimateGas":
            return hex(self.gas)
        if method == "eth_call" and "from" in params[0]:     # a dry run of a withdrawal
            if self.revert and params[0]["data"].startswith("0x" + X.evm.selector(self.revert).hex()):
                raise RuntimeError("execution reverted")
            return "0x"
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
        self.assertIn("unused balance:  0.710000 USDC", t)
        self.assertIn("seller earnings: 0.170000 USDC", t)
        self.assertEqual(X.fmt(715_000), "0.715000")
        self.assertEqual(X.fmt(3_000_001), "3.000001")
        t2 = X.report(WHO, X.balances(FakeRpc(avail=0, locked=5), WHO))
        self.assertIn("paid to the seller", t2)
        self.assertNotIn("comes back", t2)
        c = X.calls(b)
        self.assertEqual([x[0] for x in c], ["unused balance", "seller earnings"])
        self.assertTrue(c[0][1].startswith("0x" + X.evm.selector("cashOut(uint256)").hex()))
        self.assertTrue(c[0][1].endswith(f"{710_000:064x}"))
        self.assertEqual(X.calls(X.balances(FakeRpc(avail=0), WHO)), [])
        self.assertIn("Nothing to take out", X.report(WHO, X.balances(FakeRpc(avail=0), WHO)))

    def test_rpc_must_be_arbitrum_one(self):
        X.check_chain(FakeRpc())
        with self.assertRaises(X.CashoutError):
            X.check_chain(FakeRpc(chain=1))

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
        self.assertIn("eth_estimateGas", rpc.log)
        seen = []
        real = acct.sign_transaction
        acct2 = type("A", (), {"address": acct.address,
                               "sign_transaction": lambda _, tx: (seen.append(tx), real(tx))[1]})()
        rpc = FakeRpc(avail=710_000, payable=3_000_000)
        X.send(rpc, acct2, X.balances(rpc, acct.address), ask=lambda p: "y", out=out.append)
        self.assertEqual([t["gas"] for t in seen], [int(90_000 * 1.3)] * 2)

    @unittest.skipUnless(Account, "eth-account not installed")
    def test_send_simulates_first_and_uses_estimated_gas(self):
        acct = Account.create()
        out = []
        rpc = FakeRpc(avail=710_000, payable=170_000, revert="withdrawPayable()")
        self.assertEqual(X.send(rpc, acct, X.balances(rpc, acct.address), ask=lambda p: "y", out=out.append), [])
        self.assertEqual(rpc.sent, [])
        self.assertIn("would refuse", out[-1])
        # the fee check uses the estimate: 2 x 90k x 1.3 at 2 x gas price
        rpc = FakeRpc(avail=710_000, payable=170_000, eth=2 * 10 ** 7 * int(90_000 * 1.3) * 2 - 1)
        self.assertEqual(X.send(rpc, acct, X.balances(rpc, acct.address), ask=lambda p: "y", out=out.append), [])
        self.assertIn("needs about", out[-1])

    @unittest.skipUnless(Account, "eth-account not installed")
    def test_keystore(self):
        acct = Account.create()
        p = Path(tempfile.mkdtemp()) / "keystore.json"
        p.write_text(json.dumps(Account.encrypt(acct.key, "pw", kdf="scrypt", iterations=2 ** 4)))
        self.assertEqual(X.open_keystore(p, "pw").address, acct.address)


if __name__ == "__main__":
    unittest.main()
