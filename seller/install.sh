#!/usr/bin/env bash
# =============================================================================
# SRNet seller pack: installer for a fresh Ubuntu 24.04 server (Debian 12 untested)
# (x86_64, root). Buyers pay you in USDC on Arbitrum One and Base, straight to
# your payout address; the watcher checks each payment on chain (read-only).
#
# LEGAL (read before you enable forwarding):
#   Running an exit forwards other people's traffic to the internet from YOUR
#   IP address. You are responsible for that traffic under your local law and
#   your host's terms of service. Only tcp/80 and tcp/443 are forwarded to the
#   internet. Buyers' DNS is answered by the exit's own recursive resolver (unbound,
#   resolving from the root servers itself: no forwarder, no query logs), reachable
#   only through the tunnel; DNS to any other server is never forwarded.
#   Exit forwarding is OFF unless you pass BOTH
#     --enable-exit  --i-accept-exit-operator-responsibility
#
# Session Router binary (pick one; default --source):
#   --source                 build the patched router from the pinned public
#                            upstream (build-session-router.sh; 20-60 min)
#   --prebuilt [URL]         download a release binary and verify it against
#                            PREBUILT_SHA256 (or --prebuilt-sha256 HEX)
#   --binary PATH            use a local build (optionally --binary-sha256 HEX)
#
# Seller settings:
#   --payout 0xADDR          payout wallet ADDRESS (no private key is needed)
#   --price-per-hour-usdc N  hourly price (default 1.00)
#   --chain NAME             chain to take USDC on: arbitrum, base (repeatable; default both)
#   --rpc CHAINID=URL        your own RPC for a chain (repeatable; default: public RPCs)
#   --testnets               allow test chains (arbitrum-sepolia, base-sepolia, anvil); tests only
#   --country XX             ISO 3166-1 alpha-2 country of the exit's IP (default: detected
#                            from the public IPv4 via two geo-IP services that must agree)
#   --region NAME  --max-hours N
#   --cap-usdc N             largest single payment you will quote (default 20)
#   --quotes-per-hour N      new quotes per hour overall on the claim desk (default 150)
#   --board-submit-url URL   where the hourly listing refresh is sent (default on mainnet chains:
#                            the exit board; 'none' = re-sign locally only)
#
# Host settings:
#   --listen-port N (1193/udp)  --wan-if IF  --ssh-port N  --max-mbit N (100; 0=off)
#   --speed-cap-mbps N       per-buyer speed cap, each direction (default 10; 0=unlimited)
#   --no-dns                 no exit resolver (buyers' clients refuse to connect unless they opt
#                            out of tunnel DNS; only for sellers who cannot run unbound)
#   --no-firewall            do not touch ufw (you manage the firewall)
#   --no-start               write everything but do not start services
#   --prefix DIR             install prefix (/opt/srnet-seller)
#
# Other modes:
#   --dry-run --root DIR     write files under DIR, log system commands only
#   --health                 run health checks on an installed seller
#   --uninstall [--purge]    remove services, firewall rules, NAT; --purge also
#                            deletes keys, data and the service users
# =============================================================================
set -euo pipefail

# Release binary pin. Empty in a clone (--prebuilt then needs --prebuilt-sha256);
# scripts/make-release.sh stamps these in the release pack. --source is the fallback.
PREBUILT_URL=""
PREBUILT_SHA256=""

CLAIM_ADDR=fd2e:7365:736a::1  # submit router tun address; claimd binds here only
PREFIX=/opt/srnet-seller; MODE=""; BINARY=""; BINARY_SHA=""; PREBUILT_ARG=""
ENABLE_EXIT=0; ACK=0; PORT=1193; WAN_IF=""; SSH_PORT=""; MAX_MBIT=100; SPEED_CAP=10; FIREWALL=1; START=1
ROOT=""; DRY=0; WATCHER_ENV=""; PRICE="1.00"; CAP_USDC="20"; QUOTES_PER_HOUR=150; MAX_HOURS=168
CHAINS=(); RPCS=(); TESTNETS=0; PAYOUT=""; REGION=""; COUNTRY=""; ACTION=install; PURGE=0; BOARD_URL=default
TUN=sr-tun-seller; TUN_NET4=10.193.0.0/16
# Exit resolver: an address outside the TUN range (on lo), so the router treats answers like any
# clearnet reply; buyers reach it only through the tunnel. Advertised in the listing as "dns".
DNS_IP=10.195.0.53; DNS=1
DNS_CONF=/etc/unbound/srnet-seller-dns.conf; DNS_KEY=/var/lib/unbound/srnet-seller-root.key
EXIT_USER=srnet-exit; WATCH_USER=srnet-watch; CLAIM_USER=srnet-claim; GROUP=srnet; CLAIMS_GROUP=srnet-claims
die() { echo "install.sh: $*" >&2; exit 1; }
warn() { echo "install.sh: WARNING: $*" >&2; }
say() { echo "==> $*"; }
usage() { sed -n '2,51p' "$0"; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --binary) MODE=binary; BINARY=$2; shift 2;;
    --binary-sha256) BINARY_SHA=$2; shift 2;;
    --source) MODE=source; shift;;
    --prebuilt) MODE=prebuilt; if [ $# -gt 1 ] && [[ "$2" != --* ]]; then PREBUILT_ARG=$2; shift; fi; shift;;
    --prebuilt-sha256) PREBUILT_SHA256=$2; shift 2;;
    --prefix) PREFIX=$2; shift 2;;
    --enable-exit) ENABLE_EXIT=1; shift;;
    --i-accept-exit-operator-responsibility) ACK=1; shift;;
    --listen-port) PORT=$2; shift 2;;
    --wan-if) WAN_IF=$2; shift 2;;
    --ssh-port) SSH_PORT=$2; shift 2;;
    --max-mbit) MAX_MBIT=$2; shift 2;;
    --speed-cap-mbps) SPEED_CAP=$2; shift 2;;
    --no-firewall) FIREWALL=0; shift;;
    --no-dns) DNS=0; shift;;
    --no-start) START=0; shift;;
    --watcher-env) WATCHER_ENV=$2; shift 2;;
    --price-per-hour-usdc|--price-usdc) PRICE=$2; shift 2;;
    --cap-usdc) CAP_USDC=$2; shift 2;;
    --quotes-per-hour) QUOTES_PER_HOUR=$2; shift 2;;
    --max-hours) MAX_HOURS=$2; shift 2;;
    --chain) CHAINS+=("$2"); shift 2;;
    --testnets) TESTNETS=1; shift;;
    --chainid|--contract) die "$1 is gone: buyers now pay your --payout address directly (see --chain)";;
    --payout) PAYOUT=$2; shift 2;;
    --region) REGION=$2; shift 2;;
    --country) COUNTRY=$2; shift 2;;
    --rpc) RPCS+=("$2"); shift 2;;
    --board-submit-url) BOARD_URL=$2; shift 2;;
    --dry-run) DRY=1; shift;;
    --root) ROOT=$2; shift 2;;
    --health) ACTION=health; shift;;
    --uninstall) ACTION=uninstall; shift;;
    --purge) PURGE=1; shift;;
    -h|--help) usage;;
    *) die "unknown arg $1 (see --help)";;
  esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
PACK_VERSION=$(cat "$HERE/VERSION" 2>/dev/null || echo unknown)
# Pack sources: this directory (seller pack) + exit-lease issuer next to it.
ADMISSION_PY=""
for c in "$HERE/../exit-lease/admission.py" "$HERE/../contrib/exit-lease/admission.py" "$HERE/admission.py"; do
  [ -f "$c" ] && ADMISSION_PY=$c && break
done
[ -n "$ADMISSION_PY" ] || die "exit-lease/admission.py not found next to the seller pack"

[ "$DRY" = 1 ] && [ -z "$ROOT" ] && die "--dry-run needs --root DIR"
[ "$DRY" = 0 ] && [ "$(id -u)" != 0 ] && die "run as root (sudo), or use --dry-run --root DIR"
R="$ROOT"; P="$R$PREFIX"
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*" >> "$R/commands.log"; else "$@"; fi; }
have_systemd() { [ "$DRY" = 0 ] && [ -d /run/systemd/system ]; }

