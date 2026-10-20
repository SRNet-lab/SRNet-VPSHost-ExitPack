"""Board rows, reminder, shop payment check, the add-on object, the tab backend's HTTP guard,
and `srnet buy --no-wait` output."""
import functools
import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import ME, Chain, Clock, desk_transport

F = helpers.seller_fixtures()
import chains as C  # noqa: E402
import claimd as D  # noqa: E402

from srnet_exits import board, pay, reminder, shopcheck  # noqa: E402


class BoardRows(unittest.TestCase):
    def test_v3_listing_is_buyable_and_old_one_needs_update(self):
        s = F.Seller(price="0.50", chains=(42161, 8453))
        old = {"listing": {"v": 2, "seller": "z" * 52 + ".sesh", "price_per_hour_usdc": "1", "max_hours": 24,
                           "chainid": 42161, "issued": F.T0}}
        now = board.OLD_LISTINGS_SHOWN_UNTIL - 86400
        rows = board.rows([s.listing_doc, old], now)
        by = {r["seller"]: r for r in rows}
        new = by[s.sesh]
        self.assertTrue(new["buyable"])
        self.assertEqual(sorted(new["chains"]), ["Arbitrum One", "Base"])
        self.assertEqual(new["price"], "0.50")
        self.assertEqual(by[old["listing"]["seller"]]["note"], "seller needs to update")
        self.assertFalse(by[old["listing"]["seller"]]["buyable"])
        t = board.table([s.listing_doc, old], now)
        self.assertIn("CHAINS", t)
        self.assertIn("seller needs to update", t)

    def test_old_listings_hidden_after_the_cutoff(self):
        s = F.Seller()
        old = {"listing": {"v": 2, "seller": "z" * 52 + ".sesh", "issued": F.T0}}
        later = board.OLD_LISTINGS_SHOWN_UNTIL + 1
        self.assertEqual([r["seller"] for r in board.rows([s.listing_doc, old], later)], [s.sesh])


class Reminder(unittest.TestCase):
    def home(self, end):
        h = Path(tempfile.mkdtemp())
        (h / "state.json").write_text(json.dumps({"exits": {"leases": {F.CLIENT: {"end": end}}}}))
        return h

    def test_warns_once_about_ten_minutes_before_then_exits(self):
        clock = Clock(1000)
        h = self.home(1000 + 3600)
        told = []
        self.assertEqual(reminder.run(h, F.CLIENT, clock=clock, sleep=clock.sleep, notify=told.append), 0)
        self.assertEqual(len(told), 1)
        self.assertIn("ends in about 10 min", told[0])
        self.assertIn(f"srnet buy --seller {F.CLIENT} --hours 1", told[0])

    def test_a_new_purchase_moves_the_warning(self):
        clock = Clock(1000)
        h = self.home(1000 + 900)
        told = []

        def sleep(s):
            clock.sleep(s)
            if len(told) == 1 and clock.t > 1000 + 400:
                (h / "state.json").write_text(json.dumps({"exits": {"leases": {F.CLIENT: {"end": 1000 + 4000}}}}))
        reminder.run(h, F.CLIENT, clock=clock, sleep=sleep, notify=told.append)
        self.assertEqual(len(told), 2)

    def test_no_lease_exits_quietly(self):
        self.assertEqual(reminder.run(Path(tempfile.mkdtemp()), F.CLIENT, notify=self.fail), 0)


