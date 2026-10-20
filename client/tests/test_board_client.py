import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import helpers  # noqa: E402

sys.stdin = io.StringIO()
from srnet_exits import board as cb  # noqa: E402
from srnet_exits.cli import main  # noqa: E402
import boardfmt as B  # noqa: E402
import listing as L  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

QUOTE_KEY = "ab" * 32
CLAIM = "http://" + "y" * 52 + ".sesh/claim"


def keyfile(d: Path, name: str):
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    p = d / name
    p.write_text((seed + pub).hex() + "\n")
    return p, L.sesh_of(pub)


def _sign(keypath, body):
    return {"listing": body, "sig": L.load_identity(keypath).sign(L.canonical(body)).hex()}


def listing_at(keypath, issued, region="us-east"):
    key = L.load_identity(keypath)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"v": 3, "seller": L.sesh_of(pub), "issued": int(issued), "price_per_hour_usdc": "0.50",
            "max_hours": 168, "payout": "0x" + "aa" * 20, "chains": [8453, 42161], "token": "USDC",
            "quote_key": QUOTE_KEY, "claim_url": CLAIM, "region": region, "policy": ["tcp/443", "tcp/80"],
            "dns": "10.195.0.53"}
    return _sign(keypath, body)


def old_listing_at(keypath, issued):
    """A listing from before direct payments (v2)."""
    key = L.load_identity(keypath)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"v": 2, "seller": L.sesh_of(pub), "issued": int(issued), "price_per_hour_usdc": "0.50",
            "max_hours": 168, "payout": "0x" + "aa" * 20, "vendor_id": "0x" + "bb" * 32, "chainid": 42161,
            "contract": "0x" + "c0" * 20, "region": "eu", "policy": ["tcp/443"], "dns": "10.195.0.53"}
    return _sign(keypath, body)


