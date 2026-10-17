#!/usr/bin/env bash
# bootstrap.sh picks the pinned release router, checks both hashes, and falls back to a
# source build when the tarball is missing or wrong. install.sh is stubbed; runs as fake root.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
unshare -r true 2>/dev/null || { echo "bootstrap tests: SKIP (no user namespaces)"; exit 0; }
fail() { echo "FAIL: $*"; exit 1; }; n=0; ok() { n=$((n+1)); }

# a release tree with a stub installer that prints its args
cp -a "$ROOT" "$T/src"; rm -rf "$T/src/.git"
printf '#!/bin/sh\necho "INSTALL $*"\n' > "$T/src/seller/install.sh"
mkdir "$T/dist"; printf 'fake router\n' > "$T/dist/session-router"; printf 'v\n' > "$T/dist/exit-lease-verify"
SHA=$(sha256sum "$T/dist/session-router" | cut -d' ' -f1)
printf 'source_date_epoch=1700000000\nsha256=%s\n' "$SHA" > "$T/dist/BUILDINFO"
"$T/src/scripts/build-release.sh" --version v9.9.9 --dist "$T/dist" --out "$T/rel" >/dev/null
RT="$T/rel/v9.9.9/session-router-v9.9.9-linux-x86_64.tar.gz"
(cd "$T/src" && git init -q && git add -A && git -c user.name=t -c user.email=t@t commit -qm t)
"$T/src/scripts/make-release.sh" --version v9.9.9 --router-tarball "$RT" --out "$T/rel" >/dev/null
D="$T/rel/v9.9.9"
grep -q "ROUTER_BIN_SHA256=$SHA" "$D/bootstrap.sh" || fail "binary hash not pinned"; ok
grep -q "^ROUTER_URL=https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack/releases/download/v9.9.9/session-router-v9.9.9-linux-x86_64.tar.gz$" "$D/bootstrap.sh" || fail "router url"; ok
grep -q "session-router-v9.9.9-linux-x86_64.tar.gz" "$D/SHA256SUMS" || fail "tarball not in SHA256SUMS"; ok
(cd "$D" && sha256sum -c --quiet SHA256SUMS) || fail "SHA256SUMS"; ok

run() { SRNET_PAYOUT=0x$(printf 'ab%.0s' {1..20}) SRNET_ACCEPT_EXIT_RESPONSIBILITY=yes "$@" \
          unshare -r bash "$D/bootstrap.sh" --non-interactive --pack "$D/srnet-seller-v9.9.9.tar.gz" "${ARGS[@]}" 2>&1 </dev/null; }
ARGS=(--router "$RT"); out=$(run env)
echo "$out" | grep -q -- "--binary .*/session-router --binary-sha256 $SHA" || fail "release binary not used: $out"; ok
echo "$out" | grep -q -- "--source" && fail "source with a good tarball"; ok

printf 'junk' > "$T/bad.tgz"; ARGS=(--router "$T/bad.tgz"); out=$(run env)
echo "$out" | grep -q "INSTALL .*--source" || fail "no source fallback on bad tarball: $out"; ok
echo "$out" | grep -q "sha256 mismatch" || fail "mismatch not reported"; ok

# tarball whose hash matches but whose binary was swapped: refuse
mkdir -p "$T/evil/session-router-v9.9.9-linux-x86_64"; printf 'evil\n' > "$T/evil/session-router-v9.9.9-linux-x86_64/session-router"
tar -C "$T/evil" -czf "$T/evil.tgz" session-router-v9.9.9-linux-x86_64
E=$(sha256sum "$T/evil.tgz" | cut -d' ' -f1)
sed "s/^ROUTER_SHA256=.*/ROUTER_SHA256=$E/" "$D/bootstrap.sh" > "$T/b2.sh"
out=$(SRNET_PAYOUT=0x$(printf 'ab%.0s' {1..20}) SRNET_ACCEPT_EXIT_RESPONSIBILITY=yes unshare -r bash "$T/b2.sh" \
      --non-interactive --pack "$D/srnet-seller-v9.9.9.tar.gz" --router "$T/evil.tgz" 2>&1 </dev/null)
echo "$out" | grep -q "INSTALL .*--source" || fail "swapped binary accepted: $out"; ok

ARGS=(--router "$RT"); out=$(run env SRNET_BUILD=source)
echo "$out" | grep -q "INSTALL .*--source" || fail "SRNET_BUILD=source ignored"; ok

ARGS=(--router "$RT"); out=$(run env SRNET_COUNTRY=NL)
echo "$out" | grep -q "INSTALL .*--country NL" || fail "SRNET_COUNTRY not passed"; ok

# unpinned bootstrap (no --router-tarball) goes to source
"$T/src/scripts/make-release.sh" --version v9.9.8 --out "$T/rel" >/dev/null
out=$(SRNET_PAYOUT=0x$(printf 'ab%.0s' {1..20}) SRNET_ACCEPT_EXIT_RESPONSIBILITY=yes unshare -r bash "$T/rel/v9.9.8/bootstrap.sh" \
      --non-interactive --pack "$T/rel/v9.9.8/srnet-seller-v9.9.8.tar.gz" 2>&1 </dev/null)
echo "$out" | grep -q "INSTALL .*--source" || fail "unpinned should build from source: $out"; ok
echo "bootstrap tests: OK ($n)"