# ----------------------------------------------------------------- uninstall
if [ "$ACTION" = uninstall ]; then
  [ "$DRY" = 1 ] && { mkdir -p "$R"; : > "$R/commands.log"; }
  CONF="$P/config/install.conf"
  # shellcheck disable=SC1090
  [ -f "$CONF" ] && . "$CONF"
  say "stopping services"
  for u in srnet-seller-listing.timer srnet-seller-listing.service srnet-seller-watcher.service \
           srnet-seller-claimd.service srnet-seller-exit.service srnet-seller-submit.service srnet-seller-nat.service \
           srnet-seller-dns.service; do
    if have_systemd || [ "$DRY" = 1 ]; then run systemctl disable --now "$u" || true; fi
    run rm -f "$R/etc/systemd/system/$u"
  done
  if have_systemd || [ "$DRY" = 1 ]; then run systemctl daemon-reload || true; fi
  run nft delete table inet srnet_seller 2>/dev/null || true
  run rm -f "$R/etc/sysctl.d/90-srnet-seller.conf" "$R/usr/local/sbin/srnet-seller" "$R$DNS_CONF" "$R$DNS_KEY"
  run ip addr del "$DNS_IP/32" dev lo 2>/dev/null || true
  if [ "${FIREWALL:-1}" = 1 ] && { [ "$DRY" = 1 ] || command -v ufw >/dev/null; }; then
    say "removing firewall rules added by the installer (ufw itself stays enabled)"
    run ufw delete allow "${PORT}/udp" || true
    run ufw route delete allow in on "$TUN" out on "${WAN_IF:-eth0}" proto tcp to any port 80,443 || true
    run ufw delete allow in on sr-tun-submit to "$CLAIM_ADDR" port 80 proto tcp || true
    for pr in udp tcp; do run ufw delete allow in on "$TUN" to "$DNS_IP" port 53 proto $pr || true; done
  fi
  if [ "$PURGE" = 1 ]; then
    warn "--purge: deleting $PREFIX including the exit identity key, issuer key and listing"
    run rm -rf "$P"
    for u in "$CLAIM_USER" "$WATCH_USER" "$EXIT_USER"; do id "$u" >/dev/null 2>&1 && run userdel "$u" || true; done
    for g in "$CLAIMS_GROUP" "$GROUP"; do getent group "$g" >/dev/null 2>&1 && run groupdel "$g" || true; done
  else
    run rm -rf "$P/bin" "$P/pack" "$P/run" "$P/spool"
    say "kept $PREFIX/config (keys), $PREFIX/listing, $PREFIX/claims (used payments) and $PREFIX/data; use --uninstall --purge to delete them"
  fi
  say "uninstalled"
  exit 0
fi

# ----------------------------------------------------------------- health
health() {
  local fails=0 ok
  # retries (services may still be starting); no pipefail so `cmd | grep -q` cannot fail on SIGPIPE
  chk() { local t; for t in 1 2 3 4 5; do
            if (set +o pipefail; eval "$2") >/dev/null 2>&1; then echo "  ok   $1"; return 0; fi
            [ "${HEALTH_RETRY:-1}" = 1 ] && [ $t -lt 5 ] && sleep 2 || break
          done; echo "  FAIL $1"; fails=$((fails+1)); }
  # shellcheck disable=SC1090
  [ -f "$P/config/install.conf" ] && . "$P/config/install.conf"
  echo "health checks ($PREFIX):"
  chk "patched binary present" "grep -aq 'srnet-exit-admission/v2' '$P/bin/session-router'"
  chk "clock synced" "timedatectl show -p NTPSynchronized --value | grep -qx yes"
  chk "exit identity key 0600" "[ \"\$(stat -c %a '$P/config/exit.key')\" = 600 ]"
  chk "issuer key 0600" "[ \"\$(stat -c %a '$P/config/issuer.key')\" = 600 ]"
  chk "admission store readable by exit user" "runuser -u $EXIT_USER -- test -r '$P/run/admissions.json'"
  chk "signed listing verifies" "python3 '$P/pack/listing.py' verify '$P/listing/listing.json'"
  chk "listing refreshed within 3 h" "python3 -c 'import json,sys,time;sys.exit(time.time()-json.load(open(sys.argv[1]))[\"listing\"][\"issued\"]>10800)' '$P/listing/listing.json'"
  chk "router DNS listener off" "grep -q '^listen=$' '$P/config/exit.ini'"
  if [ -d /run/systemd/system ]; then
    chk "srnet-seller-exit active" "systemctl is-active --quiet srnet-seller-exit"
    chk "router listening on udp/${PORT}" "ss -Hlun | grep -q ':${PORT}\\b'"
    chk "tun $TUN up" "ip link show $TUN"
    if [ "${ENABLE_EXIT:-0}" = 1 ]; then
      chk "nat table loaded" "nft list table inet srnet_seller"
      chk "DNS forward drop rule (buyers reach no other resolver)" "nft list table inet srnet_seller | grep -q 'dport 53 drop'"
      if [ "${DNS:-0}" = 1 ]; then
        chk "exit resolver active" "systemctl is-active --quiet srnet-seller-dns"
        chk "exit resolver answers on $DNS_IP (from the root servers)" "python3 '$P/pack/dnscheck.py' $DNS_IP example.com"
        chk "exit resolver: no forwarder, no query logs, QNAME minimisation, DNSSEC" "python3 '$P/pack/dnscheck.py' --conf '$DNS_CONF'"
        chk "exit resolver listens only on $DNS_IP" "! ss -Hlnup | grep unbound | grep -v '$DNS_IP:53'"
        chk "router policy carries DNS to the exit resolver" "grep -qx 'policy=udp/53' '$P/config/exit.ini'"
        chk "listing advertises dns $DNS_IP" "python3 -c 'import json,sys;sys.exit(json.load(open(sys.argv[1]))[\"listing\"].get(\"dns\")!=sys.argv[2])' '$P/listing/listing.json' $DNS_IP"
      fi
      if [ "${SPEED_CAP:-0}" -gt 0 ]; then
        chk "per-buyer speed cap ${SPEED_CAP} Mbit/s" "nft list table inet srnet_seller | grep 'set cap_down4'"
      fi
      chk "ip_forward=1" "[ \"\$(sysctl -n net.ipv4.ip_forward)\" = 1 ]"
    fi
    chk "submit/claim router active" "systemctl is-active --quiet srnet-seller-submit"
    if [ -n "${PAYOUT:-}" ]; then
      chk "claim endpoint active" "systemctl is-active --quiet srnet-seller-claimd"
      chk "claim endpoint listening on [$CLAIM_ADDR]:80" "ss -Hltn | grep -qF '[$CLAIM_ADDR]:80'"
    fi
    chk "listing refresh timer active (hourly)" "systemctl is-active --quiet srnet-seller-listing.timer"
    if [ -n "${PAYOUT:-}" ]; then chk "srnet-seller-watcher active" "systemctl is-active --quiet srnet-seller-watcher"; fi
  fi
  if [ "${FIREWALL:-1}" = 1 ] && command -v ufw >/dev/null; then
    chk "ufw active" "ufw status | grep -q 'Status: active'"
    chk "ufw allows udp/${PORT}" "ufw status | grep -q '${PORT}/udp'"
    [ -n "${PAYOUT:-}" ] && chk "ufw allows claims on sr-tun-submit" "ufw status | grep -q 'on sr-tun-submit'"
  fi
  [ -n "${PAYOUT:-}" ] && chk "claim desk settings hold no RPC URLs" "! grep -q 'rpc\\|http' '$P/config/claimd.env'"
  [ -n "${PAYOUT:-}" ] && chk "quote key 0600" "[ \"\$(stat -c %a '$P/config/quote.key')\" = 600 ]"
  [ -n "${PAYOUT:-}" ] && chk "keys split: issuer key $WATCH_USER, quote key $CLAIM_USER" "[ \"\$(stat -c %U '$P/config/issuer.key')\" = $WATCH_USER ] && [ \"\$(stat -c %U '$P/config/quote.key')\" = $CLAIM_USER ]"
  [ -n "${PAYOUT:-}" ] && chk "listing quote_key matches quote.key" "python3 -c 'import json,sys;sys.path.insert(0,sys.argv[1]);import quote as Q;sys.exit(json.load(open(sys.argv[2]))[\"listing\"].get(\"quote_key\")!=Q.pub_hex(Q.load_key(sys.argv[3])))' '$P/pack' '$P/listing/listing.json' '$P/config/quote.key'"
  [ "$fails" = 0 ] && echo "health: OK" || echo "health: $fails check(s) failed"
  return "$fails"
}
if [ "$ACTION" = health ]; then health; exit $?; fi

