#!/usr/bin/env bash
# End-to-end test in a throwaway Ubuntu 24.04 container (podman or docker; nothing on the host
# changes). Usage:  tests/test_container.sh [PATH/TO/session-router]
#   phase A (always): dry-run tests, dry-run install, cloud-init schema check (no network)
#   phase B (with a binary): real install under systemd with CAP_NET_ADMIN + /dev/net/tun in the
#            container's own network namespace, health checks, listing refresh, uninstall --purge
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd); ROOT=$(cd "$HERE/.." && pwd)
C=$(command -v podman || command -v docker || true)
[ -n "$C" ] || { echo "no podman/docker; skipping container test"; exit 0; }
[ "$(id -u)" = 0 ] || C="sudo $C"
BIN=${1:-}
BASE=docker.io/library/ubuntu:24.04@sha256:f610ab94648195aa356059f5b41d6085c9d4d903c072430cdd1af7bdb646106b
IMG=localhost/srnet-seller-test:24.04
FOUNDRY=$(sed -n 's/^FOUNDRY_URL=//p' "$HERE/install.sh" | sed "s/\$FOUNDRY_VERSION/$(sed -n 's/^FOUNDRY_VERSION=//p' "$HERE/install.sh")/g; s/\${FOUNDRY_VERSION}/$(sed -n 's/^FOUNDRY_VERSION=//p' "$HERE/install.sh")/g")
FSHA=$(sed -n 's/^FOUNDRY_SHA256=//p' "$HERE/install.sh")
RT=""; [ -n "$BIN" ] && [ -f "$(dirname "$BIN")/RUNTIME-DEPS" ] && RT=$(tr '\n' ' ' < "$(dirname "$BIN")/RUNTIME-DEPS")

CTX=$(mktemp -d); trap 'rm -rf "$CTX"' EXIT
cat > "$CTX/Containerfile" <<CF
FROM $BASE
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q && apt-get install -y -q --no-install-recommends systemd systemd-sysv dbus \\
      python3 python3-cryptography nftables ufw iptables iproute2 ca-certificates curl tar kmod \\
      cloud-init $RT && rm -rf /var/lib/apt/lists/*
# Foundry cast: verify the exact tarball pinned in install.sh (the installer would download it).
RUN curl -fsSL -o /tmp/f.tgz "$FOUNDRY" && echo "$FSHA  /tmp/f.tgz" | sha256sum -c - \\
    && mkdir -p /opt/srnet-seller/bin && tar -xzf /tmp/f.tgz -C /opt/srnet-seller/bin cast && rm /tmp/f.tgz
CMD ["/sbin/init"]
CF
echo "== building test image (network only for package download)"
$C build --network host -q -t "$IMG" "$CTX" >/dev/null

echo "== phase A: dry-run tests + cloud-init schema"
$C run --rm --network none -v "$ROOT:/src:ro" "$IMG" bash -ec '
cp -a /src /w && cd /w
P=$( [ -d seller ] && echo seller || echo seller-pack )
find . -name __pycache__ -prune -exec rm -rf {} +
(cd $P && python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)")
$P/tests/test_install.sh
printf "x srnet-exit-admission/v2 y Inbound session rejected by auth policy z\n" > /tmp/fakebin
$P/install.sh --binary /tmp/fakebin --enable-exit --i-accept-exit-operator-responsibility \
  --payout 0x00000000000000000000000000000000000000aa --dry-run --root /stage >/tmp/out 2>&1
grep -q "SRNet seller pack .* installed" /tmp/out
python3 $P/listing.py verify /stage/opt/srnet-seller/listing/listing.json
if [ -f cloud-init/user-data.yaml ]; then
  sed -e "s|@BOOTSTRAP_URL@|https://example.invalid/bootstrap.sh|g" -e "s|@BOOTSTRAP_SHA256@|$(printf 0%.0s {1..64})|g" \
    cloud-init/user-data.yaml > /tmp/ud.yaml
  cloud-init schema -c /tmp/ud.yaml
fi
echo "phase A: OK"'

[ -n "$BIN" ] || { echo "no binary given: phase B skipped"; exit 0; }
echo "== phase B: real install under systemd"
NAME=srnet-seller-e2e-$$
$C rm -f "$NAME" >/dev/null 2>&1 || true
$C run -d --name "$NAME" --systemd=always --cap-add NET_ADMIN --cap-add NET_RAW --device /dev/net/tun \
  -v "$ROOT:/src:ro" -v "$(cd "$(dirname "$BIN")" && pwd)/$(basename "$BIN"):/sr-bin:ro" "$IMG" >/dev/null
trap '$C rm -f "$NAME" >/dev/null 2>&1; rm -rf "$CTX"' EXIT
for i in $(seq 1 30); do $C exec "$NAME" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break; sleep 1; done
$C exec "$NAME" bash -ec '
cp -a /src /w && cd /w; P=$( [ -d seller ] && echo seller || echo seller-pack )
cp /sr-bin /tmp/session-router
$P/install.sh --binary /tmp/session-router --enable-exit --i-accept-exit-operator-responsibility \
  --payout 0x00000000000000000000000000000000000000aa --price-per-hour-usdc 1.00 --country US --region test > /tmp/install.log 2>&1 \
  || { cat /tmp/install.log; exit 1; }
tail -32 /tmp/install.log
echo "--- second run (idempotent)"
K1=$(sha256sum < /opt/srnet-seller/config/exit.key)
$P/install.sh --binary /tmp/session-router --enable-exit --i-accept-exit-operator-responsibility \
  --payout 0x00000000000000000000000000000000000000aa --price-per-hour-usdc 1.00 --country US --region test > /tmp/install2.log 2>&1
[ "$K1" = "$(sha256sum < /opt/srnet-seller/config/exit.key)" ] && echo "identity kept"
grep -E "^health" /tmp/install2.log
echo "--- process / caps"
ps -o user=,comm= -C session-router
grep -E "^Cap(Eff|Prm|Amb)" /proc/$(pgrep -x session-router)/status
ip -br addr show sr-tun-seller
ss -Hlun | grep 1193 || true
nft list table inet srnet_seller | grep -E "dport 53 drop|masquerade|blocked4" | head -3
ufw status | head -8
echo "--- listing refresh"
I1=$(python3 -c "import json;print(json.load(open(\"/opt/srnet-seller/listing/listing.json\"))[\"listing\"][\"issued\"])")
sleep 2; systemctl start srnet-seller-listing.service
I2=$(python3 -c "import json;print(json.load(open(\"/opt/srnet-seller/listing/listing.json\"))[\"listing\"][\"issued\"])")
[ "$I2" -gt "$I1" ] && echo "listing re-signed ($I1 -> $I2)"
systemctl list-timers srnet-seller-listing.timer --no-pager | head -3
echo "--- exit-lease admission store as seen by the exit user"
runuser -u srnet-exit -- cat /opt/srnet-seller/run/admissions.json; echo
journalctl -u srnet-seller-exit --no-pager -n 15 | cut -c1-200
echo "--- uninstall --purge"
srnet-seller uninstall --purge > /tmp/un.log 2>&1 || { cat /tmp/un.log; exit 1; }
! systemctl is-active --quiet srnet-seller-exit && echo "exit stopped"
! nft list table inet srnet_seller >/dev/null 2>&1 && echo "nft table removed"
[ ! -e /opt/srnet-seller ] && echo "prefix removed"
! id srnet-exit >/dev/null 2>&1 && echo "users removed"
echo "phase B: done"'
