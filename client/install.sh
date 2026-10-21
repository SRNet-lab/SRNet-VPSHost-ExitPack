#!/usr/bin/env bash
# srnet-exits installer for Linux (made for Ubuntu 24.04): buy exit time and connect
# through them, without the SRNet client. Installs into your user account only; the one-time
# Session Router setup asks sudo once.
#   client/install.sh                   install or upgrade
#   client/install.sh --no-router-setup skip the Session Router setup (run srnet-exits setup-router later)
#   client/install.sh --uninstall       remove srnet-exits (your ~/.srnet stays)
#   client/install.sh --uninstall --force   the same while connected: disconnects first
set -euo pipefail
PREFIX="${SRNET_EXITS_PREFIX:-$HOME/.local/share/srnet-exits}"
BIN="${SRNET_BIN:-$HOME/.local/bin}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CMD="srnet-exits"
LAUNCHER_MARK="# srnet-exits launcher (written by SRNet-VPSHost-ExitPack client/install.sh)"

UNINSTALL=0; ROUTER=1; FORCE=0
for a in "$@"; do
  case "$a" in
    --uninstall) UNINSTALL=1;;
    --no-router-setup) ROUTER=0;;
    --force) FORCE=1;;
    -h|--help) sed -n '2,8p' "$0"; exit 0;;
    *) echo "install.sh: unknown option $a (see --help)" >&2; exit 2;;
  esac
done

if [ "$(id -u)" = 0 ] && [ "${SRNET_ALLOW_ROOT:-0}" != 1 ]; then
  echo "Run this as your own user, not with sudo: srnet-exits keeps your paid time in your ~/.srnet," >&2
  echo "and the steps that need root ask sudo for your password themselves." >&2
  exit 2
fi

RC_MARK="# srnet-exits: ~/.local/bin on PATH (added by SRNet-VPSHost-ExitPack client/install.sh)"
# The one line we add after RC_MARK (the bin dir shell-quoted, so no path can run as code).
if [ "$BIN" = "$HOME/.local/bin" ]; then
  RC_LINE='case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH";; esac'
else
  QBIN=$(printf '%q' "$BIN")
  RC_LINE="case \":\$PATH:\" in *:${QBIN}:*) ;; *) export PATH=${QBIN}:\"\$PATH\";; esac"
fi
ours() { [ -f "$1" ] && grep -qxF "$LAUNCHER_MARK" "$1"; }

INSTALL_MARK="$PREFIX/.srnet-exits-install"
VPY="$PREFIX/venv/bin/python"

# install and uninstall touch $PREFIX only if it is empty, missing, or one of our installs
not_an_install() {
  echo "Not $1 $PREFIX: it has no .srnet-exits-install and venv/bin/python, so it doesn't look like" >&2
  echo "a srnet-exits install. Check SRNET_EXITS_PREFIX." >&2
  exit 1
}

# TERM every process of this user that runs this install's python with srnet_exits: the page
# (srnet-exits ui), the lease reminder and the tunnel DNS forwarder.
stop_ours() {
  local vpy="$PREFIX/venv/bin/python" d pid cl a0
  for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    [ "$pid" = "$$" ] && continue
    [ -O "$d" ] || continue
    cl=$(tr '\0' '\n' < "$d/cmdline" 2>/dev/null) || continue
    a0=${cl%%$'\n'*}
    [ "$a0" = "$vpy" ] || continue
    case "$cl" in *srnet_exits*) kill -TERM "$pid" 2>/dev/null && echo "stopped $pid (${cl//$'\n'/ })";; esac
  done
  return 0
}