# ----------------------------------------------------------------- validate
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "bad --listen-port"
[ ${#CHAINS[@]} -gt 0 ] || CHAINS=(arbitrum base)
TESTNET_FLAG=""; [ "$TESTNETS" = 1 ] && TESTNET_FLAG=--testnets
CHAIN_IDS=$(python3 - "$HERE" "$TESTNETS" "${CHAINS[@]}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import chains as C
try:
    print(" ".join(str(c) for c in sorted({C.by_name(n, sys.argv[2] == "1") for n in sys.argv[3:]})))
except C.ChainError as e:
    sys.exit(f"install.sh: {e}")
PY
) || exit 1
MAINNET=0; for c in $CHAIN_IDS; do case $c in 42161|8453) MAINNET=1;; esac; done
[ "$MAINNET" = 1 ] && warn "real USDC: buyers pay your --payout address directly. Keep the cap small."
[[ "$MAX_HOURS" =~ ^[1-9][0-9]{0,2}$ ]] || die "bad --max-hours (whole hours, 1..999)"
[[ "$PRICE" =~ ^[0-9]+([.][0-9]{1,6})?$ ]] || die "bad --price-per-hour-usdc"
MIN_RATE=$(python3 -c "from decimal import Decimal as D;import sys;print(int(D(sys.argv[1])*1000000))" "$PRICE")
[ "$MIN_RATE" -ge 100000 ] || die "--price-per-hour-usdc must be at least 0.10"
[[ "$CAP_USDC" =~ ^[0-9]+([.][0-9]{1,6})?$ ]] || die "bad --cap-usdc"
[[ "$QUOTES_PER_HOUR" =~ ^[1-9][0-9]{0,4}$ ]] || die "bad --quotes-per-hour (a whole number, at least 1)"
RPC_ARGS=""
for r in "${RPCS[@]}"; do
  [[ "$r" =~ ^[0-9]+=https?://[A-Za-z0-9._:/?=%-]+$ ]] || die "bad --rpc (CHAINID=URL): $r"
  [[ " $CHAIN_IDS " == *" ${r%%=*} "* ]] || die "--rpc for chain ${r%%=*}, which is not one of your chains ($CHAIN_IDS)"
  RPC_ARGS="$RPC_ARGS --rpc $r"
done
[[ "$REGION" =~ ^[A-Za-z0-9._-]{0,64}$ ]] || die "bad --region (alnum/._- only, max 64)"
[[ "$MAX_MBIT" =~ ^[0-9]{1,5}$ ]] || die "bad --max-mbit"
# Country: --country wins; else ask two geo-IP services over IPv4 and require them to agree.
GEOIP_URLS=${SRNET_GEOIP_URLS:-"https://ipinfo.io/country https://ifconfig.co/country-iso"}
detect_country() {
  local u c out=""
  for u in $GEOIP_URLS; do
    c=$(curl -4 -fsS --proto '=https,file' --max-time 10 "$u" 2>/dev/null | tr -d '[:space:]' | tr a-z A-Z) || return 1
    [[ "$c" =~ ^[A-Z]{2}$ ]] || return 1
    [ -z "$out" ] || [ "$out" = "$c" ] || { warn "geo-IP services disagree ($out vs $c)"; return 1; }
    out=$c
  done
  [ -n "$out" ] && echo "$out"
}
if [ "$ACTION" = install ]; then
  if [ -z "$COUNTRY" ]; then
    COUNTRY=$(detect_country) || COUNTRY=""
    [ -n "$COUNTRY" ] || die "could not detect the exit's country; pass --country XX (ISO 3166-1 alpha-2)"
    say "country detected from public IPv4: $COUNTRY (override with --country XX)"
  fi
  COUNTRY=${COUNTRY^^}
  [[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] || die "bad --country (ISO 3166-1 alpha-2, e.g. US)"
fi
[[ "$SPEED_CAP" =~ ^[0-9]{1,5}$ ]] || die "bad --speed-cap-mbps (whole Mbit/s, 0 = unlimited)"
SPEED_CAP=$((10#$SPEED_CAP))
[ -z "$WAN_IF" ] || [[ "$WAN_IF" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$ ]] || die "bad --wan-if"
[ -z "$SSH_PORT" ] || [[ "$SSH_PORT" =~ ^[0-9]{1,5}$ ]] || die "bad --ssh-port"
[ -z "$PAYOUT" ] || [[ "$PAYOUT" =~ ^0x[0-9a-fA-F]{40}$ ]] || die "bad --payout (an address: 0x + 40 hex; never a private key)"
[[ "$PAYOUT" =~ ^0x0{40}$ ]] && die "--payout is the zero address"
DEFAULT_BOARD_URL=http://exitsrjnc83har7a17mdtjpgi1j3odgiphaffe9ejfq4wnbdxkio.sesh/submit
if [ "$BOARD_URL" = default ]; then [ "$TESTNETS" = 0 ] && BOARD_URL=$DEFAULT_BOARD_URL || BOARD_URL=""; fi
[ "$BOARD_URL" = none ] && BOARD_URL=""
[ -z "$BOARD_URL" ] || [[ "$BOARD_URL" =~ ^https?://[A-Za-z0-9._:/?=%-]+$ ]] || die "bad --board-submit-url"
if [ "$ENABLE_EXIT" = 1 ] && [ "$ACK" != 1 ]; then
  cat >&2 <<'TXT'
LEGAL: Running an exit forwards other people's traffic from YOUR IP.
Re-run with --i-accept-exit-operator-responsibility if you accept this.
Only tcp/80 and tcp/443 are forwarded to the internet; buyers' DNS is answered by
the exit's own resolver (tunnel only, no query logs) and never forwarded elsewhere.
TXT
  exit 2
fi
[ -z "$MODE" ] && MODE=source
[ "$MODE" = binary ] && { [ -f "$BINARY" ] || die "--binary $BINARY not found"; }

[ "$DRY" = 1 ] && { mkdir -p "$R"; : > "$R/commands.log"; }
mkdir -p "$P/bin" "$P/config" "$P/run" "$P/watch" "$P/watch/admit" "$P/data" "$P/claims" "$P/logs" "$P/pack" "$P/listing" "$R/etc/systemd/system" \
  "$R/etc/sysctl.d" "$R/usr/local/sbin"

# ----------------------------------------------------------------- packages
APT_RUNTIME="python3 python3-cryptography nftables ufw iproute2 ca-certificates curl tar"
# Shared libraries the release (prebuilt) router binary links against on Ubuntu 24.04.
APT_ROUTER_LIBS="libevent-core-2.1-7t64 libevent-pthreads-2.1-7t64 libunbound8 libspdlog1.12 libfmt9 libsodium23 libgnutls30t64 libnettle8t64 libzstd1"
[ "$MODE" = source ] || APT_RUNTIME="$APT_RUNTIME $APT_ROUTER_LIBS"
DNS_PKG_NEW=0
if [ "$ENABLE_EXIT" = 1 ] && [ "$DNS" = 1 ]; then
  [ "$DRY" = 0 ] && ! dpkg -s unbound >/dev/null 2>&1 && DNS_PKG_NEW=1
  APT_RUNTIME="$APT_RUNTIME unbound dns-root-data"
else
  DNS=0
fi
say "packages: $APT_RUNTIME"
if [ "$DRY" = 0 ] && command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q || die "apt-get update failed"
  missing=""; for p in $APT_RUNTIME; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
  if [ -n "$missing" ]; then apt-get install -y -q --no-install-recommends $missing || die "could not install:$missing"; fi
else
  run apt-get update -q
  run apt-get install -y --no-install-recommends $APT_RUNTIME
fi

# ----------------------------------------------------------------- users
# The claim desk (network-facing) runs as its own user: it holds the quote key but can't read the
# admission issuer key or the watcher's record of used payments. It shares only the claims dir
# with the watcher, through the $CLAIMS_GROUP group.
say "service users: $EXIT_USER (router), $WATCH_USER (watcher), $CLAIM_USER (claim desk), group $GROUP"
if [ "$DRY" = 1 ]; then
  run groupadd --system "$GROUP"
  run groupadd --system "$CLAIMS_GROUP"
  run useradd --system --no-create-home --home-dir "$PREFIX/data" --shell /usr/sbin/nologin -G "$GROUP" "$EXIT_USER"
  run useradd --system --no-create-home --home-dir "$PREFIX/run" --shell /usr/sbin/nologin -G "$GROUP,$CLAIMS_GROUP" "$WATCH_USER"
  run useradd --system --no-create-home --home-dir "$PREFIX/claims" --shell /usr/sbin/nologin -G "$GROUP,$CLAIMS_GROUP" "$CLAIM_USER"
else
  getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
  getent group "$CLAIMS_GROUP" >/dev/null || groupadd --system "$CLAIMS_GROUP"
  id "$EXIT_USER" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$PREFIX/data" --shell /usr/sbin/nologin -G "$GROUP" "$EXIT_USER"
  id "$WATCH_USER" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$PREFIX/run" --shell /usr/sbin/nologin -G "$GROUP,$CLAIMS_GROUP" "$WATCH_USER"
  id -nG "$WATCH_USER" | grep -qw "$CLAIMS_GROUP" || usermod -aG "$CLAIMS_GROUP" "$WATCH_USER"   # upgrades
  id "$CLAIM_USER" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$PREFIX/claims" --shell /usr/sbin/nologin -G "$GROUP,$CLAIMS_GROUP" "$CLAIM_USER"
fi

# ----------------------------------------------------------------- binary
sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
case "$MODE" in
  binary)
    SRC_BIN=$BINARY
    if [ -n "$BINARY_SHA" ]; then [ "$(sha256_of "$SRC_BIN")" = "$BINARY_SHA" ] || die "--binary sha256 mismatch"; fi;;
  prebuilt)
    URL=${PREBUILT_ARG:-$PREBUILT_URL}
    [ -n "$URL" ] || die "no prebuilt binary is pinned in this copy of the pack: use the release bootstrap.sh, --prebuilt URL --prebuilt-sha256 HEX, --source (default) or --binary"
    [[ "$PREBUILT_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "--prebuilt needs a pinned sha256 (PREBUILT_SHA256 or --prebuilt-sha256)"
    if [ -f "$URL" ]; then cp "$URL" "$TMPD/session-router"
    elif [ "$DRY" = 1 ]; then run curl -fsSL -o "$TMPD/session-router" "$URL"; SRC_BIN=""
    else curl -fsSL --proto '=https' --tlsv1.2 -o "$TMPD/session-router" "$URL"; fi
    if [ -f "$TMPD/session-router" ]; then
      GOT=$(sha256_of "$TMPD/session-router")
      [ "$GOT" = "$PREBUILT_SHA256" ] || die "prebuilt sha256 mismatch: got $GOT, pinned $PREBUILT_SHA256"
      SRC_BIN=$TMPD/session-router
    fi;;
  source)
    BUILD_SH=""; for c in "$HERE/build-session-router.sh" "$HERE/../scripts/build-session-router.sh"; do [ -f "$c" ] && BUILD_SH=$c && break; done
    [ -n "$BUILD_SH" ] || die "build-session-router.sh not found"
    if [ "$DRY" = 1 ]; then
      run bash "$BUILD_SH" --install-deps --out "$PREFIX/build"; SRC_BIN=""
    else
      SWAP=""
      mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo)
      if [ "$mem_kb" -lt 3500000 ] && [ ! -e /srnet-build.swap ]; then
        say "low memory ($((mem_kb/1024)) MB): adding a temporary 3 GB swap file for the build"
        if fallocate -l 3G /srnet-build.swap 2>/dev/null && chmod 600 /srnet-build.swap && mkswap -q /srnet-build.swap && swapon /srnet-build.swap 2>/dev/null; then
          SWAP=/srnet-build.swap
        else rm -f /srnet-build.swap; warn "could not add swap; the build may run out of memory"; fi
      fi
      jobs=$(nproc); [ "$mem_kb" -lt 3500000 ] && jobs=1
      say "building Session Router from the pinned public upstream (this takes a while)"
      bash "$BUILD_SH" --install-deps --jobs "$jobs" --out "$P/build" --work "$TMPD/work"
      [ -n "$SWAP" ] && { swapoff "$SWAP" || true; rm -f "$SWAP"; }
      SRC_BIN=$P/build/session-router
    fi;;
esac
if [ -n "${SRC_BIN:-}" ]; then
  # Refuse builds without lease admission + inbound auth enforcement (stock routers admit everyone).
  grep -aq 'srnet-exit-admission/v2' "$SRC_BIN" || die "binary lacks signed lease admission (exit patch); refusing"
  grep -aq 'Inbound session rejected by auth policy' "$SRC_BIN" || die "binary lacks inbound auth enforcement; refusing"
  install -m 755 "$SRC_BIN" "$P/bin/session-router"
  BIN_SHA=$(sha256_of "$P/bin/session-router")
  if [ "$DRY" = 0 ] && command -v ldd >/dev/null; then
    MISSING_LIBS=$(ldd "$P/bin/session-router" 2>/dev/null | awk '/not found/{print $1}' | tr '\n' ' ')
    [ -z "$MISSING_LIBS" ] || die "session-router is missing shared libraries: $MISSING_LIBS(install them with apt-get, or use --source)"
  fi
else
  BIN_SHA="(dry-run)"
fi
say "session-router sha256: $BIN_SHA"

# ----------------------------------------------------------------- pack files
for f in watcher.py claimd.py evm.py listing.py chains.py quote.py keccak256.py seshdns.py dnscheck.py; do install -m 755 "$HERE/$f" "$P/pack/$f"; done
install -m 644 "$HERE/VERSION" "$P/pack/VERSION"
install -m 755 "$ADMISSION_PY" "$P/pack/admission.py"
install -m 755 "$0" "$P/pack/install.sh"
[ -f "$HERE/build-session-router.sh" ] && install -m 755 "$HERE/build-session-router.sh" "$P/pack/"
cat > "$R/usr/local/sbin/srnet-seller" <<SH
#!/bin/sh
# srnet-seller status|health|listing|logs|held|admit <quote id>|uninstall [--purge]
set -e
if [ "\$(id -u)" != 0 ]; then
  echo "srnet-seller needs root. Run it with sudo: sudo srnet-seller \${*:-status}" >&2; exit 1
fi
case "\${1:-status}" in
  status) systemctl --no-pager status srnet-seller-exit srnet-seller-watcher srnet-seller-nat || true;;
  health) exec $PREFIX/pack/install.sh --health;;
  listing) exec cat $PREFIX/listing/listing.json;;
  logs) exec journalctl -u srnet-seller-exit -u srnet-seller-watcher -n 200 --no-pager;;
  held) exec runuser -u $WATCH_USER -- python3 $PREFIX/pack/watcher.py held --state $PREFIX/watch/watcher-state.json;;
  admit) [ -n "\${2:-}" ] || { echo "usage: srnet-seller admit <quote id>" >&2; exit 2; }
    exec runuser -u $WATCH_USER -- python3 $PREFIX/pack/watcher.py admit --state $PREFIX/watch/watcher-state.json --quote "\$2";;
  uninstall) shift; exec $PREFIX/pack/install.sh --uninstall "\$@";;
  *) echo "usage: srnet-seller status|health|listing|logs|held|admit <quote id>|uninstall [--purge]" >&2; exit 2;;
