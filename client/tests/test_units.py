"""Small pieces kept from the old client: listing files, the purchase warning, exit resolver rules,
the kill switch rules, and buying from an exit without its own resolver."""
import io as _io
import json
import os
import subprocess
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

    def test_wan_lets_out_only_replies_to_inbound_connections(self):
        from srnet_exits import inbound
        ks = killswitch.KillSwitch(name="srnet_ks_test")
        scripts = []

        class R:
            returncode, stdout, stderr = 0, "", ""
        ks._nft_f = lambda script, check=True: scripts.append(script) or R()
        ks._nft = lambda *a, check=True: R()
        ks.arm("eth0")
        rules = [l for l in scripts[0].splitlines() if "established" in l]
        self.assertEqual(len(rules), 1, rules)
        self.assertIn('oifname "eth0"', rules[0])
        self.assertIn(f"ct mark and {inbound.MARK:#x} == {inbound.MARK:#x}", rules[0])
        # it comes before the DNS drop, the overlay UDP allow and the policy drop does the rest
        lines = scripts[0].splitlines()
        self.assertLess(lines.index(rules[0]), next(i for i, l in enumerate(lines) if "dport 53 drop" in l))


class RouterCfg(unittest.TestCase):
    def ini(self, text):
        d = Path(tempfile.mkdtemp()) / "client.ini"
        d.write_text(text)
        return d

    def test_stock_config_has_both_problems(self):
        from srnet_exits import routercfg
        p = self.ini("[router]\n[network]\n#keyfile=\n#blackhole-routes=true\n[dns]\nbind=127.3.2.1:53\n")
        probs = routercfg.problems(p)
        self.assertEqual(len(probs), 2)
        self.assertTrue(any("persistent key" in x for x in probs))
        self.assertTrue(any("blackhole-routes" in x for x in probs))

    def test_fixed_config_is_ok(self):
        from srnet_exits import routercfg
        p = self.ini("[network]\nkeyfile=/var/lib/session-router/client/client.key\nblackhole-routes=false\n"
                     "# comment\n[dns]\n")
        self.assertEqual(routercfg.problems(p), [])

    def test_unreadable_config_is_not_a_problem_here(self):
        from srnet_exits import routercfg
        self.assertIsNone(routercfg.read("/nonexistent/client.ini"))
        self.assertIsNone(routercfg.problems("/nonexistent/client.ini"))

    def test_unreadable_as_user_is_read_through_sudo_when_allowed(self):
        from srnet_exits import priv, routercfg
        p = self.ini("[network]\n")
        calls = []

        class R:
            returncode, stdout, stderr = 0, "[network]\nkeyfile=/k\nblackhole-routes=false\n", ""

        def denied(self, *a, **k):
            raise PermissionError(13, "denied")
        with mock.patch.object(Path, "read_text", denied), \
                mock.patch.object(priv, "run", lambda cmd, **k: calls.append(cmd) or R()):
            self.assertIsNone(routercfg.problems(p))          # buy: no sudo prompt, just unknown
            self.assertEqual(routercfg.problems(p, privileged=True), [])
        self.assertEqual(calls, [["cat", str(p)]])

    def test_fix_command_is_setup_router(self):
        from srnet_exits import routercfg
        self.assertEqual(routercfg.FIX, "srnet setup-router")


STOCK_INI = """# Session Router client config
[router]
netid=srnet

[network]
# keyfile=
exit-node=
blackhole-routes=true   # default

[logging]
level=info
"""