if [ "$UNINSTALL" = 1 ]; then
  SHOME="${SRNET_HOME:-$HOME/.srnet}"
  STATE="$SHOME/state.json"
  CONNECTED=0
  if [ -f "$STATE" ]; then
    if [ -x "$VPY" ]; then
      "$VPY" -I -c 'import json,sys; sys.exit(1 if (json.load(open(sys.argv[1])).get("connected")) else 0)' "$STATE" 2>/dev/null \
        || CONNECTED=1
    elif grep -q '"connected": *{' "$STATE"; then
      CONNECTED=1
    fi
  fi
  if [ "$CONNECTED" = 1 ] && [ "$FORCE" != 1 ]; then
    echo "An exit is still connected. Disconnect first (srnet-exits disconnect), or run with --force." >&2
    exit 1
  fi
  if [ -e "$PREFIX" ] && { [ ! -f "$INSTALL_MARK" ] || [ ! -x "$VPY" ]; }; then
    not_an_install removing
  fi
  # --force while connected: a full disconnect first (DNS back, the exit unmapped, the tunnel
  # routes gone, the kill switch removed), while the installed command is still here.
  DISC=""; STILL=0; KS_LEFT=0
  if [ "$CONNECTED" = 1 ]; then
    echo "An exit is still connected. Disconnecting first (sudo may ask for your password)."
    if [ -x "$VPY" ]; then
      if SRNET_EXITS_PROG=srnet-exits "$VPY" -I -m srnet_exits --home "$SHOME" disconnect; then
        DISC=ok
      else
        DISC=failed; KS_LEFT=1
      fi
      "$VPY" -I -c 'import json,sys; sys.exit(1 if (json.load(open(sys.argv[1])).get("connected")) else 0)' "$STATE" 2>/dev/null \
        || STILL=1
    else
      # no installed Python left to talk to Session Router: remove the kill switch directly
      DISC=novenv; STILL=1; KS_LEFT=1
      NFT=$(command -v nft || { [ -x /usr/sbin/nft ] && echo /usr/sbin/nft; } || true)
      if [ -n "$NFT" ]; then
        if [ "$(id -u)" = 0 ]; then SUDO=(); elif [ -t 0 ]; then SUDO=(sudo); else SUDO=(sudo -n); fi
        if ERR=$("${SUDO[@]}" "$NFT" delete table inet srnet_ks 2>&1); then
          KS_LEFT=0; echo "kill switch disarmed"
        else
          case "$ERR" in *"No such file"*|*"does not exist"*) KS_LEFT=0;; esac
        fi
      else
        KS_LEFT=0
      fi
    fi
  fi
  # Tunnel DNS still set up (a connect, or one cut short): put the link's DNS back before the
  # forwarder it points at is stopped, else the machine is left without DNS.
  TUN=""
  if [ -f "$STATE" ]; then
    if [ -x "$VPY" ]; then
      TUN=$("$VPY" -I -c 'import json,sys; print(((json.load(open(sys.argv[1])).get("tunnel_dns") or {}).get("saved") or {}).get("tun") or "")' "$STATE" 2>/dev/null || true)
    else
      TUN=$(grep -o '"tun": *"[^"]*"' "$STATE" 2>/dev/null | head -n 1 | sed 's/.*"\([^"]*\)"$/\1/' || true)
    fi
  fi
  case "$TUN" in *[!A-Za-z0-9_.-]*) TUN="<tun interface>";; esac
  DNS_LEFT=""
  if [ -n "$TUN" ] && [ -n "$DISC" ]; then
    DNS_LEFT="$TUN"   # the disconnect above already tried and could not
  elif [ -n "$TUN" ]; then
    echo "Putting DNS on $TUN back as it was before connecting (sudo may ask for your password)."
    if [ -x "$VPY" ] && "$VPY" -I -c 'import sys; from pathlib import Path; from srnet_exits import cli; from srnet_exits.state import State; sys.exit(0 if cli.dns_down(State(Path(sys.argv[1]))) else 1)' "$SHOME"; then
      :
    else
      DNS_LEFT="$TUN"
    fi
  fi
  if [ -e "$PREFIX" ]; then
    stop_ours
    rm -rf "$PREFIX"
  fi
  if ours "$BIN/$CMD"; then rm -f "$BIN/$CMD"; fi
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$rc" ] && grep -qF "$RC_MARK" "$rc"; then
      # drop our marker, the blank line we put before it, and the next line only if it is exactly ours
      SRNET_RC_M="$RC_MARK" SRNET_RC_L="$RC_LINE" awk '{ a[NR] = $0 }
        END { m = ENVIRON["SRNET_RC_M"]; l = ENVIRON["SRNET_RC_L"]   # (awk -v would eat backslashes)
              for (i = 1; i <= NR; i++) {
                if (a[i] == m) { if (a[i + 1] == l) i++; continue }
                if (a[i] == "" && a[i + 1] == m) continue
                print a[i] } }' "$rc" > "$rc.srnet-tmp" \
        && cat "$rc.srnet-tmp" > "$rc"
      rm -f "$rc.srnet-tmp"
    fi
  done
  echo "Removed srnet-exits. Your paid time and settings in ~/.srnet were left in place, and so were"
  echo "Session Router's settings (the original client.ini is kept as client.ini.srnet-orig)."
  if [ "$DISC" = ok ] && [ "$STILL" = 0 ]; then
    echo "You were connected: the exit is disconnected and your traffic goes out your own connection again."
  fi
  if [ "$STILL" = 1 ]; then
    echo
    echo "Session Router did not confirm that the exit is unmapped, so traffic may still go through it."
    echo "Restarting Session Router drops the exit: sudo systemctl restart session-router-client"
    echo "To clear the rest, reinstall and run srnet-exits disconnect, or reboot."
  fi
  if [ "$KS_LEFT" = 1 ]; then
    echo
    echo "The kill switch may still block traffic outside the tunnel."
    echo "Remove it with: sudo nft delete table inet srnet_ks"
  fi
  if [ -n "$DNS_LEFT" ]; then
    echo
    echo "DNS on $DNS_LEFT could not be put back and pointed at the stopped forwarder."
    echo "Restore DNS with: sudo resolvectl revert $DNS_LEFT"
  fi
  exit 0
