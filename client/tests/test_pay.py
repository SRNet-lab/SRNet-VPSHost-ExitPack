"""Buying exit time: quote bound to this client, cap, exact amount on the right chain, the seller's
watcher admitting us, multi-hour and renewal, and the plain "no matching payment yet" messages.

The seller side is the real claim desk and watcher from the seller pack, on a fake chain."""
import json
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import ME, Chain, Clock, desk_transport

F = helpers.seller_fixtures()
import admission as A  # noqa: E402
import chains as C  # noqa: E402
import claimd as D  # noqa: E402
import quote as Q  # noqa: E402
import watcher as Wt  # noqa: E402

from srnet_exits import claim, pay  # noqa: E402

HOUR = 3600
OTHER_ME = F.OTHER


@unittest.skipUnless(helpers.HAVE_CORE, "srnet-client (the SRNet core) not found")
class Flow(unittest.TestCase):
    def setUp(self, **seller):
        from srnet.state import State
        self.clock = Clock(F.T0)
        self.s = F.Seller(price="1.00", **seller)
        self.desk = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock)
        self.chains = {c: Chain(c, self.clock) for c in (42161, 8453)}
        get = self.chains.get

        class Chains:
            def get(_, cid):
                if cid not in self.chains:
                    raise F.evm.RpcError("no rpc")
                return get(cid)
        self.watcher = Wt.Watcher(Chains(), state_dir=self.s.state, seller_sesh=self.s.sesh, listing=self.s.listing_path,
                                  issuer_key=str(self.s.issuer), store=str(self.s.store), confirmations=2,
                                  admission=A, clock=self.clock)
        self.home = Path(tempfile.mkdtemp())
        self.st = State(self.home)
        self.cfg = {}
        self.events = []
        self.buyer = self.make_buyer()

    def make_buyer(self, cfg=None):
        def sleep(s):
            self.clock.sleep(s)
            self.watcher.poll_spool()   # the seller's watcher runs alongside
        return pay.Buyer(self.st, self.cfg if cfg is None else cfg, transport=desk_transport(self.desk),
                         rpc_factory=lambda cid: self.chains[cid], clock=self.clock, sleep=sleep)

    def buy(self, hours=1, chain=None, **kw):
        return self.buyer.quote(self.s.listing_doc, hours, ME, chain=chain, **kw)

    def pay_quote(self, rec, value=None, chain=None, to=None, n=1):
        q = rec["doc"]["quote"]
        cid = chain or q["chainid"]
        return self.chains[cid].pay(F.tx(n), C.usdc(cid), to or q["payout"], q["amount"] if value is None else value)

    def watch(self, rec):
        return self.buyer.watch(rec, on_event=lambda k, d: self.events.append(k), poll_s=5)

    def admitted(self, client=ME):
        return [r for r in A.Store(self.s.store).records() if r["client_sesh"] == client]


