import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import claimd as C  # noqa: E402
import evm as E  # noqa: E402
from watcher import claim_message  # noqa: E402

CONTRACT = "0x" + "cc" * 20
CHAIN = 8453
CLIENT = "ycbogyadycbogyadycbogyadycbogyadycbogyadycbogyadycbo.sesh"
KEY = 0x4C0883A69102937D6231471B5DBB6204FE5129617082792AE468D01A3F362318


def sign(msg: str, d: int = KEY) -> str:
    z = int.from_bytes(E.personal_hash(msg), "big")
    k = int.from_bytes(hashlib.sha256(d.to_bytes(32, "big") + z.to_bytes(32, "big")).digest(), "big") % E._N
    R = E._mul(k, E._G)
    r = R[0] % E._N
    s = pow(k, -1, E._N) * (z + r * d) % E._N
    v = R[1] & 1
    if s > E._N // 2:
        s, v = E._N - s, v ^ 1
    return "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex() + bytes([27 + v]).hex()


def claim(hid=5, client=CLIENT):
    return {"v": 1, "hold_id": hid, "client_sesh": client,
            "sig": sign(claim_message(CONTRACT, CHAIN, hid, client))}


class CheckTest(unittest.TestCase):
    def test_good(self):
        self.assertIsNone(C.check_claim(claim(), CONTRACT, CHAIN))

    def test_bad(self):
        good = claim()
        for k, v, why in [("v", 2, "bad version"), ("hold_id", 0, "bad hold_id"), ("hold_id", "5", "bad hold_id"),
                          ("hold_id", True, "bad hold_id"), ("client_sesh", "x.sesh", "bad client_sesh"),
                          ("sig", "0x12", "bad sig"), ("sig", "0x" + "00" * 65, "bad sig")]:
            c = dict(good, **{k: v})
            self.assertEqual(C.check_claim(c, CONTRACT, CHAIN), why, k)
        self.assertEqual(C.check_claim(dict(good, extra=1), CONTRACT, CHAIN), "bad fields")
        self.assertEqual(C.check_claim([], CONTRACT, CHAIN), "bad fields")

    def test_bind(self):
        for h in ("127.0.0.1", "::1", "fd2e:7365:736a::1", "10.194.0.1"):
            self.assertTrue(C.bind_ok(h), h)
        for h in ("0.0.0.0", "::", "8.8.8.8", "2001:db8::1x", "example.com"):
            self.assertFalse(C.bind_ok(h), h)

    def test_limiter(self):
        L = C.Limiter(per_source=(2, 10), overall=(3, 10))
        self.assertEqual([L.allow("a", 0), L.allow("a", 1), L.allow("a", 2)], [True, True, False])
        self.assertTrue(L.allow("b", 2))
        self.assertFalse(L.allow("c", 3))  # overall cap
        self.assertTrue(L.allow("c", 20))  # window passed


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp()) / "spool"
        self.srv = C.serve("127.0.0.1", 0, self.spool, CONTRACT, CHAIN, C.Limiter(per_source=(5, 60)))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, body, path="/claim"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, r.read().decode().strip()

    def test_flow(self):
        body = json.dumps(claim(7)).encode()
        self.assertEqual(self.post(body), (202, "received"))
        self.assertEqual(self.post(body), (202, "already received"))
        files = list(self.spool.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["hold_id"], 7)
        self.assertEqual(self.post(b"{", )[0], 400)
        self.assertEqual(self.post(b"x" * 2000)[0], 413)
        self.assertEqual(self.post(body, "/other")[0], 404)
        self.assertEqual(self.post(body)[0], 202)
        self.assertEqual(self.post(body)[0], 429)  # 6th request from this source
        self.assertEqual(len(list(self.spool.glob("*.json"))), 1)

    def test_public_refused(self):
        with self.assertRaises(SystemExit):
            C.serve("0.0.0.0", 0, self.spool, CONTRACT, CHAIN)


BUYER = E.recover(E.personal_hash(claim_message(CONTRACT, CHAIN, 1, CLIENT)), claim(1)["sig"]).lower()
SELLER = "0x" + "5e" * 20
FAR = 2**40


class GateTest(unittest.TestCase):
    def gate(self, leases, **kw):
        self.calls = []

        def lookup(h):
            self.calls.append(h)
            v = leases.get(h)
            if isinstance(v, Exception):
                raise v
            return v
        t = [0.0]
        g = C.LeaseGate(lookup, SELLER, clock=lambda: t[0], **kw)
        return g, t

    def test_checks(self):
        g, _ = self.gate({1: (BUYER, SELLER, FAR, True), 2: (BUYER, SELLER, FAR, False),
                          3: (BUYER, "0x" + "11" * 20, FAR, True), 4: ("0x" + "22" * 20, SELLER, FAR, True),
                          5: (BUYER, SELLER, 1, True), 6: RuntimeError("rpc down")})
        self.assertIsNone(g.check(1, BUYER))
        self.assertEqual(g.check(2, BUYER), "lease not active")
        self.assertEqual(g.check(3, BUYER), "lease not for this seller")
        self.assertEqual(g.check(4, BUYER), "signer is not the buyer")
        self.assertEqual(g.check(5, BUYER), "lease not active")
        self.assertEqual(g.check(6, BUYER), "chain unavailable")
        self.assertEqual(g.check(9, BUYER), "unknown lease")

    def test_cache(self):
        g, t = self.gate({1: (BUYER, SELLER, FAR, True)}, ttl=30)
        g.check(1, BUYER); g.check(1, BUYER)
        self.assertEqual(self.calls, [1])
        t[0] = 31
        g.check(1, BUYER)
        self.assertEqual(self.calls, [1, 1])
        g.check(9, BUYER); t[0] = 37; g.check(9, BUYER)  # misses expire sooner
        self.assertEqual(self.calls, [1, 1, 9, 9])


class GatedServerTest(ServerTest):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp()) / "spool"
        leases = {7: (BUYER, SELLER, FAR, True), 8: ("0x" + "22" * 20, SELLER, FAR, True)}
        gate = C.LeaseGate(lambda h: leases.get(h), SELLER)
        self.srv = C.serve("127.0.0.1", 0, self.spool, CONTRACT, CHAIN, C.Limiter(per_source=(5, 60)), gate)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def test_rejects_before_spool(self):
        self.assertEqual(self.post(json.dumps(claim(8)).encode()), (403, "signer is not the buyer"))
        self.assertEqual(self.post(json.dumps(claim(99)).encode()), (403, "unknown lease"))
        self.assertEqual(list(self.spool.glob("*.json")), [])
        self.assertEqual(self.post(json.dumps(claim(7)).encode()), (202, "received"))


if __name__ == "__main__":
    unittest.main()
