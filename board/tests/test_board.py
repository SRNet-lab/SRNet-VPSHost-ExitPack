import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(HERE.parent), str(ROOT / "seller")]

import boardfmt as B  # noqa: E402
import board as admin  # noqa: E402
import board_server  # noqa: E402
import listing as L  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def keyfile(d: Path, name: str):
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    p = d / name
    p.write_text((seed + pub).hex() + "\n")
    return p, L.sesh_of(pub)


CLAIM = "http://" + "y" * 52 + ".sesh/claim"
V3 = {"chains": [8453, 42161], "token": "USDC", "quote_key": "ab" * 32, "claim_url": CLAIM}


def listing(keypath, price="1.00", payout="0x" + "aa" * 20, **extra):
    extra.setdefault("region", "us-east")
    for k, v in V3.items():
        extra.setdefault(k, v)
    return L.make(L.load_identity(keypath), price_per_hour_usdc=price, max_hours=168, payout=payout,
                  policy=["tcp/443"], **extra)


def legacy_v2(keypath, issued=None, **extra):
    """An old contract-era listing."""
    key = L.load_identity(keypath)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"v": 2, "seller": L.sesh_of(pub), "issued": int(time.time() if issued is None else issued),
            "price_per_hour_usdc": "1.00", "max_hours": 168, "payout": "0x" + "aa" * 20,
            "vendor_id": "0x" + "bb" * 32, "chainid": 42161, "contract": "0x" + "7e" * 20, "policy": ["tcp/443"]}
    body.update(extra)
    return {"listing": body, "sig": key.sign(L.canonical(body)).hex()}


def quiet(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*a)
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.bkey, self.bsesh = keyfile(self.d, "board.key")
        self.state = self.d / "state"
        self.www = self.d / "www"
        rc, out, _ = quiet(admin.main, ["init", "--state", str(self.state), "--key", str(self.bkey)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), self.bsesh)
        self.s1key, self.s1 = keyfile(self.d, "s1.key")
        self.s2key, self.s2 = keyfile(self.d, "s2.key")

    def submit(self, doc, name="l.json"):
        p = self.d / name
        p.write_text(json.dumps(doc))
        return quiet(admin.main, ["submit", "--state", str(self.state), str(p)])

    def sign(self, *extra):
        rc, out, err = quiet(admin.main, ["sign", "--state", str(self.state), "--key", str(self.bkey),
                                          "--out", str(self.www), *extra])
        self.assertEqual(rc, 0, err)

    def verify(self, **kw):
        return B.verify_board((self.www / "board.json").read_bytes(), (self.www / "board.sig").read_text(),
                              self.bsesh, **kw)


