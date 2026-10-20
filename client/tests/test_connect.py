"""`srnet connect` / `disconnect` / `status` in the exits add-on: paid time comes from a confirmed
direct payment (no chain reads at connect), DNS goes through the exit's own resolver, the kill
switch defaults, and the client .sesh DNS default."""
import io as _io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import ME, SELLER

sys.stdin = _io.StringIO()


class _Base(unittest.TestCase):
    STOCK = {"tun": "sr-tun0", "dns": ["127.3.2.1"], "domains": ["~sesh", "~snode"], "default_route": False}

    def setUp(self):
        from srnet_exits import cli
        self.cli = cli
        FakeRpc.calls = []

    def home(self, dns="10.195.0.53", left=3600, seller=SELLER):
        from srnet_exits.state import State
        home = Path(tempfile.mkdtemp())
        (home / "visibility.ack").write_text("I UNDERSTAND\n")
        st = State(home)
        st.data["exits"] = {"quotes": {}, "leases": {seller: {
            "seller": seller, "end": int(time.time()) + left, "dns": dns, "client_sesh": ME,
            "chainid": 8453, "quotes": ["00112233aabbccdd"]}}}
        st.save()
        return home

    def run_cli(self, home, *extra):
        cli = self.cli
        self.dns_calls = getattr(self, "dns_calls", [])
        self.inbound_calls = getattr(self, "inbound_calls", [])
        if getattr(self, "dns_cur", None) is None:
            self.dns_cur = dict(self.STOCK)

        def apply(saved, listen=cli.tunneldns.LISTEN):
            self.dns_calls.append(("apply", saved))
            self.dns_cur.update(dns=[listen], domains=["~.", "~sesh", "~snode"], default_route=True)

        def restore(saved):
            self.dns_calls.append(("restore", saved))
            self.dns_cur.clear()
            self.dns_cur.update(saved)
        with mock.patch.object(cli.tunneldns, "resolved_active", lambda: getattr(self, "resolved", True)), \
             mock.patch.object(cli.tunneldns, "snapshot", lambda tun: dict(self.dns_cur)), \
             mock.patch.object(cli.tunneldns, "start_proxy", getattr(self, "start_proxy", lambda *a, **k: 4242)), \
             mock.patch.object(cli.tunneldns, "stop_proxy", lambda home: self.dns_calls.append(("stop",)) or True), \
             mock.patch.object(cli.tunneldns, "proxy_pid", lambda home: 4242), \
             mock.patch.object(cli.tunneldns, "apply", apply), \
             mock.patch.object(cli.tunneldns, "restore", restore), \
             mock.patch.object(cli.tunneldns, "tun_networks", lambda tun: getattr(self, "tun_nets", ["172.16.0.0/16"])), \
             mock.patch.object(cli, "load_listings", getattr(self, "board", lambda *a, **k: [])), \
             mock.patch.object(cli, "_rpc", FakeRpc), \
             mock.patch.object(cli.reminder, "start", lambda *a: None), \
             mock.patch.object(cli, "guess_wan", lambda *a: "eth0"), \
             mock.patch.object(cli.routercfg, "problems", getattr(self, "router_problems", lambda path, privileged=False: [])), \
             mock.patch.object(cli.inbound, "up", lambda wan: self.inbound_calls.append(("up", wan))), \
             mock.patch.object(cli.inbound, "down", lambda: self.inbound_calls.append(("down",))), \
             mock.patch.object(cli, "own_sesh", lambda: ME):
            return cli.main(["--home", str(home), *extra])

    def st(self, home):
        return json.loads((home / "state.json").read_text())


class FakeRpc:
    calls = []

    def __init__(self, endpoint):
        pass

    def map_exit(self, seller, full_tunnel=True):
        FakeRpc.calls.append(("map", seller))
        return {"result": "ok"}

    def unmap_exit(self, seller):
        FakeRpc.calls.append(("unmap", seller))
        return {"result": "ok"}

    def list_exits(self):
        return {"result": [{"address": SELLER, "exit_capable": True, "session_live": True}]}


