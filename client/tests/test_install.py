"""client/install.sh with a stand-in Python: install, reinstall, uninstall and the PATH line.
Nothing is downloaded; the stand-in only logs what the installer asked for."""
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
        env.update(PYTHON=str(self.py), FAKE_LOG=str(self.log), HOME=str(self.home),
                   PATH=f"{self.fake}:/usr/bin:/bin", SRNET_ROUTER_INI=str(self.ini), **extra)
        if bin_dir is not None:
            env["SRNET_BIN"] = str(bin_dir)
        return subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True, text=True, cwd=str(self.tmp))

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