class AdminFlowTest(Base):
    def test_new_listing_goes_live_without_approval(self):
        rc, out, _ = self.submit(listing(self.s1key))
        self.assertIn(": live", out)
        self.sign()
        body, good, _ = self.verify()
        self.assertEqual([g["listing"]["seller"] for g in good], [self.s1])
        self.assertEqual(body["seq"], 1)

    def test_bad_listing_rejected_on_submit(self):
        doc = listing(self.s1key)
        doc["listing"]["price_per_hour_usdc"] = "0.01"
        rc, out, _ = self.submit(doc)
        self.assertEqual(rc, 1)
        self.assertIn("rejected:bad signature", out)

    def test_sanity_checks(self):
        cases = [({"chains": [31337]}, "unsupported chain"),
                 ({"chains": [1]}, "unsupported chain"),
                 ({"chains": []}, "bad chains"),
                 ({"chains": [8453, 8453]}, "bad chains"),
                 ({"token": "USDT"}, "only USDC is supported"),
                 ({"contract": "0x" + "c0" * 20}, "unexpected contract"),
                 ({"quote_key": "12"}, "bad quote_key"),
                 ({"claim_url": "http://example.com/claim"}, "bad claim_url"),
                 ({"payout": "0x" + "00" * 20}, "bad or zero payout"),
                 ({"payout": "0xnope"}, "bad or zero payout"),
                 ({"price_per_hour_usdc": "0"}, "price_per_hour_usdc must be at least 0.10"),
                 ({"price_per_hour_usdc": "0.05"}, "price_per_hour_usdc must be at least 0.10"),
                 ({"max_hours": 0}, "bad max_hours")]
        for i, (over, why) in enumerate(cases):
            body = {"price_per_hour_usdc": "1.00", "max_hours": 168, "payout": "0x" + "aa" * 20,
                    "region": "x", "policy": ["tcp/443"], **V3, **over}
            doc = L.make(L.load_identity(self.s1key), **body)
            rc, out, _ = self.submit(doc, f"s{i}.json")
            self.assertIn(f"rejected:{why}", out, over)

    def test_payout_change_goes_live_immediately(self):
        self.submit(listing(self.s1key))
        time.sleep(1.1)
        rc, out, _ = self.submit(listing(self.s1key, payout="0x" + "dd" * 20, country="DE"), "e.json")
        self.assertIn("refreshed", out)
        self.sign()
        _, good, _ = self.verify()
        self.assertEqual(good[0]["listing"]["payout"], "0x" + "dd" * 20)
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn('"payout"', audit)

    def test_ban_and_unban(self):
        self.submit(listing(self.s1key))
        self.submit(listing(self.s2key), "b.json")
        self.sign()
        self.assertEqual(len(self.verify()[1]), 2)
        quiet(admin.main, ["ban", "--state", str(self.state), self.s1, "--reason", "complaints"])
        self.sign()
        self.assertEqual([g["listing"]["seller"] for g in self.verify()[1]], [self.s2])
        time.sleep(1.1)
        rc, out, _ = self.submit(listing(self.s1key), "c.json")
        self.assertIn("rejected:banned", out)
        rc, out, _ = quiet(admin.main, ["list", "--state", str(self.state)])
        self.assertIn("reason=complaints", out)
        self.assertIn("complaints", (self.state / "audit.jsonl").read_text())
        self.assertEqual(quiet(admin.main, ["unban", "--state", str(self.state), self.s1])[0], 0)
        time.sleep(1.1)
        rc, out, _ = self.submit(listing(self.s1key), "d.json")
        self.assertIn("refreshed", out)
        self.sign()
        self.assertEqual(len(self.verify()[1]), 2)

    def test_approve_command_gone(self):
        with self.assertRaises(SystemExit):
            quiet(admin.main, ["approve", "--state", str(self.state), self.s1])

    def test_expired_listing_dropped(self):
        old = listing(self.s1key, expires=int(time.time()) + 2)
        self.submit(old)
        self.sign()
        self.assertEqual(len(self.verify()[1]), 1)
        _, good, dropped = self.verify(now=time.time() + 10)
        self.assertEqual(good, [])
        self.assertTrue(any("expired" in r for r in dropped))

    def test_ingest_inbox(self):
        (self.state / "inbox" / "x.json").write_text(json.dumps(listing(self.s1key)))
        rc, out, _ = quiet(admin.main, ["ingest", "--state", str(self.state)])
        self.assertIn("live", out)
        self.assertTrue((self.state / "inbox" / "processed" / "x.json").exists())


