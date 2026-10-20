"""srnet-exits on its own: no SRNet client code, the same state as the add-on, hints that name the
command you ran, doctor, the sudo refusal and the small Exits page."""
import io
import json
import os
import pkgutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (paths)
import srnet_exits
from srnet_exits import standalone

PROG_ENV = {"SRNET_EXITS_PROG": "srnet-exits"}


class _NoCore:
    """Make the SRNet client (package `srnet`) unimportable for a while (other modules stay loaded)."""

    def find_spec(self, name, path=None, target=None):
        if name == "srnet" or name.startswith("srnet."):
            raise ImportError(f"{name} blocked: srnet-exits must run without the SRNet client")
        return None

    def __enter__(self):
        self.saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "srnet" or k.startswith("srnet.")}
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *a):
        sys.meta_path.remove(self)
        sys.modules.update(self.saved)
        return False


def run_main(argv, env=PROG_ENV):
    out, err = io.StringIO(), io.StringIO()
    with _NoCore(), mock.patch.dict(os.environ, env), \
            mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
        try:
            rc = standalone.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class NoClientCode(unittest.TestCase):
    def test_every_module_imports_without_the_srnet_client(self):
        names = [m.name for m in pkgutil.iter_modules(srnet_exits.__path__) if m.name != "__main__"]
        self.assertIn("standalone", names)
        code = ("import sys\n"
                "class B:\n"
                "    def find_spec(self, n, p=None, t=None):\n"
                "        if n == 'srnet' or n.startswith('srnet.'): raise ImportError(n)\n"
                "sys.meta_path.insert(0, B())\n"
                "import importlib\n"
                f"for n in {names!r}: importlib.import_module('srnet_exits.' + n)\n"
                "print('imported', len(sys.modules))\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PYTHONPATH=str(helpers.CLIENT)), cwd=str(helpers.CLIENT))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("imported", r.stdout)

    def test_no_source_line_imports_the_srnet_client(self):
        for p in Path(srnet_exits.__file__).parent.glob("*.py"):
            for line in p.read_text().splitlines():
                s = line.strip()
                self.assertFalse(s.startswith(("from srnet.", "import srnet.", "from srnet import")) or s == "import srnet",
                                 f"{p.name}: {s}")

    def test_vendored_files_carry_the_license_header(self):
        for n in ("state.py", "rpc.py"):
            head = (Path(srnet_exits.__file__).parent / n).read_text().splitlines()[:3]
            self.assertIn("# SPDX-License-Identifier: Apache-2.0", head)
            self.assertIn("# Copyright (c) 2026 SRNet contributors.", head)


class Commands(unittest.TestCase):
    def test_help_lists_every_command(self):
        rc, out, _ = run_main(["--help"])
        self.assertEqual(rc, 0)
        for c in ("exits", "buy", "paid", "connect", "disconnect", "status", "verify", "doctor", "setup-router", "ui"):
            self.assertIn(c, out)
        self.assertIn("usage: srnet-exits", out)

    def test_version(self):
        rc, out, _ = run_main(["--version"])
        self.assertEqual((rc, out.strip()), (0, f"srnet-exits {srnet_exits.__version__}"))

    def test_hints_name_srnet_exits(self):
        home = tempfile.mkdtemp()
        rc, _, err = run_main(["--home", home, "buy"])
        self.assertEqual(rc, 2)
        self.assertIn("srnet-exits buy --seller <id>", err)
        self.assertIn("(see: srnet-exits exits)", err)

    def test_addon_hints_still_name_srnet(self):
        home = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if k != "SRNET_EXITS_PROG"}
        with mock.patch.dict(os.environ, env, clear=True):
            rc, _, err = run_main(["--home", home, "buy"], env={})
        self.assertEqual(rc, 2)
        self.assertIn("which seller? srnet buy", err)

    def test_unknown_prog_value_falls_back_to_srnet(self):
        from srnet_exits import prog
        with mock.patch.dict(os.environ, {"SRNET_EXITS_PROG": "rm -rf"}):
            self.assertEqual(prog.P(), "srnet")

    def test_setup_router_passes_the_name_through_sudo(self):
        from srnet_exits import cli
        ini = Path(tempfile.mkdtemp()) / "client.ini"
        ini.write_text("[network]\n")
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "Session Router is already set up for exits\n", "")
        with mock.patch.object(cli.priv, "run", fake_run):
            rc, out, _ = run_main(["--home", tempfile.mkdtemp(), "setup-router", "--ini", str(ini)])
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0][1:], ["-I", "-m", "srnet_exits.routercfg", "apply", "--ini", str(ini),
                                       "--prog", "srnet-exits"])
        self.assertIn("already set up", out)

    def test_routercfg_root_side_uses_the_name(self):
        from srnet_exits import routercfg
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(routercfg.os, "geteuid", lambda: 0), \
                mock.patch.object(routercfg, "apply", lambda *a, **k: print(routercfg.fix()) or 0), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            os.environ.pop("SRNET_EXITS_PROG", None)
            self.assertEqual(routercfg.main(["apply", "--prog", "srnet-exits"]), 0)
            self.assertEqual(out.getvalue().strip(), "srnet-exits setup-router")
            os.environ.pop("SRNET_EXITS_PROG", None)
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr", io.StringIO()):
                routercfg.main(["apply", "--prog", "evil"])

    def test_refuses_sudo(self):
        with mock.patch.object(standalone.os, "geteuid", lambda: 0):
            rc, _, err = run_main(["exits"], env=dict(PROG_ENV, SUDO_USER="alice", SRNET_ALLOW_ROOT=""))
        self.assertEqual(rc, 2)
        self.assertIn("not with sudo", err)
        self.assertIn("srnet-exits exits", err)

    def test_status_uses_the_shared_state_file(self):
        home = Path(tempfile.mkdtemp())
        (home / "state.json").write_text(json.dumps({"connected": None, "pending_buy": None, "listings_path": None,
                                                    "exits": {"quotes": {}, "leases": {}}}))
        def no_router(endpoint):
            raise RuntimeError("Session Router is not running")
        with mock.patch("srnet_exits.cli._rpc", no_router):
            rc, out, _ = run_main(["--home", str(home), "status"])
        self.assertEqual(rc, 0)
        info = json.loads(out)
        self.assertIsNone(info["connected"])
        self.assertEqual(info["list_exits_error"], "Session Router is not running")


