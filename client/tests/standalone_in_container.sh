#!/usr/bin/env bash
# Runs inside a fresh Ubuntu container (seller/tests/test_container.sh, phase A): install the
# buyer client on its own as a normal user, with no SRNet client anywhere, and use it.
# Offline: pip takes its packages from /wheels. Session Router is faked by three small
# commands (session-router-config, systemctl, and a client.ini), so setup-router runs for real.
set -euo pipefail
id tester >/dev/null 2>&1 || useradd -m -s /bin/bash tester
echo "tester ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/tester
mkdir -p /etc/session-router /var/lib/session-router/client
printf '[network]\n' > /etc/session-router/client.ini
chmod 640 /etc/session-router/client.ini
cat > /usr/local/bin/session-router-config <<'S'
#!/bin/sh
[ "$1" = -k ] && head -c 64 /dev/urandom > "$2"
S
cat > /usr/local/bin/systemctl <<'S'
#!/bin/sh
case "$1" in
  list-unit-files) echo "session-router-client.service enabled";;
  is-active) exit 3;;
  restart) echo "restart $2" >> /tmp/systemctl.log;;
esac
exit 0
S
chmod 755 /usr/local/bin/session-router-config /usr/local/bin/systemctl
rm -rf /home/tester/SRNet-VPSHost-ExitPack
cp -a /src /home/tester/SRNet-VPSHost-ExitPack
find /home/tester/SRNet-VPSHost-ExitPack -name __pycache__ -prune -exec rm -rf {} +
chown -R tester:tester /home/tester/SRNet-VPSHost-ExitPack

runuser -u tester -- env -i HOME=/home/tester USER=tester PATH=/usr/local/bin:/usr/bin:/bin \
  PIP_NO_INDEX=1 PIP_FIND_LINKS=/wheels PIP_DISABLE_PIP_VERSION_CHECK=1 bash -euo pipefail -c '
cd ~
fail() { echo "FAIL: $*"; exit 1; }
! command -v srnet >/dev/null || fail "an srnet command is already here"
echo "--- install"
SRNet-VPSHost-ExitPack/client/install.sh > /tmp/i1.log 2>&1 || { cat /tmp/i1.log; fail install; }
cat /tmp/i1.log
grep -q "Session Router: set up for exits" /tmp/i1.log || fail "router setup not reported"
grep -q "Added /home/tester/.local/bin to PATH for new terminals in: ~/.bashrc" /tmp/i1.log || fail "PATH line"
sudo grep -qx "blackhole-routes=false" /etc/session-router/client.ini || fail "blackhole-routes not set"
sudo grep -q "^keyfile=" /etc/session-router/client.ini || fail "keyfile not set"
[ -f /etc/session-router/srnet-exits.ok ] || fail "marker missing"
VPY=~/.local/share/srnet-exits/venv/bin/python
! "$VPY" -c "import srnet" 2>/dev/null || fail "the SRNet client is importable"
[ ! -e ~/.local/bin/srnet ] || fail "installer wrote an srnet command"
echo "--- new terminal finds it"
bash -ic "command -v srnet-exits" 2>/dev/null | grep -qx /home/tester/.local/bin/srnet-exits || fail "not on PATH in a new shell"
export PATH="$HOME/.local/bin:$PATH"
srnet-exits --version
srnet-exits --help | grep -q "setup-router" || fail help
echo "--- doctor"
srnet-exits doctor --skip clock > /tmp/doc.log 2>&1 || true
cat /tmp/doc.log
grep -q "Session Router settings for exits (persistent key, no blackhole routes): ok (as recorded by srnet-exits setup-router)" /tmp/doc.log || fail "doctor router settings"
grep -q "installed but not running" /tmp/doc.log || fail "doctor router state"
grep -q "\[ok  \] exits client: srnet-exits" /tmp/doc.log || fail "doctor version line"
echo "--- setup-router again"
srnet-exits setup-router | tee /tmp/sr2.log
grep -q "already set up" /tmp/sr2.log || fail "setup-router not idempotent"
echo "--- commands"
set +o pipefail   # grep -q may close the pipe early
printf "[]\n" > /tmp/empty.json
srnet-exits exits --listings /tmp/empty.json
out() { "$@" 2>&1 || true; }
out srnet-exits buy | grep -q "which seller? srnet-exits buy" || fail "buy hint"
{ printf "I UNDERSTAND\n" | srnet-exits connect y3yi9753qbe4gxr31t1urywd3yd9pk4a157g7jeg9xsz6iecu73y.sesh 2>&1 || true; } \
  | grep -q "no paid time for this seller; buy some first: srnet-exits buy" || fail "connect hint"
out srnet-exits verify --chain nochain --tx 0x00 --to 0x00 --amount 1 | grep -q "error" || fail verify
out sudo ~/.local/bin/srnet-exits exits | grep -q "not with sudo" || fail "sudo refusal"
out srnet-exits status | grep -q "list_exits_error" || fail status
set -o pipefail
echo "--- page"
srnet-exits ui --no-browser --port 48921 > /tmp/ui.log 2>&1 &
UIPID=$!
for i in $(seq 1 50); do grep -q "127.0.0.1:48921" /tmp/ui.log && break; sleep 0.2; done
"$VPY" - <<PY
import json, urllib.request
b = urllib.request.urlopen("http://127.0.0.1:48921/").read()
assert b"/tab.js" in b, b[:200]
r = urllib.request.Request("http://127.0.0.1:48921/api/addon/exits/status", data=b"{}", method="POST",
                           headers={"X-Core": "1", "Content-Type": "application/json"})
j = json.load(urllib.request.urlopen(r))
assert j["ok"] and j["leases"] == [], j
try:
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:48921/api/addon/exits/status", data=b"{}",
                           headers={"Content-Type": "application/json"}))
    raise SystemExit("no X-Core was accepted")
except urllib.error.HTTPError as e:
    assert e.code == 403
print("page ok")
PY
kill $UIPID
echo "--- client tests with the installed environment (no SRNet client; the netns lab needs a writable /proc/sys)"
cd ~/SRNet-VPSHost-ExitPack/client
SRNET_SKIP_LAB=1 "$VPY" -m unittest discover -s tests -t tests > /tmp/ut.log 2>&1 || { tail -60 /tmp/ut.log; fail "client tests"; }
grep -E "^(Ran|OK)" /tmp/ut.log
cd ~
echo "--- reinstall"
SRNet-VPSHost-ExitPack/client/install.sh > /tmp/i2.log 2>&1 || { cat /tmp/i2.log; fail reinstall; }
[ "$(grep -c "added by SRNet-VPSHost-ExitPack" ~/.bashrc)" = 1 ] || fail "PATH line twice"
srnet-exits --version
echo "--- uninstall"
cp ~/.bashrc /tmp/bashrc.before
SRNet-VPSHost-ExitPack/client/install.sh --uninstall
[ ! -e ~/.local/bin/srnet-exits ] && [ ! -e ~/.local/share/srnet-exits ] || fail "left files"
! grep -q "srnet-exits" ~/.bashrc || fail "PATH line left"
[ -d ~/.srnet ] || fail "~/.srnet removed"
cp /etc/skel/.bashrc /tmp/skel.bashrc
diff -u /tmp/skel.bashrc ~/.bashrc && echo "bashrc back to the original"
echo "standalone client: OK"
'