class BoardVerifyTest(Base):
    def setUp(self):
        super().setUp()
        self.submit(listing(self.s1key))
        self.sign()

    def test_tampered_body(self):
        b = json.loads((self.www / "board.json").read_text())
        b["listings"] = []
        with self.assertRaisesRegex(B.BoardError, "signature invalid"):
            B.verify_board(json.dumps(b).encode(), (self.www / "board.sig").read_text(), self.bsesh)

    def test_wrong_pin(self):
        _, other = keyfile(self.d, "other.key")
        with self.assertRaisesRegex(B.BoardError, "not the pinned"):
            B.verify_board((self.www / "board.json").read_bytes(), (self.www / "board.sig").read_text(), other)

    def test_board_signed_by_other_key_claiming_pinned_name(self):
        okey, _ = keyfile(self.d, "evil.key")
        body = json.loads((self.www / "board.json").read_text())
        sig = L.load_identity(okey).sign(B.canonical(body)).hex()
        with self.assertRaisesRegex(B.BoardError, "signature invalid"):
            B.verify_board(json.dumps(body).encode(), sig, self.bsesh)

    def test_expired_board(self):
        with self.assertRaisesRegex(B.BoardError, "expired"):
            self.verify(now=time.time() + 8 * 86400)

    def test_rollback_and_equivocation(self):
        body, _, _ = self.verify()
        with self.assertRaisesRegex(B.BoardError, "rollback"):
            self.verify(seen={"seq": body["seq"] + 1})
        with self.assertRaisesRegex(B.BoardError, "equivocation"):
            self.verify(seen={"seq": body["seq"], "hash": "00" * 32})
        self.verify(seen={"seq": body["seq"], "hash": B.body_hash(body)})

    def test_bad_listing_inside_signed_board_dropped(self):
        # operator mistake/compromise of a listing: board sig valid, listing sig not -> dropped
        key = L.load_identity(self.bkey)
        body = json.loads((self.www / "board.json").read_text())
        forged = json.loads(json.dumps(body["listings"][0]))
        forged["listing"]["payout"] = "0x" + "ee" * 20
        body["listings"].append(forged)
        sig = B.sign_board(key, body)
        _, good, dropped = B.verify_board(json.dumps(body).encode(), sig, self.bsesh)
        self.assertEqual(len(good), 1)
        self.assertEqual(good[0]["listing"]["payout"], "0x" + "aa" * 20)
        self.assertTrue(any("bad listing signature" in r for r in dropped))

    def test_cli_verify(self):
        rc, out, _ = quiet(admin.main, ["verify", str(self.www), "--pin", self.bsesh])
        self.assertEqual(rc, 0)
        self.assertIn("board OK", out)


class ServerClientTest(Base):
    def setUp(self):
        super().setUp()
        self.submit(listing(self.s1key))
        self.sign()
        board_server.make_handler.__globals__["sys"].stderr = io.StringIO()  # silence access log
        self.srv = board_server.serve(self.www, self.state / "inbox", "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        self.home = self.d / "home"
        self.home.mkdir()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        board_server.make_handler.__globals__["sys"].stderr = sys.__stderr__

    def test_refuses_public_bind(self):
        with self.assertRaises(SystemExit):
            board_server.serve(self.www, None, "0.0.0.0", 0)

    def test_only_board_files_served(self):
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.url + "../state/state.json")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.url + "audit.jsonl")

    def test_submit_endpoint(self):
        doc = listing(self.s2key)
        req = urllib.request.Request(self.url + "submit", data=json.dumps(doc).encode(), method="POST")
        self.assertEqual(urllib.request.urlopen(req).status, 202)
        bad = json.loads(json.dumps(doc))
        bad["listing"]["price_per_hour_usdc"] = "9"
        req = urllib.request.Request(self.url + "submit", data=json.dumps(bad).encode(), method="POST")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(req)
        rc, out, _ = quiet(admin.main, ["ingest", "--state", str(self.state)])
        self.assertIn("live", out)


def listing_at(keypath, issued, payout="0x" + "aa" * 20, price="1.00"):
    """A v3 listing signed with an explicit issued time."""
    key = L.load_identity(keypath)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"v": 3, "seller": L.sesh_of(pub), "issued": int(issued), "price_per_hour_usdc": price,
            "max_hours": 168, "payout": payout, **V3, "region": "us-east", "policy": ["tcp/443", "tcp/80"]}
    return {"listing": body, "sig": key.sign(L.canonical(body)).hex()}


