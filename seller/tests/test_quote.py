"""quote.py and chains.py: quote binding, unique amounts, caps, expiry, payment matching."""
import unittest

from fixtures import C, CLIENT, OTHER, PAYOUT, Q, T0, Seller, buyer_key, tx

USDC_ARB = C.usdc(42161)


class Chains(unittest.TestCase):
    def test_official_usdc_only(self):
        self.assertEqual(C.usdc(42161), "0xaf88d065e77c8cc2239327c5edb3a432268e5831")
        self.assertEqual(C.usdc(8453), "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
        self.assertEqual(set(C.table()), {42161, 8453})
        for bad in (1, 10, 137, 84532, 421614, 31337, "8453", True):
            self.assertFalse(C.supported(bad), bad)
        self.assertTrue(C.supported(84532, testnets=True))
        with self.assertRaises(C.ChainError):
            C.usdc(1)

    def test_amounts(self):
        self.assertEqual(C.parse_usdc("1.50"), 1_500_000)
        self.assertEqual(C.parse_usdc("0.000001"), 1)
        for bad in ("0", "-1", "1.0000001", "abc", ""):
            with self.assertRaises(C.ChainError):
                C.parse_usdc(bad)
        self.assertEqual(C.fmt_usdc(1_500_000), "1.50")
        self.assertEqual(C.fmt_usdc(3_000_417), "3.000417")
        self.assertEqual(C.fmt_usdc(5), "0.000005")

    def test_eip681(self):
        self.assertEqual(C.eip681(8453, PAYOUT, 3_000_417),
                         "ethereum:0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@8453/transfer?address="
                         + PAYOUT + "&uint256=3000417")
        with self.assertRaises(C.ChainError):
            C.eip681(1, PAYOUT, 1)

    def test_rpc_override_and_names(self):
        self.assertEqual(C.rpc_url(8453), "https://mainnet.base.org")
        self.assertEqual(C.rpc_url(8453, {"8453": "https://my.rpc"}), "https://my.rpc")
        self.assertEqual(C.by_name("Base"), 8453)
        self.assertEqual(C.by_name("arbitrum"), 42161)
        with self.assertRaises(C.ChainError):
            C.by_name("polygon")


class Quotes(unittest.TestCase):
    def setUp(self):
        self.s = Seller(price="1.00", max_hours=24)
        self.qkey = Q.load_key(self.s.quote_key)
        self.bk, self.bpub = buyer_key()
        self.req = Q.make_request(self.s.sesh, CLIENT, 3, 42161, self.bk, now=T0)

    def quote(self, req=None, taken=(), cap=20_000_000, now=T0):
        return Q.make_quote(self.qkey, req or self.req, payout=PAYOUT, price_per_hour=1_000_000,
                            taken=set(taken), cap=cap, now=now)

    def test_request_checks(self):
        ok = lambda r: Q.request_error(r, self.s.sesh, [42161, 8453], 24, now=T0)  # noqa: E731
        self.assertIsNone(ok(self.req))
        self.assertEqual(ok(Q.sign_request({**self.req, "hours": 25}, self.bk)), "at most 24 hours per purchase")
        self.assertEqual(ok(Q.sign_request({**self.req, "hours": 0}, self.bk)), "hours must be a whole number, at least 1")
        self.assertEqual(ok(Q.sign_request({**self.req, "hours": 1.5}, self.bk)), "hours must be a whole number, at least 1")
        self.assertEqual(ok(Q.sign_request({**self.req, "chainid": 1}, self.bk)), "this seller does not take payments on that chain")
        self.assertEqual(Q.request_error(self.req, self.s.sesh, [8453], 24, now=T0),
                         "this seller does not take payments on that chain")
        self.assertEqual(ok(Q.sign_request({**self.req, "token": "USDT"}, self.bk)), "only USDC is accepted")
        self.assertEqual(ok(Q.sign_request({**self.req, "seller": OTHER}, self.bk)), "wrong seller")
        self.assertEqual(ok(Q.sign_request({**self.req, "client_sesh": "x.sesh"}, self.bk)), "bad client_sesh")
        self.assertEqual(ok(Q.sign_request({**self.req, "extra": 1}, self.bk)), "bad request")

    def test_multi_hour_amount_is_unique_and_signed(self):
        doc, pid = self.quote()
        q = doc["quote"]
        self.assertEqual(q["amount"], 3_000_000 + pid)
        self.assertTrue(1 <= pid <= Q.PAY_ID_MAX)
        self.assertEqual(q["token_address"], USDC_ARB)
        self.assertEqual(q["expires"] - q["issued"], Q.QUOTE_TTL_S)
        self.assertTrue(Q.verify_quote(doc, self.s.listing["quote_key"]))
        bad = {"quote": {**q, "amount": q["amount"] + 1}, "sig": doc["sig"]}
        self.assertFalse(Q.verify_quote(bad, self.s.listing["quote_key"]))

    def test_unique_amount_skips_taken(self):
        seq = iter([5, 5, 7])
        amt, pid = Q.unique_amount(100, {106}, rng=lambda n: next(seq))
        self.assertEqual((amt, pid), (108, 8))
        with self.assertRaises(Q.QuoteError) as e:
            Q.unique_amount(100, set(range(101, 102 + int(Q.POOL_BUSY * Q.PAY_ID_MAX))))
        self.assertIn("busy", str(e.exception))

    def test_unique_amount_tracks_full_amounts_across_prices(self):
        """1h at 1.00 with id 5000 and 1h at 1.004 with id 1000 are the same amount; never hand it out twice."""
        taken = {1_000_000 + 5000}
        amt, _ = Q.unique_amount(1_004_000, taken, rng=lambda n: 999)
        self.assertNotEqual(amt, 1_005_000)
        self.assertNotIn(amt, taken)

    def test_unique_amount_stays_small_until_busy(self):
        base = 10_000_000                                   # 10 USDC: 5% is over PAY_ID_MAX
        widths = []
        Q.unique_amount(base, set(), rng=lambda n: widths.append(n) or 0)
        self.assertEqual(widths[-1], Q.PAY_ID_SMALL)        # < 1 cent normally
        half = {base + p for p in range(1, Q.PAY_ID_SMALL // 2 + 2)}
        amt, pid = Q.unique_amount(base, half, rng=lambda n: widths.append(n) or n - 1)
        self.assertEqual(widths[-1], 19_999)                # then grows in small steps
        self.assertTrue(Q.PAY_ID_SMALL < pid <= 19_999 and amt not in half)
        more = {base + p for p in range(1, 10_001)}
        Q.unique_amount(base, more, rng=lambda n: widths.append(n) or n - 1)
        self.assertEqual(widths[-1], 49_999)
        most = {base + p for p in range(1, 25_001)}
        Q.unique_amount(base, most, rng=lambda n: widths.append(n) or n - 1)
        self.assertEqual(widths[-1], Q.PAY_ID_MAX)

    def test_extra_is_never_more_than_half_the_price(self):
        import listing as L
        for b in (2, 10_000, 50_000, 80_000, L.MIN_PRICE_UNITS, 150_000, 10**6, 10**8):
            self.assertLessEqual(Q.extra_cap(b), b // 2 or 1, b)
        self.assertEqual(Q.extra_cap(50_000), 25_000)
        self.assertEqual(Q.extra_cap(L.MIN_PRICE_UNITS), Q.EXTRA_MIN)   # the 0.10 minimum keeps the full 0.05
        for _ in range(200):
            amt, pid = Q.unique_amount(10_000, set())
            self.assertTrue(0 < pid <= 5_000 and amt == 10_000 + pid)

    def test_extra_is_capped_at_five_cents_or_five_percent(self):
        self.assertEqual(Q.extra_cap(500_000), 50_000)      # 0.50: 0.05 USDC
        self.assertEqual(Q.extra_cap(1_000_000), 50_000)    # 1.00: 5% is the same 0.05
        self.assertEqual(Q.extra_cap(1_500_000), 75_000)    # 1.50: 5% = 0.075
        self.assertEqual(Q.extra_cap(4_000_000), Q.PAY_ID_MAX)
        base = 500_000
        taken = {base + p for p in range(1, 25_001)}        # 19,999 range more than half used
        seen = []
        amt, pid = Q.unique_amount(base, taken, rng=lambda n: seen.append(n) or n - 1)
        self.assertEqual(seen[-1], 50_000)                  # may not grow past the cap
        self.assertTrue(25_000 < pid <= 50_000 and amt not in taken)
        with self.assertRaises(Q.QuoteError) as e:
            Q.unique_amount(base, {base + p for p in range(1, 45_001)})
        self.assertIn("busy", str(e.exception))
        # the buyer refuses an extra over the cap, even if signed by the seller
        req = Q.make_request(self.s.sesh, CLIENT, 1, 42161, self.bk, now=T0)
        doc, _ = Q.make_quote(self.qkey, req, payout=PAYOUT, price_per_hour=1_000_000, taken=set(), now=T0)
        body = dict(doc["quote"], amount=1_000_000 + 50_001)
        bad = {"quote": body, "sig": self.qkey.sign(Q._canon(Q.QUOTE_DOMAIN, body)).hex()}
        self.assertIn("amount", Q.quote_error(bad, listing=self.s.listing, req=req, cap=20_000_000, now=T0))

    def test_request_is_signed_and_timed(self):
        ok = lambda r, now=T0: Q.request_error(r, self.s.sesh, [42161, 8453], 24, now=now)  # noqa: E731
        self.assertIsNone(ok(self.req))
        # someone else naming this buyer key can't sign for it
        thief, _ = buyer_key()
        forged = Q.sign_request({**self.req, "buyer_key": self.bpub}, thief)
        self.assertIn("not signed", ok(forged))
        # changing a signed field breaks the signature
        self.assertIn("not signed", ok({**self.req, "hours": 4}))
        self.assertEqual(ok({k: v for k, v in self.req.items() if k != "sig"}), "bad request")
        # time window: 2 minutes either way
        self.assertIsNone(ok(self.req, now=T0 + Q.REQUEST_SKEW_S))
        self.assertIn("clock", ok(self.req, now=T0 + Q.REQUEST_SKEW_S + 1))
        self.assertIn("clock", ok(self.req, now=T0 - Q.REQUEST_SKEW_S - 1))
        # two requests made in the same second still differ (nonce)
        self.assertNotEqual(Q.make_request(self.s.sesh, CLIENT, 3, 42161, self.bk, now=T0)["sig"], self.req["sig"])

    def test_seller_rechecks_stored_quote_against_listing(self):
        terms = Q.listing_terms(self.s.listing)
        doc, _ = self.quote()
        self.assertIsNone(Q.terms_error(doc, terms))

        def resign(**kw):
            body = dict(doc["quote"], **kw)
            return {"quote": body, "sig": self.qkey.sign(Q._canon(Q.QUOTE_DOMAIN, body)).hex()}
        cases = [({"payout": "0x" + "dd" * 20}, "payout"), ({"chainid": 10}, "chain"),
                 ({"token_address": "0x" + "ee" * 20}, "USDC"), ({"hours": 25}, "hours"), ({"hours": 0}, "hours"),
                 ({"amount": 3_000_000}, "amount"), ({"amount": 1}, "amount"), ({"price_per_hour": 1}, "amount"),
                 ({"expires": doc["quote"]["issued"] + Q.MAX_QUOTE_TTL_S + 1}, "times"),
                 ({"seller": OTHER}, "seller")]
        for kw, msg in cases:
            self.assertIn(msg, Q.terms_error(resign(**kw), terms), kw)
        forged, _ = Q.make_quote(Q.load_key(Seller().quote_key), self.req, payout=PAYOUT,
                                 price_per_hour=1_000_000, taken=set(), now=T0)
        self.assertIn("not signed", Q.terms_error(forged, terms))

    def test_buyer_accepts_wide_ids(self):
        doc, _ = self.quote()
        self.assertIsNone(Q.quote_error(doc, listing=self.s.listing, req=self.req, cap=20_000_000,
                                        now=doc["quote"]["issued"]))

    def test_seller_cap(self):
        with self.assertRaises(Q.QuoteError):
            self.quote(req={**self.req, "hours": 20}, cap=20_000_000)
        self.quote(req={**self.req, "hours": 19}, cap=20_000_000)

    def test_buyer_checks_quote(self):
        doc, _ = self.quote(now=T0)
        chk = lambda d, **kw: Q.quote_error(d, listing=self.s.listing, req=self.req,  # noqa: E731
                                            cap=kw.get("cap", 20_000_000), now=kw.get("now", T0 + 5))
        self.assertIsNone(chk(doc))
        # buyer cap
        self.assertIn("over your limit", chk(doc, cap=2_000_000))
        # expiry
        self.assertIn("expired", chk(doc, now=T0 + Q.QUOTE_TTL_S + 1))
        # a quote bound to someone else's address or key
        other = Q.make_request(self.s.sesh, OTHER, 3, 42161, self.bk, now=T0)
        d2, _ = self.quote(req=other)
        self.assertIn("client_sesh", chk(d2))
        # not signed by the listing's quote key
        forged, _ = Q.make_quote(Q.load_key(Seller().quote_key), self.req, payout=PAYOUT,
                                 price_per_hour=1_000_000, taken=set(), now=T0)
        self.assertIn("not signed", chk(forged))
        # wrong payout / overpriced, re-signed by the real key so only the content is wrong
        for field, val, msg in (("payout", "0x" + "dd" * 20, "payout"), ("amount", 4_000_000, "amount"),
                                ("token_address", "0x" + "ee" * 20, "official USDC")):
            body = {**doc["quote"], field: val}
            d = {"quote": body, "sig": self.qkey.sign(Q._canon(Q.QUOTE_DOMAIN, body)).hex()}
            self.assertIn(msg, chk(d), field)

    def test_paid_notice_bound_to_buyer_key(self):
        doc, _ = self.quote()
        q = doc["quote"]
        paid = Q.make_paid(self.bk, q, tx(1))
        self.assertIsNone(Q.paid_error(paid, q))
        thief, _ = buyer_key()
        self.assertIn("not signed by the buyer", Q.paid_error(Q.make_paid(thief, q, tx(1)), q))
        self.assertIn("not signed by the buyer", Q.paid_error({**paid, "tx": tx(2)}, q))
        self.assertEqual(Q.paid_error({**paid, "quote_id": "0" * 16}, q), "wrong quote")
        with self.assertRaises(Q.QuoteError):
            Q.make_paid(self.bk, q, "0x1234")

    def test_payment_matching(self):
        doc, _ = self.quote(now=T0)
        q = doc["quote"]
        ok = [{"token": USDC_ARB, "from": "0x1", "to": PAYOUT, "value": q["amount"]}]
        self.assertIsNone(Q.payment_error(q, ok, T0 + 30, True))
        self.assertIn("failed", Q.payment_error(q, ok, T0 + 30, False))
        wrong_amt = [{**ok[0], "value": q["amount"] - 1}]
        self.assertIn("amount mismatch", Q.payment_error(q, wrong_amt, T0 + 30, True))
        base_usdc = [{**ok[0], "token": C.usdc(8453)}]  # right amount, other chain's token address
        self.assertIn("no USDC transfer", Q.payment_error(q, base_usdc, T0 + 30, True))
        fake_token = [{**ok[0], "token": "0x" + "ee" * 20}]
        self.assertIn("no USDC transfer", Q.payment_error(q, fake_token, T0 + 30, True))
        other_to = [{**ok[0], "to": "0x" + "dd" * 20}]
        self.assertIn("no USDC transfer", Q.payment_error(q, other_to, T0 + 30, True))
        self.assertIn("older than the quote", Q.payment_error(q, ok, T0 - 3600, True))
        self.assertIn("after the quote expired", Q.payment_error(q, ok, q["expires"] + 600, True))
        # batch tx with several transfers: one exact match is enough
        mixed = [{**ok[0], "value": 5}, ok[0]]
        self.assertIsNone(Q.payment_error(q, mixed, T0 + 30, True))


if __name__ == "__main__":
    unittest.main()
