"""client/install.sh with a stand-in Python: install, reinstall, uninstall and the PATH line.
Nothing is downloaded; the stand-in only logs what the installer asked for."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
INSTALL = CLIENT / "install.sh"

FAKE_PY = r'''#!/usr/bin/env bash
# stand-in python: logs calls; "-m venv D" makes D/bin/python a copy of this script
echo "$*" >> "$FAKE_LOG"
# the uninstall's DNS step: log it, answer with FAKE_DNS_RC
case "$*" in *"cli.dns_down"*) echo "dns_down ${@: -1}" >> "$FAKE_LOG"; exit "${FAKE_DNS_RC:-0}";; esac
# the uninstall's state checks run real Python code
if [ "$1" = "-I" ] && [ "$2" = "-c" ]; then shift; exec "$REAL_PY" "$@"; fi
case "$*" in *"-m srnet_exits setup-router"*) exit "${FAKE_ROUTER_RC:-0}";; esac
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; exit 0; fi
exit 0
'''
MARK = "# srnet-exits: ~/.local/bin on PATH (added by SRNet-VPSHost-ExitPack client/install.sh)"
LAUNCHER = "# srnet-exits launcher (written by SRNet-VPSHost-ExitPack client/install.sh)"


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "client/install.sh is for Linux")
class InstallSh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = self.tmp / "fakebin"
        self.fake.mkdir()
        self.py = self.fake / "python3"
        self.py.write_text(FAKE_PY)
        self.py.chmod(0o755)
        sc = self.fake / "systemctl"   # no Session Router service here
        sc.write_text("#!/bin/sh\nexit 1\n")
        sc.chmod(0o755)
        self.log = self.tmp / "log"
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.prefix = self.home / ".local/share/srnet-exits"
        self.bin = self.home / ".local/bin"
        self.ini = self.tmp / "client.ini"

    def run_install(self, *args, bin_dir=None, **extra):
        self.log.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SRNET_", "FAKE_"))}
        env.update(PYTHON=str(self.py), FAKE_LOG=str(self.log), HOME=str(self.home), REAL_PY=sys.executable,
                   PATH=f"{self.fake}:/usr/bin:/bin", SRNET_ROUTER_INI=str(self.ini), **extra)
        if bin_dir is not None:
            env["SRNET_BIN"] = str(bin_dir)
        return subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True, text=True, cwd=str(self.tmp))

    def dns_calls(self):
        return [l for l in self.calls() if l.startswith("dns_down ")]

    def calls(self, word=""):
        return [l for l in self.log.read_text().splitlines() if word in l]

    # -------------------------------------------------------------- install
    def test_unknown_option_is_refused(self):
        r = self.run_install("--with-exits")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown option --with-exits", r.stderr)
        self.assertFalse(self.prefix.exists())

    def test_install_makes_venv_launcher_and_copy(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls("-m venv"), [f"-m venv {self.prefix}/venv"])
        pips = self.calls("-m pip install")
        self.assertEqual(pips[-1], f"-m pip install --quiet --upgrade {self.prefix}/src")
        self.assertFalse(any(l.split()[-1] in ("srnet-exits", "srnet-client") for l in pips), pips)
        src = self.prefix / "src"
        for p in ("pyproject.toml", "LICENSE", "client/srnet_exits/standalone.py", "client/README.md",
                  "seller/chains.py", "board/boardfmt.py", "contrib/exit-lease/lease_flow.py"):
            self.assertTrue((src / p).exists(), p)
        self.assertEqual(list(src.rglob("__pycache__")), [])
        self.assertFalse((src / "contrib/legacy-cashout").exists())
        launcher = (self.bin / "srnet-exits").read_text().splitlines()
        self.assertEqual(launcher[1], LAUNCHER)
        self.assertEqual(launcher[2], f'exec {self.prefix}/venv/bin/python -I -m srnet_exits "$@"')
        self.assertTrue(os.access(self.bin / "srnet-exits", os.X_OK))
        self.assertFalse((self.bin / "srnet").exists())
        self.assertIn("srnet-exits doctor", r.stdout)
        self.assertIn("Session Router is a separate program", r.stdout)
        self.assertIn("https://github.com/SRNet-lab/SRNet-Desktop#1-session-router", r.stdout)

    def test_launcher_runs_the_venv_python(self):
        self.run_install()
        r = subprocess.run([str(self.bin / "srnet-exits"), "status"], capture_output=True, text=True,
                           env=dict(os.environ, FAKE_LOG=str(self.log)))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls()[-1], "-I -m srnet_exits status")

    def test_reinstall_keeps_the_venv_and_one_path_line(self):
        (self.home / ".bashrc").write_text("alias ll='ls -l'\n")
        self.assertEqual(self.run_install().returncode, 0)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls("-m venv"), [])
        self.assertEqual((self.home / ".bashrc").read_text().count(MARK), 1)
        self.assertNotIn("Added", r.stdout)

    def test_path_line_for_new_terminals(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        rc = (self.home / ".bashrc").read_text()
        self.assertIn(MARK, rc)
        self.assertFalse((self.home / ".zshrc").exists())
        self.assertIn("Added", r.stdout)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', r.stdout)
        out = subprocess.run(["bash", "-c", f'PATH=/usr/bin:/bin; . "{self.home}/.bashrc"; . "{self.home}/.bashrc"; echo "$PATH"'],
                             capture_output=True, text=True, env={"HOME": str(self.home)}).stdout.strip()
        self.assertEqual(out, f"{self.bin}:/usr/bin:/bin")

    def test_zshrc_only_if_there(self):
        (self.home / ".zshrc").write_text("# mine\n")
        self.run_install()
        self.assertIn(MARK, (self.home / ".zshrc").read_text())

    def test_odd_bin_dir_is_quoted(self):
        b = self.tmp / "my bin $(touch PWNED)"
        r = self.run_install(bin_dir=b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((b / "srnet-exits").exists())
        out = subprocess.run(["bash", "-c", f'PATH=/usr/bin; . "{self.home}/.bashrc"; echo "$PATH"'],
                             capture_output=True, text=True, env={"HOME": str(self.home)}, cwd=str(self.tmp)).stdout.strip()
        self.assertEqual(out, f"{b}:/usr/bin")
        self.assertFalse((self.tmp / "PWNED").exists())

    def test_does_not_replace_someone_elses_command(self):
        self.bin.mkdir(parents=True)
        (self.bin / "srnet-exits").write_text("#!/bin/sh\necho mine\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("wasn't written by this installer", r.stderr)
        self.assertEqual((self.bin / "srnet-exits").read_text(), "#!/bin/sh\necho mine\n")
        self.assertEqual(self.run_install("--force").returncode, 0)
        self.assertIn(LAUNCHER, (self.bin / "srnet-exits").read_text())

    def test_leaves_the_srnet_client_alone(self):
        self.bin.mkdir(parents=True)
        (self.bin / "srnet").write_text("#!/bin/sh\n# SRNet client\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.bin / "srnet").read_text(), "#!/bin/sh\n# SRNet client\n")
        r = self.run_install("--uninstall")
        self.assertTrue((self.bin / "srnet").exists())

    def test_refuses_root(self):
        fid = self.fake / "id"
        fid.write_text("#!/bin/sh\necho 0\n")
        fid.chmod(0o755)
        r = self.run_install()
        self.assertEqual(r.returncode, 2)
        self.assertIn("not with sudo", r.stderr)
        self.assertFalse(self.prefix.exists())

    # -------------------------------------------------------------- router setup
    def test_router_setup_runs_once_with_the_ini(self):
        self.ini.write_text("[network]\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls("setup-router"), [f"-I -m srnet_exits setup-router --ini {self.ini}"])
        self.assertIn("sudo asks for your password once", r.stdout)
        self.assertIn("set up for exits", r.stdout)

    def test_router_setup_failure_is_reported_not_fatal(self):
        self.ini.write_text("[network]\n")
        r = self.run_install(FAKE_ROUTER_RC="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not set up for exits yet; run: srnet-exits setup-router", r.stdout)

    def test_no_router_setup_and_no_ini(self):
        self.ini.write_text("[network]\n")
        r = self.run_install("--no-router-setup")
        self.assertEqual(self.calls("setup-router"), [])
        self.assertIn("setup skipped; run once: srnet-exits setup-router", r.stdout)
        self.ini.unlink()
        r = self.run_install()
        self.assertEqual(self.calls("setup-router"), [])
        self.assertIn("After installing Session Router, run once: srnet-exits setup-router", r.stdout)

    # -------------------------------------------------------------- uninstall
    def test_uninstall_removes_only_what_it_added(self):
        desktop = ("# srnet: ~/.local/bin on PATH (added by SRNet-Desktop install.sh)\n"
                   'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH";; esac\n')
        before = "alias ll='ls -l'\n\n" + desktop
        (self.home / ".bashrc").write_text(before)
        state = self.home / ".srnet"
        state.mkdir()
        (state / "state.json").write_text('{\n  "connected": null\n}')
        self.assertEqual(self.run_install().returncode, 0)
        self.assertNotEqual((self.home / ".bashrc").read_text(), before)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.home / ".bashrc").read_text(), before)
        self.assertFalse(self.prefix.exists())
        self.assertFalse((self.bin / "srnet-exits").exists())
        self.assertTrue((state / "state.json").exists())
        self.assertIn("left in place", r.stdout)

    def test_uninstall_keeps_a_changed_path_line(self):
        self.run_install()
        rc = self.home / ".bashrc"
        rc.write_text(rc.read_text().replace("export PATH=", "export  PATH="))
        self.run_install("--uninstall")
        text = rc.read_text()
        self.assertNotIn(MARK, text)
        self.assertIn("export  PATH=", text)

    def test_uninstall_refuses_while_connected(self):
        self.run_install()
        state = self.home / ".srnet"
        state.mkdir()
        (state / "state.json").write_text('{\n  "connected": {\n    "seller": "x.sesh"\n  }\n}')
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 1)
        self.assertIn("srnet-exits disconnect", r.stderr)
        self.assertTrue(self.prefix.exists())
        self.assertEqual(self.run_install("--uninstall", "--force").returncode, 0)
        self.assertFalse(self.prefix.exists())

    def test_uninstall_reads_compact_json(self):
        self.run_install()
        state = self.home / ".srnet"
        state.mkdir()
        (state / "state.json").write_text('{"connected":{"seller":"x.sesh"},"tunnel_dns":null}')
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("still connected", r.stderr)
        self.assertTrue(self.prefix.exists())
        (state / "state.json").write_text('{"connected":null}')
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertFalse(self.prefix.exists())

    def test_uninstall_without_the_venv_falls_back_to_grep(self):
        self.run_install()
        shutil.rmtree(self.prefix / "venv")
        state = self.home / ".srnet"
        state.mkdir()
        (state / "state.json").write_text('{\n  "connected": {\n    "seller": "x.sesh"\n  }\n}')
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 1)
        self.assertIn("still connected", r.stderr)

    def test_force_while_connected_names_the_kill_switch_command(self):
        self.run_install()
        state = self.home / ".srnet"
        state.mkdir()
        (state / "state.json").write_text('{"connected":{"seller":"x.sesh","killswitch":true}}')
        r = self.run_install("--uninstall", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sudo nft delete table inet srnet_ks", r.stdout)
        self.assertFalse(self.prefix.exists())
        r = self.run_install("--uninstall")
        self.assertNotIn("srnet_ks", r.stdout)

    def test_install_writes_the_marker(self):
        self.run_install()
        self.assertTrue((self.prefix / ".srnet-exits-install").is_file())

    def test_uninstall_never_removes_a_dir_that_is_not_an_install(self):
        wrong = self.home   # SRNET_EXITS_PREFIX=$HOME by mistake
        (wrong / "notes.txt").write_text("mine\n")
        r = self.run_install("--uninstall", SRNET_EXITS_PREFIX=str(wrong))
        self.assertEqual(r.returncode, 1)
        self.assertIn("doesn't look like", r.stderr)
        self.assertTrue((wrong / "notes.txt").exists())
        # the marker alone is not enough: the venv python must be there too
        other = self.tmp / "other"
        other.mkdir()
        (other / ".srnet-exits-install").write_text("x\n")
        (other / "data").write_text("keep\n")
        self.assertEqual(self.run_install("--uninstall", SRNET_EXITS_PREFIX=str(other)).returncode, 1)
        self.assertTrue((other / "data").exists())
        # a directory without the marker is refused by both install and uninstall
        self.run_install()
        (self.prefix / ".srnet-exits-install").unlink()
        self.assertEqual(self.run_install("--uninstall").returncode, 1)
        self.assertEqual(self.run_install().returncode, 1)
        self.assertTrue((self.prefix / "venv/bin/python").exists())

    def test_install_refuses_a_prefix_that_is_not_an_install(self):
        src = self.home / "src"
        src.mkdir()
        (src / "keep.c").write_text("mine\n")
        r = self.run_install(SRNET_EXITS_PREFIX=str(self.home))
        self.assertEqual(r.returncode, 1)
        self.assertIn("Not installing into", r.stderr)
        self.assertIn("doesn't look like", r.stderr)
        self.assertTrue((src / "keep.c").exists())
        self.assertFalse((self.home / ".srnet-exits-install").exists())
        self.assertFalse((self.home / "venv").exists())
        self.assertEqual(self.calls("-m venv"), [])
        # an empty directory is fine, and so is our own install again
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(self.run_install(SRNET_EXITS_PREFIX=str(empty)).returncode, 0)
        self.assertTrue((empty / ".srnet-exits-install").exists())
        self.assertEqual(self.run_install(SRNET_EXITS_PREFIX=str(empty)).returncode, 0)

    def _tunnel_state(self, connected=True):
        state = self.home / ".srnet"
        state.mkdir(exist_ok=True)
        d = {"connected": {"seller": "x.sesh"} if connected else None,
             "tunnel_dns": {"saved": {"tun": "sr-tun0", "dns": ["127.3.2.1"], "domains": ["~sesh"],
                                      "default_route": False}, "forwarder_pid": 4242}}
        (state / "state.json").write_text(json.dumps(d))
        return state

    def test_force_while_connected_puts_dns_back_first(self):
        self.run_install()
        state = self._tunnel_state()
        r = self.run_install("--uninstall", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.dns_calls(), [f"dns_down {state}"])
        self.assertIn("Putting DNS on sr-tun0 back", r.stdout)
        self.assertNotIn("resolvectl revert", r.stdout)
        self.assertIn("sudo nft delete table inet srnet_ks", r.stdout)
        self.assertFalse(self.prefix.exists())

    def test_dns_that_could_not_be_put_back_gets_the_resolvectl_line(self):
        self.run_install()
        self._tunnel_state()
        r = self.run_install("--uninstall", "--force", FAKE_DNS_RC="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Restore DNS with: sudo resolvectl revert sr-tun0", r.stdout)
        self.assertIn("sudo nft delete table inet srnet_ks", r.stdout)
        # the environment already gone: the line is printed from the state file alone
        r = self.run_install("--uninstall", "--force")
        self.assertEqual(self.dns_calls(), [])
        self.assertIn("Restore DNS with: sudo resolvectl revert sr-tun0", r.stdout)

    def test_leftover_tunnel_dns_is_put_back_even_when_not_connected(self):
        self.run_install()
        self._tunnel_state(connected=False)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.dns_calls()), 1)
        self.assertNotIn("srnet_ks", r.stdout)

    def test_no_dns_step_without_tunnel_dns(self):
        self.run_install()
        state = self.home / ".srnet"
        state.mkdir()
        (state / "state.json").write_text('{"connected": null, "tunnel_dns": null}')
        r = self.run_install("--uninstall")
        self.assertEqual(self.dns_calls(), [])
        self.assertNotIn("DNS", r.stdout)

    def test_uninstall_stops_the_page_reminder_and_dns_proxy_of_this_install(self):
        self.run_install()
        vpy = str(self.prefix / "venv/bin/python")
        nap = "import time; time.sleep(60)"

        def start(argv0, *rest):
            p = subprocess.Popen([argv0, "-c", nap, *rest], executable=sys.executable)
            self.addCleanup(lambda: (p.kill(), p.wait()))
            return p
        ours = [start(vpy, "-I", "-m", "srnet_exits", "ui"), start(vpy, "-m", "srnet_exits.reminder"),
                start(vpy, "-m", "srnet_exits.dnsproxy")]
        other_venv = start("/opt/elsewhere/venv/bin/python", "-m", "srnet_exits.reminder")
        not_exits = start(vpy, "-m", "something_else")
        import time
        time.sleep(0.3)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        for p in ours:
            self.assertEqual(p.wait(10), -15)
        self.assertEqual(r.stdout.count("stopped "), 3)
        self.assertIsNone(other_venv.poll())
        self.assertIsNone(not_exits.poll())

    def test_uninstall_leaves_a_foreign_command(self):
        self.bin.mkdir(parents=True)
        (self.bin / "srnet-exits").write_text("#!/bin/sh\necho mine\n")
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertTrue((self.bin / "srnet-exits").exists())

    def test_uninstall_twice_is_fine(self):
        self.run_install()
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertEqual(self.run_install("--uninstall").returncode, 0)


if __name__ == "__main__":
    unittest.main()