class LivenessTest(Base):
    def st(self):
        return admin._load(self.state)

    def save(self, st):
        admin._save(self.state, st)

    def board_at(self, now):
        st = self.st()
        body = admin.build_body(st, now, 24)
        sig = B.sign_board(L.load_identity(self.bkey), body)
        return B.verify_board(json.dumps(body).encode(), sig, self.bsesh, now=now)

    def test_constants(self):
        self.assertEqual(B.LISTING_REFRESH_S, 3600)
        self.assertEqual(B.LISTING_HIDE_AFTER_S, 3 * 3600)
        self.assertEqual(self.st()["max_listing_age_s"], 3 * 3600)

    def test_hidden_after_3h_without_refresh(self):
        t0 = time.time()
        st = self.st()
        self.assertEqual(admin.submit_doc(self.state, st, listing_at(self.s1key, t0), now=t0), "live")
        self.save(st)
        self.assertEqual(len(self.board_at(t0 + 2 * 3600)[1]), 1)          # refreshed 2 h ago: shown
        self.assertEqual(len(self.board_at(t0 + 3 * 3600 - 60)[1]), 1)     # just under 3 h: shown
        self.assertEqual(self.board_at(t0 + 3 * 3600 + 1)[1], [])          # 3 h without refresh: hidden
        # an older board that still carries it is filtered by the client too
        st = self.st()
        body = admin.build_body(st, t0, 24)
        sig = B.sign_board(L.load_identity(self.bkey), body)
        _, good, dropped = B.verify_board(json.dumps(body).encode(), sig, self.bsesh, now=t0 + 3 * 3600 + 1)
        self.assertEqual(good, [])
        self.assertTrue(dropped)

    def test_reappears_on_refresh_without_reapproval(self):
        t0 = time.time()
        st = self.st()
        admin.submit_doc(self.state, st, listing_at(self.s1key, t0), now=t0)
        t1 = t0 + 5 * 3600                                                  # hidden for 2 h
        self.assertEqual(admin.build_body(st, t1, 24)["listings"], [])
        res = admin.submit_doc(self.state, st, listing_at(self.s1key, t1, price="0.75"), now=t1)
        self.assertEqual(res, "refreshed")
        self.assertEqual(st["sellers"][self.s1]["status"], "live")
        self.save(st)
        _, good, _ = self.board_at(t1 + 60)
        self.assertEqual([d["listing"]["price_per_hour_usdc"] for d in good], ["0.75"])
        audit = (self.state / "audit.jsonl").read_text()
        self.assertIn('"reshown": true', audit)

    def test_payout_change_goes_live_after_hidden(self):
        t0 = time.time()
        st = self.st()
        admin.submit_doc(self.state, st, listing_at(self.s1key, t0), now=t0)
        t1 = t0 + 5 * 3600
        res = admin.submit_doc(self.state, st, listing_at(self.s1key, t1, payout="0x" + "dd" * 20), now=t1)
        self.assertEqual(res, "refreshed")
        self.assertEqual(len(admin.build_body(st, t1, 24)["listings"]), 1)

    def test_stale_refresh_rejected(self):
        t0 = time.time()
        st = self.st()
        res = admin.submit_doc(self.state, st, listing_at(self.s1key, t0 - 3 * 3600 - 5), now=t0)
        self.assertTrue(res.startswith("rejected"), res)

    def test_listing_requires_hourly_fields(self):
        t0 = time.time()
        doc = listing_at(self.s1key, t0)
        del doc["listing"]["max_hours"]
        key = L.load_identity(self.s1key)
        doc["sig"] = key.sign(L.canonical(doc["listing"])).hex()
        self.assertEqual(admin.submit_doc(self.state, self.st(), doc, now=t0), "rejected:missing max_hours")

    def test_tick_ingests_and_signs(self):
        t0 = time.time()
        st = self.st()
        admin.submit_doc(self.state, st, listing_at(self.s1key, t0 - 60), now=t0)
        st["max_listing_age_s"] = 30 * 86400          # an older state file
        self.save(st)
        (self.state / "inbox" / "r.json").write_text(json.dumps(listing_at(self.s1key, t0)))
        rc, out, _ = quiet(admin.main, ["tick", "--state", str(self.state), "--key", str(self.bkey),
                                        "--out", str(self.www)])
        self.assertEqual(rc, 0, out)
        self.assertIn("refreshed", out)
        self.assertEqual(self.st()["max_listing_age_s"], 3 * 3600)
        body = json.loads((self.www / "board.json").read_text())
        self.assertEqual(body["max_listing_age_s"], 3 * 3600)
        self.assertEqual(body["listings"][0]["listing"]["issued"], int(t0))

