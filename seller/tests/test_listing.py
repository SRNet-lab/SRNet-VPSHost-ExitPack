import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import listing as L  # noqa: E402
import quote as Q  # noqa: E402

CLAIM = "http://" + "y" * 52 + ".sesh/claim"


def quote_key_file() -> str:
    p = Path(tempfile.mkdtemp()) / "quote.key"
    Q.new_key_file(p)
    return str(p)
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


class ListingTest(unittest.TestCase):
    def setUp(self):
        k = Ed25519PrivateKey.generate()
        seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.f = Path(tempfile.mkdtemp()) / "exit.key"
        self.f.write_text((seed + pub).hex())
        self.pub = pub

    def test_zb32_roundtrip_known(self):
        # random keys: zbase32 encode/decode must round-trip
        for _ in range(50):
            b = Ed25519PrivateKey.generate().public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            s = L.sesh_of(b)
            self.assertEqual(len(s), 57)
            self.assertEqual(L.pub_of_sesh(s), b)

    def test_make_verify_and_tamper(self):
        doc = L.make(L.load_identity(self.f), price_per_hour_usdc="1.00", max_hours=168, payout="0x" + "aa" * 20)
        self.assertEqual(doc["listing"]["seller"], L.sesh_of(self.pub))
        self.assertTrue(L.verify(doc))
        bad = json.loads(json.dumps(doc)); bad["listing"]["price_per_hour_usdc"] = "0.01"
        self.assertFalse(L.verify(bad))
        other = L.sesh_of(Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        bad2 = json.loads(json.dumps(doc)); bad2["listing"]["seller"] = other
        self.assertFalse(L.verify(bad2))

    def test_identity_mismatch_rejected(self):
        raw = bytes.fromhex(self.f.read_text())
        self.f.write_text((raw[:32] + bytes(32)).hex())
        with self.assertRaises(ValueError):
            L.load_identity(self.f)


    def test_refresh_resigns_same_offer(self):
        import tempfile, time as _t
        d = Path(tempfile.mkdtemp())
        doc = L.make(L.load_identity(self.f), price_per_hour_usdc="1.00", max_hours=168, payout="0x" + "aa" * 20)
        doc["listing"]["issued"] -= 7200
        doc["sig"] = L.load_identity(self.f).sign(L.canonical(doc["listing"])).hex()
        (d / "listing.json").write_text(json.dumps(doc))
        new = L.refresh(L.load_identity(self.f), d / "listing.json")
        self.assertTrue(L.verify(new))
        self.assertGreaterEqual(new["listing"]["issued"], int(_t.time()) - 2)
        for k in ("price_per_hour_usdc", "max_hours", "payout", "seller", "v"):
            self.assertEqual(new["listing"][k], doc["listing"][k])
        self.assertEqual(json.loads((d / "listing.json").read_text()), new)

    def test_refresh_refuses_other_identity(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        doc = L.make(L.load_identity(self.f), price_per_hour_usdc="1.00", max_hours=1, payout="0x" + "aa" * 20)
        (d / "listing.json").write_text(json.dumps(doc))
        with self.assertRaises(ValueError):
            L.refresh(Ed25519PrivateKey.generate(), d / "listing.json")

    def test_refresh_submits_to_board(self):
        import tempfile, threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(202); self.end_headers(); self.wfile.write(b"queued")

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        d = Path(tempfile.mkdtemp())
        doc = L.make(L.load_identity(self.f), price_per_hour_usdc="1.00", max_hours=1, payout="0x" + "aa" * 20)
        (d / "listing.json").write_text(json.dumps(doc))
        rc = L.main(["refresh", "--identity", str(self.f), "--listing", str(d / "listing.json"),
                     "--submit-url", f"http://127.0.0.1:{srv.server_address[1]}/submit"])
        srv.shutdown()
        self.assertEqual(rc, 0)
        self.assertEqual(len(got), 1)
        self.assertTrue(L.verify(got[0]))
    def test_claim_url(self):
        base = ["make", "--identity", str(self.f), "--price-per-hour-usdc", "1.00", "--payout", "0x" + "aa" * 20,
                "--quote-key-file", quote_key_file(), "--country", "US"]
        url = "http://" + "y" * 52 + ".sesh/claim"
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(L.main(base + ["--claim-url", url]), 0)
        doc = json.loads(out.getvalue())
        self.assertTrue(L.verify(doc))
        self.assertEqual(doc["listing"]["claim_url"], url)
        for bad in ("https://x.sesh/claim", "http://" + "y" * 52 + ".sesh/other", "http://1.2.3.4/claim"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                L.main(base + ["--claim-url", bad])



class ExitVersionTest(unittest.TestCase):
    def setUp(self):
        k = Ed25519PrivateKey.generate()
        seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.d = Path(tempfile.mkdtemp())
        self.f = self.d / "exit.key"
        self.f.write_text((seed + pub).hex())

    def _make(self, *extra):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = L.main(["make", "--identity", str(self.f), "--price-per-hour-usdc", "1.00",
                         "--payout", "0x" + "aa" * 20, "--quote-key-file", quote_key_file(),
                         "--claim-url", CLAIM, "--country", "us", *extra])
        self.assertEqual(rc, 0)
        return json.loads(out.getvalue())

    def test_pack_version_file(self):
        v = (Path(L.__file__).resolve().parent / "VERSION").read_text().strip()
        self.assertEqual(L.pack_version(), v)
        self.assertIsNotNone(L.version_key(v))

    def test_make_sets_exit_version(self):
        doc = self._make()
        self.assertEqual(doc["listing"]["exit_version"], L.pack_version())
        self.assertTrue(L.verify(doc))
        self.assertEqual(self._make("--exit-version", "")["listing"].get("exit_version"), None)

    def test_refresh_updates_exit_version(self):
        doc = self._make("--exit-version", "0.0.9")
        (self.d / "listing.json").write_text(json.dumps(doc))
        new = L.refresh(L.load_identity(self.f), self.d / "listing.json")
        self.assertEqual(new["listing"]["exit_version"], L.pack_version())
        self.assertTrue(L.verify(new))
        old = dict(doc["listing"]); old.pop("exit_version")
        (self.d / "listing.json").write_text(json.dumps(L.make(L.load_identity(self.f), **{
            k: v for k, v in old.items() if k not in ("v", "seller", "issued")})))
        self.assertEqual(L.refresh(L.load_identity(self.f), self.d / "listing.json")["listing"]["exit_version"],
                         L.pack_version())

    def test_country(self):
        self.assertEqual(self._make()["listing"]["country"], "US")
        self.assertEqual(L.country_text({"country": "DE"}), "DE")
        self.assertEqual(L.country_text({}), "unknown")
        self.assertEqual(L.country_text({"country": "usa"}), "unknown")
        with self.assertRaises(SystemExit):
            self._make("--country", "USA")

    def test_speed_cap(self):
        self.assertEqual(self._make("--speed-cap-mbps", "10")["listing"]["speed_cap_mbps"], 10)
        self.assertNotIn("speed_cap_mbps", self._make()["listing"])
        self.assertEqual(L.speed_cap_text({"speed_cap_mbps": 10}), "10 Mbps")
        self.assertEqual(L.speed_cap_text({"speed_cap_mbps": 0}), "unlimited")
        self.assertEqual(L.speed_cap_text({}), "unknown")
        self.assertEqual(L.speed_cap_text({"speed_cap_mbps": "10"}), "unknown")
        doc = self._make("--speed-cap-mbps", "25")
        (self.d / "listing.json").write_text(json.dumps(doc))
        self.assertEqual(L.refresh(L.load_identity(self.f), self.d / "listing.json")["listing"]["speed_cap_mbps"], 25)
        with self.assertRaises(SystemExit):
            self._make("--speed-cap-mbps", "-1")

    def test_min_version_compare(self):
        self.assertTrue(L.meets_min_version({}, None))
        self.assertFalse(L.meets_min_version({}, "0.1.0"))
        self.assertFalse(L.meets_min_version({"exit_version": "junk"}, "0.1.0"))
        self.assertTrue(L.meets_min_version({"exit_version": "0.1.0"}, "0.1.0"))
        self.assertTrue(L.meets_min_version({"exit_version": "0.10.0+x"}, "0.9.1"))
        self.assertFalse(L.meets_min_version({"exit_version": "0.0.9"}, "0.1.0"))
        with self.assertRaises(ValueError):
            L.meets_min_version({"exit_version": "0.1.0"}, "latest")

class V3Fields(unittest.TestCase):
    def setUp(self):
        self.f = Path(tempfile.mkdtemp()) / "exit.key"
        k = Ed25519PrivateKey.generate()
        self.f.write_text(k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                          serialization.NoEncryption()).hex())
        self.qk = quote_key_file()

    def make(self, *extra):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            L.main(["make", "--identity", str(self.f), "--price-per-hour-usdc", "0.50", "--payout", "0x" + "Aa" * 20,
                    "--quote-key-file", self.qk, "--claim-url", CLAIM, "--country", "US", *extra])
        return json.loads(out.getvalue())["listing"]

    def test_defaults_both_chains_usdc(self):
        b = self.make()
        self.assertEqual(b["v"], 3)
        self.assertEqual(b["chains"], [8453, 42161])
        self.assertEqual(b["token"], "USDC")
        self.assertEqual(b["quote_key"], Q.pub_hex(Q.load_key(self.qk)))
        self.assertEqual(b["payout"], "0x" + "aa" * 20)
        for gone in ("vendor_id", "contract", "chainid"):
            self.assertNotIn(gone, b)
        self.assertIsNone(L.listing_error(b))

    def test_one_chain_and_bad_chain(self):
        self.assertEqual(self.make("--chain", "base")["chains"], [8453])
        with self.assertRaises(SystemExit):
            self.make("--chain", "ethereum")
        with self.assertRaises(SystemExit):
            self.make("--chain", "base-sepolia")
        self.assertEqual(self.make("--chain", "base-sepolia", "--testnets")["chains"], [84532])

    def test_listing_error(self):
        b = self.make()
        self.assertIsNone(L.listing_error(b))
        self.assertEqual(L.listing_error({**b, "chains": [84532]}), "unsupported chain")
        self.assertIsNone(L.listing_error({**b, "chains": [84532]}, testnets=True))
        self.assertEqual(L.listing_error({**b, "v": 2}), L.NEEDS_UPDATE)
        self.assertEqual(L.listing_error({**b, "v": 9}), "unsupported listing version")
        self.assertEqual(L.listing_error({**b, "token": "DAI"}), "only USDC is supported")
        nq = dict(b); del nq["quote_key"]
        self.assertEqual(L.listing_error(nq), "missing quote_key")

    def test_min_price_one_cent(self):
        b = self.make()
        self.assertIsNone(L.listing_error({**b, "price_per_hour_usdc": "0.01"}))
        for p in ("0.009999", "0.000001", "0"):
            self.assertEqual(L.listing_error({**b, "price_per_hour_usdc": p}), "price_per_hour_usdc must be at least 0.01")
        self.assertEqual(L.listing_error({**b, "price_per_hour_usdc": "1.0000001"}), "bad price_per_hour_usdc")

    def test_refresh_refuses_v2(self):
        d = Path(tempfile.mkdtemp())
        key = L.load_identity(self.f)
        body = {"v": 2, "seller": L.sesh_of(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                                             serialization.PublicFormat.Raw)),
                "issued": 1, "price_per_hour_usdc": "1.00"}
        (d / "l.json").write_text(json.dumps({"listing": body, "sig": key.sign(L.canonical(body)).hex()}))
        with self.assertRaises(ValueError):
            L.refresh(key, d / "l.json")


class DnsField(unittest.TestCase):
    def test_valid_dns_private_ipv4_only(self):
        import listing as LL
        for ok in ("10.195.0.53", "172.16.1.1", "192.168.0.53", "100.64.0.1"):
            self.assertTrue(LL.valid_dns(ok), ok)
        for bad in ("1.1.1.1", "9.9.9.9", "8.8.8.8", "fd00::1", "", None, 53, "10.195.0.53:53", "localhost"):
            self.assertFalse(LL.valid_dns(bad), bad)


if __name__ == "__main__":
    unittest.main()