class Connect(_Base):
    def test_connect_inside_paid_time(self):
        home = self.home()
        out = _io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 0)
        self.assertIn(("map", SELLER), FakeRpc.calls)
        c = self.st(home)["connected"]
        self.assertEqual(c["seller"], SELLER)
        self.assertGreater(c["lease_end"], time.time())
        self.assertIn("paid time left", out.getvalue())

    def test_reconnect_after_disconnect_needs_no_new_purchase(self):
        home = self.home()
        self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 0)
        out = _io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(self.run_cli(home, "disconnect"), 0)
        self.assertIn("no new purchase", out.getvalue())
        self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 0)

    def test_no_paid_time_refuses(self):
        for home in (self.home(left=-5), self.home(seller="z" * 52 + ".sesh")):
            err = _io.StringIO()
            with mock.patch("sys.stderr", err):
                self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 1)
            self.assertIn("no paid time for this seller", err.getvalue())
            self.assertIn("srnet buy --seller", err.getvalue())
        self.assertEqual(FakeRpc.calls, [])

    def test_exit_not_capable_unmaps(self):
        class NotCapable(FakeRpc):
            def list_exits(self):
                return {"result": []}
        home = self.home()
        with mock.patch.object(self.cli, "_rpc", NotCapable), \
                mock.patch.object(self.cli.inbound, "up", lambda wan: None), \
                mock.patch("srnet_exits.backend.LiveBackend.wait_capable", lambda *a: False), \
                mock.patch("sys.stdout", _io.StringIO()):
            rc = self.cli.main(["--home", str(home), "connect", SELLER, "--no-killswitch", "--system-dns"])
        self.assertEqual(rc, 1)
        self.assertIn(("unmap", SELLER), FakeRpc.calls)

    def test_missing_nft_refuses_connect_clearly(self):
        home = self.home()
        err = _io.StringIO()
        with mock.patch.object(self.cli.killswitch, "find_nft", lambda: None), mock.patch("sys.stderr", err):
            self.assertEqual(self.run_cli(home, "connect", SELLER), 1)
        self.assertIn("sudo apt install nftables", err.getvalue())
        self.assertIn("--no-killswitch", err.getvalue())
        self.assertEqual(FakeRpc.calls, [], "nothing is mapped when the kill switch cannot be armed")