class QuoteBinding(Flow):
    def test_quote_is_bound_to_this_client_and_listing(self):
        rec = self.buy(hours=2)
        q = rec["doc"]["quote"]
        self.assertEqual(q["client_sesh"], ME)
        self.assertEqual(q["seller"], self.s.sesh)
        self.assertEqual(q["hours"], 2)
        self.assertEqual(q["chainid"], 42161, "Arbitrum by default")
        self.assertEqual(q["buyer_key"], Q.pub_hex(pay.buyer_key(self.home)))
        self.assertEqual(q["token_address"].lower(), "0xaf88d065e77c8cC2239327C5EDb3A432268e5831".lower())
        self.assertTrue(2_000_000 < q["amount"] <= 2_000_000 + Q.PAY_ID_MAX)
        self.assertLessEqual(q["expires"] - q["issued"], 16 * 60)
        info = self.buyer.info(rec)
        self.assertEqual(info["address"], F.PAYOUT)
        self.assertEqual(info["uri"], "ethereum:0xaf88d065e77c8cC2239327C5EDb3A432268e5831@42161" f"/transfer?address={F.PAYOUT}"
                                      f"&uint256={q['amount']}")
        self.assertEqual(self.st.data["exits"]["quotes"][q["id"]]["state"], "open")

    def test_quote_request_is_signed_with_the_buyer_key(self):
        seen = []
        real = self.desk.quote
        self.desk.quote = lambda req, src="local": (seen.append(req), real(req, src))[1]
        self.buy()
        req = seen[0]
        self.assertEqual(req["buyer_key"], Q.pub_hex(pay.buyer_key(self.home)))
        self.assertEqual(req["ts"], int(self.clock()))
        self.assertIsNone(Q.request_error(req, self.s.sesh, [42161, 8453], 168, now=int(self.clock())))

    def test_clock_far_off_gets_a_clock_hint(self):
        b = pay.Buyer(self.st, self.cfg, transport=desk_transport(self.desk),
                      rpc_factory=lambda cid: self.chains[cid], clock=lambda: self.clock() - 600)
        with self.assertRaises(pay.BuyError) as e:
            b.quote(self.s.listing_doc, 1, ME)
        self.assertIn("clock", str(e.exception))

    def test_payment_screen_shows_the_payment_id_part(self):
        from srnet_exits import cli
        rec = self.buy(hours=2)
        i = self.buyer.info(rec)
        q = rec["doc"]["quote"]
        self.assertEqual(i["price"], 2_000_000)
        self.assertEqual(i["extra"], q["amount"] - 2_000_000)
        self.assertTrue(0 < i["extra"] <= Q.extra_cap(2_000_000))
        text = cli.payment_text(i)
        self.assertIn(f"That is 2.00 USDC for 2 h plus {C.fmt_usdc(i['extra'])} USDC as a payment ID", text)

    def test_two_open_quotes_get_different_amounts(self):
        a = self.buy()["doc"]["quote"]["amount"]
        b = self.buy()["doc"]["quote"]["amount"]
        self.assertNotEqual(a, b)

    def test_base_on_request(self):
        self.assertEqual(self.buy(chain="base")["doc"]["quote"]["chainid"], 8453)
        self.assertEqual(self.buy(chain="8453")["doc"]["quote"]["chainid"], 8453)

    def evil_transport(self, change, key=None):
        """Seller answers with a quote changed by change(quote), re-signed with key (default: the real key)."""
        real = desk_transport(self.desk)
        key = key or Q.load_key(self.s.quote_key)

        def evil(url, body, timeout=30, dns=None):
            code, doc = real(url, body)
            if url.endswith("/quote") and code == 200:
                change(doc["quote"])
                doc["sig"] = key.sign(Q._canon(Q.QUOTE_DOMAIN, doc["quote"])).hex()
            return code, doc
        return evil

    def test_quote_tampering_is_caught_even_when_signed(self):
        cases = {"client_sesh": OTHER_ME, "payout": "0x" + "bb" * 20, "hours": 3, "chainid": 8453,
                 "token_address": "0x" + "cc" * 20, "amount": 1, "price_per_hour": 2_000_000,
                 "expires": F.T0 - 10}
        for field, value in cases.items():
            self.buyer.transport = self.evil_transport(lambda q, f=field, v=value: q.__setitem__(f, v))
            with self.assertRaises(pay.BuyError, msg=field):
                self.buy(hours=1)
            self.clock.t += 20 * 60  # let the desk's open quotes lapse
        self.assertEqual(self.st.data.get("exits", {}).get("quotes", {}), {})

    def test_quote_signed_by_another_key_is_refused(self):
        k = Path(tempfile.mkdtemp()) / "other.key"
        Q.new_key_file(k)
        self.buyer.transport = self.evil_transport(lambda q: None, key=Q.load_key(k))
        with self.assertRaises(pay.BuyError) as e:
            self.buy()
        self.assertIn("not signed by this seller", str(e.exception))

    def test_bad_listing_signature_or_old_listing(self):
        doc = json.loads(json.dumps(self.s.listing_doc))
        doc["listing"]["payout"] = "0x" + "bb" * 20
        with self.assertRaises(pay.BuyError) as e:
            self.buyer.quote(doc, 1, ME)
        self.assertIn("signature", str(e.exception))
        old = {"listing": {"v": 2, "seller": self.s.sesh}, "sig": "00"}
        with self.assertRaises(pay.BuyError):
            self.buyer.quote(old, 1, ME)

    def test_hours_out_of_range(self):
        for h in (0, 169, "2"):
            with self.assertRaises(pay.BuyError):
                self.buy(hours=h)


class Cap(Flow):
    def test_default_cap_is_twenty_usdc(self):
        self.buy(hours=19)
        with self.assertRaises(pay.BuyError) as e:
            self.buy(hours=20)
        self.assertIn("over your limit of 20", str(e.exception))

    def test_cap_from_flag_and_config(self):
        with self.assertRaises(pay.BuyError):
            self.buy(hours=3, max_usdc="2.5")
        self.buyer = self.make_buyer({"exits": {"max_payment_usdc": "2"}})
        with self.assertRaises(pay.BuyError):
            self.buy(hours=2)
        self.buy(hours=1)
        self.desk.cap = C.parse_usdc("100")   # the seller's own cap is separate
        self.assertEqual(self.buy(hours=30, max_usdc="40")["doc"]["quote"]["hours"], 30)

    def test_seller_cap_refuses_large_quotes(self):
        self.desk.cap = C.parse_usdc("5")
        with self.assertRaises(pay.BuyError):
            self.buy(hours=6)