esac
SH
chmod 755 "$R/usr/local/sbin/srnet-seller"

# ----------------------------------------------------------------- keys
# Issuer key (signs lease admissions): kept across reinstalls.
if [ ! -f "$P/config/issuer.key" ]; then
  python3 "$P/pack/admission.py" keygen --out "$P/config/issuer.key" > "$P/config/issuer.pub"
fi
chmod 600 "$P/config/issuer.key"; chmod 644 "$P/config/issuer.pub"
ISSUER_PUB=$(tr -d '\n' < "$P/config/issuer.pub")
[ -f "$P/run/admissions.json" ] || echo '{"admissions":{}}' > "$P/run/admissions.json"
chmod 640 "$P/run/admissions.json"

# Router keyfiles (128 hex = seed || pubkey). exit.key's .sesh is the seller id; claim.key is the
# submit/claim router, whose .sesh is the claim endpoint in the listing.
new_router_key() {
  [ -f "$1" ] || python3 - "$1" <<'PY'
import os, secrets, sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
seed = secrets.token_bytes(32)
pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, ((seed + pub).hex() + "\n").encode()); os.close(fd)
PY
  chmod 600 "$1"
}
new_router_key "$P/config/exit.key"
new_router_key "$P/config/claim.key"
# Quote key: signs payment quotes on the claim host; its public half is in the listing.
QUOTE_PUB=$(python3 -c "import sys;sys.path.insert(0,sys.argv[1]);import quote as Q;print(Q.new_key_file(sys.argv[2]))" "$P/pack" "$P/config/quote.key")
chmod 600 "$P/config/quote.key"

