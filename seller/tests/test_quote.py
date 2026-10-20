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
        self.req = Q.make_request(self.s.sesh, CLIENT, 3, 42161, self.bpub)

    def quote(self, req=None, taken=(), cap=20_000_000, now=T0):
        return Q.make_quote(self.qkey, req or self.req, payout=PAYOUT, price_per_hour=1_000_000,
                            taken=set(taken), cap=cap, now=now)

    def test_request_checks(self):
        ok = lambda r: Q.request_error(r, self.s.sesh, [42161, 8453], 24)  # noqa: E731
        self.assertIsNone(ok(self.req))
        self.assertEqual(ok({**self.req, "hours": 25}), "at most 24 hours per purchase")
        self.assertEqual(ok({**self.req, "hours": 0}), "hours must be a whole number, at least 1")
        self.assertEqual(ok({**self.req, "hours": 1.5}), "hours must be a whole number, at least 1")
        self.assertEqual(ok({**self.req, "chainid": 1}), "this seller does not take payments on that chain")
        self.assertEqual(Q.request_error(self.req, self.s.sesh, [8453], 24),
                         "this seller does not take payments on that chain")
        self.assertEqual(ok({**self.req, "token": "USDT"}), "only USDC is accepted")
        self.assertEqual(ok({**self.req, "seller": OTHER}), "wrong seller")
        self.assertEqual(ok({**self.req, "client_sesh": "x.sesh"}), "bad client_sesh")
        self.assertEqual(ok({**self.req, "extra": 1}), "bad request")

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
        amt, pid = Q.unique_amount(100, {6}, rng=lambda n: next(seq))
        self.assertEqual((amt, pid), (108, 8))
        amt, pid = Q.unique_amount(100, set(range(1, Q.PAY_ID_MAX)), rng=lambda n: 0)
        self.assertEqual(pid, Q.PAY_ID_MAX)
        with self.assertRaises(Q.QuoteError):
            Q.unique_amount(100, set(range(1, Q.PAY_ID_MAX + 1)))

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
        other = Q.make_request(self.s.sesh, OTHER, 3, 42161, self.bpub)
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