class InboundSessions(_Base):
    """connect must not cut SSH/RDP sessions to this machine's own public IP."""

    def test_inbound_routing_goes_up_before_the_tunnel_and_down_at_disconnect(self):
        home = self.home()
        seen = []
        orig_map = FakeRpc.map_exit

        def map_exit(rpc, seller, full_tunnel=True):
            seen.append(list(self.inbound_calls))
            return orig_map(rpc, seller, full_tunnel)
        with mock.patch.object(FakeRpc, "map_exit", map_exit), mock.patch("sys.stdout", _io.StringIO()):
            self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch", "--system-dns"), 0)
        self.assertEqual(seen, [[("up", "eth0")]], "inbound routing must be in place before map_exit")
        with mock.patch("sys.stdout", _io.StringIO()):
            self.assertEqual(self.run_cli(home, "disconnect"), 0)
        self.assertEqual(self.inbound_calls[-1], ("down",))

    def test_failed_unmap_keeps_inbound_routing_and_the_record(self):
        home = self.home()
        with mock.patch("sys.stdout", _io.StringIO()):
            self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch", "--system-dns"), 0)

        def unmap_fails(rpc, seller):
            raise ConnectionError("no answer from Session Router")
        err, out = _io.StringIO(), _io.StringIO()
        with mock.patch.object(FakeRpc, "unmap_exit", unmap_fails), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", err):
            self.run_cli(home, "disconnect")
        self.assertNotIn(("down",), self.inbound_calls)
        self.assertIn("kept the routing that keeps SSH/RDP", err.getvalue())
        self.assertEqual(self.st(home)["connected"]["seller"], SELLER)
        # next time Session Router answers: unmapped, and the inbound routing goes
        with mock.patch("sys.stdout", _io.StringIO()):
            self.assertEqual(self.run_cli(home, "disconnect"), 0)
        self.assertEqual(self.inbound_calls[-1], ("down",))
        self.assertIn(("unmap", SELLER), FakeRpc.calls)
        self.assertIsNone(self.st(home)["connected"])

    def test_connect_refuses_when_inbound_routing_fails(self):
        home = self.home()
        err = _io.StringIO()

        def boom(wan):
            raise self.cli.inbound.InboundError("no default route on eth0")
        with mock.patch.object(self.cli.inbound, "up", boom), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdout", _io.StringIO()), \
                mock.patch.object(self.cli, "_rpc", FakeRpc), mock.patch.object(self.cli, "guess_wan", lambda *a: "eth0"):
            rc = self.cli.main(["--home", str(home), "connect", SELLER, "--no-killswitch", "--system-dns"])
        self.assertEqual(rc, 1)
        self.assertIn("cut your SSH/RDP session", err.getvalue())
        self.assertEqual(FakeRpc.calls, [], "nothing is mapped")

    def test_connect_refuses_with_router_blackhole_routes_on(self):
        home = self.home()
        self.router_problems = lambda path, privileged=False: ["Session Router's blackhole-routes is on: while connected ..."]
        err = _io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", _io.StringIO()):
            self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch", "--system-dns"), 1)
        self.assertIn("srnet setup-router", err.getvalue())
        self.assertEqual(FakeRpc.calls, [])
        self.assertEqual(self.inbound_calls, [], "nothing touched before the check")

    def test_buy_refuses_without_a_persistent_router_key(self):
        home = self.home()
        self.router_problems = lambda path, privileged=False: ["Session Router has no persistent key (keyfile= in [network]) ..."]
        err = _io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", _io.StringIO()):
            self.assertEqual(self.run_cli(home, "buy", SELLER, "--hours", "1", "--client-sesh", ME), 1)
        self.assertIn("srnet setup-router", err.getvalue())

    def test_setup_router_runs_the_root_helper_once_through_priv(self):
        import subprocess as sp
        import tempfile
        home = self.home()
        calls = []
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / "client.ini"
            ini.write_text("[network]\n")

            def run(cmd, **kw):
                calls.append(cmd)
                self.assertEqual(kw.get("cwd"), "/")
                return sp.CompletedProcess(cmd, 0, "restarted session-router-client\n", "")
            out = _io.StringIO()
            with mock.patch.object(self.cli.priv, "run", run), mock.patch("sys.stdout", out):
                self.assertEqual(self.run_cli(home, "setup-router", "--ini", str(ini)), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1:], ["-I", "-m", "srnet_exits.routercfg", "apply", "--ini", str(ini), "--prog", "srnet"])
            self.assertIn("restarted", out.getvalue())
            err = _io.StringIO()
            with mock.patch.object(self.cli.priv, "run", run), mock.patch("sys.stderr", err):
                self.assertEqual(self.run_cli(home, "setup-router", "--ini", str(Path(d) / "none.ini")), 1)
            self.assertIn("install Session Router first", err.getvalue())
            self.assertEqual(len(calls), 1)

    def test_guess_wan_skips_the_tunnel(self):
        outs = {"show": "default dev sr-tun0 scope link\ndefault via 10.0.0.1 dev ens3 proto dhcp\n",
                "get": "1.1.1.1 dev sr-tun0 src 172.16.0.1\n"}
        with mock.patch.object(self.cli.subprocess, "check_output",
                               lambda cmd, **k: outs["show" if "show" in cmd else "get"]):
            self.assertEqual(self.cli.guess_wan("sr-tun0"), "ens3")