fi

if [ "$(uname -s)" != Linux ]; then
  echo "srnet-exits is for Linux for now (connect uses nftables and systemd-resolved)." >&2
  exit 1
fi
[ -f "$SRC/pyproject.toml" ] && [ -d "$SRC/client/srnet_exits" ] \
  || { echo "Run this from a copy of SRNet-VPSHost-ExitPack (client/install.sh)." >&2; exit 1; }
if [ -e "$BIN/$CMD" ] && ! ours "$BIN/$CMD" && [ "$FORCE" != 1 ]; then
  echo "$BIN/$CMD exists and wasn't written by this installer; not replacing it (--force does)." >&2
  exit 1
fi

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "Python 3.10 or newer is required." >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 or newer is required (found $("$PY" -V))." >&2; exit 1; }
"$PY" -c 'import venv, ensurepip' 2>/dev/null \
  || { echo "Python venv support is missing. On Ubuntu: sudo apt install python3-venv" >&2; exit 1; }

if [ -e "$PREFIX" ] && [ ! -f "$INSTALL_MARK" ] && [ -n "$(ls -A "$PREFIX" 2>/dev/null || echo x)" ]; then
  not_an_install "installing into"
fi
echo "Installing srnet-exits to $PREFIX"
mkdir -p "$PREFIX" "$BIN"
printf '%s\n' "srnet-exits install (client/install.sh). install.sh --uninstall removes this directory." \
  > "$INSTALL_MARK"
# A copy of just what the package needs, so the build never writes into your download.
rm -rf "$PREFIX/src"
mkdir -p "$PREFIX/src/client" "$PREFIX/src/contrib"
cp "$SRC/pyproject.toml" "$SRC/LICENSE" "$SRC/LICENSE-APACHE-2.0.txt" "$PREFIX/src/"
cp -R "$SRC/client/srnet_exits" "$SRC/client/README.md" "$PREFIX/src/client/"
cp -R "$SRC/seller" "$SRC/board" "$PREFIX/src/"
cp -R "$SRC/contrib/exit-lease" "$PREFIX/src/contrib/"
find "$PREFIX/src" -name __pycache__ -prune -exec rm -rf {} +

[ -x "$VPY" ] || "$PY" -m venv "$PREFIX/venv"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet --upgrade "$PREFIX/src"

QVPY=$(printf '%q' "$VPY")
cat > "$BIN/$CMD.srnet-tmp" <<W
#!/usr/bin/env bash
$LAUNCHER_MARK
exec $QVPY -I -m srnet_exits "\$@"
W
chmod 755 "$BIN/$CMD.srnet-tmp"
mv -f "$BIN/$CMD.srnet-tmp" "$BIN/$CMD"