# ----------------------------------------------------------------- policy + config
DNS_ADV=""; [ "$DNS" = 1 ] && DNS_ADV=$DNS_IP
python3 - "$P/config/seller-policy.json" "$PRICE" "$CAP_USDC" "$MAX_HOURS" "$CHAIN_IDS" \
  "${PAYOUT:-0x0000000000000000000000000000000000000000}" "$REGION" "$MAX_MBIT" "$SPEED_CAP" "$COUNTRY" "$DNS_ADV" <<'PY'
import json, sys
from pathlib import Path
path, price, cap, max_hours, chain_ids, payout, region, mbit, speed, country, dns = sys.argv[1:12]
Path(path).write_text(json.dumps({
    "price_per_hour_usdc": price, "cap_usdc": cap, "max_hours": int(max_hours),
    "chains": [int(c) for c in chain_ids.split()], "token": "USDC", "payout": payout, "country": country,
    "region": region,
    "ports": ["tcp/443", "tcp/80"], "dns_forwarding": False, "dns_resolver": dns or None,
    "max_mbit": int(mbit), "speed_cap_mbps": int(speed),
    "notes": "Only tcp/80 and tcp/443 are forwarded to the internet. "
             + ("DNS: the exit's own recursive resolver at " + dns + " (tunnel only; resolves from the root "
                "servers, no forwarder, no query logs). " if dns else "No exit resolver. ")
             + "DNS to any other server is never forwarded.",
}, indent=2) + "\n")
PY

if [ "$ENABLE_EXIT" = 1 ]; then
  EXIT_BLOCK="[exit]
enable=true
routed-range=public
# Web traffic to the internet. udp/tcp 53 only reach the exit's own resolver ($DNS_IP):
# nftables drops forwarded DNS to anywhere else.
policy=tcp/443
policy=tcp/80"
  [ "$DNS" = 1 ] && EXIT_BLOCK="$EXIT_BLOCK
policy=udp/53
policy=tcp/53"
else
  EXIT_BLOCK="[exit]
enable=false"
fi
cat > "$P/config/exit.ini" <<INI
# Generated by the SRNet seller pack $PACK_VERSION. Lease-gated: empty whitelist,
# signed admissions only (SR_EXIT_LEASE_* in the systemd unit).
[router]
data-dir=$PREFIX/data

[logging]
level=info

$EXIT_BLOCK

[network]
keyfile=$PREFIX/config/exit.key
ifname=$TUN
ifaddr=10.193.0.1/16
ifaddr=fd2e:7365:7369::1/64
reachable=true
auth-type=whitelist
auto-routing=false
blackhole-routes=false

[api]
enabled=true
bind=127.0.0.1:1192

[bind]
listen=:$PORT

[dns]
# No router DNS listener. Buyers' lookups go to the exit resolver (srnet-seller-dns, unbound on
# $DNS_IP) through the tunnel; port 53 is never forwarded to other servers.
listen=
INI

# Small client-only router: sends the hourly listing to the board and takes buyers' lease claims
# (claimd.py on its tun address). The exit identity can't take replies or unadmitted sessions,
# so both go through this one. No exit, DNS on loopback only.
cat > "$P/config/submit.ini" <<INI
[router]
data-dir=$PREFIX/data/submit

[logging]
level=warning

[network]
reachable=true
keyfile=$PREFIX/config/claim.key
ifname=sr-tun-submit
ifaddr=10.194.0.1/16
ifaddr=$CLAIM_ADDR/64
auto-routing=false
blackhole-routes=false

[bind]
listen=:$((PORT + 1))

[dns]
listen=127.0.0.1:5354

[api]
enabled=false
INI

# ----------------------------------------------------------------- ownership
run chown -R root:"$GROUP" "$PREFIX"
run chmod 750 "$PREFIX" "$PREFIX/config"
run chown "$EXIT_USER":"$EXIT_USER" "$PREFIX/config/exit.key" "$PREFIX/config/claim.key"
mkdir -p "$P/data/submit"; run chown -R "$EXIT_USER":"$EXIT_USER" "$PREFIX/data"  # -R: re-runs keep router state writable
run chmod 700 "$PREFIX/data"
run chown "$WATCH_USER":"$WATCH_USER" "$PREFIX/config/issuer.key"
run chown "$CLAIM_USER":"$CLAIM_USER" "$PREFIX/config/quote.key"
# used payments: the watcher's alone (moved out of the shared claims dir on upgrade)
if [ -f "$P/claims/watcher-state.json" ] && [ ! -f "$P/watch/watcher-state.json" ]; then
  run mv "$PREFIX/claims/watcher-state.json" "$PREFIX/watch/watcher-state.json"
fi
run chown -R "$WATCH_USER":"$WATCH_USER" "$PREFIX/watch"   # watch/admit too: `srnet-seller admit` writes there
run chmod 700 "$PREFIX/watch" "$PREFIX/watch/admit"
run chown -R "$WATCH_USER":"$GROUP" "$PREFIX/run" "$PREFIX/logs"
run chown -R "$CLAIM_USER":"$CLAIMS_GROUP" "$PREFIX/claims"
run chmod -R u=rwX,g=rwX,o= "$PREFIX/claims"
run find "$PREFIX/claims" -type d -exec chmod g+s {} +
run chown -R "$EXIT_USER":"$GROUP" "$PREFIX/listing"
run chmod 755 "$PREFIX/listing"
run chmod 750 "$PREFIX/run" "$PREFIX/logs"
run chmod 640 "$PREFIX/config/exit.ini" "$PREFIX/config/seller-policy.json"

