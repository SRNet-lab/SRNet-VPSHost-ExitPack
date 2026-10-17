"""End-to-end on MockChain (prepaid hourly leases): claim → admit for the whole lease;
lease cancelled (either side) or ended → revoke at once."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "exit-lease"), str(HERE.parent.parent / "contrib" / "exit-lease")]
import watcher as W  # noqa: E402
import admission as A  # noqa: E402

SELLER = "yryonyebyryonyebyryonyebyryonyebyryonyebyryonyebyryo.sesh"  # valid z-base32 fixture (0x01 * 32)
CLIENT = "ycbogyadycbogyadycbogyadycbogyadycbogyadycbogyadycbo.sesh"  # valid z-base32 fixture (0x03 * 32)
WALLET = "0x" + "aa" * 20
USER = "0x" + "11" * 20


class E2E(unittest.TestCase):
    def test_admit_per_paid_hour_then_revoke_on_seller_stop(self):
        d = Path(tempfile.mkdtemp()); key = d / "k"; pub = A.keygen(key)
        now = int(time.time()); chain = W.MockChain(clock=now)
        w = W.Watcher(chain, W.MockVerifier(), payout_wallet=WALLET, seller_sesh=SELLER,
                      issuer_key=str(key), store=str(d / "a.json"), state=str(d / "s.json"),
                      min_amount=500)  # listed rate: 0.0005 USDC/h in base units
        # 3-hour lease at 500/h
        chain.holds[1] = W.Hold(USER, 1500, WALLET, now + 3 * 3600, True, rate_per_hour=500)
        msg = W.claim_message(chain.contract, chain.chainid, 1, CLIENT)
        ok, tid = w.handle_claim({"v": 1, "hold_id": 1, "client_sesh": CLIENT, "sig": f"mock:{USER}:{msg}"})
        self.assertTrue(ok); self.assertEqual(tid, "hold-31337-1-h0")
        rec = A.Store(d / "a.json").records()[0]
        self.assertEqual(A.verify_record(rec, pub, SELLER, CLIENT), "admission")
        self.assertLessEqual(rec["exp"], now + 3600 + 2)  # current paid hour only
        self.assertGreaterEqual(rec["exp"], now + 3600 - 2)
        self.assertEqual(w.reconcile(), [])
        chain.holds[1].active = False  # seller stopped mid-lease (the buyer has no cancel)
        self.assertEqual(w.reconcile(), ["ended 1"])
        self.assertEqual(A.Store(d / "a.json").records(), [])
        # Below listed hourly rate -> refused
        chain.holds[2] = W.Hold(USER, 499, WALLET, now + 3600, True, rate_per_hour=499)
        msg2 = W.claim_message(chain.contract, chain.chainid, 2, CLIENT)
        self.assertEqual(w.handle_claim({"v": 1, "hold_id": 2, "client_sesh": CLIENT,
                                         "sig": f"mock:{USER}:{msg2}"}), (False, "rate_below_price"))


if __name__ == "__main__":
    unittest.main()
