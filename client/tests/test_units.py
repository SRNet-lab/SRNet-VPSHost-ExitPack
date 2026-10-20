"""Small pieces kept from the old client: listing files, the purchase warning, exit resolver rules,
the kill switch rules, and buying from an exit without its own resolver."""
import io as _io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import ME

F = helpers.seller_fixtures()
from srnet_exits import killswitch, listings, warning  # noqa: E402

sys.stdin = _io.StringIO()


class ListingsFile(unittest.TestCase):
    def test_drops_bad_keeps_good(self):
        s = F.Seller(dns="10.195.0.53")
        bad = json.loads(json.dumps(s.listing_doc))
        bad["listing"]["price_per_hour_usdc"] = "0.01"
        path = s.d / "board.json"
        path.write_text(json.dumps({"listings": [s.listing_doc, bad]}))
        board = listings.load_board(path)
        self.assertEqual([d["listing"]["seller"] for d in board], [s.sesh])
        self.assertEqual(listings.find_seller(board, s.sesh)["listing"]["seller"], s.sesh)
        self.assertIsNone(listings.find_seller(board, F.CLIENT))


class Warning(unittest.TestCase):
    def test_ack_required_then_cached(self):
        d = Path(tempfile.mkdtemp())
        with mock.patch("builtins.input", return_value="nope"), mock.patch("sys.stdout", _io.StringIO()):
            with self.assertRaises(SystemExit):
                warning.ensure_ack(d)
        self.assertFalse(warning.acked(d))
        with mock.patch("builtins.input", return_value="I UNDERSTAND"), mock.patch("sys.stdout", _io.StringIO()):
            warning.ensure_ack(d)
        self.assertTrue(warning.acked(d))
        warning.ensure_ack(d)  # no input needed

    def test_text_is_about_direct_final_payments(self):
        t = warning.WARNING.lower()
        self.assertIn("final", t)
        self.assertNotIn("auto-renew", t.replace("no auto-renew", ""))
        for gone in ("relayer", "balance contract", "3%", "gasless"):
            self.assertNotIn(gone, t)


class ExitDns(unittest.TestCase):
    def test_only_private_ipv4_counts_as_an_exit_resolver(self):
        self.assertEqual(listings.exit_dns({"dns": "10.195.0.53"}), "10.195.0.53")
        for bad in ({}, {"dns": None}, {"dns": "1.1.1.1"}, {"dns": "9.9.9.9"}, {"dns": "fd00::1"},
                    {"dns": "10.195.0.53:53"}, {"dns": 53}, None):
            self.assertIsNone(listings.exit_dns(bad), bad)


class KillSwitch(unittest.TestCase):
    def test_arm_disarm_dry(self):
        ks = killswitch.KillSwitch(name="srnet_ks_test")
        scripts = []

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        ks._nft_f = lambda script, check=True: scripts.append(script) or R()
        ks._nft = lambda *a, check=True: scripts.append(" ".join(a)) or R()
        ks.arm("eth0", bootstrap_ips=["1.2.3.4"])
        self.assertTrue(any("meta l4proto udp" in s for s in scripts))
        self.assertTrue(any("dport 53" in s for s in scripts))
        self.assertTrue(any('oifname "sr-tun0"' in s for s in scripts))
        ks.disarm()
        self.assertTrue(any("delete" in s for s in scripts))


@unittest.skipUnless(helpers.HAVE_CORE, "srnet-client (the SRNet core) not found")
class BuyNoExitDns(unittest.TestCase):
    def test_buy_refuses_exit_without_resolver_before_asking_for_a_quote(self):
        from srnet_exits import cli
        s = F.Seller()
        home = Path(tempfile.mkdtemp())
        (home / "visibility.ack").write_text("I UNDERSTAND\n")
        err = _io.StringIO()
        with mock.patch.object(cli.listings, "load_board", lambda p: [s.listing_doc]), \
                mock.patch.object(cli.pay, "Buyer", lambda *a, **k: self.fail("asked for a quote")), \
                mock.patch("sys.stderr", err):
            rc = cli.main(["--home", str(home), "buy", s.sesh, "--listings", "x.json", "--client-sesh", ME])
        self.assertEqual(rc, 1)
        self.assertIn("no DNS resolver", err.getvalue())


if __name__ == "__main__":
    unittest.main()
