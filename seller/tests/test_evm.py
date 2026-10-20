"""evm.py: stdlib ABI encoding and Transfer log decoding."""
import unittest

from fixtures import evm

ADDR = "0x" + "ab" * 20


class Evm(unittest.TestCase):
    def test_selector_and_encoding(self):
        self.assertEqual(evm.selector("transfer(address,uint256)").hex(), "a9059cbb")
        self.assertEqual(evm.selector("balanceOf(address)").hex(), "70a08231")
        got = evm.encode_call("transfer(address,uint256)", ADDR, 7).hex()
        self.assertEqual(got, "a9059cbb" + "00" * 12 + "ab" * 20 + f"{7:064x}")

    def test_decode(self):
        raw = bytes.fromhex("00" * 12 + "11" * 20 + f"{7:064x}" + f"{1:064x}")
        self.assertEqual(evm.decode(["address", "uint256", "bool"], raw), ("0x" + "11" * 20, 7, True))

    def test_transfer_topic(self):
        from keccak256 import keccak256
        self.assertEqual("0x" + keccak256(b"Transfer(address,address,uint256)").hex(), evm.TRANSFER_TOPIC)

    def test_decode_transfer(self):
        log = {"address": "0xAF88d065e77c8cC2239327C5EDb3A432268e5831",
               "topics": [evm.TRANSFER_TOPIC, evm.addr_topic("0x" + "11" * 20), evm.addr_topic(ADDR)],
               "data": hex(3_000_417), "transactionHash": "0x" + "AB" * 32, "blockNumber": "0x10", "logIndex": "0x0"}
        t = evm.decode_transfer(log)
        self.assertEqual(t["token"], "0xaf88d065e77c8cc2239327c5edb3a432268e5831")
        self.assertEqual((t["from"], t["to"], t["value"], t["block"]), ("0x" + "11" * 20, ADDR, 3_000_417, 16))
        self.assertIsNone(evm.decode_transfer({**log, "topics": log["topics"][:2]}))  # ERC-721 style / junk
        self.assertIsNone(evm.decode_transfer({**log, "topics": ["0x" + "00" * 32] + log["topics"][1:]}))
        self.assertIsNone(evm.decode_transfer({"topics": None}))
        rc = {"status": "0x1", "logs": [log, {"address": ADDR, "topics": [], "data": "0x"}]}
        self.assertEqual(len(evm.receipt_transfers(rc)), 1)
        self.assertTrue(evm.receipt_ok(rc))
        self.assertFalse(evm.receipt_ok({"status": "0x0"}))

    def test_no_signing_code(self):
        from pathlib import Path
        src = Path(evm.__file__).read_text()
        for word in ("eth_sendRawTransaction", "eth_sendTransaction", "private", "sign("):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