class Doctor(unittest.TestCase):
    def test_lines_and_exit_code(self):
        def addon_doctor(st, line):
            line(True, "exits add-on", "srnet-exits x")
            line(None, "buyer key", "made on your first purchase")
        checks = (("router", lambda: (True, "Session Router", "running")),
                  ("sesh", lambda: (False, ".sesh names", "no answer")),
                  ("clock", lambda: (True, "clock", "within 1 s")))
        with mock.patch.object(standalone, "CHECKS", checks), \
                mock.patch("srnet_exits.addon.ADDON.doctor", addon_doctor):
            rc, out, _ = run_main(["--home", tempfile.mkdtemp(), "doctor"])
            self.assertEqual(rc, 1)
            self.assertIn("[ok  ] Session Router: running", out)
            self.assertIn("[FAIL] .sesh names: no answer", out)
            self.assertIn("[warn] buyer key", out)
            self.assertIn("1 problem(s) above", out)
            rc, out, _ = run_main(["--home", tempfile.mkdtemp(), "doctor", "--skip", "sesh"])
            self.assertEqual(rc, 0)
            self.assertIn("all good", out)

    def test_router_missing_says_how_to_get_it(self):
        with mock.patch.object(standalone, "_router_installed", lambda: False):
            ok, what, detail = standalone.c_router()
        self.assertFalse(ok)
        self.assertIn("SRNet-Desktop README, Install, steps 1 and 2", detail)
        self.assertIn("https://github.com/SRNet-lab/SRNet-Desktop#1-session-router", detail)

    def test_router_stopped(self):
        with mock.patch.object(standalone, "_router_installed", lambda: True), \
                mock.patch.object(standalone, "_router_running", lambda: False):
            ok, _, detail = standalone.c_router()
        self.assertFalse(ok)
        self.assertIn("sudo systemctl start session-router-client", detail)

    def test_clock(self):
        import email.utils
        import time

        class R:
            def __init__(self, t):
                self.headers = {"Date": email.utils.formatdate(t, usegmt=True)}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        for skew, want in ((0, True), (90, None), (600, False)):
            with mock.patch.object(standalone.urllib.request, "urlopen", lambda *a, **k: R(time.time() - skew)):
                self.assertIs(standalone.c_clock()[0], want, skew)


