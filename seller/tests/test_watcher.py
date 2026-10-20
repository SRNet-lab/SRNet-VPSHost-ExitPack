"""watcher.py: verify a reported payment on chain, then admit the bound buyer for the quoted hours."""
import json
import unittest

from fixtures import A, C, CLIENT, OTHER, PAYOUT, Q, T0, FakeChains, Seller, buyer_key, tx
import claimd as D
import watcher as W

HOUR = 3600


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.s = Seller(price="1.00")
        self.clock = Clock()
        self.desk = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock)
        self.chains = FakeChains(42161, 8453)
        self.w = W.Watcher(self.chains, state_dir=self.s.state, seller_sesh=self.s.sesh,
                           issuer_key=str(self.s.issuer), store=str(self.s.store), confirmations=2,
                           admission=A, clock=self.clock)
        self.bk, self.bpub = buyer_key()

    def quote(self, hours=2, chainid=42161, client=CLIENT, key=None):
        k, pub = key or (self.bk, self.bpub)
        code, doc = self.desk.quote(Q.make_request(self.s.sesh, client, hours, chainid, pub))
        self.assertEqual(code, 200, doc)
        return doc["quote"]

    def pay(self, q, n, value=None, token=None, to=PAYOUT, when=None, chainid=None, ok=True, block=None):
        rpc = self.chains.get(chainid or q["chainid"])
        rpc.mine(tx(n), [(token or C.usdc(q["chainid"]), to, q["amount"] if value is None else value)],
                 when=T0 + 20 if when is None else when, ok=ok, block=block)
        rpc.head += 5

    def report(self, q, n, key=None):
        code, _ = self.desk.paid(Q.make_paid(key or self.bk, q, tx(n)))
        self.assertEqual(code, 202)
        return self.w.poll_spool()

    def spool(self, q, n):
        """A notice written straight to the spool (claimd answers paid quotes itself)."""
        (self.s.state / "spool" / f"{q['id']}-{n}.json").write_text(json.dumps(Q.make_paid(self.bk, q, tx(n))))
        return self.w.poll_spool()

    def status(self, q):
        return json.loads((self.s.state / "status" / f"{q['id']}.json").read_text())

    def admitted(self, client=CLIENT):
        return [r for r in A.Store(self.s.store).records() if r["client_sesh"] == client]

    def test_good_payment_admits_for_quoted_hours(self):
        q = self.quote(hours=3)
        self.pay(q, 1)
        (_, verdict, tid), = self.report(q, 1)
        self.assertEqual((verdict, tid), ("admit", f"q-{q['id']}"))
        rec, = self.admitted()
        self.assertEqual(rec["exp"], T0 + 3 * HOUR)
        self.assertEqual(A.verify_record(rec, self.s.issuer_pub, self.s.sesh, CLIENT, now=T0), "admission")
        st = self.status(q)
        self.assertEqual((st["status"], st["lease_end"]), ("admitted", T0 + 3 * HOUR))
        self.assertEqual(self.desk.status(q["id"])[1]["status"], "admitted")

    def test_second_purchase_extends(self):
        q1 = self.quote(hours=1)
        self.pay(q1, 1)
        self.report(q1, 1)
        self.clock.t += 1800
        q2 = self.quote(hours=2)
        self.pay(q2, 2, when=self.clock.t + 10)
        self.report(q2, 2)
        self.assertEqual(max(r["exp"] for r in self.admitted()), T0 + 3 * HOUR)

    def test_replay_same_tx_for_another_quote(self):
        q1 = self.quote(hours=1)
        self.pay(q1, 1)
        self.report(q1, 1)
        q2 = self.quote(hours=1)
        (_, verdict, why), = self.report(q2, 1)
        self.assertEqual((verdict, why), ("reject", "payment_already_used"))
        self.assertEqual(self.status(q2)["status"], "rejected")

    def test_quote_used_once(self):
        q = self.quote(hours=1)
        self.pay(q, 1)
        self.report(q, 1)
        self.pay(q, 2)
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(2)))[1]["status"], "admitted")
        (_, verdict, why), = self.spool(q, 2)
        self.assertEqual((verdict, why), ("reject", "quote_already_paid"))
        (_, verdict, why), = self.spool(q, 1)  # the same report again is fine
        self.assertEqual((verdict, why), ("admit", "already_admitted"))
        self.assertEqual(len(self.admitted()), 1)

    def test_wrong_amount(self):
        q = self.quote()
        self.pay(q, 1, value=q["amount"] - 1)
        (_, verdict, why), = self.report(q, 1)
        self.assertEqual(verdict, "reject")
        self.assertTrue(why.startswith("no_matching_payment: amount mismatch"), why)
        self.assertEqual(self.admitted(), [])
        # the buyer can still pay the right amount on the same quote
        self.pay(q, 2)
        self.assertEqual(self.report(q, 2)[0][1], "admit")

    def test_paid_on_wrong_chain(self):
        q = self.quote(chainid=42161)
        # right amount, but sent on Base: the tx doesn't exist on Arbitrum
        self.pay(q, 1, chainid=8453, token=C.usdc(8453))
        (_, verdict, why), = self.report(q, 1)
        self.assertEqual((verdict, why), ("wait", "not_mined_yet"))
        self.clock.t = q["expires"] + 3600
        self.assertEqual(self.w.poll_spool()[0][1], "wait")   # still retried well after expiry
        self.clock.t = T0 + W.NOT_MINED_S + 1
        (_, verdict, why), = self.w.poll_spool()
        self.assertEqual(verdict, "reject")
        self.assertIn("does not know that transaction", why)

    def test_wrong_token_and_recipient_and_failed_tx(self):
        q = self.quote()
        self.pay(q, 1, token="0x" + "ee" * 20)
        self.assertIn("no USDC transfer", self.report(q, 1)[0][2])
        self.pay(q, 2, to="0x" + "dd" * 20)
        self.assertIn("no USDC transfer", self.report(q, 2)[0][2])
        self.pay(q, 3, ok=False)
        self.assertIn("failed", self.report(q, 3)[0][2])

    def test_old_payment_not_accepted(self):
        q = self.quote()
        self.pay(q, 1, when=T0 - 2 * HOUR)
        self.assertIn("older than the quote", self.report(q, 1)[0][2])

    def test_expired_quote(self):
        q = self.quote()
        self.pay(q, 1, when=q["expires"] + 600)
        self.assertIn("after the quote expired", self.report(q, 1)[0][2])

    def test_waits_for_confirmations(self):
        q = self.quote()
        rpc = self.chains.get(42161)
        rpc.mine(tx(1), [(C.usdc(42161), PAYOUT, q["amount"])], when=T0 + 20)  # at head
        (_, verdict, why), = self.report(q, 1)
        self.assertEqual((verdict, why), ("wait", "waiting_for_confirmations"))
        rpc.head += 1
        self.assertEqual(self.w.poll_spool(), [])          # backing off
        self.clock.t += W.BACKOFF_S[0]
        self.assertEqual(self.w.poll_spool()[0][1], "admit")

    def test_chain_down_then_up(self):
        q = self.quote()
        self.pay(q, 1)
        self.chains.get(42161).down = True
        self.assertEqual(self.report(q, 1)[0][1], "wait")
        self.assertEqual(self.status(q)["note"], "chain_unavailable")
        self.chains.get(42161).down = False
        self.clock.t += W.BACKOFF_S[0]
        self.assertEqual(self.w.poll_spool()[0][1], "admit")

    def test_admission_only_for_bound_address(self):
        """Someone who learns the quote and tx can't get in: the notice needs the buyer key and
        the admission names the quoted .sesh."""
        q = self.quote()
        self.pay(q, 1)
        thief, _ = buyer_key()
        self.assertEqual(self.desk.paid(Q.make_paid(thief, q, tx(1)))[0], 403)
        # forged spool file straight on disk is rejected by the watcher too
        (self.s.state / "spool" / "forged.json").write_text(json.dumps(Q.make_paid(thief, q, tx(1))))
        (_, verdict, why), = self.w.poll_spool()
        self.assertEqual((verdict, why), ("reject", "bad_buyer_signature"))
        self.report(q, 1)
        self.assertEqual(self.admitted(OTHER), [])
        self.assertEqual(len(self.admitted(CLIENT)), 1)

    def test_state_survives_restart_and_crash_reissue(self):
        q = self.quote()
        self.pay(q, 1)
        self.report(q, 1)
        w2 = W.Watcher(self.chains, state_dir=self.s.state, seller_sesh=self.s.sesh,
                       issuer_key=str(self.s.issuer), store=str(self.s.store), confirmations=2, admission=A,
                       clock=self.clock)
        q2 = self.quote()
        self.desk.paid(Q.make_paid(self.bk, q2, tx(1)))
        self.assertEqual(w2.poll_spool()[0][2], "payment_already_used")
        # crash after issuing but before saving: the next run replaces the record
        w2.state = {"used_tx": {}, "done": {}, "clients": {}, "retry": {}}
        self.w = w2
        self.assertEqual(self.spool(q, 1)[0][1], "admit")
        self.assertEqual(len(self.admitted()), 1)

    def test_tidy_and_stop(self):
        q = self.quote(hours=1)
        self.pay(q, 1)
        self.report(q, 1)
        self.assertEqual(self.w.stop(CLIENT), 1)
        self.assertEqual(self.admitted(), [])
        q = self.quote(hours=1)
        self.pay(q, 2)
        self.report(q, 2)
        self.clock.t += 2 * HOUR
        self.assertEqual(self.w.tidy(), 1)
        self.assertEqual(self.w.state["clients"], {})

    def test_rpc_outage_never_rejects_a_real_payment(self):
        """A long RPC outage, even days past expiry, only delays the admission."""
        q = self.quote()
        self.pay(q, 1, when=q["expires"] - 60)
        self.chains.get(42161).down = True
        self.assertEqual(self.report(q, 1)[0][1], "wait")
        delays = []
        for _ in range(6):
            nxt = self.w.state["retry"][f"{q['id']}-{tx(1)[2:18]}.json"]["next"]
            delays.append(nxt - self.clock.t)
            self.clock.t = nxt
            self.assertEqual(self.w.poll_spool()[0][1], "wait")
        self.assertEqual(delays[:3], list(W.BACKOFF_S))
        self.assertEqual(max(delays), W.BACKOFF_S[-1])
        self.clock.t = q["expires"] + 3 * 86400
        self.chains.get(42161).down = False
        self.assertEqual(self.w.poll_spool()[0][1], "admit")
        self.assertEqual(self.w.state["retry"], {})

    def test_late_notice_is_still_taken(self):
        """The buyer paid in time but reports a day later (closed the terminal)."""
        q = self.quote()
        self.pay(q, 1, when=q["expires"] - 30)
        self.clock.t = q["expires"] + 86400
        self.assertEqual(self.report(q, 1)[0][1], "admit")

    def test_quote_older_than_keep_is_refused(self):
        q = self.quote()
        self.pay(q, 1)
        self.clock.t = q["issued"] + Q.KEEP_QUOTES_S + 1
        self.assertEqual(self.spool(q, 1)[0][1:], ("reject", "quote_too_old"))

    def test_block_time_window(self):
        q = self.quote()
        self.pay(q, 1, when=q["issued"] - Q.EARLY_SKEW_S - 1)
        self.assertIn("older than the quote", self.report(q, 1)[0][2])
        self.pay(q, 2, when=q["issued"] - Q.EARLY_SKEW_S)
        self.assertEqual(self.report(q, 2)[0][1], "admit")
        q = self.quote()
        self.pay(q, 3, when=q["expires"] + Q.LATE_SKEW_S + 1)
        self.assertIn("after the quote expired", self.report(q, 3)[0][2])

    def test_per_chain_confirmations(self):
        w = W.Watcher(self.chains, state_dir=self.s.state, seller_sesh=self.s.sesh, issuer_key=str(self.s.issuer),
                      store=str(self.s.store), admission=A, clock=self.clock)
        self.assertEqual((w.confirmations(42161), w.confirmations(8453)), (20, 5))
        self.assertEqual(self.w.confirmations(8453), 2)
        q = self.quote(chainid=8453)
        rpc = self.chains.get(8453)
        rpc.mine(tx(1), [(C.usdc(8453), PAYOUT, q["amount"])], when=T0 + 20)
        rpc.head += 3
        self.desk.paid(Q.make_paid(self.bk, q, tx(1)))
        self.assertEqual(w.poll_spool()[0][1:], ("wait", "waiting_for_confirmations"))
        rpc.head += 1
        self.clock.t += W.BACKOFF_S[0]
        self.assertEqual(w.poll_spool()[0][1], "admit")

    def test_state_writes_are_fsynced(self):
        import os
        from unittest import mock
        calls = []
        real = os.fsync
        with mock.patch.object(W.os, "fsync", side_effect=lambda fd: (calls.append(fd), real(fd))):
            self.w._save()
        self.assertGreaterEqual(len(calls), 1)

    def test_rpc_flag(self):
        self.assertEqual(W.parse_rpcs(["8453=https://x"]), {"8453": "https://x"})
        with self.assertRaises(SystemExit):
            W.parse_rpcs(["8453"])


if __name__ == "__main__":
    unittest.main()
