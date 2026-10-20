"""claimd.py: quotes from the signed listing, open-quote limits, paid notices, HTTP surface."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from fixtures import CLIENT, OTHER, Q, T0, Seller, buyer_key, tx
import claimd as D


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.s = Seller(price="0.50", max_hours=48)
        self.clock = Clock()
        self.desk = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock)
        self.bk, self.bpub = buyer_key()

    def req(self, **kw):
        r = Q.make_request(self.s.sesh, CLIENT, 4, 8453, self.bpub)
        r.update(kw)
        return r

    def test_quote_follows_listing(self):
        code, doc = self.desk.quote(self.req())
        self.assertEqual(code, 200, doc)
        q = doc["quote"]
        self.assertEqual(q["payout"], self.s.listing["payout"])
        self.assertEqual(q["price_per_hour"], 500_000)
        self.assertTrue(2_000_000 < q["amount"] <= 2_000_000 + Q.PAY_ID_MAX)
        self.assertEqual(q["expires"], T0 + Q.QUOTE_TTL_S)
        self.assertIsNone(Q.quote_error(doc, listing=self.s.listing, req=self.req(), cap=20_000_000, now=T0))
        self.assertEqual(self.desk.quote(self.req(hours=49))[0], 400)
        self.assertEqual(self.desk.quote(self.req(chainid=1))[0], 400)

    def test_seller_cap_default_20(self):
        code, doc = self.desk.quote(self.req(hours=41))   # 20.50 + id > 20
        self.assertEqual(code, 403)
        self.assertIn("limit of 20.00", doc["error"])
        self.assertEqual(self.desk.quote(self.req(hours=39))[0], 200)

    def test_amounts_unique_among_open_quotes(self):
        amounts = set()
        for i in range(3):
            _, k = buyer_key()
            c = [CLIENT, OTHER, "y" * 52 + ".sesh"][i]
            code, doc = self.desk.quote(self.req(client_sesh=c, buyer_key=k))
            self.assertEqual(code, 200)
            amounts.add(doc["quote"]["amount"])
        self.assertEqual(len(amounts), 3)

    def test_open_quote_limit_per_client(self):
        for _ in range(D.MAX_OPEN_PER_CLIENT):
            self.assertEqual(self.desk.quote(self.req())[0], 200)
        self.assertEqual(self.desk.quote(self.req())[0], 429)
        self.clock.t += Q.QUOTE_TTL_S + 1
        self.assertEqual(self.desk.quote(self.req())[0], 200)

    def test_paid_notice(self):
        _, doc = self.desk.quote(self.req())
        q = doc["quote"]
        self.assertEqual(self.desk.status(q["id"]), (200, {"status": "waiting"}))
        thief, _ = buyer_key()
        self.assertEqual(self.desk.paid(Q.make_paid(thief, q, tx(1)))[0], 403)
        self.assertEqual(self.desk.paid({**Q.make_paid(self.bk, q, tx(1)), "quote_id": "0" * 16})[0], 404)
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(1))), (202, {"status": "received"}))
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(1)))[0], 202)
        self.assertEqual(len(list((self.s.state / "spool").glob("*.json"))), 1)
        self.clock.t = q["expires"] + D.PAID_GRACE_S + 1
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(2)))[0], 410)
        self.assertEqual(self.desk.status(q["id"])[1]["status"], "expired")

    def test_listing_must_match_quote_key(self):
        other = Seller()
        desk = D.Desk(self.s.state, other.quote_key, self.s.listing_path)
        with self.assertRaises(RuntimeError):
            desk.quote(self.req())


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.s = Seller()
        self.desk = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path)
        self.srv = D.serve("127.0.0.1", 0, self.desk)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data), timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        bk, bpub = buyer_key()
        req = Q.make_request(self.s.sesh, CLIENT, 1, 42161, bpub)
        code, doc = self.call("/claim/quote", req)
        self.assertEqual(code, 200)
        qid = doc["quote"]["id"]
        self.assertEqual(self.call(f"/claim/status/{qid}"), (200, {"status": "waiting"}))
        self.assertEqual(self.call("/claim/paid", Q.make_paid(bk, doc["quote"], tx(9)))[0], 202)
        self.assertEqual(self.call("/claim", {"v": 1})[0], 410)
        self.assertEqual(self.call("/claim/status/zz")[0], 404)
        self.assertEqual(self.call("/claim/quote", {"pad": "x" * 3000})[0], 413)

    def test_refuses_public_bind(self):
        with self.assertRaises(SystemExit):
            D.serve("8.8.8.8", 0, self.desk)
        self.assertTrue(D.bind_ok("fd2e:7365:736a::1"))
        self.assertFalse(D.bind_ok("0.0.0.0"))

    def test_rate_limit(self):
        lim = D.Limiter(per_source=(2, 60), overall=(100, 60))
        self.assertTrue(lim.allow("a", 0) and lim.allow("a", 1))
        self.assertFalse(lim.allow("a", 2))
        self.assertTrue(lim.allow("b", 2))


if __name__ == "__main__":
    unittest.main()