# ----------------------------------------------------------------- listing
[ -n "$PAYOUT" ] || warn "no --payout: the listing has a placeholder address and buyers can't pay; re-run with --payout 0xADDRESS"
CHAIN_ARGS=""; for c in $CHAIN_IDS; do CHAIN_ARGS="$CHAIN_ARGS --chain $c"; done
CLAIM_SESH=$(python3 -c "import sys;sys.path.insert(0,sys.argv[1]);import listing as L;k=bytes.fromhex(open(sys.argv[2]).read().strip());print(L.sesh_of(k[32:]))" "$P/pack" "$P/config/claim.key")
python3 "$P/pack/listing.py" make --identity "$P/config/exit.key" --price-per-hour-usdc "$PRICE" \
  --max-hours "$MAX_HOURS" --payout "${PAYOUT:-0x0000000000000000000000000000000000000000}" $( [ -n "$PAYOUT" ] || echo --draft ) \
  $CHAIN_ARGS $TESTNET_FLAG --quote-key-file "$P/config/quote.key" \
  --country "$COUNTRY" --region "$REGION" --speed-cap-mbps "$SPEED_CAP" --policy tcp/443 --policy tcp/80 --claim-url "http://$CLAIM_SESH/claim" \
  ${DNS_ADV:+--dns "$DNS_ADV"} > "$P/listing/listing.json.new"
# Keep the listing being replaced (with the time, stored in the file) so the watcher still
# honours quotes made under the old price or max hours.
[ -f "$P/listing/listing.json" ] && python3 "$P/pack/listing.py" retire --listing "$P/listing/listing.json" >/dev/null
mv "$P/listing/listing.json.new" "$P/listing/listing.json"
chmod 644 "$P/listing/listing.json"
ln -sfn ../listing/listing.json "$P/config/listing.json"
SELLER_SESH=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['listing']['seller'])" "$P/listing/listing.json")
# Hourly listing refresh (liveness): optional board submit URL.
cat > "$P/config/listing.env" <<ENV
# Board endpoint for the hourly listing refresh (empty: re-sign locally only).
BOARD_SUBMIT_URL=$BOARD_URL
SRNET_SESH_DNS=127.0.0.1:5354
ENV
chmod 644 "$P/config/listing.env"

if [ -n "$WATCHER_ENV" ]; then
  install -m 640 "$WATCHER_ENV" "$P/config/watcher.env"
else
  cat > "$P/config/watcher.env" <<ENV
# SRNet seller watcher and claim endpoint. No wallet key is needed: payments go straight to the
# payout address in the listing, and the watcher only reads the chain.
SELLER_SESH=$SELLER_SESH
CAP_USDC=$CAP_USDC
# Extra watcher/claimd flags: your own RPCs (--rpc CHAINID=URL), --testnets.
CHAIN_FLAGS="$RPC_ARGS $TESTNET_FLAG"
ENV
fi
run chown root:"$WATCH_USER" "$PREFIX/config/watcher.env"
chmod 640 "$P/config/watcher.env"
# The claim desk gets its own settings only (no RPC URLs, which may hold API keys).
cat > "$P/config/claimd.env" <<ENV
# SRNet seller claim endpoint.
CAP_USDC=$CAP_USDC
# New quotes per hour overall. Above 267 a busy desk may refuse some cheap purchases for a while.
QUOTES_PER_HOUR=$QUOTES_PER_HOUR
# 1: log each quote's salted source tag (never the address), to check that buyers show up apart.
SRNET_CLAIM_DEBUG_SOURCES=0
ENV
run chown root:"$CLAIMS_GROUP" "$PREFIX/config/claimd.env"
chmod 640 "$P/config/claimd.env"

# ----------------------------------------------------------------- network
if [ -z "$WAN_IF" ]; then
  if [ "$DRY" = 1 ]; then WAN_IF=eth0
  else WAN_IF=$(ip -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p'); fi
fi
[[ "$WAN_IF" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$ ]] || die "could not detect the WAN interface; pass --wan-if"
if [ -z "$SSH_PORT" ]; then
  SSH_PORT=$( { [ "$DRY" = 0 ] && sshd -T 2>/dev/null | awk '/^port /{print $2; exit}'; } || true)
  SSH_PORT=${SSH_PORT:-22}
fi

# nftables (own table; loaded by srnet-seller-nat.service at boot):
# - forward from the TUN: drop DNS, drop private/link-local/metadata ranges, allow only tcp 80/443
# - input from the TUN: only replies, plus DNS to the exit resolver ($DNS_IP), rate limited per buyer
#   (buyers cannot reach other host services); the resolver address is not reachable from outside
# - masquerade the TUN's IPv4 range out of the WAN interface. IPv6 is not forwarded.
# - per-buyer speed cap: the router gives each admitted buyer its own TUN address (and maps no
#   address for anyone not admitted), so a rate limit keyed by that address is a per-buyer cap.
CAP_SETS=""; CAP_UP=""; CAP_DOWN=""
if [ "$SPEED_CAP" -gt 0 ]; then
  CAP_KB=$((SPEED_CAP * 125)); CAP_BURST=$(( CAP_KB / 4 > 64 ? CAP_KB / 4 : 64 ))
  CAP_SETS="  set cap_up4 { type ipv4_addr; size 65535; flags dynamic,timeout; timeout 10m; }
  set cap_down4 { type ipv4_addr; size 65535; flags dynamic,timeout; timeout 10m; }"
  CAP_UP="    iifname \"$TUN\" oifname \"$WAN_IF\" tcp dport { 80, 443 } update @cap_up4 { ip saddr limit rate over $CAP_KB kbytes/second burst $CAP_BURST kbytes } drop"
  CAP_DOWN="    oifname \"$TUN\" ct state established,related update @cap_down4 { ip daddr limit rate over $CAP_KB kbytes/second burst $CAP_BURST kbytes } drop"
fi
DNS_SET=""; DNS_IN=""; DNS_GUARD=""
if [ "$DNS" = 1 ]; then
  DNS_SET="  set dns_rl4 { type ipv4_addr; size 65535; flags dynamic,timeout; timeout 1m; }"
  DNS_IN="    iifname \"$TUN\" ip daddr $DNS_IP meta l4proto { tcp, udp } th dport 53 update @dns_rl4 { ip saddr limit rate over 100/second burst 200 packets } drop
    iifname \"$TUN\" ip daddr $DNS_IP meta l4proto { tcp, udp } th dport 53 accept"
  DNS_GUARD="    ip daddr $DNS_IP iifname != { \"$TUN\", \"lo\" } drop"
fi
cat > "$P/config/nat.nft" <<NFT
table inet srnet_seller
delete table inet srnet_seller
table inet srnet_seller {
  set blocked4 {
    type ipv4_addr; flags interval
    elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12,
                 192.0.0.0/24, 192.168.0.0/16, 198.18.0.0/15, 224.0.0.0/3 }
  }
$CAP_SETS
$DNS_SET
  chain forward_filter {
    type filter hook forward priority -10; policy accept;
    iifname "$TUN" meta l4proto { tcp, udp } th dport 53 drop
    iifname "$TUN" ip daddr @blocked4 drop
    iifname "$TUN" meta nfproto ipv6 drop
$CAP_UP
    iifname "$TUN" oifname "$WAN_IF" tcp dport { 80, 443 } accept
    iifname "$TUN" drop
$CAP_DOWN
    oifname "$TUN" ct state established,related accept
    oifname "$TUN" drop
  }
  chain input_filter {
    type filter hook input priority -10; policy accept;
$DNS_GUARD
    iifname "$TUN" ct state established,related accept
$DNS_IN
    iifname "$TUN" drop
  }
  chain postrouting_nat {
    type nat hook postrouting priority 100;
    ip saddr $TUN_NET4 oifname "$WAN_IF" masquerade
  }
}
NFT

# bandwidth cap on the TUN (traffic towards buyers), applied after the TUN appears.
cat > "$P/bin/srnet-seller-shape" <<SH
#!/bin/sh
# Applies a $MAX_MBIT Mbit/s cap on $TUN (0 = off). Non-fatal and quiet: waits up to 60 s for the
# TUN; if the router never brings it up (it failed to start), it exits without logging errors.
[ "$MAX_MBIT" -gt 0 ] || exit 0
i=0; while [ \$i -lt 60 ] && ! ip link show $TUN >/dev/null 2>&1; do sleep 1; i=\$((i+1)); done
ip link show $TUN >/dev/null 2>&1 || exit 0
tc qdisc replace dev $TUN root cake bandwidth ${MAX_MBIT}mbit 2>/dev/null || \
  tc qdisc replace dev $TUN root tbf rate ${MAX_MBIT}mbit burst 256kb latency 100ms 2>/dev/null || true