class MirrorTest(Base):
    def setUp(self):
        super().setUp()
        self.submit(listing(self.s1key))
        self.sign()
        board_server.make_handler.__globals__["sys"].stderr = io.StringIO()
        self.mwww = self.d / "mirror"
        self.srv = board_server.serve(self.mwww, None, "127.0.0.1", 0, pin=self.bsesh)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        board_server.make_handler.__globals__["sys"].stderr = sys.__stderr__

    def test_publish_and_serve(self):
        rc, out, err = quiet(admin.main, ["publish", "--out", str(self.www), self.url])
        self.assertEqual(rc, 0, err)
        got = urllib.request.urlopen(self.url + "board.json").read()
        self.assertEqual(got, (self.www / "board.json").read_bytes())

    def test_tick_publishes(self):
        rc, out, err = quiet(admin.main, ["tick", "--state", str(self.state), "--key", str(self.bkey),
                                          "--out", str(self.www), "--publish", self.url,
                                          "--publish", "http://127.0.0.1:1/"])
        self.assertEqual(rc, 0, err)
        self.assertIn("failed", err)  # dead mirror reported, live one updated
        self.assertEqual(json.loads((self.mwww / "board.json").read_text())["seq"], 2)

    def test_mirror_refuses_rollback_and_forgery(self):
        old_b, old_s = (self.www / "board.json").read_text(), (self.www / "board.sig").read_text()
        self.sign()
        quiet(admin.main, ["publish", "--out", str(self.www), self.url])
        msg = json.dumps({"body": old_b, "sig": old_s.strip()}).encode()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(urllib.request.Request(self.url + "publish", data=msg, method="POST"))
        self.assertEqual(cm.exception.code, 409)
        okey, _ = keyfile(self.d, "evil.key")
        body = json.loads(old_b)
        body["seq"] = 99
        msg = json.dumps({"body": json.dumps(body), "sig": L.load_identity(okey).sign(B.canonical(body)).hex()})
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(self.url + "publish", data=msg.encode(), method="POST"))
        self.assertEqual(json.loads((self.mwww / "board.json").read_text())["seq"], 2)

    def test_mirror_has_no_submit(self):
        req = urllib.request.Request(self.url + "submit", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 404)

    def test_bind_rules(self):
        self.assertTrue(board_server.bind_ok("127.0.0.1"))
        self.assertTrue(board_server.bind_ok("10.67.0.1"))
        self.assertTrue(board_server.bind_ok("fd00::1"))
        self.assertFalse(board_server.bind_ok("0.0.0.0"))
        self.assertFalse(board_server.bind_ok("8.8.8.8"))



class ExitVersionBoardTest(Base):
    def test_version_change_is_refresh_and_min_filter(self):
        self.submit(listing(self.s1key, exit_version="0.1.0"))
        time.sleep(1.1)
        rc, out, _ = self.submit(listing(self.s1key, exit_version="0.2.0"), "r.json")
        self.assertIn("refreshed", out)
        self.submit(listing(self.s2key), "b.json")  # no exit_version: still valid
        rc, out, _ = quiet(admin.main, ["list", "--state", str(self.state)])
        self.assertIn("exit_version=0.2.0", out)
        self.assertIn("exit_version=unknown", out)
        self.sign()
        self.assertEqual(len(self.verify()[1]), 2)
        rc, out, _ = quiet(admin.main, ["config", "--state", str(self.state), "--min-exit-version", "0.1.5"])
        self.assertEqual(rc, 0)
        self.sign()
        self.assertEqual([g["listing"]["seller"] for g in self.verify()[1]], [self.s1])
        quiet(admin.main, ["config", "--state", str(self.state), "--min-exit-version", "off"])
        self.sign()
        self.assertEqual(len(self.verify()[1]), 2)
        rc, _, _ = quiet(admin.main, ["config", "--state", str(self.state), "--min-exit-version", "new"])
        self.assertEqual(rc, 1)

    def test_country_change_goes_live(self):
        self.submit(listing(self.s1key, country="US"))
        time.sleep(1.1)
        rc, out, _ = self.submit(listing(self.s1key, country="US", region="us-west"), "r.json")
        self.assertIn("refreshed", out)
        time.sleep(1.1)
        rc, out, _ = self.submit(listing(self.s1key, country="DE"), "d.json")
        self.assertIn("refreshed", out)
        rc, out, _ = quiet(admin.main, ["list", "--state", str(self.state)])
        self.assertIn("country=DE", out)
        self.sign()
        self.assertEqual(self.verify()[1][0]["listing"]["country"], "DE")
        self.submit(listing(self.s2key), "b.json")
        rc, out, _ = quiet(admin.main, ["list", "--state", str(self.state)])
        self.assertIn("country=unknown", out)
        rc, out, _ = self.submit(listing(self.s2key, country="de"), "c.json")
        self.assertIn("rejected:bad country", out)

    def test_speed_cap_change_is_refresh(self):
        self.submit(listing(self.s1key, speed_cap_mbps=10))
        time.sleep(1.1)
        rc, out, _ = self.submit(listing(self.s1key, speed_cap_mbps=0), "r.json")
        self.assertIn("refreshed", out)
        self.submit(listing(self.s2key), "b.json")
        rc, out, _ = quiet(admin.main, ["list", "--state", str(self.state)])
        self.assertIn("speed_cap=unlimited", out)
        self.assertIn("speed_cap=unknown", out)
        rc, out, _ = self.submit(listing(self.s2key, speed_cap_mbps="fast"), "c.json")
        self.assertIn("rejected:bad speed_cap_mbps", out)

    def test_bad_exit_version_rejected(self):
        rc, out, _ = self.submit(listing(self.s1key, exit_version="<script>"))
        self.assertIn("rejected:bad exit_version", out)

