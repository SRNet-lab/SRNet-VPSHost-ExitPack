"""evm.py: stdlib ABI encoding and EIP-191 recovery, checked against cast output."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import evm  # noqa: E402

# anvil account 0; signature produced by `cast wallet sign`.
ADDR0 = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
SIG = ("0x889a82ad9d0cafcca82ef531bb2864ed34e5c9338171aa20637fb42cc1cb790c"
       "0fec37e224557fd491d06a2fd3d139f0d1d57af66b0f2b779c2f14dcb193cca51c")


class Evm(unittest.TestCase):
    def test_selector_and_encoding(self):
        self.assertEqual(evm.selector("transfer(address,uint256)").hex(), "a9059cbb")
        got = evm.encode_call("startLease(bytes32,uint256,uint256)", "0x" + "bb" * 32, 1_000_000, 1).hex()
        self.assertEqual(got, "427bb23a" + "bb" * 32 + f"{1_000_000:064x}" + f"{1:064x}")

    def test_decode(self):
        raw = bytes.fromhex("00" * 12 + "11" * 20 + f"{7:064x}" + f"{1:064x}")
        self.assertEqual(evm.decode(["address", "uint256", "bool"], raw), ("0x" + "11" * 20, 7, True))

    def test_recover_matches_cast(self):
        self.assertTrue(evm.verify_personal(ADDR0, "hello srnet", SIG))
        self.assertFalse(evm.verify_personal(ADDR0, "hello srnet!", SIG))
        self.assertFalse(evm.verify_personal("0x" + "11" * 20, "hello srnet", SIG))
        self.assertFalse(evm.verify_personal(ADDR0, "hello srnet", "0x1234"))
        self.assertFalse(evm.verify_personal(ADDR0, "hello srnet", "zz"))

    def test_chains(self):
        self.assertIn(42161, evm.CHAINS)
        for c in (421614, 84532, 11155111):
            self.assertNotIn(c, evm.CHAINS)
        self.assertNotIn(1, evm.CHAINS)


if __name__ == "__main__":
    unittest.main()