exit 0
SH
chmod 755 "$P/bin/srnet-seller-shape"

# ----------------------------------------------------------------- systemd units
cat > "$R/etc/systemd/system/srnet-seller-exit.service" <<UNIT
[Unit]
Description=SRNet seller exit (lease-gated Session Router)
After=network-online.target srnet-seller-nat.service
Wants=network-online.target

[Service]
Type=simple
User=$EXIT_USER
Group=$EXIT_USER
SupplementaryGroups=$GROUP
WorkingDirectory=$PREFIX/data
Environment=SR_EXIT_LEASE_ADMISSIONS=$PREFIX/run/admissions.json
Environment=SR_EXIT_LEASE_ISSUER_PUBKEY=$ISSUER_PUB
ExecStart=$PREFIX/bin/session-router $PREFIX/config/exit.ini
ExecStartPost=+$PREFIX/bin/srnet-seller-shape
Restart=on-failure
RestartSec=5
# Only CAP_NET_ADMIN (TUN + addresses). Nothing else.
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
NoNewPrivileges=yes
DevicePolicy=closed
DeviceAllow=/dev/net/tun rw
ProtectSystem=strict
ReadWritePaths=$PREFIX/data
ReadOnlyPaths=$PREFIX/run $PREFIX/config
ProtectHome=yes
PrivateTmp=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
SystemCallArchitectures=native
LimitNOFILE=65536
TasksMax=512
MemoryMax=1536M

[Install]
WantedBy=multi-user.target
UNIT

cat > "$R/etc/systemd/system/srnet-seller-watcher.service" <<UNIT
[Unit]
Description=SRNet seller payment watcher
After=network-online.target srnet-seller-exit.service
Wants=network-online.target

[Service]
Type=simple
User=$WATCH_USER
Group=$GROUP
SupplementaryGroups=$CLAIMS_GROUP
EnvironmentFile=$PREFIX/config/watcher.env
Environment=PATH=$PREFIX/bin:/usr/bin:/bin
Environment=HOME=$PREFIX/run
ExecStart=/usr/bin/python3 $PREFIX/pack/watcher.py --dir $PREFIX/claims --seller \${SELLER_SESH} \\
  --listing $PREFIX/listing/listing.json --issuer-key $PREFIX/config/issuer.key --store $PREFIX/run/admissions.json \\
  --state $PREFIX/watch/watcher-state.json \$CHAIN_FLAGS
Restart=on-failure
RestartSec=10
UMask=0007
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateDevices=yes
ProtectSystem=strict
ReadWritePaths=$PREFIX/run $PREFIX/watch $PREFIX/claims $PREFIX/logs
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
MemoryMax=512M

[Install]
WantedBy=multi-user.target
UNIT

cat > "$R/etc/systemd/system/srnet-seller-submit.service" <<UNIT
[Unit]
Description=SRNet seller submit router (client only, reaches the board)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$EXIT_USER
Group=$EXIT_USER
WorkingDirectory=$PREFIX/data
ExecStart=$PREFIX/bin/session-router $PREFIX/config/submit.ini
Restart=on-failure
RestartSec=10
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
NoNewPrivileges=yes
DevicePolicy=closed
DeviceAllow=/dev/net/tun rw
ProtectSystem=strict
ReadWritePaths=$PREFIX/data
ReadOnlyPaths=$PREFIX/config
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

cat > "$R/etc/systemd/system/srnet-seller-claimd.service" <<UNIT
[Unit]
Description=SRNet seller claim endpoint (buyers' quotes and payments, on the submit router's tun only)
After=srnet-seller-submit.service
Requires=srnet-seller-submit.service

[Service]
Type=simple
User=$CLAIM_USER
Group=$CLAIMS_GROUP
SupplementaryGroups=$GROUP
EnvironmentFile=$PREFIX/config/claimd.env
ExecStart=/usr/bin/python3 $PREFIX/pack/claimd.py --host $CLAIM_ADDR --port 80 --dir $PREFIX/claims \
  --quote-key $PREFIX/config/quote.key --listing $PREFIX/listing/listing.json --cap-usdc \${CAP_USDC} \
  --quotes-per-hour \${QUOTES_PER_HOUR} $TESTNET_FLAG
Restart=always
RestartSec=5
UMask=0007
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
PrivateDevices=yes
ProtectSystem=strict
ReadWritePaths=$PREFIX/claims
InaccessiblePaths=$PREFIX/config/issuer.key $PREFIX/config/watcher.env $PREFIX/run $PREFIX/watch
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6
MemoryMax=128M
TasksMax=64

[Install]
WantedBy=multi-user.target
UNIT

cat > "$R/etc/systemd/system/srnet-seller-listing.service" <<UNIT
[Unit]
Description=SRNet seller listing refresh (re-sign and resubmit)
After=network-online.target
Wants=network-online.target
# A failed board submit (say the submit router is still connecting right after install) is
# tried again every 5 minutes instead of waiting for the next hourly run.
StartLimitIntervalSec=0

[Service]
Type=oneshot
Restart=on-failure
RestartSec=300
User=$EXIT_USER
Group=$GROUP
EnvironmentFile=-$PREFIX/config/listing.env
ExecStart=/usr/bin/python3 $PREFIX/pack/listing.py refresh --identity $PREFIX/config/exit.key --listing $PREFIX/listing/listing.json
UMask=0022
NoNewPrivileges=yes
CapabilityBoundingSet=
PrivateDevices=yes
ProtectSystem=strict
ReadWritePaths=$PREFIX/listing
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UNIT

cat > "$R/etc/systemd/system/srnet-seller-listing.timer" <<UNIT
[Unit]
Description=Refresh the SRNet seller listing every hour (boards hide listings after 3 h without a refresh)

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat > "$R/etc/systemd/system/srnet-seller-nat.service" <<UNIT
[Unit]
Description=SRNet seller exit forwarding rules (nftables, ip_forward)
Before=srnet-seller-exit.service
After=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/sysctl -q -w net.ipv4.ip_forward=1
ExecStart=/usr/sbin/nft -f $PREFIX/config/nat.nft
ExecStop=-/usr/sbin/nft delete table inet srnet_seller

[Install]
WantedBy=multi-user.target
UNIT

# Exit resolver: unbound, recursive from the root servers (no forward-zone), DNSSEC-validating,
# QNAME minimisation, no query/reply logging, answers only buyers' TUN addresses.
if [ "$DNS" = 1 ]; then
  DO6=no; [ "$DRY" = 0 ] && [ -n "$(ip -6 route show default 2>/dev/null)" ] && DO6=yes
  mkdir -p "$R/etc/unbound"
  cat > "$R$DNS_CONF" <<CONF
