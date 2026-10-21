#!/usr/bin/env bash
# Run the netns lab: every case in fresh namespaces of an unprivileged user namespace (no root
# needed, but user namespaces and the nft tool must be available). Exit 0 when all cases pass.
#   tests/lab/run.sh [case ...]     cases: full rpfilter metrics foreign legacy container udpgap
set -u
cd "$(dirname "$0")"
command -v nft >/dev/null || [ -x /usr/sbin/nft ] || { echo "SKIP: no nft"; exit 0; }
unshare -rn true 2>/dev/null || { echo "SKIP: no user namespaces"; exit 0; }
CASES="${*:-full rpfilter metrics foreign legacy container udpgap}"
fail=0
for c in $CASES; do
  rp=$(mktemp -u /tmp/srnet_lab_rp.XXXXXX)
  out=$(LABRP=$rp timeout 120 unshare -rn bash -c 'bash setup.sh </dev/null >/dev/null 2>"$LABRP.err" || { cat "$LABRP.err"; exit 3; }
      python3 -u lab.py "$LABRP" '"$c"'; rc=$?; kill $(cat "$LABRP") 2>/dev/null; exit $rc' 2>&1)
  rc=$?
  rm -f "$rp" "$rp.err"
  echo "$out" | sed "s/^/[$c] /"
  if [ $rc -eq 0 ]; then echo "[$c] PASS"; else echo "[$c] FAIL (rc=$rc)"; fail=1; fi
done
exit $fail