# Exits need two Session Router client settings (persistent key, blackhole-routes=false).
# srnet-exits setup-router sets them, keeping the rest of client.ini; it asks sudo once.
ROUTER_SETUP=""
ROUTER_INI="${SRNET_ROUTER_INI:-/etc/session-router/client.ini}"
if [ "$ROUTER" = 1 ] && [ "${SRNET_SKIP_ROUTER_SETUP:-0}" != 1 ]; then
  if [ -e "$ROUTER_INI" ]; then
    echo
    echo "Setting up Session Router for exits (persistent key, blackhole-routes=false; the rest of"
    echo "$ROUTER_INI stays as it is). sudo asks for your password once."
    if "$BIN/$CMD" setup-router --ini "$ROUTER_INI"; then ROUTER_SETUP=ok; else ROUTER_SETUP=failed; fi
  else
    ROUTER_SETUP=noini
  fi
fi

# New terminals: put $BIN on PATH in ~/.bashrc (and ~/.zshrc if there is one), once.
RC_DONE=""
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [ "$rc" = "$HOME/.zshrc" ] && [ ! -f "$rc" ]; then continue; fi
  if [ -f "$rc" ] && grep -qF "$RC_MARK" "$rc"; then continue; fi
  [ -d "$HOME" ] || continue
  if printf '\n%s\n%s\n' "$RC_MARK" "$RC_LINE" 2>/dev/null >> "$rc"; then
    RC_DONE="$RC_DONE ${rc/#"$HOME"/\~}"
  fi
done

echo
echo "Done. Next steps:"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    echo
    echo "  $BIN is not on your PATH in this terminal. Run this, then try $CMD again:"
    echo
    if [ "$BIN" = "$HOME/.local/bin" ]; then
      echo '    export PATH="$HOME/.local/bin:$PATH"'
    else
      echo "    export PATH=\"$BIN:\$PATH\""
    fi
    echo
    echo "  New terminals have it already."
    ;;
esac
[ -n "$RC_DONE" ] && echo "  Added $BIN to PATH for new terminals in:$RC_DONE"
echo "  $CMD doctor     check Session Router, .sesh names, the clock and what exits need"
echo "  $CMD exits      list exits; buy time with: $CMD buy --seller <id> --hours N"
echo "  $CMD ui         the same in a small page in your browser"
if command -v session-router >/dev/null 2>&1 || [ -x /usr/bin/session-router ] || \
   systemctl list-unit-files session-router-client.service 2>/dev/null | grep -q session-router-client; then
  if systemctl is-active --quiet session-router-client 2>/dev/null; then
    echo "  Session Router: installed and running (session-router-client)"
  else
    echo "  Session Router: installed but not running; start it: sudo systemctl start session-router-client"
  fi
else
  echo "  Session Router is a separate program: install and start it first (SRNet-Desktop README,"
  echo "  Install, steps 1 and 2: https://github.com/SRNet-lab/SRNet-Desktop#1-session-router)."
fi
case "$ROUTER_SETUP" in
  ok) echo "  Session Router: set up for exits (persistent key, no blackhole routes)";;
  failed) echo "  Session Router is not set up for exits yet; run: $CMD setup-router";;
  noini) echo "  After installing Session Router, run once: $CMD setup-router";;
  *) [ "$ROUTER" = 0 ] && echo "  Session Router setup skipped; run once: $CMD setup-router";;
esac
command -v nft >/dev/null 2>&1 || [ -x /usr/sbin/nft ] \
  || echo "  nftables is missing (connect needs it for the kill switch): sudo apt install nftables"
if [ -x "$HOME/.local/share/srnet-client/venv/bin/python" ] \
   && [ -d "$HOME/.local/share/srnet-client/venv/lib" ] \
   && find "$HOME/.local/share/srnet-client/venv/lib" -maxdepth 3 -name srnet_exits -type d 2>/dev/null | grep -q .; then
  echo "  The SRNet client here has the exits add-on too. Both use the same ~/.srnet, buyer key and"
  echo "  connection, so 'srnet connect' and '$CMD connect' are the same thing."
fi
exit 0
