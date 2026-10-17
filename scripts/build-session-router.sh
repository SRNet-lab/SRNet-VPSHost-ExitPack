#!/usr/bin/env bash
# Reproducible build of the lease-gated Session Router exit.
#
# Clones the PUBLIC upstream at a pinned commit, verifies the commit and submodule
# hashes, applies the exit patches, copies contrib/exit-lease, builds a generic
# x86_64 binary (no -march=native) against the distro libraries and prints its sha256.
# Target: Ubuntu 24.04 LTS (the binary runs on the distro release it was built on).
#
#   build-session-router.sh [--out DIR] [--work DIR] [--jobs N] [--tests]
#                           [--install-deps] [--static-deps] [--keep-work]
#
#   --install-deps  apt-get install the build toolchain + libraries first (needs root)
#   --static-deps   experimental: build the pinned static deps instead of distro libs.
#                   At this upstream pin it fails (static libunbound lacks unbound-event.h).
#   --tests         also build + run the exit-lease unit tests and exit-lease-verify
#   --print-deps    print the apt packages this build needs and exit
set -euo pipefail

UPSTREAM_URL=https://github.com/session-foundation/session-router.git
UPSTREAM_COMMIT=afb98f959f4f7e0ef996aef02fa21a6caf26b1e9
# Submodules the daemon needs, with the public commits pinned by UPSTREAM_COMMIT.
SUBMODULES="cmake/session-deps external/CLI11 external/mlkem-native external/nlohmann external/oxen-libquic external/oxen-mq external/sqlite_orm external/pybind11"

APT_DEPS="build-essential g++ cmake ninja-build git pkg-config automake autoconf libtool \
python3 python3-dev curl ca-certificates patch xz-utils file"
APT_SYSTEM_DEPS="libsodium-dev libzmq3-dev libcurl4-openssl-dev libevent-dev nettle-dev \
libunbound-dev libssl-dev nlohmann-json3-dev libsqlite3-dev libsystemd-dev libgnutls28-dev \
libzstd-dev libfmt-dev libspdlog-dev libcli11-dev libcap-dev libuv1-dev"


OUT=$PWD/dist; WORK=""; JOBS=$(nproc 2>/dev/null || echo 2); TESTS=0; DEPS=0; SYSTEM=1; KEEP=0; PRINT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift 2;;
    --work) WORK=$2; shift 2;;
    --jobs) JOBS=$2; shift 2;;
    --tests) TESTS=1; shift;;
    --install-deps) DEPS=1; shift;;
    --system-deps) SYSTEM=1; shift;;
    --static-deps) SYSTEM=0; shift;;
    --keep-work) KEEP=1; shift;;
    --print-deps) PRINT=1; shift;;
    -h|--help) sed -n '2,16p' "$0"; exit 0;;
    *) echo "unknown arg $1" >&2; exit 1;;
  esac
done
log() { echo "[build] $*" >&2; }

pkgs=$APT_DEPS; [ "$SYSTEM" = 1 ] && pkgs="$pkgs $APT_SYSTEM_DEPS"; [ "$TESTS" = 1 ] && pkgs="$pkgs python3-cryptography"
[ "$PRINT" = 1 ] && { echo $pkgs; exit 0; }

HERE=$(cd "$(dirname "$0")" && pwd)
# Works from the SRNet-VPSHost-ExitPack repo (scripts/) and from a seller pack copy.
for c in "$HERE/../patches" "$HERE/patches" "$HERE/../../patches"; do [ -f "$c/0001-exit-lease-admission-and-policy.patch" ] && PATCHES=$(cd "$c" && pwd) && break; done
for c in "$HERE/../contrib/exit-lease" "$HERE/../exit-lease" "$HERE/exit-lease"; do [ -f "$c/verify_main.cpp" ] && EXIT_LEASE=$(cd "$c" && pwd) && break; done
: "${PATCHES:?patches/ directory not found next to this script}"
: "${EXIT_LEASE:?exit-lease/ (verify_main.cpp) not found next to this script}"
if [ "$DEPS" = 1 ]; then
  log "apt-get install: $pkgs"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q && apt-get install -y -q --no-install-recommends $pkgs
fi
for t in git cmake ninja g++ curl patch; do command -v $t >/dev/null || { echo "missing $t (use --install-deps)" >&2; exit 1; }; done

[ -n "$WORK" ] || { WORK=$(mktemp -d); [ "$KEEP" = 1 ] || trap 'rm -rf "$WORK"' EXIT; }
SRC=$WORK/session-router; BUILD=$WORK/build
mkdir -p "$OUT"

if [ ! -d "$SRC/.git" ]; then
  log "fetching $UPSTREAM_URL @ $UPSTREAM_COMMIT"
  git init -q "$SRC"
  git -C "$SRC" remote add origin "$UPSTREAM_URL"
  git -C "$SRC" fetch -q --depth 1 origin "$UPSTREAM_COMMIT"
  git -C "$SRC" -c advice.detachedHead=false checkout -q FETCH_HEAD
fi
GOT=$(git -C "$SRC" rev-parse HEAD)
[ "$GOT" = "$UPSTREAM_COMMIT" ] || { echo "upstream commit mismatch: $GOT != $UPSTREAM_COMMIT" >&2; exit 1; }
log "upstream commit verified: $GOT"

