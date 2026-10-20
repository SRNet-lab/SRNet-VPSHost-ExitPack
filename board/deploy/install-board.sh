#!/usr/bin/env bash
# Install the SRNet board on one host: three Session Router client instances (one per board
# .sesh name), a board server behind each, and an hourly timer that ingests seller refreshes,
# re-signs the board and pushes it to the two mirrors.
#
#   install-board.sh --binary /path/session-router --signing-key board.key \
#                    --name-key primary.key --name-key mirror1.key --name-key mirror2.key
#   install-board.sh --uninstall        (keeps /var/lib/srnet-board and keys)
#
# The first --name-key is the primary (takes seller submissions). Keys are copied to
# /etc/srnet-board (mode 600). Board servers bind only their router's tun IPv6 address (Session
# Router hands out AAAA records for .sesh names); ufw allows
# tcp/80 on those interfaces only. Nothing is opened on the public interface.
set -euo pipefail

PREFIX=/opt/srnet-board; ETC=/etc/srnet-board; VAR=/var/lib/srnet-board; USER_=srnet-board
BIN=""; SIGN=""; KEYS=(); ACTION=install; BASEPORT=1201; NET=10.21; NET6=fd2e:7365:7368:21
HERE=$(cd "$(dirname "$0")/.." && pwd)
die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case $1 in
    --binary) BIN=$2; shift 2;;
    --signing-key) SIGN=$2; shift 2;;
    --name-key) KEYS+=("$2"); shift 2;;
    --uninstall) ACTION=uninstall; shift;;
    -h|--help) sed -n 2,14p "$0"; exit 0;;
    *) die "unknown option $1";;
  esac
done
[ "$(id -u)" = 0 ] || die "run as root"

units() { for i in 0 1 2; do echo "srnet-board-router@$i.service srnet-board-server@$i.service"; done; echo srnet-board-tick.timer; }

if [ $ACTION = uninstall ]; then
  systemctl disable --now $(units) srnet-board-tick.service 2>/dev/null || true
  rm -f /etc/systemd/system/srnet-board-*.service /etc/systemd/system/srnet-board-*.timer
  systemctl daemon-reload
  if command -v ufw >/dev/null; then
    for i in 0 1 2; do ufw --force delete allow in on sr-board$i to $NET6$i::1 port 80 proto tcp >/dev/null 2>&1 || true; done
  fi
  echo "removed units; $VAR and $ETC kept"
  exit 0
fi