class ShopCheck(unittest.TestCase):
    def setUp(self):
        self.ch = Chain(8453, Clock(F.T0))
        self.to = "0x" + "ab" * 20
        self.usdc = C.usdc(8453)

    def test_exact_payment(self):
        tx = self.ch.pay(F.tx(1), self.usdc, self.to, 3_500_000)
        r = shopcheck.check(self.ch, 8453, tx, self.to, 3_500_000)
        self.assertTrue(r["paid"], r)
        self.assertEqual(r["amount"], "3.50")
        self.assertIn("basescan.org", r["link"])

    def test_mismatches(self):
        tx = self.ch.pay(F.tx(2), self.usdc, self.to, 3_000_000)
        self.assertIn("amount mismatch", shopcheck.check(self.ch, 8453, tx, self.to, 3_500_000)["why"])
        fake = self.ch.pay(F.tx(3), "0x" + "99" * 20, self.to, 3_500_000)
        self.assertIn("no USDC transfer", shopcheck.check(self.ch, 8453, fake, self.to, 3_500_000)["why"])
        self.assertIn("another chain", shopcheck.check(self.ch, 42161, tx, self.to, 3_000_000)["why"])
        self.assertIn("not mined", shopcheck.check(self.ch, 8453, F.tx(77), self.to, 1)["why"])
        failed = self.ch.pay(F.tx(4), self.usdc, self.to, 1, ok=False)
        self.assertIn("failed", shopcheck.check(self.ch, 8453, failed, self.to, 1)["why"])
        self.assertIn("unsupported", shopcheck.check(self.ch, 1, tx, self.to, 1)["why"])


class Addon(unittest.TestCase):
    def test_entry_object(self):
        import argparse
        from srnet_exits.addon import ADDON
        self.assertEqual((ADDON.name, ADDON.title), ("exits", "Exits"))
        ap = argparse.ArgumentParser()
        cmds = ADDON.register(ap.add_subparsers(dest="cmd"))
        self.assertEqual(sorted(cmds), ["buy", "connect", "disconnect", "exits", "paid", "status", "verify"])
        a = ap.parse_args(["buy", "--seller", F.CLIENT, "--hours", "3", "--chain", "base"])
        self.assertEqual((a.hours, a.chain), (3, "base"))
        m = ADDON.ui_manifest()
        self.assertEqual(m["name"], "exits")
        self.assertIn("connect", m["slow"])

    def test_no_wallet_or_eth_libraries(self):
        import sys
        import srnet_exits.addon  # noqa: F401
        import srnet_exits.cli  # noqa: F401
        import srnet_exits.ui  # noqa: F401
        for mod in ("eth_account", "eth_abi", "web3", "eth_keys"):
            self.assertNotIn(mod, sys.modules)