if [ ! -f "$SRC/.srnet-patched" ]; then
  log "submodules (public pins)"
  git -C "$SRC" submodule update -q --init --recursive --depth 1 --jobs 4 -- $SUBMODULES
  [ "$TESTS" = 1 ] && git -C "$SRC" submodule update -q --init --depth 1 -- test/Catch2
  for p in "$PATCHES"/0000-*.patch "$PATCHES"/0001-*.patch; do
    log "apply $(basename "$p")"
    git -C "$SRC" apply --check "$p"
    git -C "$SRC" apply "$p"
  done
  for p in "$PATCHES"/session-deps/*.patch; do
    [ -f "$p" ] || continue
    # Every vendored session-deps copy that builds ngtcp2 (the router's and libquic's).
    for f in $(find "$SRC" -path '*/session-deps/deps/libngtcp2.cmake' -not -path '*/.git/*'); do
      d=$(dirname "$(dirname "$f")")
      log "apply session-deps/$(basename "$p") in ${d#"$SRC"/}"
      git -C "$d" apply --check "$p"
      git -C "$d" apply "$p"
    done
  done
  mkdir -p "$SRC/contrib/exit-lease"
  cp -r "$EXIT_LEASE"/. "$SRC/contrib/exit-lease/"
  rm -rf "$SRC/contrib/exit-lease/__pycache__" "$SRC/contrib/exit-lease/.pytest_cache"
  touch "$SRC/.srnet-patched"
fi
# Record the submodule commits actually used.
git -C "$SRC" submodule status -- $SUBMODULES | sed 's/^/[build] submodule /' >&2

CMAKE_ARGS=(-G Ninja -DCMAKE_BUILD_TYPE=Release -DSROUTER_NATIVE_BUILD=OFF -DSROUTER_JEMALLOC=OFF
  -DBUILD_SHARED_LIBS=OFF -DSUBMODULE_CHECK=OFF -DWITH_SYSTEMD=OFF -DSROUTER_WARNINGS_AS_ERRORS=OFF
  -DCMAKE_EXPORT_COMPILE_COMMANDS=OFF)
if [ "$SYSTEM" = 1 ]; then CMAKE_ARGS+=(-DBUILD_STATIC_DEPS=OFF -DSTATIC_LINK=OFF)
else CMAKE_ARGS+=(-DBUILD_STATIC_DEPS=ON -DSTATIC_LINK=ON); fi
[ "$TESTS" = 1 ] && CMAKE_ARGS+=(-DSROUTER_TESTS=ON)
# Reproducibility: fixed timestamps and path prefixes.
export SOURCE_DATE_EPOCH=$(git -C "$SRC" log -1 --format=%ct)
export CFLAGS="${CFLAGS:-} -ffile-prefix-map=$WORK=/build"
export CXXFLAGS="${CXXFLAGS:-} -ffile-prefix-map=$WORK=/build"

log "configure (${CMAKE_ARGS[*]})"
cmake -S "$SRC" -B "$BUILD" "${CMAKE_ARGS[@]}" >"$WORK/configure.log" 2>&1 || { tail -50 "$WORK/configure.log"; exit 1; }
log "build session-router (jobs=$JOBS)"
cmake --build "$BUILD" --target session-router -j "$JOBS"

BIN=$(find "$BUILD" -maxdepth 2 -type f -name session-router -perm -u+x | head -1)
[ -n "$BIN" ] || { echo "session-router binary not found" >&2; exit 1; }
strip --strip-all -o "$OUT/session-router" "$BIN"
grep -aq 'srnet-exit-admission/v2' "$OUT/session-router" || { echo "patched marker missing" >&2; exit 1; }
grep -aq 'Inbound session rejected by auth policy' "$OUT/session-router" || { echo "auth marker missing" >&2; exit 1; }

if [ "$TESTS" = 1 ]; then
  log "tests: test_exit_lease_admission + exit-lease-verify"
  cmake --build "$BUILD" --target test_exit_lease_admission exit-lease-verify -j "$JOBS"
  "$BUILD/test/test_exit_lease_admission"
  if python3 -c 'import cryptography' 2>/dev/null; then
    (cd "$SRC/contrib/exit-lease" && EXIT_LEASE_VERIFY="$BUILD/test/exit-lease-verify" python3 -m unittest discover -s tests -v)
  else log "python3-cryptography missing: skipping exit-lease python tests"; fi
  cp "$BUILD/test/exit-lease-verify" "$OUT/exit-lease-verify"
fi

( cd "$OUT" && sha256sum session-router > session-router.sha256 )
# Runtime packages the binary links against (used by install.sh --prebuilt).
if command -v dpkg >/dev/null; then
  ldd "$OUT/session-router" | awk '/=> \//{print $3}' | xargs -r -n1 readlink -f | xargs -r dpkg -S 2>/dev/null \
    | cut -d: -f1 | sort -u > "$OUT/RUNTIME-DEPS" || true
fi
{ echo "upstream_url=$UPSTREAM_URL"; echo "upstream_commit=$UPSTREAM_COMMIT"
  echo "patches=$(cd "$PATCHES" && sha256sum 0000-*.patch 0001-*.patch session-deps/*.patch 2>/dev/null | awk '{printf "%s:%s ", $2, $1}')"
  echo "static_deps=$([ "$SYSTEM" = 1 ] && echo no || echo yes)"
  [ -f /etc/os-release ] && echo "os=$(. /etc/os-release; echo "$ID $VERSION_ID")"
  echo "compiler=$(g++ --version | head -1)"; echo "source_date_epoch=$SOURCE_DATE_EPOCH"
  echo "sha256=$(cut -d' ' -f1 "$OUT/session-router.sha256")"; } > "$OUT/BUILDINFO"
file "$OUT/session-router" >&2 || true
echo "sha256 $(cat "$OUT/session-router.sha256")"