class TunnelDNS(_Base):
    def connect(self, *extra):
        home = self.home()
        return home, self.run_cli(home, "connect", SELLER, "--no-killswitch", *extra)

    def test_connect_routes_dns_through_exit_and_disconnect_restores_exactly(self):
        home, rc = self.connect()
        self.assertEqual(rc, 0)
        s = self.st(home)
        self.assertEqual(s["tunnel_dns"]["saved"], self.STOCK)
        self.assertEqual(s["connected"]["dns"], "tunnel")
        self.assertEqual(self.dns_cur["domains"][0], "~.")
        self.assertEqual(self.run_cli(home, "disconnect"), 0)
        self.assertEqual(self.dns_cur, self.STOCK)
        self.assertIsNone(self.st(home)["tunnel_dns"])
        self.assertEqual([c[0] for c in self.dns_calls], ["apply", "restore", "stop"])

    def test_disconnect_restores_dns_before_unmapping(self):
        home, _ = self.connect()
        order = []
        self.dns_calls = order
        FakeRpc.calls = order
        self.run_cli(home, "disconnect")
        self.assertLess(order.index(("restore", self.STOCK)), order.index(("unmap", SELLER)))

    def test_dns_failure_unmaps_instead_of_leaking(self):
        def boom(*a, **k):
            raise self.cli.tunneldns.TunnelDNSError("DNS over the exit does not answer")
        self.start_proxy = boom
        home, rc = self.connect()
        self.assertEqual(rc, 1)
        self.assertIn(("unmap", SELLER), FakeRpc.calls)
        self.assertIsNone(self.st(home).get("connected"))
        self.assertEqual(self.dns_cur, self.STOCK)

    def test_refuses_without_resolved(self):
        self.resolved = False
        _, rc = self.connect()
        self.assertEqual(rc, 1)
        self.assertEqual(FakeRpc.calls, [])

    def test_system_dns_flag_leaves_dns_alone(self):
        self.resolved = False
        home, rc = self.connect("--system-dns")
        self.assertEqual(rc, 0)
        self.assertEqual(self.dns_calls, [])
        self.assertEqual(self.st(home)["connected"]["dns"], "system")

    def test_second_connect_keeps_the_original_snapshot(self):
        home, _ = self.connect()
        self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 0)
        self.assertEqual(self.st(home)["tunnel_dns"]["saved"], self.STOCK)

    def test_our_settings_without_a_record_fall_back_to_stock(self):
        self.dns_cur = {"tun": "sr-tun0", "dns": [self.cli.tunneldns.LISTEN], "domains": ["~.", "~sesh", "~snode"],
                        "default_route": True}
        home, _ = self.connect()
        self.assertEqual(self.st(home)["tunnel_dns"]["saved"], self.STOCK)

    def test_status_words(self):
        home, _ = self.connect()
        out = _io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", _io.StringIO()), \
                mock.patch.object(self.cli.killswitch.KillSwitch, "status", lambda self: None), \
                mock.patch.object(self.cli.killswitch, "find_nft", lambda: "/usr/sbin/nft"):
            self.run_cli(home, "status")
        j = json.loads(out.getvalue())
        self.assertTrue(j["killswitch"].startswith("unknown (checking needs root"))
        self.assertNotIn("sudo srnet", j["killswitch"])
        self.assertIn("through the exit", j["dns"])
        self.assertEqual(j["leases"][0]["seller"], SELLER)


class DisconnectWhenNotConnected(_Base):
    def test_leftover_check_needing_sudo_is_a_note_not_a_failure(self):
        home = self.home()

        def needs_sudo(ks):
            raise self.cli.killswitch.KillSwitchError("needs root and sudo did not allow it")
        err = _io.StringIO()
        with mock.patch.object(self.cli.killswitch.KillSwitch, "disarm", needs_sudo), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdout", _io.StringIO()):
            self.assertEqual(self.run_cli(home, "disconnect"), 0)
        self.assertIn("could not check", err.getvalue())


