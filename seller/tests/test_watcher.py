import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "exit-lease"))
sys.path.insert(0, str(HERE.parent.parent / "contrib" / "exit-lease"))
import watcher as W  # noqa: E402
import admission as A  # noqa: E402

SELLER = "yryonyebyryonyebyryonyebyryonyebyryonyebyryonyebyryo.sesh"  # valid z-base32 fixture (0x01 * 32)
CLIENT = "ycbogyadycbogyadycbogyadycbogyadycbogyadycbogyadycbo.sesh"  # valid z-base32 fixture (0x03 * 32)
OURS = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
USER = "0x" + "11" * 20


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.key = self.d / "issuer.key"
        self.pub = A.keygen(self.key)
        self.store = self.d / "adm.json"
        self.now = int(time.time())
        self.chain = W.MockChain(clock=self.now)
        self.w = W.Watcher(self.chain, W.MockVerifier(), payout_wallet=OURS, seller_sesh=SELLER,
                           issuer_key=str(self.key), store=str(self.store), state=str(self.d / "st.json"),
                           min_amount=100, provisional_ttl=900, paid_ttl=3600)

    def hold(self, hid, wallet=OURS, amount=100, active=True, dl=3600, rate=None, start=None):
        self.chain.holds[hid] = W.Hold(USER, amount, wallet, self.now + dl, active, rate_per_hour=rate,
                                       start=None if start is None else self.now + start)

    def claim(self, hid, client=CLIENT, signer=USER):
        msg = W.claim_message(self.chain.contract, self.chain.chainid, hid, client)
        return {"v": 1, "hold_id": hid, "client_sesh": client, "sig": f"mock:{signer.lower()}:{msg}"}

    def verify(self, client=CLIENT):
        recs = A.Store(self.store).records()
        return [A.verify_record(r, self.pub, SELLER, client) for r in recs]

    def test_valid_claim_admits_and_caps_at_deadline(self):
        self.hold(1, dl=300)
        ok, tid = self.w.handle_claim(self.claim(1))
        self.assertTrue(ok, tid)
        self.assertEqual(tid, "hold-31337-1-h0")
        rec = A.Store(self.store).records()[0]
        self.assertLessEqual(rec["exp"], self.now + 300 + 2)  # lease end
        self.assertEqual(self.verify(), ["admission"])

    def test_refusals(self):
        self.hold(2, wallet=OTHER); self.hold(3, active=False); self.hold(4, dl=-1); self.hold(5, amount=500, rate=99)
        self.hold(6)
        cases = {
            2: (self.claim(2), "hold_not_for_this_seller"),
            3: (self.claim(3), "hold_not_active"),
            4: (self.claim(4), "hold_expired"),
            5: (self.claim(5), "rate_below_price"),
            6: (self.claim(6, signer=OTHER), "claim_sig_not_hold_user"),
            99: (self.claim(99), "hold_not_active"),
        }
        for hid, (c, why) in cases.items():
            self.assertEqual(self.w.handle_claim(c), (False, why), hid)
        self.assertEqual(A.Store(self.store).records(), [])

    def test_claim_sig_binds_client(self):
        self.hold(7)
        c = self.claim(7)
        c["client_sesh"] = SELLER  # swap client after signing
        self.assertEqual(self.w.handle_claim(c), (False, "claim_sig_not_hold_user"))

    def test_no_double_claim(self):
        self.hold(8)
        ok, tid = self.w.handle_claim(self.claim(8))
        self.assertTrue(ok)
        self.assertEqual(self.w.handle_claim(self.claim(8)), (True, tid))  # same claim delivered twice
        self.assertEqual(self.w.handle_claim(self.claim(8, client=SELLER)), (False, "hold_already_claimed"))
        self.assertEqual(len(A.Store(self.store).records()), 1)

    def test_bad_input(self):
        self.assertEqual(self.w.handle_claim({"v": 2}), (False, "bad_version"))
        self.assertEqual(self.w.handle_claim({"v": 1}), (False, "missing_field"))
        self.assertEqual(self.w.handle_claim({"v": 1, "hold_id": 1, "client_sesh": "x.sesh", "sig": ""}),
                         (False, "bad_client"))

    def test_seller_stop_or_end_revokes(self):
        self.hold(10); self.hold(11)
        self.w.handle_claim(self.claim(10)); self.w.handle_claim(self.claim(11))
        self.assertEqual(self.w.reconcile(), [])  # both still running
        self.chain.holds[11].active = False  # stopped by the seller (the buyer cannot cancel)
        ev = self.w.reconcile()
        self.assertEqual(ev, ["ended 11"])
        tids = sorted(r["tid"] for r in A.Store(self.store).records())
        self.assertEqual(tids, ["hold-31337-10-h0"])
        self.assertEqual(self.w.reconcile(), [])

    def test_admits_per_paid_hour_and_revokes_when_paid_time_ends(self):
        self.hold(40, dl=3 * 3600, start=0)  # 3 prepaid hours starting now
        ok, tid = self.w.handle_claim(self.claim(40))
        self.assertEqual(tid, "hold-31337-40-h0")
        rec = A.Store(self.store).records()[0]
        self.assertLessEqual(rec["exp"], self.now + 3600 + 2, "admission covers only the current paid hour")
        self.assertEqual(self.w.reconcile(), [])
        self.chain.clock = self.now + 3600 - 60  # within the renew margin of hour 0
        self.assertEqual(self.w.reconcile(), ["hour 40 hold-31337-40-h1"])
        self.assertEqual(self.w.state["claimed"]["40"]["exp"], self.now + 7200)
        self.chain.clock = self.now + 7200 - 30
        self.assertEqual(self.w.reconcile(), ["hour 40 hold-31337-40-h2"])
        self.assertEqual(self.w.state["claimed"]["40"]["exp"], self.now + 3 * 3600)
        self.chain.clock = self.now + 3 * 3600 - 30  # last hour: nothing more is paid, no new admission
        self.assertEqual(self.w.reconcile(), [])
        self.chain.clock = self.now + 3 * 3600  # paid time over -> revoke everything
        self.assertEqual(self.w.reconcile(), ["ended 40"])
        self.assertEqual(A.Store(self.store).records(), [])

    def test_future_renewal_admitted_until_end_of_its_first_hour(self):
        self.hold(50, dl=600 + 3600, start=600)  # renewal: starts when the previous lease ends in 10 min
        ok, tid = self.w.handle_claim(self.claim(50))
        self.assertTrue(ok, tid)
        self.assertEqual(self.w.state["claimed"]["50"]["exp"], self.now + 600 + 3600)

    def test_refuses_mainnet(self):
        with self.assertRaises(SystemExit):
            W.Watcher(W.MockChain(chainid=8453), W.MockVerifier(), payout_wallet=OURS, seller_sesh=SELLER,
                      issuer_key=str(self.key), store=str(self.store), state=str(self.d / "x.json"))

    def test_spool(self):
        self.hold(20)
        sp = self.d / "spool"; sp.mkdir()
        (sp / "a.json").write_text(json.dumps(self.claim(20)))
        (sp / "b.json").write_text("{not json")
        out = self.w.poll_spool(sp)
        self.assertEqual([(n, ok) for n, ok, _ in out], [("a.json", True), ("b.json", False)])
        self.assertTrue((sp / "done" / "a.json").exists())
        self.assertTrue((sp / "rejected" / "b.json").exists())

    def test_state_persists(self):
        self.hold(30)
        self.w.handle_claim(self.claim(30))
        w2 = W.Watcher(self.chain, W.MockVerifier(), payout_wallet=OURS, seller_sesh=SELLER,
                       issuer_key=str(self.key), store=str(self.store), state=str(self.d / "st.json"))
        self.assertEqual(w2.handle_claim(self.claim(30, client=SELLER)), (False, "hold_already_claimed"))

    # renewedBy chains
    def renewal(self, prev, nid, user=USER, wallet=OURS):
        p = self.chain.holds[prev]
        self.chain.holds[nid] = W.Hold(user, 100, wallet, p.deadline + 3600, True, rate_per_hour=100,
                                       start=p.deadline)
        self.chain.renewals[prev] = nid

    def test_arbitrum_one_accepted(self):
        W.Watcher(W.MockChain(chainid=42161), W.MockVerifier(), payout_wallet=OURS, seller_sesh="yryonyebyryonyebyryonyebyryonyebyryonyebyryonyebyryo.sesh",
                  issuer_key=str(self.key), store=str(self.store), state=str(self.d / "one.json"))

    def test_follows_four_renewals_without_claims(self):
        self.hold(1, rate=100, start=0)
        self.assertTrue(self.w.handle_claim(self.claim(1))[0])
        prev = 1
        for nid in (2, 3, 4, 5):
            self.renewal(prev, nid)
            self.chain.clock = self.chain.holds[prev].deadline - 300
            ev = self.w.reconcile()
            self.assertIn(f"renewal {nid} of {prev} hold-31337-{nid}-h0", ev)
            self.assertEqual(self.w.state["claimed"][str(nid)]["client"], CLIENT)
            self.chain.clock = self.chain.holds[prev].deadline
            self.assertIn(f"ended {prev}", self.w.reconcile())
            prev = nid
        live = [r for r in A.Store(self.store).records()]
        self.assertEqual({r["tid"] for r in live}, {"hold-31337-5-h0"})

    def test_late_claim_for_followed_renewal_is_ok(self):
        self.hold(1, rate=100, start=0)
        self.w.handle_claim(self.claim(1))
        self.renewal(1, 2)
        self.w.reconcile()
        ok, why = self.w.handle_claim(self.claim(2))
        self.assertTrue(ok, why)
        self.assertFalse(self.w.handle_claim(self.claim(2, client="ybndrfg8ejkmcpqxot1uwisza345h769ybndrfg8ejkmcpqxot1u.sesh"))[0])

    def test_renewal_by_other_buyer_or_seller_refused(self):
        self.hold(1, rate=100, start=0)
        self.w.handle_claim(self.claim(1))
        self.renewal(1, 2, user="0x" + "99" * 20)
        self.assertIn("renewal 2 of 1 refused", self.w.reconcile())
        self.assertNotIn("2", self.w.state["claimed"])


    def test_auto_claim_earned_on_issue(self):
        class FakeClaim:
            def __init__(self):
                self.calls = []
            def claim_earned(self, lease_id):
                self.calls.append(lease_id)
                return f"0xdead{lease_id}"
        fake = FakeClaim()
        self.w.claim_earned = fake
        self.hold(60, dl=3600)
        ok, tid = self.w.handle_claim(self.claim(60))
        self.assertTrue(ok, tid)
        self.assertEqual(fake.calls, [60])
        self.assertEqual(self.w.state["claimed"]["60"]["last_claim_earned"], "0xdead60")

    def test_auto_claim_earned_failure_still_admits(self):
        class Boom:
            def claim_earned(self, lease_id):
                raise RuntimeError("no gas")
        self.w.claim_earned = Boom()
        self.hold(61, dl=3600)
        ok, tid = self.w.handle_claim(self.claim(61))
        self.assertTrue(ok, tid)
        self.assertIn("RuntimeError", self.w.state["claimed"]["61"]["last_claim_earned_error"])

if __name__ == "__main__":
    unittest.main()