class SetupRouter(unittest.TestCase):
    def test_stock_ini_gets_both_and_keeps_the_rest(self):
        from srnet_exits import routercfg
        new, key, changed = routercfg.edit_text(STOCK_INI)
        self.assertTrue(changed)
        self.assertEqual(key, routercfg.KEY)
        self.assertIn("[network]\nkeyfile=" + routercfg.KEY + "\n# keyfile=\nexit-node=\nblackhole-routes=false\n", new)
        for keep in ("# Session Router client config", "netid=srnet", "[logging]\nlevel=info"):
            self.assertIn(keep, new)
        self.assertEqual(new.count("blackhole-routes"), 1)
        self.assertEqual(new.count("[network]"), 1)

    def test_idempotent(self):
        from srnet_exits import routercfg
        once, _, _ = routercfg.edit_text(STOCK_INI)
        twice, _, changed = routercfg.edit_text(once)
        self.assertFalse(changed)
        self.assertEqual(once, twice)

    def test_existing_keyfile_is_kept(self):
        from srnet_exits import routercfg
        new, key, changed = routercfg.edit_text("[network]\nkeyfile=/srv/my.key\nblackhole-routes=false\n")
        self.assertEqual((key, changed), ("/srv/my.key", False))
        new, key, changed = routercfg.edit_text("[network]\nkeyfile=/srv/my.key\n")
        self.assertEqual(key, "/srv/my.key")
        self.assertTrue(changed)
        self.assertEqual(new.count("keyfile="), 1)

    def test_empty_keyfile_line_is_filled_and_no_network_section_is_added(self):
        from srnet_exits import routercfg
        new, _, _ = routercfg.edit_text("[network]\nkeyfile=\n")
        self.assertEqual(new.count("keyfile="), 1)
        self.assertIn("keyfile=" + routercfg.KEY, new)
        new, _, changed = routercfg.edit_text("[router]\nnetid=srnet")
        self.assertTrue(changed)
        self.assertTrue(new.startswith("[router]\nnetid=srnet\n"))
        self.assertEqual(routercfg.read.__name__, "read")
        import configparser
        cp = configparser.ConfigParser(); cp.read_string(new)
        self.assertEqual(cp["network"]["blackhole-routes"], "false")

    def test_result_passes_the_check(self):
        import tempfile
        from srnet_exits import routercfg
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "client.ini"
            p.write_text(routercfg.edit_text(STOCK_INI)[0])
            self.assertEqual(routercfg.problems(p), [])

    def test_apply_makes_key_edits_restarts_and_second_run_does_nothing(self):
        import os, stat, tempfile
        from srnet_exits import routercfg
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / "client.ini"; ini.write_text(STOCK_INI); os.chmod(ini, 0o640)
            key = Path(d) / "client" / "client.key"; key.parent.mkdir()
            calls = []

            def run(cmd, **kw):
                calls.append(cmd)
                if cmd[0] == "session-router-config":
                    Path(cmd[2]).write_bytes(b"k" * 64)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            log = []
            self.assertEqual(routercfg.apply(ini, str(key), run=run, log=log.append), 0)
            self.assertEqual(calls[0][:2], ["session-router-config", "-k"])
            self.assertNotEqual(calls[0][2], str(key), "generated in a private temp dir, not in place")
            self.assertEqual(calls[1], ["systemctl", "restart", "session-router-client"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(stat.S_IMODE(ini.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertEqual((Path(d) / "client.ini.srnet-orig").read_text(), STOCK_INI)
            self.assertEqual(routercfg.problems(ini), [])
            calls.clear(); log.clear()
            self.assertEqual(routercfg.apply(ini, str(key), run=run, log=log.append), 0)
            self.assertEqual(calls, [])
            self.assertIn("already set up", log[0])

    def test_apply_without_session_router_config_says_so(self):
        import tempfile
        from srnet_exits import routercfg
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / "client.ini"; ini.write_text(STOCK_INI)

            def run(cmd, **kw):
                raise FileNotFoundError(2, "No such file or directory", cmd[0])
            log = []
            (Path(d) / "k").mkdir()
            self.assertEqual(routercfg.apply(ini, str(Path(d) / "k" / "client.key"), run=run, log=log.append), 1)
            self.assertIn("session-router-client package", log[0])
            self.assertEqual(ini.read_text(), STOCK_INI, "client.ini untouched when the key can't be made")

    def _fake_run(self, calls):
        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "session-router-config":
                Path(cmd[2]).write_bytes(b"k" * 64)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return run

    def test_apply_refuses_a_symlinked_key_and_never_writes_through_it(self):
        import tempfile
        from srnet_exits import routercfg
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / "client.ini"; ini.write_text(STOCK_INI)
            kd = Path(d) / "client"; kd.mkdir()
            target = Path(d) / "profile.sh"
            (kd / "client.key").symlink_to(target)          # dangling link planted by the router user
            log, calls = [], []
            self.assertEqual(routercfg.apply(ini, str(kd / "client.key"), run=self._fake_run(calls),
                                             log=log.append), 1)
            self.assertIn("symbolic link", log[0])
            self.assertFalse(target.exists())
            self.assertEqual(ini.read_text(), STOCK_INI)

    def test_make_key_never_follows_a_link_that_appears_meanwhile(self):
        import tempfile
        from srnet_exits import routercfg
        with tempfile.TemporaryDirectory() as d:
            kd = Path(d) / "client"; kd.mkdir()
            target = Path(d) / "evil"

            def run(cmd, **kw):   # the race: the link shows up while the key is being generated
                Path(cmd[2]).write_bytes(b"k")
                (kd / "client.key").symlink_to(target)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            err = routercfg.make_key(kd / "client.key", run)
            self.assertIn("not touching it", err)
            self.assertFalse(target.exists())

    def test_marker_lets_an_unprivileged_check_pass_until_client_ini_changes(self):
        import tempfile, time as _t
        from srnet_exits import routercfg
        with tempfile.TemporaryDirectory() as d:
            ini = Path(d) / "client.ini"; ini.write_text(STOCK_INI)
            kd = Path(d) / "client"; kd.mkdir()
            self.assertEqual(routercfg.apply(ini, str(kd / "client.key"), restart=False,
                                             run=self._fake_run([]), log=lambda m: None), 0)
            marker = Path(d) / routercfg.MARKER
            self.assertEqual(oct(marker.stat().st_mode & 0o777), "0o644")
            with mock.patch.object(Path, "read_text", self._deny_ini(Path.read_text, ini)):
                r = routercfg.read(ini)
                self.assertEqual(r["source"], "marker")
                self.assertEqual(routercfg.problems(ini), [])
            # client.ini edited by hand later: the marker no longer counts
            ini.write_text(STOCK_INI + "\n# edited\n")
            os.utime(ini, ns=(_t.time_ns(), _t.time_ns() + 10**9))
            with mock.patch.object(Path, "read_text", self._deny_ini(Path.read_text, ini)):
                self.assertIsNone(routercfg.read(ini))
            # an older setup without a marker gets one from a re-run, with no restart
            ini.write_text(routercfg.edit_text(STOCK_INI, str(kd / "client.key"))[0]); marker.unlink()
            calls, log = [], []
            self.assertEqual(routercfg.apply(ini, str(kd / "client.key"), run=self._fake_run(calls),
                                             log=log.append), 0)
            self.assertEqual(calls, [])
            self.assertTrue(marker.exists())

    @staticmethod
    def _deny_ini(orig, ini):
        def read_text(self, *a, **k):
            if self == ini:
                raise PermissionError(13, "Permission denied", str(self))
            return orig(self, *a, **k)
        return read_text

    def test_check_and_edit_agree_on_odd_files(self):
        import tempfile
        from srnet_exits import routercfg
        odd = ["[network]\nkeyfile=\n[logging]\nlevel=1\n[network]\nblackhole-routes=true\n",
               "[ network ]\nBlackhole-Routes=true\n",
               "[Network]\nKEYFILE = /k ; comment\nblackhole-routes : yes\n",
               "[network]\nblackhole-routes=true\nblackhole-routes=on\n",
               "netid=x\n"]
        for text in odd:
            new, key, changed = routercfg.edit_text(text, "/var/k")
            self.assertTrue(changed, text)
            v = routercfg.values(new)
            self.assertFalse(v["blackhole_routes"], new)
            self.assertEqual(v["keyfile"], key, new)
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "client.ini"; p.write_text(new)
                self.assertEqual(routercfg.problems(p), [], new)
            self.assertEqual(routercfg.edit_text(new, "/var/k")[2], False, new)
        self.assertEqual(routercfg.edit_text(odd[2], "/var/k")[1], "/k")

    def test_apply_without_ini_says_install_session_router(self):
        from srnet_exits import routercfg
        log = []
        self.assertEqual(routercfg.apply("/nonexistent/client.ini", log=log.append), 1)
        self.assertIn("install Session Router", log[0])

    def test_root_helper_refuses_without_root(self):
        from srnet_exits import routercfg
        if os.geteuid() == 0:
            self.skipTest("running as root")
        self.assertEqual(routercfg.main(["apply", "--ini", "/nonexistent"]), 2)


class Inbound(unittest.TestCase):
    def test_nft_script_marks_inbound_and_fwmarks_only_replies(self):
        from srnet_exits import inbound
        s = inbound.nft_script("enp1s0")
        m = f"{inbound.MARK:#x}"
        self.assertIn(f'pre iifname "enp1s0" ct direction original ct mark set ct mark or {m}', s)
        self.assertIn("type route hook output", s)
        self.assertIn(f"ct mark and {m} == {m} ct direction reply meta mark set meta mark or {m}", s)
        self.assertNotIn("oifname", s)   # nothing that this machine starts gets the mark
        with self.assertRaises(ValueError):
            inbound.nft_script('eth0"; flush ruleset; "')

    def test_only_one_mark_bit_is_ever_set(self):
        from srnet_exits import inbound
        self.assertEqual(bin(inbound.MARK).count("1"), 1)
        s = inbound.nft_script("eth0", svm_was_0=True)
        for line in s.splitlines():
            if "mark set" in line:   # always "x mark set x mark or BIT", never a plain overwrite
                self.assertRegex(line, r"(ct|meta) mark set \1 mark or 0x2000000$")
        c = inbound.route_cmds("eth0", [{"dst": "default", "gateway": "10.0.0.1"}], [])
        self.assertIn("-4 rule add fwmark 0x2000000/0x2000000 lookup 5352 priority 5352", c)

    def test_inbound_packets_and_forwarded_replies_are_marked(self):
        from srnet_exits import inbound
        s = inbound.nft_script("eth0")
        self.assertIn('pre iifname "eth0" ct mark and 0x2000000 == 0x2000000 fib daddr type local meta mark set', s)
        self.assertIn('pre iifname != "eth0" ct direction reply ct mark and 0x2000000 == 0x2000000 meta mark set', s)
        self.assertIn("prerouting priority -90", s)   # after DNAT
        self.assertNotIn(inbound.SVM_CHAIN, s)
        self.assertIn(f"add chain inet srnet_inbound {inbound.SVM_CHAIN}", inbound.nft_script("eth0", True))

    def test_route_cmds_copy_the_wan_routes_with_metrics(self):
        from srnet_exits import inbound
        v4 = [{"dst": "default", "gateway": "45.77.216.1", "prefsrc": "45.77.217.74", "metric": 100},
              {"dst": "default", "gateway": "45.77.216.254", "metric": 200},
              {"dst": "45.77.216.0/23", "prefsrc": "45.77.217.74"},
              {"dst": "1.2.3.4; reboot", "gateway": "x"},
              {"dst": "default", "gateway": "172.31.1.1", "flags": ["onlink"]}]
        v6 = [{"dst": "default", "gateway": "fe80::1", "metric": 1024}]
        c = inbound.route_cmds("enp1s0", v4, v6, 5353)
        self.assertIn("-4 route append default via 45.77.216.1 dev enp1s0 src 45.77.217.74 metric 100 table 5353", c)
        self.assertIn("-4 route append default via 45.77.216.254 dev enp1s0 metric 200 table 5353", c)
        self.assertIn("-4 route append 45.77.216.0/23 dev enp1s0 src 45.77.217.74 table 5353", c)
        self.assertIn("-4 route append default via 172.31.1.1 dev enp1s0 onlink table 5353", c)
        self.assertIn("-6 route append default via fe80::1 dev enp1s0 metric 1024 table 5353", c)
        self.assertIn("-6 rule add fwmark 0x2000000/0x2000000 lookup 5353 priority 5353", c)
        self.assertFalse(any("reboot" in x for x in c))
        self.assertFalse(any(" del " in x or "flush" in x for x in c), "never removes anything")

    def test_pick_slot_skips_foreign_rules_and_tables(self):
        from srnet_exits import inbound
        free = lambda n: False
        self.assertEqual(inbound.pick_slot([], [], free), 5352)
        user = [{"priority": 5352, "fwmark": "0x1", "table": "5352"}]
        self.assertEqual(inbound.pick_slot(user, [], free), 5353)
        self.assertEqual(inbound.pick_slot([], [{"priority": 100, "table": "5352"}], free), 5353)
        self.assertEqual(inbound.pick_slot([], [], lambda n: n in (5352, 5353)), 5354)
        self.assertIsNone(inbound.pick_slot([], [], lambda n: True))

    def test_our_rules_are_only_the_masked_mark(self):
        from srnet_exits import inbound
        rules = [{"priority": 5352, "fwmark": "0x1", "table": "5352"},
                 {"priority": 5353, "fwmark": "0x2000000", "fwmask": "0x2000000", "table": "5353"},
                 {"priority": 5354, "fwmark": "0x2000000", "table": "5354"},
                 {"priority": 0, "table": "local"}]
        self.assertEqual(inbound.our_rules(rules, []), [("-4", 5353, "5353", "0x2000000/0x2000000")])
        # same bit and mask, but not one of our slots: someone else's, never removed
        foreign = [{"priority": 100, "fwmark": "0x2000000", "fwmask": "0x2000000", "table": "100"},
                   {"priority": 5352, "fwmark": "0x2000000", "fwmask": "0x2000000", "table": "5353"},
                   {"priority": 5368, "fwmark": "0x2000000", "fwmask": "0x2000000", "table": "5368"},
                   {"priority": 5352, "fwmark": "0x2000000", "fwmask": "0x2000000", "table": "vpn"}]
        self.assertEqual(inbound.our_rules(foreign, foreign), [])
        # the exact rule of earlier versions (connected before an update) is cleaned up, nothing near it
        legacy = [{"priority": 5352, "fwmark": "0x5352", "table": "5352"},
                  {"priority": 5352, "fwmark": "0x5352", "table": "100"},
                  {"priority": 5000, "fwmark": "0x5352", "table": "5352"}]
        self.assertEqual(inbound.our_rules([], legacy), [("-6", 5352, "5352", "0x5352")])

    def test_rules_are_read_with_numeric_tables(self):
        from srnet_exits import inbound
        from unittest import mock
        seen = []
        with mock.patch.object(inbound, "_ipj", lambda *a: seen.append(a) or []), \
                mock.patch.object(inbound.priv, "run", lambda *a, **k: type("R", (), {"returncode": 1})()), \
                mock.patch("srnet_exits.killswitch.find_nft", lambda: None):
            inbound.down()
        rule_calls = [a for a in seen if "rule" in a]
        self.assertTrue(rule_calls)
        self.assertTrue(all(a[0] == "-N" for a in rule_calls), rule_calls)

    def test_down_removes_only_our_rule_and_restores_src_valid_mark(self):
        from srnet_exits import inbound
        from unittest import mock
        calls = []

        class R:
            def __init__(self, rc=0):
                self.returncode, self.stdout, self.stderr = rc, "", ""
        rules = {"-4": [{"priority": 5352, "fwmark": "0x1", "table": "5352"},
                        {"priority": 5353, "fwmark": "0x2000000", "fwmask": "0x2000000", "table": "5353"}],
                 "-6": []}

        def run(cmd, input=None, **k):
            calls.append((cmd, input))
            return R(0)
        with mock.patch.object(inbound.priv, "run", run), \
                mock.patch.object(inbound, "_ipj", lambda *a: rules[a[1]] if a[:1] == ("-N",) and a[2] == "rule" else []), \
                mock.patch("srnet_exits.killswitch.find_nft", lambda: "/usr/sbin/nft"):
            inbound.down()
        flat = [" ".join(c[0]) + " | " + (c[1] or "") for c in calls]
        self.assertTrue(any("src_valid_mark=0" in f for f in flat), flat)
        batch = next(c[1] for c in calls if c[0][:2] == ["ip", "-4"])
        self.assertIn("rule del priority 5353 fwmark 0x2000000/0x2000000 lookup 5353", batch)
        self.assertIn("route flush table 5353", batch)
        self.assertNotIn("5352", batch)

    def test_ip_batch_runs_one_batch_per_family_without_the_flag_in_the_lines(self):
        from srnet_exits import inbound
        from unittest import mock
        calls = []

        class R:
            returncode, stdout, stderr = 0, "", ""
        with mock.patch.object(inbound.priv, "run", lambda cmd, input=None, **k: calls.append((cmd, input)) or R()):
            inbound._ip_batch(inbound.route_cmds("eth0", [{"dst": "default", "gateway": "10.0.0.1"}],
                                                 [{"dst": "default", "gateway": "fe80::1"}]))
        self.assertEqual([c[0][:2] for c in calls], [["ip", "-4"], ["ip", "-6"]])
        for _, text in calls:
            self.assertFalse(any(l.startswith("-") for l in text.splitlines()), text)
        self.assertIn("route append default via 10.0.0.1 dev eth0 table 5352", calls[0][1])

    def test_up_refuses_without_a_wan_default_route(self):
        from srnet_exits import inbound
        from unittest import mock
        with mock.patch.object(inbound, "_routes", lambda fam, wan: []):
            with self.assertRaises(inbound.InboundError):
                inbound.up("eth0")

    @unittest.skipUnless(os.geteuid() == 0 and killswitch.find_nft(), "needs root and nft")
    def test_scripts_parse(self):
        import subprocess
        from srnet_exits import inbound
        if subprocess.run([killswitch.find_nft(), "list", "tables"], capture_output=True).returncode:
            self.skipTest("nft cannot talk to netlink here")
        r = subprocess.run([killswitch.find_nft(), "-c", "-f", "-"], input=inbound.nft_script("eth0"),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


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


class Wording(unittest.TestCase):
    def test_restore_says_restoring_not_switching(self):
        from srnet_exits import tunneldns
        seen = []
        with mock.patch.object(tunneldns.priv, "run", lambda cmd, what="", **k: seen.append(what)):
            tunneldns.restore({"tun": "sr-tun0", "dns": ["10.0.0.1"], "domains": [], "default_route": False})
            tunneldns.apply({"tun": "sr-tun0", "dns": [], "domains": [], "default_route": False})
        self.assertIn("restoring", seen[0])
        self.assertNotIn("to the tunnel", seen[0])
        self.assertIn("to the tunnel", seen[1])

    def test_times_are_local_with_their_zone(self):
        import time as _t
        from srnet_exits import cli, reminder
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Chicago"; _t.tzset()
            t = 1792524780   # 2026-10-20 19:33 UTC
            self.assertEqual(cli._hhmm(t), "14:33 CDT")
            self.assertIn("(14:33 CDT)", reminder.message("x" * 52, t, t - 600))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            _t.tzset()


class RootHelperIsolation(unittest.TestCase):
    def test_python_I_ignores_a_module_planted_in_the_current_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "srnet_exits"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("print('HIJACKED')\n")
            (pkg / "routercfg.py").write_text("print('HIJACKED')\n")
            plain = subprocess.run([sys.executable, "-m", "srnet_exits.routercfg"], cwd=d,
                                   capture_output=True, text=True)
            self.assertIn("HIJACKED", plain.stdout, "control: without -I the cwd module runs")
            iso = subprocess.run([sys.executable, "-I", "-m", "srnet_exits.routercfg"], cwd=d,
                                 capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=d))
            self.assertNotIn("HIJACKED", iso.stdout + iso.stderr)


class NetnsLab(unittest.TestCase):
    """tests/lab/run.sh: the real nft/ip rules in network namespaces (skips without userns/nft)."""
    @unittest.skipUnless(sys.platform.startswith("linux") and os.environ.get("SRNET_SKIP_LAB") != "1", "Linux only")
    def test_lab(self):
        lab = Path(__file__).resolve().parent / "lab" / "run.sh"
        r = subprocess.run(["bash", str(lab)], capture_output=True, text=True, timeout=600)
        if r.stdout.startswith("SKIP"):
            self.skipTest(r.stdout.strip())
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-1000:])