class SharedWithTheAddon(unittest.TestCase):
    def test_state_matches_the_srnet_client(self):
        """Same file, same defaults, same layout: the add-on and srnet-exits read each other's state."""
        from srnet_exits.state import State
        home = Path(tempfile.mkdtemp())
        st = State(home)
        self.assertEqual(st.data, {"pending_buy": None, "connected": None, "listings_path": None})
        self.assertTrue((home / "spool").is_dir())
        st.data["connected"] = {"seller": helpers.SELLER}
        st.save()
        if helpers.HAVE_CORE:
            from srnet.state import State as CoreState
            self.assertEqual(CoreState(home).data, st.data)
            core = CoreState(Path(tempfile.mkdtemp()))
            self.assertEqual(State(core.root).data, core.data)

    def test_buyer_key_path_is_shared(self):
        from srnet_exits import pay
        self.assertEqual(pay.key_path(Path("/h")), Path("/h/exits/buyer.key"))

    @unittest.skipUnless(helpers.HAVE_CORE, "SRNet client not found (sibling SRNet-Desktop or $SRNET_CLIENT_DIR)")
    def test_addon_mode_still_works_through_srnet(self):
        """`srnet` from the SRNet client loads the add-on and keeps naming `srnet` in its hints."""
        env = {k: v for k, v in os.environ.items() if k != "SRNET_EXITS_PROG"}
        env.update(PYTHONPATH=os.pathsep.join([str(helpers.CLIENT), str(helpers.CORE_DIR)]),
                   SRNET_ADDON_MODULES="srnet_exits.addon:ADDON", SRNET_HOME=tempfile.mkdtemp())
        r = subprocess.run([sys.executable, "-m", "srnet", "--help"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for c in ("exits", "buy", "connect", "verify", "setup-router"):
            self.assertIn(c, r.stdout)
        self.assertNotIn("not installed", r.stdout)
        r = subprocess.run([sys.executable, "-m", "srnet", "buy"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("which seller? srnet buy", r.stderr)


class Page(unittest.TestCase):
    def setUp(self):
        import threading
        from srnet_exits import page
        from srnet_exits.state import State
        self.st = State(Path(tempfile.mkdtemp()))
        self.srv = page.serve(self.st, 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]
        self.tok = {"X-Token": self.srv.token}

    def req(self, method, path, body=None, headers=None, raw=None, token=True):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            h.update(self.tok)
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else raw
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.status, r.read(), dict(r.getheaders())
        c.close()
        return out

    def test_page_and_scripts(self):
        code, body, hdr = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"/tab.js", body)
        self.assertIn(b"Session Technology Foundation", body)
        self.assertIn("script-src 'self'", hdr["Content-Security-Policy"])
        for p in ("/page.js", "/tab.js", "/tab.css", "/page.css"):
            self.assertEqual(self.req("GET", p)[0], 200, p)
        self.assertEqual(self.req("GET", "/page.py")[0], 404)
        self.assertEqual(self.req("GET", "/../ui.py")[0], 404)

    def test_api_needs_x_core_loopback_host_and_no_foreign_origin(self):
        x = {"X-Core": "1"}
        self.assertEqual(self.req("POST", "/api/addon/exits/status", {})[0], 403)
        self.assertEqual(self.req("POST", "/api/addon/exits/status", {}, dict(x, Host="evil.example"))[0], 403)
        self.assertEqual(self.req("POST", "/api/addon/exits/status", {},
                                  dict(x, Origin="http://evil.example"))[0], 403)
        self.assertEqual(self.req("GET", "/", None, {"Host": "rebind.example:%d" % self.port})[0], 403)
        self.assertEqual(self.req("POST", "/api/addon/exits/status", {}, dict(x, **{"Content-Type": "text/plain"}))[0], 415)
        code, body, _ = self.req("POST", "/api/addon/exits/status", {},
                                 dict(x, Origin=f"http://127.0.0.1:{self.port}"))
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertTrue(j["ok"])
        self.assertFalse(j["acked"])
        self.assertEqual(self.req("POST", "/api/addon/exits/nope", {}, x)[0], 404)

    def test_every_api_call_needs_this_runs_token(self):
        x = {"X-Core": "1"}
        before = json.loads(self.req("POST", "/api/addon/exits/settings", {}, x)[1])
        for t in (None, "", "x" * 43, "é", self.srv.token + "x"):
            h = dict(x) if t is None else dict(x, **{"X-Token": t})
            code = self.req("POST", "/api/addon/exits/settings", {"max_payment_usdc": "999.00"}, h, token=False)[0]
            self.assertEqual(code, 403, t)
            self.assertEqual(self.req("POST", "/api/addon/exits/hello", {}, h, token=False)[0], 403, t)
        after = json.loads(self.req("POST", "/api/addon/exits/settings", {}, x)[1])
        self.assertEqual(before, after)
        self.assertEqual(self.req("POST", "/api/addon/exits/hello", {}, x)[0], 200)

    def test_qr_needs_the_token(self):
        from srnet_exits import ui
        with mock.patch.object(ui.Backend, "qr", lambda self, q: b"<svg/>"):
            path = "/api/addon/exits/qr.svg?q=0011223344556677"
            self.assertNotEqual(self.req("GET", path, None, {"X-Core": "1"}, token=False)[0], 200)
            self.assertNotEqual(self.req("GET", path, None, {"X-Core": "1", "X-Token": "nope"}, token=False)[0], 200)
            code, body, hdr = self.req("GET", path, None, {"X-Core": "1"})
            self.assertEqual((code, body), (200, b"<svg/>"))
            self.assertEqual(hdr["Content-Type"], "image/svg+xml")

    def test_bad_content_length_is_400(self):
        for v in ("abc", "1e3", "0x10"):
            code, body, _ = self.req("POST", "/api/addon/exits/status", None,
                                     {"X-Core": "1", "Content-Type": "application/json", "Content-Length": v}, raw=b"{}")
            self.assertEqual(code, 400, v)
        self.assertEqual(self.req("POST", "/api/addon/exits/status", None,
                                  {"X-Core": "1", "Content-Type": "application/json", "Content-Length": "-1"},
                                  raw=b"{}")[0], 413)

    def test_page_js_takes_the_token_from_the_hash(self):
        js = self.req("GET", "/page.js")[1].decode()
        self.assertIn("location.hash", js)
        self.assertIn("sessionStorage", js)
        self.assertIn("history.replaceState", js)
        self.assertIn('"X-Token"', js)
        tab = self.req("GET", "/tab.js")[1].decode()
        self.assertIn("sh.headers ? sh.headers()", tab)


    def test_quote_needs_the_warning_first(self):
        code, body, _ = self.req("POST", "/api/addon/exits/quote",
                                 {"seller": helpers.SELLER, "hours": 1, "client_sesh": helpers.ME}, {"X-Core": "1"})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body).get("need_ack"))

    def test_connect_from_the_page_names_srnet_exits(self):
        from srnet_exits import ui
        seen = {}

        def fake(cmd, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(cmd, 1, "", "no paid time")
        (self.st.root / "visibility.ack").write_text("I UNDERSTAND\n")
        with mock.patch.dict(os.environ, PROG_ENV), mock.patch.object(ui.subprocess, "run", fake):
            code, body, _ = self.req("POST", "/api/addon/exits/connect", {"seller": helpers.SELLER}, {"X-Core": "1"})
        self.assertEqual(code, 200)
        self.assertEqual(seen["env"]["SRNET_EXITS_PROG"], "srnet-exits")
        self.assertFalse(json.loads(body)["ok"])


class PageRun(unittest.TestCase):
    """ui.json, the verified "already running" and the random port."""

    def setUp(self):
        from srnet_exits.state import State
        self.st = State(Path(tempfile.mkdtemp()))

    def start(self):
        import threading
        from srnet_exits import page
        made = []
        real = page.serve

        def keep(*a, **k):
            made.append(real(*a, **k))
            return made[-1]
        out = io.StringIO()
        with mock.patch.object(page, "serve", keep), mock.patch("sys.stdout", out):
            t = threading.Thread(target=page.run, args=(self.st, 0, False), daemon=True)
            t.start()
            for _ in range(200):
                if made and (self.st.root / "ui.json").exists():
                    break
                import time
                time.sleep(0.02)
        self.addCleanup(lambda: (made[0].shutdown(), t.join(5)))
        return made[0], t, out

    def test_writes_ui_json_private_and_removes_it_on_stop(self):
        from srnet_exits import page
        srv, t, out = self.start()
        p = self.st.root / "ui.json"
        info = json.loads(p.read_text())
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(info, {"port": srv.server_address[1], "token": srv.token, "pid": os.getpid()})
        self.assertNotEqual(info["port"], 48921)
        self.assertGreaterEqual(len(info["token"]), 43)
        self.assertIn(f"http://127.0.0.1:{info['port']}/#t={info['token']}", out.getvalue())
        self.assertEqual(page.running(self.st), (info["port"], info["token"]))
        srv.shutdown()
        t.join(5)
        self.assertFalse(p.exists())

    def test_second_run_reuses_the_verified_page(self):
        from srnet_exits import page
        srv, _, _ = self.start()
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.object(page, "serve", side_effect=AssertionError("new page")):
            self.assertEqual(page.run(self.st, 0, False), 0)
        self.assertIn(f"/#t={srv.token} (already running)", out.getvalue())

    def test_a_squatter_on_the_recorded_port_is_not_trusted(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from srnet_exits import page

        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                b = json.dumps({"ok": True, "app": "srnet-exits", "proof": "0" * 64}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            do_GET = do_POST
        fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        self.addCleanup(fake.server_close)
        self.addCleanup(fake.shutdown)
        fport = fake.server_address[1]
        (self.st.root / "ui.json").write_text(json.dumps({"port": fport, "token": "t" * 43, "pid": 1}))
        self.assertIsNone(page.running(self.st))
        srv, _, out = self.start()
        self.assertNotEqual(srv.server_address[1], fport)
        self.assertNotIn("already running", out.getvalue())

    def test_ui_json_of_another_user_or_a_symlink_is_ignored(self):
        from srnet_exits import page
        srv, _, _ = self.start()
        with mock.patch.object(page.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(page.running(self.st))
        p = self.st.root / "ui.json"
        real = self.st.root / "elsewhere.json"
        p.replace(real)
        p.symlink_to(real)
        self.assertIsNone(page.running(self.st))

    def test_term_removes_ui_json(self):
        import signal
        import time
        env = dict(os.environ, SRNET_HOME=str(self.st.root), PYTHONPATH=os.pathsep.join(sys.path))
        p = subprocess.Popen([sys.executable, "-m", "srnet_exits", "ui", "--no-browser"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        info = self.st.root / "ui.json"
        for _ in range(250):
            if info.exists():
                break
            time.sleep(0.02)
        self.assertTrue(info.exists())
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(10), 143)
        p.stdout.close()
        p.stderr.close()
        self.assertFalse(info.exists())

    def test_nothing_recorded_means_a_new_page(self):
        from srnet_exits import page
        self.assertIsNone(page.running(self.st))
        (self.st.root / "ui.json").write_text("not json")
        self.assertIsNone(page.running(self.st))


if __name__ == "__main__":
    unittest.main()