if __name__ == "__main__":
    unittest.main()


class LegacyListingTest(Base):
    def test_v2_kept_for_two_weeks_then_dropped(self):
        t0 = time.time()
        st = admin._load(self.state)
        self.assertEqual(admin.submit_doc(self.state, st, legacy_v2(self.s1key, t0), now=t0), "live")
        self.assertEqual(st["legacy_until"], int(t0) + 14 * 86400)
        self.assertEqual(len(admin.build_body(st, t0 + 60, 24)["listings"]), 1)
        late = t0 + 14 * 86400 + 10
        res = admin.submit_doc(self.state, st, legacy_v2(self.s1key, late), now=late)
        self.assertEqual(res, "rejected:old v2 listing, grace period over")
        st["sellers"][self.s1]["doc"] = legacy_v2(self.s1key, late)
        self.assertEqual(admin.build_body(st, late + 1, 24)["listings"], [])

    def test_v2_fields_still_checked_during_grace(self):
        t0 = time.time()
        st = admin._load(self.state)
        for bad, why in ((dict(country="usa"), "bad country"), (dict(region="x" * 65), "bad region"),
                         (dict(dns="8.8.8.8"), "bad dns"), (dict(speed_cap_mbps="fast"), "bad speed_cap_mbps"),
                         (dict(exit_version="<script>"), "bad exit_version"), (dict(policy=["x" * 40]), "bad policy"),
                         (dict(price_per_hour_usdc="free"), "bad price_per_hour_usdc"),
                         (dict(max_hours=0), "bad max_hours")):
            self.assertEqual(admin.submit_doc(self.state, st, legacy_v2(self.s1key, t0, **bad), now=t0),
                             f"rejected:{why}", bad)
        self.assertEqual(admin.submit_doc(self.state, st, legacy_v2(self.s1key, t0, country="DE"), now=t0), "live")

    def test_v2_flagged_needs_update(self):
        self.assertEqual(L.listing_error(legacy_v2(self.s1key)["listing"]), L.NEEDS_UPDATE)
        self.submit(legacy_v2(self.s1key))
        rc, out, _ = quiet(admin.main, ["list", "--state", str(self.state)])
        self.assertIn("seller needs to update", out)

    def test_testnet_chains_only_when_enabled(self):
        doc = listing(self.s1key, chains=[84532])
        rc, out, _ = self.submit(doc)
        self.assertIn("rejected:unsupported chain", out)
        quiet(admin.main, ["config", "--state", str(self.state), "--testnets", "on"])
        rc, out, _ = self.submit(doc, "t.json")
        self.assertIn(": live", out)