class Pay(Flow):
    def test_exact_payment_is_seen_reported_and_admitted(self):
        rec = self.buy(hours=2)
        self.pay_quote(rec)
        out = self.watch(rec)
        self.assertEqual(out["status"], "admitted", out)
        self.assertIn("seen", self.events)
        self.assertIn("reported", self.events)
        lease = pay.active_lease(self.st, self.s.sesh, now=self.clock())
        self.assertAlmostEqual(lease["end"] - self.clock(), 2 * HOUR, delta=120)
        self.assertEqual(lease["client_sesh"], ME)
        self.assertEqual(len(self.admitted()), 1)
        self.assertEqual(rec["state"], "admitted")

    def test_payment_arriving_later(self):
        rec = self.buy()
        n = [0]
        real = self.buyer.sleep

        def sleep(s):
            n[0] += 1
            if n[0] == 3:
                self.pay_quote(rec)
            real(s)
        self.buyer.sleep = sleep
        self.assertEqual(self.watch(rec)["status"], "admitted")
        self.assertIn("waiting", self.events)

    def test_paid_with_tx_hash_skips_scanning(self):
        rec = self.buy()
        tx = self.pay_quote(rec)
        self.chains[42161].logs.clear()  # the scan would not find it; the tx hash is enough
        self.assertEqual(self.buyer.watch(rec, tx=tx, poll_s=5)["status"], "admitted")

    def test_wrong_amount_is_a_clear_no_match(self):
        rec = self.buy()
        q = rec["doc"]["quote"]
        self.pay_quote(rec, value=1_000_000)
        out = self.watch(rec)
        self.assertEqual(out["status"], "expired")
        self.assertIn(f"No payment of exactly {C.fmt_usdc(q['amount'])} USDC on Arbitrum", out["message"])
        self.assertIn("Don't pay this quote now", out["message"])
        self.assertNotIn("Send exactly", out["message"])
        self.assertIn("The seller received 1.00 USDC", out["message"])
        self.assertEqual(self.admitted(), [])

    def test_wrong_chain_is_a_clear_no_match(self):
        rec = self.buy()
        self.pay_quote(rec, chain=8453)
        out = self.watch(rec)
        self.assertEqual(out["status"], "expired")
        self.assertIn("arrived before this quote expired", out["message"])
        self.assertIn("arrived on Base", out["message"])
        self.assertEqual(self.admitted(), [])

    def test_wrong_tx_hash_is_rejected_by_the_seller(self):
        rec = self.buy()
        tx = self.chains[42161].pay(F.tx(9), C.usdc(42161), rec["doc"]["quote"]["payout"], 5)
        out = self.buyer.watch(rec, tx=tx, poll_s=5)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("No matching payment yet", out["message"])
        self.assertEqual(rec["state"], "rejected")

    def test_one_payment_cannot_buy_twice(self):
        a = self.buy()
        tx = self.pay_quote(a)
        self.assertEqual(self.watch(a)["status"], "admitted")
        b = self.buy()
        out = self.buyer.watch(b, tx=tx, poll_s=5)
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(len(self.admitted()), 1)

    def test_a_quote_cannot_be_paid_twice(self):
        a = self.buy()
        self.pay_quote(a, n=1)
        self.assertEqual(self.watch(a)["status"], "admitted")
        end = pay.active_lease(self.st, self.s.sesh, self.clock())["end"]
        self.pay_quote(a, n=2)
        notice = Q.make_paid(pay.buyer_key(self.home), a["doc"]["quote"], F.tx(2))
        self.desk.paid(notice)
        self.watcher.poll_spool()
        self.assertEqual(len(self.admitted()), 1)
        self.assertEqual(pay.active_lease(self.st, self.s.sesh, self.clock())["end"], end)

    def test_another_client_cannot_report_our_quote(self):
        rec = self.buy()
        tx = self.pay_quote(rec)
        k = Path(tempfile.mkdtemp()) / "k"
        Q.new_key_file(k)
        code, out = self.desk.paid(Q.make_paid(Q.load_key(k), rec["doc"]["quote"], tx))
        self.assertEqual(code, 403)

    def test_expired_quote_is_not_honoured(self):
        """A payment made after the quote expired is refused by block time, whenever it's reported."""
        rec = self.buy()
        self.clock.t += 16 * 60 + pay.PAID_GRACE_S + 5
        self.pay_quote(rec)
        self.assertEqual(self.buyer.report(rec, F.tx(1))["status"], "received")
        self.watcher.poll_spool()
        out = self.buyer.status(rec)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("after the quote expired", out["message"])

    def test_late_report_of_an_in_time_payment_is_admitted(self):
        """Paid at minute 14, closed the terminal, `srnet paid --tx` an hour later: still admitted."""
        rec = self.buy()
        self.clock.t += 14 * 60
        tx = self.pay_quote(rec)
        self.clock.t += 3600
        rec["state"] = "expired"
        self.assertIs(self.buyer.find(with_expired=True), rec)
        with self.assertRaises(pay.BuyError):
            self.buyer.find()
        rec["state"] = "open"
        self.assertEqual(self.buyer.watch(rec, tx=tx, poll_s=5)["status"], "admitted")

    def test_rpc_errors_at_the_deadline_do_not_invite_a_second_payment(self):
        rec = self.buy()
        self.chains[42161].down = True
        out = self.watch(rec)
        self.assertEqual(out["status"], "unknown")
        self.assertIn("Couldn't read Arbitrum One", out["message"])
        self.assertIn("Don't pay again", out["message"])
        self.assertIn("srnet paid --tx", out["message"])
        self.assertNotIn("No payment of exactly", out["message"])

    def test_one_good_scan_clears_an_earlier_rpc_error(self):
        rec = self.buy()
        self.chains[42161].down = True
        with self.assertRaises(F.evm.RpcError):
            self.buyer.scan_once(rec)
        rec["scan_error"] = "connection refused"
        self.chains[42161].down = False
        self.assertIn("No payment of exactly", self.watch(rec)["message"])

    def test_seller_side_chain_outage_is_explained(self):
        rec = self.buy()
        tx = self.pay_quote(rec)
        self.watcher.chains = type("Down", (), {"get": lambda _, cid: (_ for _ in ()).throw(F.evm.RpcError("down"))})()
        out = self.buyer.watch(rec, tx=tx, poll_s=5)
        self.assertEqual(out["status"], "waiting")
        self.assertIn("can't read the chain", out["message"])
        self.assertIn("Don't pay again", out["message"])