class ExitResolver(_Base):
    def test_forwarder_gets_the_exit_resolver_from_the_purchase(self):
        seen = []
        self.start_proxy = lambda home, tun, upstream, *a, **k: seen.append(upstream) or 4242
        home = self.home()
        self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 0)
        self.assertEqual(seen, ["10.195.0.53"])
        self.assertEqual(self.st(home)["connected"]["exit_dns"], "10.195.0.53")

    def test_refuses_an_exit_without_resolver_before_mapping(self):
        home = self.home(dns=None)
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 1)
        self.assertIn("no DNS resolver", err.getvalue())
        self.assertFalse([c for c in FakeRpc.calls if c[0] == "map"])

    def test_resolver_learned_from_the_board(self):
        self.board = lambda *a, **k: [{"listing": {"seller": SELLER, "dns": "10.195.0.53"}}]
        home = self.home(dns=None)
        self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 0)
        self.assertEqual(self.st(home)["connected"]["exit_dns"], "10.195.0.53")

    def test_board_unreachable_and_no_record_refuses(self):
        def down(*a, **k):
            raise OSError("no route")
        self.board = down
        home = self.home(dns=None)
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 1)
        self.assertIn("board is unavailable", err.getvalue())

    def test_public_resolver_in_listing_is_ignored(self):
        self.board = lambda *a, **k: [{"listing": {"seller": SELLER, "dns": "1.1.1.1"}}]
        home = self.home(dns=None)
        with mock.patch("sys.stderr", _io.StringIO()):
            self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 1)

    def test_resolver_inside_own_tunnel_range_refuses(self):
        self.tun_nets = ["10.195.0.0/16"]
        home = self.home()
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(self.run_cli(home, "connect", SELLER, "--no-killswitch"), 1)
        self.assertIn("inside your own tunnel range", err.getvalue())


class Helpers(unittest.TestCase):
    def test_snapshot_parses_resolvectl(self):
        from srnet_exits import tunneldns
        outs = {"dns": "Link 12 (sr-tun0): 127.3.2.1 [::1]:53\n", "domain": "Link 12 (sr-tun0): ~sesh ~snode\n",
                "default-route": "Link 12 (sr-tun0): no\n"}

        def run(cmd, capture_output, text):
            class R:
                returncode = 0
                stdout = outs[cmd[1]]
                stderr = ""
            return R()
        with mock.patch.object(tunneldns, "resolvectl", lambda: "/usr/bin/resolvectl"), \
             mock.patch.object(tunneldns.subprocess, "run", run):
            snap = tunneldns.snapshot("sr-tun0")
        self.assertEqual(snap, {"tun": "sr-tun0", "dns": ["127.3.2.1", "[::1]:53"], "domains": ["~sesh", "~snode"],
                                "default_route": False})

    def test_apply_and_restore_scripts(self):
        from srnet_exits import tunneldns
        seen = []
        with mock.patch.object(tunneldns, "resolvectl", lambda: "/usr/bin/resolvectl"), \
             mock.patch.object(tunneldns, "_root_sh", lambda script, what=None: seen.append(script)):
            saved = {"tun": "sr-tun0", "dns": ["127.3.2.1"], "domains": ["~sesh", "~snode"], "default_route": False}
            tunneldns.apply(saved)
            tunneldns.restore(saved)
        self.assertIn("dns sr-tun0 127.3.2.2:5053", seen[0])
        self.assertIn("default-route sr-tun0 true", seen[0])
        self.assertIn("default-route sr-tun0 false", seen[1])

    def test_default_tun_and_missing_nft(self):
        from srnet_exits import killswitch
        self.assertEqual(killswitch.KillSwitch().tun, "sr-tun0")
        scripts = []
        ks = killswitch.KillSwitch()
        ks._nft_f = lambda script, check=True: scripts.append(script)
        ks.disarm = lambda: False
        ks.arm("eth0")
        self.assertIn('oifname "sr-tun0" accept', scripts[0])
        with mock.patch.object(killswitch, "find_nft", lambda: None):
            with self.assertRaises(killswitch.KillSwitchError) as e:
                killswitch.KillSwitch().arm("eth0")
            self.assertIn("apt install nftables", str(e.exception))

    def test_client_sesh_dns_default_is_session_router_stock_listener(self):
        import importlib
        import os
        from srnet_exits import config
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SRNET_SESH_DNS", None)
            importlib.reload(config)
            self.assertEqual(config.sesh_dns({}), "127.3.2.1:53")
            self.assertEqual(config.sesh_dns({"sesh_dns": "127.0.0.1:53"}), "127.0.0.1:53")


if __name__ == "__main__":
    unittest.main()
