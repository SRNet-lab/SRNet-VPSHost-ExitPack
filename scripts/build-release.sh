#!/usr/bin/env bash
# Build the patched Session Router in the pinned container and pack it for a release:
#   scripts/build-release.sh --version v0.1.0 [--dist DIR] [--out release] [--engine podman|docker]
#                            [--apt-snapshot YYYYMMDDTHHMMSSZ]
# Writes, in --out/<version>/:
#   session-router-<version>-linux-x86_64.tar.gz   session-router, exit-lease-verify, BUILDINFO, RUNTIME-DEPS
#   SHA256SUMS                                     tarball and binary
# --dist DIR reuses an existing build output (the container's /out) instead of building.
# Nothing is uploaded. Upstream commit and patches are pinned in build-session-router.sh.
set -euo pipefail
VER=""; DIST=""; OUT=release; ENGINE=""; SNAP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VER=$2; shift 2;; --dist) DIST=$2; shift 2;; --out) OUT=$2; shift 2;;
    --engine) ENGINE=$2; shift 2;; --apt-snapshot) SNAP=$2; shift 2;;
    *) echo "unknown arg $1" >&2; exit 1;;
  esac
done
[[ "$VER" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "--version vX.Y.Z required" >&2; exit 1; }
ROOT=$(cd "$(dirname "$0")/.." && pwd)
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
if [ -z "$DIST" ]; then
  ENGINE=${ENGINE:-$(command -v podman || command -v docker || true)}
  [ -n "$ENGINE" ] || { echo "need podman or docker (or --dist DIR)" >&2; exit 1; }
  DIST="$STAGE/out"
  "$ENGINE" build --network host ${SNAP:+--build-arg APT_SNAPSHOT=$SNAP} \
    -f "$ROOT/docker/Dockerfile" --target out -o "$DIST" "$ROOT"
fi
for f in session-router exit-lease-verify BUILDINFO; do
  [ -f "$DIST/$f" ] || { echo "missing $DIST/$f" >&2; exit 1; }
done
BIN_SHA=$(sha256sum "$DIST/session-router" | cut -d' ' -f1)
grep -qx "sha256=$BIN_SHA" "$DIST/BUILDINFO" || { echo "BUILDINFO does not match the binary" >&2; exit 1; }
EPOCH=$(sed -n 's/^source_date_epoch=//p' "$DIST/BUILDINFO"); EPOCH=${EPOCH:-0}

NAME=session-router-$VER-linux-x86_64
mkdir -p "$STAGE/$NAME"
install -m 755 "$DIST/session-router" "$DIST/exit-lease-verify" "$STAGE/$NAME/"
install -m 644 "$DIST/BUILDINFO" "$STAGE/$NAME/"
[ -f "$DIST/RUNTIME-DEPS" ] && install -m 644 "$DIST/RUNTIME-DEPS" "$STAGE/$NAME/"
(cd "$STAGE/$NAME" && sha256sum -- * > SHA256SUMS)
D="$OUT/$VER"; mkdir -p "$D"; D=$(cd "$D" && pwd)
tar --sort=name --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner \
  --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
  -C "$STAGE" -cf - "$NAME" | gzip -9n > "$D/$NAME.tar.gz"
TAR_SHA=$(sha256sum "$D/$NAME.tar.gz" | cut -d' ' -f1)
{ echo "$TAR_SHA  $NAME.tar.gz"; echo "$BIN_SHA  $NAME/session-router"; } > "$D/SHA256SUMS"
echo "release binary in $D"
echo "  $NAME.tar.gz sha256: $TAR_SHA"
echo "  session-router sha256: $BIN_SHA"