def quiet(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*a)
    return rc, out.getvalue(), err.getvalue()


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class BoardClientTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.bkey, self.pin = keyfile(self.d, "board.key")
        self.s1key, self.s1 = keyfile(self.d, "s1.key")
        self.s2key, self.s2 = keyfile(self.d, "s2.key")
        self.seq = 0
        self.srvs, self.urls = [], []
        for i in range(3):
            www = self.d / f"www{i}"
            www.mkdir()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(www)))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.srvs.append(srv)
            self.urls.append(f"http://127.0.0.1:{srv.server_address[1]}/")
        self.home = self.d / "home"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"board_urls": self.urls, "board_pin": self.pin}))

    def tearDown(self):
        for s in self.srvs:
            s.shutdown()
            s.server_close()

    def publish(self, idx, listings, key=None, seq=None, now=None):
        now = time.time() if now is None else now
        self.seq = seq if seq is not None else self.seq + 1
        body = {"v": 1, "board": self.pin, "seq": self.seq, "issued": int(now), "expires": int(now + 86400),
                "max_listing_age_s": 10800, "listings": listings}
        sig = L.load_identity(key or self.bkey).sign(B.canonical(body)).hex()
        for i in idx:
            (self.d / f"www{i}" / "board.json").write_text(json.dumps(body))
            (self.d / f"www{i}" / "board.sig").write_text(sig)
        return body

    def test_pinned_key_and_names(self):
        self.assertTrue(cb.BOARD_PIN.endswith(".sesh"))
        self.assertEqual(len(cb.BOARD_PIN), 57)
        L.pub_of_sesh(cb.BOARD_PIN)
        self.assertEqual(len(cb.BOARD_NAMES), 3)
        self.assertTrue(all(n.startswith("exits") and n.endswith(".sesh") for n in cb.BOARD_NAMES))
        self.assertEqual(cb.sources({}), list(cb.BOARD_NAMES))
        self.assertEqual(cb.pin_of({}), cb.BOARD_PIN)
        self.assertEqual(cb.resolve(cb.BOARD_NAMES[0]), f"http://{cb.BOARD_NAMES[0]}/")

    def test_resolve_rules(self):
        self.assertTrue(cb.resolve("https://mirror.example/b").endswith("/"))
        with self.assertRaises(ValueError):
            cb.resolve("http://mirror.example/b")

    def test_exits_no_flags_prints_table(self):
        now = time.time()
        self.publish([0, 1, 2], [listing_at(self.s1key, now - 1800), listing_at(self.s2key, now - 7200, "eu")])
        rc, out, err = quiet(main, ["--home", str(self.home), "exits"])
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("SELLER"))
        self.assertIn("USDC/HR", lines[0])
        self.assertIn("LAST REFRESH", lines[0])
        self.assertEqual(len(lines), 3)
        self.assertIn("30m ago", out)
        self.assertIn("2h00m ago", out)
        self.assertIn(self.urls[0], err)

    def test_falls_through_to_next_name(self):
        now = time.time()
        okey, _ = keyfile(self.d, "evil.key")
        self.publish([0], [listing_at(self.s1key, now)], key=okey)    # forged copy on the first name
        # second name: nothing served (404); third: good
        self.publish([2], [listing_at(self.s1key, now)])
        good, info = cb.fetch(self.urls, self.pin, self.home)
        self.assertEqual(info["source"], self.urls[2])
        self.assertEqual(len(info["errors"]), 2)
        self.assertIn("signature invalid", info["errors"][0])

    def test_drops_bad_and_stale_listings(self):
        now = time.time()
        bad = listing_at(self.s2key, now)
        bad["listing"]["price_per_hour_usdc"] = "0.01"
        stale = listing_at(self.s2key, now - 3 * 3600 - 60)
        self.publish([0], [listing_at(self.s1key, now), bad, stale])
        good, info = cb.fetch(self.urls[:1], self.pin, self.home)
        self.assertEqual([g["listing"]["seller"] for g in good], [self.s1])
        self.assertEqual(len(info["dropped"]), 2)

    def test_cache_fallback_and_rollback(self):
        now = time.time()
        old = self.publish([0], [listing_at(self.s1key, now)])
        old_b = (self.d / "www0" / "board.json").read_bytes()
        old_s = (self.d / "www0" / "board.sig").read_bytes()
        self.publish([0], [listing_at(self.s1key, now)])
        _, info = cb.fetch(self.urls[:1], self.pin, self.home)
        self.assertEqual(info["seq"], old["seq"] + 1)
        (self.d / "www0" / "board.json").write_bytes(old_b)
        (self.d / "www0" / "board.sig").write_bytes(old_s)
        _, info = cb.fetch(self.urls[:1], self.pin, self.home)
        self.assertEqual(info["source"], "cache")
        self.assertIn("rollback", info["errors"][0])

    def test_nothing_verifies(self):
        rc, out, err = quiet(main, ["--home", str(self.home), "exits"])
        self.assertEqual(rc, 1)
        self.assertIn("board unavailable", err)

    def test_listings_file_override(self):
        p = self.d / "l.json"
        p.write_text(json.dumps({"listings": [listing_at(self.s1key, time.time())]}))
        rc, out, err = quiet(main, ["--home", str(self.home), "exits", "--listings", str(p)])
        self.assertEqual(rc, 0, err)
        self.assertIn(self.s1, out)

    def test_old_listing_shows_seller_needs_to_update(self):
        now = time.time()
        self.publish([0], [listing_at(self.s1key, now), old_listing_at(self.s2key, now)])
        with mock.patch.object(cb, "OLD_LISTINGS_SHOWN_UNTIL", now + 86400):
            rc, out, err = quiet(main, ["--home", str(self.home), "exits"])
        self.assertEqual(rc, 0, err)
        row = [ln for ln in out.splitlines() if self.s2[:12] in ln]
        self.assertTrue(row and "seller needs to update" in row[0], out)
        self.assertIn("CHAINS", out.splitlines()[0])
        with mock.patch.object(cb, "OLD_LISTINGS_SHOWN_UNTIL", now - 1):
            rc, out, err = quiet(main, ["--home", str(self.home), "exits"])
        self.assertNotIn(self.s2[:12], out)
        self.assertIn(self.s1[:12], out)

    def test_board_fetch_uses_the_client_sesh_dns(self):
        seen = []
        with mock.patch.object(cb, "fetch", lambda *a, **k: seen.append(k.get("dns")) or ([], {
                "source": "x", "seq": 1, "dropped": []})):
            quiet(main, ["--home", str(self.home), "exits"])
        self.assertEqual(seen, ["127.3.2.1:53"])


if __name__ == "__main__":
    unittest.main()
