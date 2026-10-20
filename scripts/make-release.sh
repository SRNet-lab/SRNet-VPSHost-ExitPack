#!/usr/bin/env bash
# Assemble release assets locally (nothing is uploaded):
#   scripts/make-release.sh --version v0.1.0 [--router-tarball release/v0.1.0/session-router-*.tar.gz]
#                           [--binary dist/session-router]
#                           [--board-submit-url URL] [--base-url URL] [--out release]
# Produces, in --out/<version>/:
#   session-router-<v>-linux-x86_64.tar.gz  (if --router-tarball, from build-release.sh; pinned into
#                                    bootstrap.sh, which falls back to a source build without it)
#   session-router-linux-x86_64     (if --binary; pinned into install.sh as PREBUILT_SHA256)
#   srnet-seller-<version>.tar.gz   (deterministic tarball: seller/ scripts/ patches/ contrib/ docker/)
#   bootstrap.sh                    (pins the tarball sha256)
#   user-data.yaml                  (cloud-init; pins bootstrap.sh sha256)
#   SHA256SUMS, ONE-LINER.txt
set -euo pipefail
VER=""; BIN=""; RTAR=""; BOARD=""; BASE=""; OUT=release
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VER=$2; shift 2;; --binary) BIN=$2; shift 2;; --router-tarball) RTAR=$2; shift 2;;
    --board-submit-url) BOARD=$2; shift 2;; --base-url) BASE=$2; shift 2;; --out) OUT=$2; shift 2;;
    *) echo "unknown arg $1" >&2; exit 1;;
  esac
done
[[ "$VER" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "--version vX.Y.Z required" >&2; exit 1; }
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BASE=${BASE:-https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack/releases/download/$VER}
D="$OUT/$VER"; mkdir -p "$D"; D=$(cd "$D" && pwd)
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
EPOCH=$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || echo 0)
NAME=srnet-seller-$VER
mkdir -p "$STAGE/$NAME"
for p in seller scripts patches contrib docker cloud-init LICENSE LICENSE-GPL3.txt README.md QUICKSTART.md; do
  cp -a "$ROOT/$p" "$STAGE/$NAME/"
done
find "$STAGE/$NAME" \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +

PREBUILT_SHA=""
if [ -n "$BIN" ]; then
  install -m 755 "$BIN" "$D/session-router-linux-x86_64"
  PREBUILT_SHA=$(sha256sum "$D/session-router-linux-x86_64" | cut -d' ' -f1)
  sed -i -e "s|^PREBUILT_URL=.*|PREBUILT_URL=$BASE/session-router-linux-x86_64|" \
         -e "s|^PREBUILT_SHA256=.*|PREBUILT_SHA256=$PREBUILT_SHA|" "$STAGE/$NAME/seller/install.sh"
fi
ROUTER_URL=""; ROUTER_SHA=""; ROUTER_BIN_SHA=""
if [ -n "$RTAR" ]; then
  RNAME=$(basename "$RTAR")
  [[ "$RNAME" =~ ^session-router-v[0-9.]+-linux-x86_64\.tar\.gz$ ]] || { echo "unexpected --router-tarball name" >&2; exit 1; }
  [ "$(cd "$(dirname "$RTAR")" && pwd)/$RNAME" = "$D/$RNAME" ] || install -m 644 "$RTAR" "$D/$RNAME"
  ROUTER_SHA=$(sha256sum "$D/$RNAME" | cut -d' ' -f1)
  ROUTER_BIN_SHA=$(tar -xzOf "$D/$RNAME" "${RNAME%.tar.gz}/SHA256SUMS" | awk '$2=="session-router"{print $1}')
  [[ "$ROUTER_BIN_SHA" =~ ^[0-9a-f]{64}$ ]] || { echo "no session-router hash in the tarball" >&2; exit 1; }
  ROUTER_URL=$BASE/$RNAME
fi
tar --sort=name --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
  -C "$STAGE" -cf - "$NAME" | gzip -9n > "$D/$NAME.tar.gz"
PACK_SHA=$(sha256sum "$D/$NAME.tar.gz" | cut -d' ' -f1)

sed -e "s|@RELEASE@|$VER|" -e "s|@PACK_URL@|$BASE/$NAME.tar.gz|" -e "s|@PACK_SHA256@|$PACK_SHA|" \
    -e "s|@DEFAULT_BOARD_SUBMIT_URL@|$BOARD|" \
    -e "s|@ROUTER_URL@|$ROUTER_URL|" -e "s|@ROUTER_SHA256@|$ROUTER_SHA|" -e "s|@ROUTER_BIN_SHA256@|$ROUTER_BIN_SHA|" \
    "$ROOT/seller/bootstrap.sh" > "$D/bootstrap.sh"
chmod 755 "$D/bootstrap.sh"
BOOT_SHA=$(sha256sum "$D/bootstrap.sh" | cut -d' ' -f1)
sed -e "s|@BOOTSTRAP_URL@|$BASE/bootstrap.sh|g" -e "s|@BOOTSTRAP_SHA256@|$BOOT_SHA|g" \
    "$ROOT/cloud-init/user-data.yaml" > "$D/user-data.yaml"
(cd "$D" && sha256sum -- * | grep -v SHA256SUMS > SHA256SUMS)
cat > "$D/ONE-LINER.txt" <<TXT
curl -fsSLo srnet-bootstrap.sh $BASE/bootstrap.sh && echo '$BOOT_SHA  srnet-bootstrap.sh' | sha256sum -c - && sudo bash srnet-bootstrap.sh
TXT
echo "release assets in $D"
echo "  pack sha256:      $PACK_SHA"
echo "  bootstrap sha256: $BOOT_SHA"
[ -n "$PREBUILT_SHA" ] && echo "  binary sha256:    $PREBUILT_SHA"
[ -n "$ROUTER_SHA" ] && echo "  router tarball:   $ROUTER_SHA (session-router $ROUTER_BIN_SHA)"
cat "$D/ONE-LINER.txt"
