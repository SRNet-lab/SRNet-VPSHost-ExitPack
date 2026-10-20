#!/usr/bin/env bash
# SRNet exit seller bootstrap: downloads the sha256-pinned seller pack release, asks for the
# payout address and hourly price (everything else has defaults), and runs install.sh.
#
#   sudo bash srnet-bootstrap.sh                     # interactive
#   sudo SRNET_PAYOUT=0x... SRNET_PRICE_PER_HOUR_USDC=1.00 \
#        SRNET_ACCEPT_EXIT_RESPONSIBILITY=yes bash srnet-bootstrap.sh --non-interactive
#
# Optional environment: SRNET_CHAINS ("arbitrum base"), SRNET_CAP_USDC (20), SRNET_COUNTRY (default: detected), SRNET_REGION,
# SRNET_BOARD_SUBMIT_URL, SRNET_BUILD=release|source, SRNET_EXTRA_ARGS (passed to install.sh).
#
# Router binary: the pinned release tarball (session-router-<version>-linux-x86_64.tar.gz) is
# downloaded from GitHub Releases and checked against ROUTER_SHA256, and the binary inside
# against ROUTER_BIN_SHA256. If that fails, or SRNET_BUILD=source, or this bootstrap pins no
# tarball, the router is built from source instead (20-60 min).
set -euo pipefail

RELEASE=@RELEASE@
PACK_URL=@PACK_URL@
PACK_SHA256=@PACK_SHA256@
DEFAULT_BOARD_SUBMIT_URL=@DEFAULT_BOARD_SUBMIT_URL@
ROUTER_URL=@ROUTER_URL@
ROUTER_SHA256=@ROUTER_SHA256@
ROUTER_BIN_SHA256=@ROUTER_BIN_SHA256@

die() { echo "bootstrap: $*" >&2; exit 1; }
NONINT=0; LOCAL_PACK=""; LOCAL_ROUTER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --non-interactive) NONINT=1; shift;;
    --pack) LOCAL_PACK=$2; shift 2;;   # use a local pack tarball (still sha256-checked)
    --router) LOCAL_ROUTER=$2; shift 2;;  # use a local router tarball (still sha256-checked)
    -h|--help) sed -n '2,16p' "$0"; exit 0;;
    *) die "unknown arg $1";;
  esac