@unittest.skipUnless(helpers.HAVE_CORE, "srnet-client (the SRNet core) not found")
class TabBackend(unittest.TestCase):
    def setUp(self):
        from srnet.state import State
        from srnet_exits import ui
        self.st = State(Path(tempfile.mkdtemp()))
        self.srv, self.base, self.tok = ui.serve(ui.Backend(self.st))
        import threading
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_token_host_and_origin_are_required(self):
        self.assertEqual(self.req("GET", "/tab.js")[0], 403)
        code, js = self.req("GET", f"/tab.js?k={self.tok}")
        self.assertEqual(code, 200)
        self.assertIn(b"SRNetAddons", js)
        self.assertEqual(self.req("POST", "/api/status", {})[0], 403)
        self.assertEqual(self.req("POST", "/api/status", {}, {"X-CSRF-Token": "nope"})[0], 403)
        self.assertEqual(self.req("POST", "/api/status", {}, {"X-CSRF-Token": self.tok,
                                                              "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/status", {}, {"X-CSRF-Token": self.tok, "Host": "evil.example"})[0], 403)
        code, out = self.req("POST", "/api/status", {}, {"X-CSRF-Token": self.tok})
        self.assertEqual(code, 200)
        j = json.loads(out)
        self.assertEqual(j["max_payment_usdc"], "20.00")
        self.assertFalse(j["acked"])

    def test_settings_limit_and_chain(self):
        h = {"X-CSRF-Token": self.tok}
        out = json.loads(self.req("POST", "/api/settings", {"max_payment_usdc": "7.5", "chain": "base"}, h)[1])
        self.assertEqual((out["max_payment_usdc"], out["chain"]), ("7.50", "base"))
        self.assertFalse(json.loads(self.req("POST", "/api/settings", {"chain": "ethereum"}, h)[1])["ok"])
        self.assertFalse(json.loads(self.req("POST", "/api/settings", {"max_payment_usdc": "-1"}, h)[1])["ok"])

    def test_quote_needs_the_warning_acknowledged(self):
        out = json.loads(self.req("POST", "/api/quote", {"seller": F.CLIENT, "hours": 1, "client_sesh": ME},
                                  {"X-CSRF-Token": self.tok})[1])
        self.assertTrue(out.get("need_ack"))


@unittest.skipUnless(helpers.HAVE_CORE, "srnet-client (the SRNet core) not found")
class BuyCli(unittest.TestCase):
    def test_buy_no_wait_prints_address_amount_and_link(self):
        from srnet_exits import cli
        s = F.Seller(price="1.00", dns="10.195.0.53")
        clock = Clock(F.T0)
        desk = D.Desk(s.state, s.quote_key, s.listing_path, clock=clock)
        lp = Path(tempfile.mkdtemp()) / "board.json"
        lp.write_text(json.dumps([s.listing_doc]))
        home = Path(tempfile.mkdtemp())
        (home / "visibility.ack").write_text("I UNDERSTAND\n")
        chain = Chain(42161, clock)
        Buyer = functools.partial(pay.Buyer, transport=desk_transport(desk), rpc_factory=lambda c: chain, clock=clock)
        out = io.StringIO()
        with mock.patch.object(cli.pay, "Buyer", Buyer), mock.patch("sys.stdout", out), \
                mock.patch.object(cli.listings, "load_board", lambda p: [s.listing_doc]):
            rc = cli.main(["--home", str(home), "buy", "--seller", s.sesh, "--hours", "2", "--client-sesh", ME,
                           "--listings", str(lp), "--no-wait"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        st = json.loads((home / "state.json").read_text())
        (qid, rec), = st["exits"]["quotes"].items()
        q = rec["doc"]["quote"]
        self.assertIn(F.PAYOUT, text.lower())
        self.assertIn(C.fmt_usdc(q["amount"]), text)
        self.assertIn("Arbitrum", text)
        self.assertIn("ethereum:", text)
        self.assertIn(f"srnet paid --quote {qid}", text)

    def test_buy_over_the_limit_is_refused_before_asking(self):
        from srnet_exits import cli
        s = F.Seller(price="15.00", dns="10.195.0.53")
        home = Path(tempfile.mkdtemp())
        (home / "visibility.ack").write_text("I UNDERSTAND\n")
        err = io.StringIO()
        Buyer = functools.partial(pay.Buyer, transport=lambda *a, **k: self.fail("asked the seller"))
        with mock.patch.object(cli.pay, "Buyer", Buyer), mock.patch("sys.stderr", err), \
                mock.patch.object(cli.listings, "load_board", lambda p: [s.listing_doc]):
            rc = cli.main(["--home", str(home), "buy", "--seller", s.sesh, "--hours", "2", "--client-sesh", ME,
                           "--listings", "x.json"])
        self.assertEqual(rc, 1)
        self.assertIn("over your limit of 20.00", err.getvalue())


class Packaging(unittest.TestCase):
    def test_no_dependency_on_unregistered_names(self):
        """pip must never fetch "srnet-client" (not ours on PyPI) for the add-on."""
        import tomllib
        root = Path(__file__).resolve().parents[2]
        deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
        names = [d.split(">")[0].split("=")[0].split("@")[0].split("[")[0].strip().lower() for d in deps]
        self.assertNotIn("srnet-client", names)
        self.assertNotIn("srnet-exits", names)

    def test_srnet_exits_without_the_core_says_so(self):
        from srnet_exits import cli
        err = io.StringIO()
        with mock.patch.dict("sys.modules", {"srnet": None, "srnet.state": None}), mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(["exits"]), 2)
        self.assertIn("needs the SRNet client", err.getvalue())


if __name__ == "__main__":
    unittest.main()