class Renew(Flow):
    def test_buy_again_extends_the_lease(self):
        a = self.buy(hours=1)
        self.pay_quote(a, n=1)
        self.assertEqual(self.watch(a)["status"], "admitted")
        end1 = pay.active_lease(self.st, self.s.sesh, self.clock())["end"]
        self.clock.t += 50 * 60
        b = self.buy(hours=3)
        self.pay_quote(b, n=2)
        self.assertEqual(self.watch(b)["status"], "admitted")
        lease = pay.active_lease(self.st, self.s.sesh, self.clock())
        self.assertAlmostEqual(lease["end"] - end1, 3 * HOUR, delta=120)
        self.assertEqual(lease["quotes"], [a["id"], b["id"]])

    def test_ending_flag_ten_minutes_before(self):
        a = self.buy(hours=1)
        self.pay_quote(a)
        self.watch(a)
        end = pay.active_lease(self.st, self.s.sesh, self.clock())["end"]
        self.assertFalse(pay.leases(self.st, now=end - 11 * 60)[0]["ending"])
        self.assertTrue(pay.leases(self.st, now=end - 9 * 60)[0]["ending"])
        self.assertIsNone(pay.active_lease(self.st, self.s.sesh, now=end + 1))


class Texts(unittest.TestCase):
    Q = {"amount": 2_000_417, "chainid": 8453, "payout": "0x" + "aa" * 20}

    def test_no_match(self):
        t = pay.no_match_text(self.Q)
        self.assertEqual(t, f"No matching payment yet. Send exactly 2.000417 USDC on Base to 0x{'aa' * 20}.")

    def test_reason_text(self):
        self.assertIn("No matching payment yet", pay.reason_text(self.Q, "no_matching_payment: amount"))
        self.assertIn("already used", pay.reason_text(self.Q, "payment_already_used"))


class ClaimUrl(unittest.TestCase):
    def test_listing_claim_url_and_loopback_test_override(self):
        import os
        from unittest import mock
        body = {"claim_url": "http://" + "y" * 52 + ".sesh/claim"}
        self.assertEqual(claim.base_url(body), body["claim_url"])
        with self.assertRaises(claim.ClaimError):
            claim.base_url({"claim_url": "http://evil.example/claim"})
        with mock.patch.dict(os.environ, {"SRNET_EXITS_TEST_CLAIM_URL": "http://127.0.0.1:9/claim"}):
            self.assertEqual(claim.base_url(body), body["claim_url"], "ignored unless test chains are on")
            self.assertEqual(claim.base_url(body, testnets=True), "http://127.0.0.1:9/claim")
        with mock.patch.dict(os.environ, {"SRNET_EXITS_TEST_CLAIM_URL": "http://10.0.0.1/claim"}):
            with self.assertRaises(claim.ClaimError):
                claim.base_url(body, testnets=True)


if __name__ == "__main__":
    unittest.main()