done
[ "$(id -u)" = 0 ] || die "run as root (sudo bash $0)"
[ "$(uname -m)" = x86_64 ] || die "x86_64 only"
. /etc/os-release 2>/dev/null || true
case "${ID:-}:${VERSION_ID:-}" in ubuntu:24.04) ;; *) echo "bootstrap: note: tested on Ubuntu 24.04; this is ${PRETTY_NAME:-unknown}" >&2;; esac
[[ "$PACK_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "this bootstrap has no pinned pack hash (unreleased copy); use a release bootstrap"
[ -t 0 ] || NONINT=1

ask() {  # ask VAR "prompt" default validator-regex
  local var=$1 prompt=$2 def=$3 re=$4 val=${!1:-}
  while :; do
    if [ -z "$val" ]; then
      [ "$NONINT" = 1 ] && { [ -n "$def" ] && val=$def || die "$var is required"; } || { read -r -p "$prompt${def:+ [$def]}: " val; val=${val:-$def}; }
    fi
    [[ "$val" =~ $re ]] && break
    echo "  invalid value: $val" >&2; [ "$NONINT" = 1 ] && exit 1; val=""
  done
  printf -v "$var" '%s' "$val"
}

echo "SRNet exit seller setup ($RELEASE)"
ask SRNET_PAYOUT "Payout wallet ADDRESS (0x..., not a private key)" "" '^0x[0-9a-fA-F]{40}$'
[[ "$SRNET_PAYOUT" =~ ^0x0{40}$ ]] && die "SRNET_PAYOUT is the zero address; set your payout wallet"
ask SRNET_PRICE_PER_HOUR_USDC "Price per hour in USDC" "1.00" '^[0-9]+([.][0-9]{1,6})?$'

cat <<'TXT'

LEGAL: an exit forwards other people's web traffic (tcp/80 and tcp/443 only; DNS is never
forwarded: buyers use this exit's own resolver, which keeps no query logs)
to the internet from THIS server's IP address. You are responsible for that traffic under
your local law and your hosting provider's terms of service.
TXT
if [ "${SRNET_ACCEPT_EXIT_RESPONSIBILITY:-}" != yes ]; then
  [ "$NONINT" = 1 ] && die "set SRNET_ACCEPT_EXIT_RESPONSIBILITY=yes to accept (required to run an exit)"
  read -r -p "Type 'yes' to accept and continue: " a; [ "$a" = yes ] || die "not accepted; nothing installed"
fi

W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
if [ -n "$LOCAL_PACK" ]; then cp "$LOCAL_PACK" "$W/pack.tgz"
else
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q || die "apt-get update failed"
  command -v curl >/dev/null || apt-get install -y -q curl ca-certificates || die "could not install curl"
  curl -fsSL --proto '=https' --tlsv1.2 -o "$W/pack.tgz" "$PACK_URL"
fi
echo "$PACK_SHA256  $W/pack.tgz" | sha256sum -c --quiet - || die "seller pack sha256 mismatch; refusing"
tar -xzf "$W/pack.tgz" -C "$W"
D=$(find "$W" -mindepth 1 -maxdepth 1 -type d | head -1)
[ -x "$D/seller/install.sh" ] || die "pack layout unexpected"

ARGS=(--enable-exit --i-accept-exit-operator-responsibility
      --payout "$SRNET_PAYOUT" --price-per-hour-usdc "$SRNET_PRICE_PER_HOUR_USDC")
for c in ${SRNET_CHAINS:-}; do [[ "$c" =~ ^[a-z0-9-]+$ ]] || die "bad SRNET_CHAINS entry $c"; ARGS+=(--chain "$c"); done
[ -n "${SRNET_CAP_USDC:-}" ] && ARGS+=(--cap-usdc "$SRNET_CAP_USDC")
B=${SRNET_BOARD_SUBMIT_URL:-$DEFAULT_BOARD_SUBMIT_URL}; [[ "$B" =~ ^https?:// ]] && ARGS+=(--board-submit-url "$B")
[ -n "${SRNET_REGION:-}" ] && ARGS+=(--region "$SRNET_REGION")
[ -n "${SRNET_COUNTRY:-}" ] && ARGS+=(--country "$SRNET_COUNTRY")
# Pinned release binary, else build from source.
router_release() {
  [[ "$ROUTER_SHA256" =~ ^[0-9a-f]{64}$ && "$ROUTER_BIN_SHA256" =~ ^[0-9a-f]{64}$ ]] || return 1
  if [ -n "$LOCAL_ROUTER" ]; then cp "$LOCAL_ROUTER" "$W/router.tgz" || return 1
  else curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$W/router.tgz" "$ROUTER_URL" || return 1
  fi
  echo "$ROUTER_SHA256  $W/router.tgz" | sha256sum -c --quiet - || { echo "bootstrap: router tarball sha256 mismatch" >&2; return 1; }
  mkdir -p "$W/router" && tar -xzf "$W/router.tgz" -C "$W/router" --no-same-owner || return 1
  RBIN=$(find "$W/router" -mindepth 2 -maxdepth 2 -type f -name session-router | head -1)
  [ -n "$RBIN" ] && echo "$ROUTER_BIN_SHA256  $RBIN" | sha256sum -c --quiet - || { echo "bootstrap: router binary sha256 mismatch" >&2; return 1; }
}
if [ "${SRNET_BUILD:-release}" != source ] && router_release; then
  echo "bootstrap: using release router $ROUTER_BIN_SHA256"
  ARGS+=(--binary "$RBIN" --binary-sha256 "$ROUTER_BIN_SHA256")
else
  [ "${SRNET_BUILD:-release}" = source ] || echo "bootstrap: no verified release binary; building from source" >&2
  ARGS+=(--source)
fi
# shellcheck disable=SC2206
[ -n "${SRNET_EXTRA_ARGS:-}" ] && ARGS+=($SRNET_EXTRA_ARGS)
exec "$D/seller/install.sh" "${ARGS[@]}"