[ -x "$BIN" ] || die "--binary must be an executable session-router"
[ -f "$SIGN" ] || die "--signing-key missing"
[ ${#KEYS[@]} = 3 ] || die "need exactly three --name-key"
command -v python3 >/dev/null && python3 -c 'import cryptography' || die "need python3 and python3-cryptography"

id $USER_ >/dev/null 2>&1 || useradd --system --home-dir $VAR --shell /usr/sbin/nologin $USER_
install -d -m 755 $PREFIX $PREFIX/board $PREFIX/seller
install -m 644 "$HERE"/boardfmt.py "$HERE"/board.py "$HERE"/board_server.py $PREFIX/board/
for f in listing.py chains.py quote.py; do install -m 644 "$HERE/../seller/$f" $PREFIX/seller/; done
install -m 755 "$BIN" $PREFIX/session-router
[ -f "$(dirname "$BIN")/../mainnet.signed" ] && install -m 644 "$(dirname "$BIN")/../mainnet.signed" $PREFIX/ || true
install -d -m 711 $ETC
cp_key() { [ "$(realpath "$1")" = "$(realpath -m "$2")" ] || install -m 600 "$1" "$2"; }
install -d -m 750 -o $USER_ -g $USER_ $VAR $VAR/www0 $VAR/www1 $VAR/www2
cp_key "$SIGN" $ETC/board.key; chown $USER_:$USER_ $ETC/board.key
NAMES=()
for i in 0 1 2; do
  cp_key "${KEYS[$i]}" $ETC/name$i.key
  NAMES+=("$(python3 -c "import sys;sys.path.insert(0,'$PREFIX/seller');import listing as L;from cryptography.hazmat.primitives import serialization as s;k=L.load_identity('$ETC/name$i.key');print(L.sesh_of(k.public_key().public_bytes(s.Encoding.Raw,s.PublicFormat.Raw)))")")
  BOOT=""; [ -f $PREFIX/mainnet.signed ] && BOOT="[bootstrap]
add-node=$PREFIX/mainnet.signed"
  cat > $ETC/router$i.ini <<INI
[router]
data-dir=$VAR/router$i
[logging]
level=warning
[network]
keyfile=$ETC/name$i.key
ifname=sr-board$i
ifaddr=$NET$i.0.1/16
ifaddr=$NET6$i::1/64
auto-routing=false
blackhole-routes=false
[bind]
listen=:$((BASEPORT + i))
[dns]
listen=
[api]
enabled=false
$BOOT
INI
  install -d -m 700 $VAR/router$i
done
PIN=$(python3 -c "import sys;sys.path.insert(0,'$PREFIX/seller');import listing as L;from cryptography.hazmat.primitives import serialization as s;k=L.load_identity('$ETC/board.key');print(L.sesh_of(k.public_key().public_bytes(s.Encoding.Raw,s.PublicFormat.Raw)))")
[ -f $VAR/state/state.json ] || runuser -u $USER_ -- python3 $PREFIX/board/board.py init --state $VAR/state --key $ETC/board.key >/dev/null

cat > /etc/systemd/system/srnet-board-router@.service <<UNIT
[Unit]
Description=SRNet board name %i (Session Router)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$PREFIX/session-router $ETC/router%i.ini
Restart=always
RestartSec=10
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/srnet-board-server@.service <<UNIT
[Unit]
Description=SRNet board server %i
After=srnet-board-router@%i.service
BindsTo=srnet-board-router@%i.service

[Service]
User=$USER_
Group=$USER_
EnvironmentFile=$ETC/server%i.env
ExecStartPre=/bin/sh -c 'for n in \$\$(seq 60); do ip -6 addr show dev sr-board%i 2>/dev/null | grep -q "inet6 fd" && exit 0; sleep 1; done; exit 1'
ExecStart=/usr/bin/python3 $PREFIX/board/board_server.py --www $VAR/www%i --host \${HOST} --port 80 --pin $PIN \$EXTRA
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$VAR
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK

[Install]
WantedBy=multi-user.target
UNIT
for i in 0 1 2; do
  EXTRA=""; [ $i = 0 ] && EXTRA="--inbox $VAR/state/inbox"
  printf 'HOST=%s\nEXTRA=%s\n' "$NET6$i::1" "$EXTRA" > $ETC/server$i.env
done
chmod 644 $ETC/server*.env

cat > /etc/systemd/system/srnet-board-tick.service <<UNIT
[Unit]
Description=SRNet board: ingest refreshes, re-sign, push to mirrors

[Service]
Type=oneshot
User=$USER_
Group=$USER_
ExecStart=/usr/bin/python3 $PREFIX/board/board.py tick --state $VAR/state --key $ETC/board.key --out $VAR/www0 --publish http://[${NET6}1::1]/ --publish http://[${NET6}2::1]/
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$VAR
ProtectHome=yes
PrivateTmp=yes
UNIT
cat > /etc/systemd/system/srnet-board-tick.timer <<UNIT
[Unit]
Description=Re-sign the SRNet board every 15 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
UNIT

if command -v ufw >/dev/null && ufw status | grep -q active; then
  for i in 0 1 2; do ufw allow in on sr-board$i to $NET6$i::1 port 80 proto tcp >/dev/null; done
fi
systemctl daemon-reload
systemctl enable --now $(units)
echo "board pin: $PIN"
for i in 0 1 2; do echo "name $i: ${NAMES[$i]}  -> [$NET6$i::1]:80$([ $i = 0 ] && echo ' (primary, takes /submit)')"; done
echo "listings go live automatically; to list or ban: runuser -u $USER_ -- python3 $PREFIX/board/board.py list|ban --state $VAR/state ..."
