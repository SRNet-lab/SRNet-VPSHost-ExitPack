"""watcher.py: verify a reported payment on chain, then admit the bound buyer for the quoted hours."""
import io
import json
import os
import unittest
from unittest import mock

from fixtures import A, C, CLIENT, OTHER, PAYOUT, Q, T0, FakeChains, Seller, buyer_key, tx
import claimd as D
import listing as L
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
        self.w = W.Watcher(self.chains, state_dir=self.s.state, seller_sesh=self.s.sesh, listing=self.s.listing_path,
                           issuer_key=str(self.s.issuer), store=str(self.s.store), confirmations=2,
                           admission=A, clock=self.clock)
        self.bk, self.bpub = buyer_key()

    def quote(self, hours=2, chainid=42161, client=CLIENT, key=None):
        k, pub = key or (self.bk, self.bpub)
        code, doc = self.desk.quote(Q.make_request(self.s.sesh, client, hours, chainid, k, now=self.clock()))
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
        w2 = W.Watcher(self.chains, state_dir=self.s.state, seller_sesh=self.s.sesh, listing=self.s.listing_path,
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
        w = W.Watcher(self.chains, state_dir=self.s.state, seller_sesh=self.s.sesh, listing=self.s.listing_path, issuer_key=str(self.s.issuer),
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


    # ------------------------------------------------------------ seller clock a bit fast
    def test_payment_soon_after_a_fast_clocked_quote_counts(self):
        q = self.quote()
        self.pay(q, 1, when=q["issued"] - 200)    # seller clock 200 s fast, buyer paid at once
        self.assertEqual(self.report(q, 1)[0][1], "admit")
        self.assertEqual(Q.EARLY_SKEW_S, 300)

    # ------------------------------------------------------------ quote files are not trusted
    def tamper(self, q, **kw):
        """What a taken-over claim desk could do: rewrite a quote file and sign it with quote.key."""
        body = dict(q, **kw)
        doc = {"quote": body, "sig": Q.load_key(self.s.quote_key).sign(Q._canon(Q.QUOTE_DOMAIN, body)).hex()}
        (self.s.state / "quotes" / f"{q['id']}.json").write_text(json.dumps({"doc": doc, "pay_id": 1}))
        return body

    def test_tampered_quote_files_are_rejected_before_the_chain_is_read(self):
        attacker = "0x" + "bb" * 20
        for n, kw in enumerate([{"payout": attacker}, {"amount": 1}, {"hours": 999}, {"chainid": 1},
                                {"price_per_hour": 1, "amount": 2}, {"expires": T0 + 86400 * 30},
                                {"token_address": "0x" + "cc" * 20}], start=1):
            key = buyer_key()
            q = self.tamper(self.quote(key=key), **kw)
            self.chains.get(42161).down = True     # a chain read would only make it wait
            out = self.report(q, n, key=key[0])
            self.chains.get(42161).down = False
            self.assertEqual(out[0][1], "reject", kw)
            self.assertIn("quote_does_not_match_listing", out[0][2])
            self.clock.t += Q.QUOTE_TTL_S + 1      # room for the next open quote
        self.assertEqual(self.admitted(), [])

    def test_unsigned_or_foreign_quote_file_is_rejected(self):
        q = self.quote()
        f = self.s.state / "quotes" / f"{q['id']}.json"
        rec = json.loads(f.read_text())
        rec["doc"]["sig"] = "00" * 64
        f.write_text(json.dumps(rec))
        self.pay(q, 1)
        self.assertIn("not signed", self.report(q, 1)[0][2])

    def test_desk_sees_a_same_size_rewrite_in_the_same_mtime_tick(self):
        q_old = self.quote(hours=1)             # at 1.00; the desk has cached this listing
        st = self.s.listing_path.stat()
        self.s.relist("2.00")
        new = self.s.listing_path.read_bytes()
        L.write(self.s.listing_path, self.s.listing_doc)
        os.utime(self.s.listing_path, ns=(st.st_atime_ns, st.st_mtime_ns))   # what a coarse clock gives
        self.assertEqual(self.s.listing_path.read_bytes(), new)
        self.assertGreater(self.quote(hours=1)["amount"], 2_000_000)
        self.assertLess(q_old["amount"], 2_000_000)
        self.assertEqual([p.name for p in self.s.listing_path.parent.glob(".listing-*")], [])

    def test_listing_change_keeps_earlier_quotes_payable(self):
        q_old = self.quote(hours=1)             # at 1.00
        self.clock.t += 60
        self.s.relist("2.00")
        q_new = self.quote(hours=1)
        self.assertGreater(q_new["amount"], 2_000_000)
        self.pay(q_old, 1, when=T0 + 30)
        self.pay(q_new, 2, when=T0 + 90)
        self.assertEqual(self.report(q_old, 1)[0][1], "admit")
        self.assertEqual(self.report(q_new, 2)[0][1], "admit")
        # a "1.00" quote dated after the change is not admitted on its own: the seller decides
        q3 = self.quote(hours=1)
        forged = self.tamper(q3, amount=1_000_500, price_per_hour=1_000_000,
                             issued=self.clock.t + W.LISTING_SKEW_S + 1,
                             expires=self.clock.t + W.LISTING_SKEW_S + 600)
        self.pay(forged, 3, when=forged["issued"] + 10)
        out = self.report(forged, 3)
        self.assertEqual(out[0][1], "hold")
        self.assertTrue(out[0][2].startswith("needs_seller: amount does not match"), out)
        self.assertNotIn(f"q-{forged['id']}", [r["tid"] for r in self.admitted()])

    def fresh_watcher(self, name="fresh-state.json"):
        self.w = W.Watcher(self.chains, state_dir=self.s.state, seller_sesh=self.s.sesh, listing=self.s.listing_path,
                           issuer_key=str(self.s.issuer), store=str(self.s.store), confirmations=2, admission=A,
                           clock=self.clock, state_file=self.s.d / name)
        return self.w

    def test_previous_listing_from_the_installer_is_honoured(self):
        q_old = self.quote(hours=1)             # at 1.00, the watcher never saw this listing
        self.clock.t += 60
        self.assertTrue(L.retire(self.s.listing_path, now=self.clock.t))   # what install.sh does
        prev = self.s.listing_path.with_name("previous.json")
        os.utime(prev, (T0 - 86400, T0 - 86400))  # copied or restored: the mtime doesn't matter
        self.s.relist("3.00")
        self.fresh_watcher()
        self.pay(q_old, 1)
        self.assertEqual(self.report(q_old, 1)[0][1], "admit")
        self.assertIn({"terms": Q.listing_terms(json.loads(prev.read_text())["history"][0]["doc"]["listing"]),
                       "until": T0 + 60}, self.w.state["listings"])

    def test_old_style_previous_json_is_ignored(self):
        q_old = self.quote(hours=1)
        self.s.listing_path.with_name("previous.json").write_text(json.dumps(self.s.listing_doc))
        self.s.relist("3.00")
        self.fresh_watcher()
        self.pay(q_old, 1)
        self.assertEqual(self.report(q_old, 1)[0][1], "hold")   # no time in it: the seller decides

    # ------------------------------------------------------------ unknown old price
    def test_price_changed_by_hand_holds_for_the_seller_then_admit(self):
        q_old = self.quote(hours=2)             # at 1.00
        self.s.relist("2.00")                   # by hand, no previous.json, watcher never saw 1.00
        self.fresh_watcher()
        self.pay(q_old, 1)
        err = io.StringIO()
        with mock.patch.object(W.sys, "stderr", err):
            out = self.report(q_old, 1)
        self.assertEqual(out[0][1], "hold")
        self.assertIn(f"srnet-seller admit {q_old['id']}", err.getvalue())
        self.assertEqual(self.admitted(), [])
        st = self.status(q_old)
        self.assertEqual((st["status"], st["note"]), ("waiting", "needs_seller"))
        self.assertEqual(self.desk.status(q_old["id"])[1]["status"], "waiting")
        self.assertEqual(list((self.s.state / "spool").glob("*.json")), [])
        self.assertEqual(len(list((self.s.state / "spool" / "held").glob("*.json"))), 1)
        self.assertEqual(self.w.poll_spool(), [])          # parked, not retried
        # the seller lists and admits it with the CLI while the watcher runs
        buf = io.StringIO()
        with mock.patch.object(W.sys, "stdout", buf):
            W.main(["held", "--state", str(self.w.state_path)])
            W.main(["admit", "--state", str(self.w.state_path), "--quote", q_old["id"]])
        self.assertIn(q_old["id"], buf.getvalue())
        with mock.patch.object(W.sys, "stderr", io.StringIO()):
            out = self.w.poll_spool()
        self.assertEqual(out[0][1:], ("admit", f"q-{q_old['id']}"))
        rec, = self.admitted()
        self.assertEqual(rec["exp"], T0 + 2 * HOUR)
        self.assertEqual(self.w.state["needs_seller"], {})
        self.assertEqual(list(self.w.admit_dir.iterdir()), [])

    def test_held_quote_still_needs_the_right_payment(self):
        q_old = self.quote(hours=1)
        self.s.relist("2.00")
        self.fresh_watcher()
        self.pay(q_old, 1, value=q_old["amount"] - 1)
        out = self.report(q_old, 1)
        self.assertEqual(out[0][1], "reject")
        self.assertTrue(out[0][2].startswith("no_matching_payment"))
        self.assertEqual(self.w.state["needs_seller"], {})

    def test_admit_of_an_unknown_quote_does_nothing(self):
        q = self.quote()
        with mock.patch.object(W.sys, "stdout", io.StringIO()):
            W.main(["admit", "--state", str(self.w.state_path), "--quote", q["id"]])
        err = io.StringIO()
        with mock.patch.object(W.sys, "stderr", err):
            self.assertEqual(self.w.poll_spool(), [])
        self.assertIn("no paid quote is waiting", err.getvalue())
        self.assertEqual(self.admitted(), [])
        with self.assertRaises(SystemExit):
            W.main(["admit", "--state", str(self.w.state_path), "--quote", "../x"])

    def test_desk_with_the_quote_key_still_cant_admit_alone(self):
        q = self.quote(hours=1)
        odd = self.tamper(q, price_per_hour=L.MIN_PRICE_UNITS, hours=168, amount=168 * L.MIN_PRICE_UNITS + 7)
        self.pay(odd, 1)
        with mock.patch.object(W.sys, "stderr", io.StringIO()):
            self.assertEqual(self.report(odd, 1)[0][1], "hold")      # the seller decides
        self.assertEqual(self.admitted(), [])

    def test_held_only_within_the_minimum_price_and_largest_max_hours(self):
        self.s.relist("2.00")                                       # history: 1.00 and 2.00, both 168 h
        for n, kw in enumerate([dict(price_per_hour=1, hours=999, amount=1_000),          # 0.000001/h
                                dict(price_per_hour=L.MIN_PRICE_UNITS - 1, hours=1, amount=L.MIN_PRICE_UNITS),
                                dict(price_per_hour=L.MIN_PRICE_UNITS, hours=169, amount=169 * L.MIN_PRICE_UNITS + 1)],
                               start=1):
            key = buyer_key()
            q = self.tamper(self.quote(hours=1, key=key), **kw)
            self.pay(q, n)
            out = self.report(q, n, key=key[0])
            self.assertEqual(out[0][1], "reject", kw)
            self.assertIn("quote_does_not_match_listing", out[0][2])
            self.clock.t += Q.QUOTE_TTL_S + 1
        self.assertEqual(self.w.state["needs_seller"], {})

    def hold_one(self):
        q = self.quote(hours=2)
        self.s.relist("2.00")
        self.fresh_watcher()
        self.pay(q, 1)
        with mock.patch.object(W.sys, "stderr", io.StringIO()):
            self.assertEqual(self.report(q, 1)[0][1], "hold")
        return q

    def test_held_list_shows_amount_hours_price_chain_and_age(self):
        q = self.hold_one()
        buf = io.StringIO()
        with mock.patch.object(W.sys, "stdout", buf), mock.patch.object(W.time, "time", return_value=T0 + 2 * 86400):
            W.main(["held", "--state", str(self.w.state_path)])
        out = buf.getvalue()
        for want in (q["id"], f"paid {C.fmt_usdc(q['amount'])} USDC", "for 2 h", "at 1.00 USDC/h", "Arbitrum One",
                     "2.0 days old", "days left to admit", tx(1)):
            self.assertIn(want, out)

    def test_unanswered_held_quote_expires_after_quote_retention(self):
        q = self.hold_one()
        self.clock.t = q["issued"] + Q.KEEP_QUOTES_S - 60
        self.w.tidy()
        self.assertIn(q["id"], self.w.state["needs_seller"])
        self.clock.t = q["issued"] + Q.KEEP_QUOTES_S + 1
        with mock.patch.object(W.sys, "stderr", io.StringIO()):
            self.w.tidy()
        self.assertEqual(self.w.state["needs_seller"], {})
        self.assertEqual(list((self.s.state / "spool" / "held").glob("*.json")), [])
        self.assertEqual(len(list((self.s.state / "spool" / "rejected").glob("*.json"))), 1)
        self.assertEqual(self.status(q)["status"], "rejected")
        self.assertEqual(Q.KEEP_QUOTES_S // 86400, 8)                # the README says 8 days

    def test_admit_refuses_another_user_and_survives_a_stuck_request(self):
        q = self.hold_one()
        other = self.w.state_path.parent.stat().st_uid + 1     # say root, when the files are srnet-watch's
        with mock.patch.object(W.os, "geteuid", return_value=other), self.assertRaises(SystemExit) as e:
            W.main(["admit", "--state", str(self.w.state_path), "--quote", q["id"]])
        self.assertIn("watcher's user", str(e.exception))
        self.assertFalse((self.w.admit_dir / q["id"]).exists())
        with mock.patch.object(W.sys, "stdout", io.StringIO()):
            W.main(["admit", "--state", str(self.w.state_path), "--quote", q["id"]])
        err = io.StringIO()
        real = W.Path.unlink
        def stuck(p, *a, **kw):
            if p.parent == self.w.admit_dir:
                raise PermissionError(13, "denied")
            return real(p, *a, **kw)
        with mock.patch.object(W.Path, "unlink", stuck), mock.patch.object(W.sys, "stderr", err):
            out = self.w.poll_spool()
            self.assertEqual(out[0][1], "admit")                   # handled once, the watcher keeps running
            self.assertEqual(self.w.poll_spool(), [])
        self.assertEqual(err.getvalue().count("can't remove admit request"), 1)

    def test_listing_history_is_short(self):
        for i in range(W.LISTING_HISTORY + 4):
            self.clock.t += 60
            self.s.relist(f"1.{10 + i}")
            self.w.poll_spool()
        self.assertEqual(len(self.w.state["listings"]), W.LISTING_HISTORY + 1)

    def test_broken_listing_logged_once_per_few_minutes(self):
        self.s.listing_path.write_text("{")
        err = io.StringIO()
        with mock.patch.object(W.sys, "stderr", err):
            for _ in range(20):
                self.w.poll_spool()
                self.clock.t += 10
            self.assertEqual(err.getvalue().count("can't use the listing yet"), 1)
            self.clock.t += W.LISTING_LOG_S
            self.w.poll_spool()
            self.assertEqual(err.getvalue().count("can't use the listing yet"), 2)
            self.s.listing_path.write_text("[]")      # a different problem is logged at once
            self.w.poll_spool()
            self.assertEqual(err.getvalue().count("can't use the listing yet"), 3)

    def test_missing_or_bad_listing_waits_instead_of_rejecting(self):
        q = self.quote()
        self.pay(q, 1)
        good = self.s.listing_path.read_text()
        self.s.listing_path.write_text(good.replace('"max_hours": 168', '"max_hours": 9999'))
        out = self.report(q, 1)
        self.assertEqual(out[0][1], "wait")
        self.assertTrue(out[0][2].startswith("listing_unavailable"))
        self.s.listing_path.write_text(good)
        self.clock.t += W.BACKOFF_S[0]
        self.assertEqual(self.w.poll_spool()[0][1], "admit")


if __name__ == "__main__":
    unittest.main()