# Generated by the SRNet seller pack $PACK_VERSION: the exit resolver for leased buyers.
# Recursive from the root servers (there is deliberately no forward-zone), no query logs.
server:
  interface: $DNS_IP
  port: 53
  ip-freebind: yes
  do-ip4: yes
  do-ip6: $DO6
  do-udp: yes
  do-tcp: yes
  access-control: 0.0.0.0/0 refuse
  access-control: ::0/0 refuse
  access-control: $TUN_NET4 allow
  access-control: $DNS_IP/32 allow
  username: "unbound"
  chroot: ""
  directory: "/var/lib/unbound"
  pidfile: ""
  use-syslog: no
  logfile: ""
  verbosity: 0
  log-queries: no
  log-replies: no
  log-tag-queryreply: no
  log-local-actions: no
  log-servfail: no
  val-log-level: 0
  statistics-interval: 0
  extended-statistics: no
  # plain validating recursion (no ECS/subnet module: buyers' addresses never go to authoritative servers)
  module-config: "validator iterator"
  auto-trust-anchor-file: "$DNS_KEY"
  qname-minimisation: yes
  qname-minimisation-strict: no
  aggressive-nsec: yes
  harden-glue: yes
  harden-dnssec-stripped: yes
  harden-below-nxdomain: yes
  hide-identity: yes
  hide-version: yes
  hide-trustanchor: yes
  minimal-responses: yes
  deny-any: yes
  prefetch: yes
  num-threads: 1
  ip-ratelimit: 100
  ratelimit: 1000
  unwanted-reply-threshold: 10000000
  # answers that point public names at private addresses are dropped (DNS rebinding)
  private-address: 10.0.0.0/8
  private-address: 172.16.0.0/12
  private-address: 192.168.0.0/16
  private-address: 169.254.0.0/16
  private-address: 100.64.0.0/10
  private-address: fd00::/8
  private-address: fe80::/10
remote-control:
  control-enable: no
CONF
  chmod 644 "$R$DNS_CONF"
  cat > "$R/etc/systemd/system/srnet-seller-dns.service" <<UNIT
[Unit]
Description=SRNet seller exit resolver (unbound, recursive from the roots, tunnel buyers only, no query logs)
After=network-online.target
Wants=network-online.target
Before=srnet-seller-exit.service

[Service]
Type=simple
ExecStartPre=-/usr/sbin/ip addr add $DNS_IP/32 dev lo
ExecStartPre=/bin/sh -c '[ -s $DNS_KEY ] || cp /usr/share/dns/root.key $DNS_KEY; chown unbound:unbound $DNS_KEY'
ExecStartPre=/usr/sbin/unbound-checkconf $DNS_CONF
ExecStart=/usr/sbin/unbound -d -p -c $DNS_CONF
ExecStopPost=-/usr/sbin/ip addr del $DNS_IP/32 dev lo
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
MemoryMax=256M

[Install]
WantedBy=multi-user.target
UNIT
else
  rm -f "$R/etc/systemd/system/srnet-seller-dns.service" "$R$DNS_CONF"
fi

# Persist install choices for --health / --uninstall.
cat > "$P/config/install.conf" <<CONF
PACK_VERSION=$PACK_VERSION
PORT=$PORT
SPEED_CAP=$SPEED_CAP
WAN_IF=$WAN_IF
SSH_PORT=$SSH_PORT
FIREWALL=$FIREWALL
ENABLE_EXIT=$ENABLE_EXIT
DNS=$DNS
PAYOUT=$PAYOUT
BIN_SHA256=$BIN_SHA
CONF

# ----------------------------------------------------------------- firewall
if [ "$FIREWALL" = 1 ]; then
  say "firewall (ufw): deny incoming except ssh/$SSH_PORT, udp/$PORT, claims on sr-tun-submit and DNS to the exit resolver on $TUN; forward only tcp 80,443 from the exit"
  run ufw allow "$SSH_PORT/tcp" comment srnet-seller-ssh
  run ufw allow "$PORT/udp" comment srnet-seller-router
  run ufw allow in on sr-tun-submit to "$CLAIM_ADDR" port 80 proto tcp comment srnet-seller-claims
  run ufw default deny incoming
  run ufw default allow outgoing
  if [ "$ENABLE_EXIT" = 1 ]; then
    run ufw route allow in on "$TUN" out on "$WAN_IF" proto tcp to any port 80,443 comment srnet-seller-exit
  fi
  if [ "$DNS" = 1 ]; then
    for pr in udp tcp; do run ufw allow in on "$TUN" to "$DNS_IP" port 53 proto $pr comment srnet-seller-dns; done
  else
    for pr in udp tcp; do run ufw delete allow in on "$TUN" to "$DNS_IP" port 53 proto $pr >/dev/null 2>&1 || true; done
  fi
  run ufw --force enable
fi

# ----------------------------------------------------------------- services
EXIT_UNITS="srnet-seller-exit.service srnet-seller-submit.service"
[ -n "$PAYOUT" ] && EXIT_UNITS="$EXIT_UNITS srnet-seller-claimd.service"
if [ "$ENABLE_EXIT" = 1 ]; then
  echo "net.ipv4.ip_forward=1" > "$R/etc/sysctl.d/90-srnet-seller.conf"
  EXIT_UNITS="srnet-seller-nat.service $EXIT_UNITS"
  [ "$DNS" = 1 ] && EXIT_UNITS="srnet-seller-dns.service $EXIT_UNITS"
else
  rm -f "$R/etc/sysctl.d/90-srnet-seller.conf"
  if have_systemd; then systemctl disable --now srnet-seller-nat.service >/dev/null 2>&1 || true; fi
fi
if [ "$DRY" = 1 ]; then
  if [ "$ENABLE_EXIT" = 1 ]; then { echo "[dry-run] nft -f - <<EOF"; cat "$P/config/nat.nft"; echo "EOF"; } >> "$R/commands.log"; fi
  run systemctl daemon-reload
  run systemctl enable --now $EXIT_UNITS srnet-seller-listing.timer
  [ -n "$PAYOUT" ] && run systemctl enable --now srnet-seller-watcher.service
elif [ "$START" = 1 ] && have_systemd; then
  systemctl daemon-reload
  # The distro's own unbound (127.0.0.1) is not ours: if the package was only just installed for the
  # exit resolver, keep it off. A seller who already ran unbound keeps theirs untouched.
  if [ "$DNS_PKG_NEW" = 1 ]; then systemctl disable --now unbound.service unbound-resolvconf.service >/dev/null 2>&1 || true; fi
  [ "$DNS" = 1 ] || systemctl disable --now srnet-seller-dns.service >/dev/null 2>&1 || true
  systemctl enable $EXIT_UNITS >/dev/null
  systemctl restart $EXIT_UNITS
  systemctl enable --now srnet-seller-listing.timer >/dev/null
  if [ -n "$PAYOUT" ]; then systemctl enable srnet-seller-watcher.service >/dev/null; systemctl restart srnet-seller-watcher.service
  else warn "no --payout: watcher and claim endpoint not started (re-run with --payout 0xADDRESS)"; fi
  sleep 3
else
  say "--no-start (or no systemd): services written but not started"
fi

# ----------------------------------------------------------------- summary
echo
echo "=== SRNet seller pack $PACK_VERSION installed under $PREFIX ==="
echo "exit forwarding: $( [ "$ENABLE_EXIT" = 1 ] && echo "ENABLED (tcp/80, tcp/443 only; DNS is never forwarded)" || echo OFF )"
echo "exit resolver:   $( [ "$DNS" = 1 ] && echo "$DNS_IP (unbound: recursive from the roots, DNSSEC, QNAME minimisation, no query logs; tunnel buyers only)" || echo "none" )"
echo "router:          udp/$PORT on $WAN_IF, binary sha256 $BIN_SHA"
echo "seller .sesh:    $SELLER_SESH"
echo "issuer pubkey:   $ISSUER_PUB"
echo "payout address:  ${PAYOUT:-(not set)}   price/hour: $PRICE USDC   chains: $CHAIN_IDS   cap: $CAP_USDC USDC"
echo "signed listing ($PREFIX/listing/listing.json, re-signed hourly${BOARD_URL:+ and sent to $BOARD_URL}):"
python3 "$P/pack/listing.py" verify "$P/listing/listing.json" >/dev/null && cat "$P/listing/listing.json"
if [ -n "$BOARD_URL" ]; then
  echo "board:           the listing is sent to the board automatically (no approval step) and"
  echo "                 appears to buyers within minutes; it is re-sent every hour (every 5 min until it gets through)."
fi
echo "manage:          sudo srnet-seller status | health | listing | logs | held | admit <quote id> | uninstall [--purge]"
echo "payments:        USDC arrives in your payout wallet directly; there is nothing to claim"
if [ "$DRY" = 0 ] && [ "$START" = 1 ] && have_systemd; then echo; health || true; fi
