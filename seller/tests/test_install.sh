#!/usr/bin/env bash
# install.sh dry-run tests (no root, no network, nothing outside a temp dir).
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd); T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*"; exit 1; }
n=0; ok() { n=$((n+1)); }
I="$HERE/install.sh"
printf 'plain binary\n' > "$T/upstream"
printf 'x srnet-exit-admission/v2 y Inbound session rejected by auth policy z\n' > "$T/fakebin"
PAY=0x00000000000000000000000000000000000000aa
U=opt/srnet-seller
# geo-IP lookups are mocked with local files (two services that agree on US)
echo us > "$T/geo1"; printf 'US\n' > "$T/geo2"; echo DE > "$T/geo3"
export SRNET_GEOIP_URLS="file://$T/geo1 file://$T/geo2"
# 1. unpatched refused
"$I" --binary "$T/upstream" --dry-run --root "$T/r1" >/dev/null 2>&1 && fail "unpatched accepted"; ok
# 2. only Arbitrum One and Base; old contract flags refused with a clear message
for c in ethereum 1 polygon base-sepolia 31337; do "$I" --binary "$T/fakebin" --chain $c --payout $PAY --dry-run --root "$T/r1b" >/dev/null 2>&1 && fail "chain $c accepted"; done
"$I" --binary "$T/fakebin" --chain base --payout $PAY --dry-run --root "$T/r1d" >/dev/null 2>&1 || fail "base refused"
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['listing']['chains']==[8453]" "$T/r1d/$U/listing/listing.json" || fail "--chain base"
for f in --contract --chainid; do
  out=$("$I" --binary "$T/fakebin" $f 1 --dry-run --root "$T/r1e" 2>&1) && fail "$f accepted"
  echo "$out" | grep -q 'pay your --payout address directly' || fail "$f message"
done
grep -rqi "0x7E0d01B7dd916BdAB96B0971ab92D160E1664431" "$T/r1d" && fail "old contract still referenced"; ok
# 3. payout must be an address, never a key
"$I" --binary "$T/fakebin" --payout 0x$(printf 'ab%.0s' {1..32}) --dry-run --root "$T/r1c" >/dev/null 2>&1 && fail "64-hex payout accepted"; ok
# 3b. prices under one cent an hour are refused
out=$("$I" --binary "$T/fakebin" --payout $PAY --price-per-hour-usdc 0.009 --dry-run --root "$T/r1p" 2>&1) && fail "0.009 accepted"
echo "$out" | grep -q "at least 0.01" || fail "min price message"; ok
# 4. default: exit OFF, empty whitelist, keys+policy+listing, no DNS, no NAT
"$I" --binary "$T/fakebin" --dry-run --root "$T/r2" --price-per-hour-usdc 1.50 --payout $PAY >/dev/null 2>&1
INI="$T/r2/$U/config/exit.ini"
grep -q '^enable=false' "$INI" || fail "exit not off"
grep -q '^auth-type=whitelist' "$INI" || fail "no whitelist"
grep -q '^auth-whitelist' "$INI" && fail "whitelist not empty"
grep -q '^policy=.*53' "$INI" && fail "DNS in policy"
grep -q '^reachable=true' "$INI" || fail "reachable"
grep -q "^keyfile=/$U/config/exit.key" "$INI" || fail "keyfile"
[ -f "$T/r2/$U/config/issuer.key" ] || fail "no issuer"
[ -f "$T/r2/$U/config/exit.key" ] || fail "no seller key"
[ "$(stat -c %a "$T/r2/$U/config/exit.key")" = 600 ] || fail "exit.key mode"
grep -q '"price_per_hour_usdc": "1.50"' "$T/r2/$U/config/seller-policy.json" || fail "policy"
grep -q '"dns_forwarding": false' "$T/r2/$U/config/seller-policy.json" || fail "dns flag"
python3 -c "import json,sys;d=json.load(open(sys.argv[1]));assert d['chains']==[8453,42161] and d['token']=='USDC' and 'contract' not in d" "$T/r2/$U/config/seller-policy.json" || fail "default chains not arbitrum + base"
LB="$T/r2/$U/listing/listing.json"
python3 -c "
import json,sys;sys.path.insert(0,'$HERE');import listing as L,quote as Q
b=json.load(open(sys.argv[1]))['listing']
assert b['v']==3 and b['chains']==[8453,42161] and b['token']=='USDC', b
assert L.listing_error(b) is None, L.listing_error(b)
assert b['quote_key']==Q.pub_hex(Q.load_key(sys.argv[2]))
for k in ('vendor_id','contract','chainid'): assert k not in b
" "$LB" "$T/r2/$U/config/quote.key" || fail "v3 listing"
[ "$(stat -c %a "$T/r2/$U/config/quote.key")" = 600 ] || fail "quote.key mode"
grep -rq 'cast\b\|foundry' "$T/r2/commands.log" && fail "foundry still downloaded"
[ -e "$T/r2/$U/pack/payout.py" ] && fail "payout.py still installed"
for f in chains.py quote.py; do [ -f "$T/r2/$U/pack/$f" ] || fail "$f not installed"; done
python3 "$HERE/listing.py" verify "$T/r2/$U/listing/listing.json" >/dev/null || fail "listing"
python3 "$HERE/listing.py" verify "$T/r2/$U/config/listing.json" >/dev/null || fail "listing symlink"
grep -q 'SR_EXIT_LEASE_ISSUER_PUBKEY=[0-9a-f]\{64\}' "$T/r2/etc/systemd/system/srnet-seller-exit.service" || fail "issuer env"
grep -q 'SR_EXIT_LEASE_ADMISSIONS=' "$T/r2/etc/systemd/system/srnet-seller-exit.service" || fail "store env"
grep -q 'nft -f' "$T/r2/commands.log" && fail "NAT while exit off"
grep -q 'route allow' "$T/r2/commands.log" && fail "forward rule while exit off"
grep -q 'ufw allow 1193/udp' "$T/r2/commands.log" || fail "router port not opened"
grep -q 'ufw allow 22/tcp' "$T/r2/commands.log" || fail "ssh not kept open"
grep -q 'ufw default deny incoming' "$T/r2/commands.log" || fail "default deny"
grep -q 'build-session-router.sh --install-deps' "$T/r2/commands.log" && fail "built from source despite --binary"
ok
# 5. watcher + claimd read the chain only and share the claims dir
W5="$T/r2/etc/systemd/system/srnet-seller-watcher.service"; C5="$T/r2/etc/systemd/system/srnet-seller-claimd.service"
grep -q -- "--dir /$U/claims --seller" "$W5" || fail "watcher dir"
grep -q -- "--quote-key /$U/config/quote.key --listing /$U/listing/listing.json --cap-usdc" "$C5" || fail "claimd args"
grep -qx 'CAP_USDC=20' "$T/r2/$U/config/watcher.env" || fail "default cap 20"
grep -q 'CONTRACT\|PAYOUT_WALLET\|KEYSTORE' "$T/r2/$U/config/watcher.env" && fail "old watcher env"
"$I" --binary "$T/fakebin" --dry-run --root "$T/r5r" --payout $PAY --rpc 8453=https://base.example/rpc --cap-usdc 5 >/dev/null 2>&1 || fail "--rpc refused"
grep -q 'CHAIN_FLAGS=" --rpc 8453=https://base.example/rpc "' "$T/r5r/$U/config/watcher.env" || fail "--rpc not passed to the watcher"
grep -qx 'CAP_USDC=5' "$T/r5r/$U/config/watcher.env" || fail "--cap-usdc"
"$I" --binary "$T/fakebin" --dry-run --root "$T/r5s" --payout $PAY --chain base --rpc 42161=https://x.example >/dev/null 2>&1 && fail "--rpc for an unused chain accepted"
"$I" --binary "$T/fakebin" --dry-run --root "$T/r5s" --payout $PAY --rpc base=https://x.example >/dev/null 2>&1 && fail "bad --rpc accepted"
ok
# 6. hardening + users + listing timer
X="$T/r2/etc/systemd/system/srnet-seller-exit.service"; W="$T/r2/etc/systemd/system/srnet-seller-watcher.service"
for k in 'User=srnet-exit' 'NoNewPrivileges=yes' 'ProtectSystem=strict' 'CapabilityBoundingSet=CAP_NET_ADMIN' \
         'AmbientCapabilities=CAP_NET_ADMIN' 'DeviceAllow=/dev/net/tun rw' 'DevicePolicy=closed'; do
  grep -qx "$k" "$X" || fail "exit unit lacks $k"; done
for k in 'User=srnet-watch' 'NoNewPrivileges=yes' 'ProtectSystem=strict' 'CapabilityBoundingSet=' 'PrivateDevices=yes'; do
  grep -qx "$k" "$W" || fail "watcher unit lacks $k"; done
grep -q 'private-key\|PRIVATE' "$W" "$T/r2/$U/config/watcher.env" && fail "watcher references a wallet key"
grep -q 'useradd --system .* srnet-exit' "$T/r2/commands.log" || fail "no exit user"
grep -q 'OnUnitActiveSec=1h' "$T/r2/etc/systemd/system/srnet-seller-listing.timer" || fail "listing timer not hourly"
grep -q 'listing.py refresh' "$T/r2/etc/systemd/system/srnet-seller-listing.service" || fail "listing refresh unit"
grep -qE 'enable --now srnet-seller-exit.service srnet-seller-submit.service srnet-seller-claimd.service srnet-seller-listing.timer' "$T/r2/commands.log" || fail "timer/claimd not enabled"
grep -q 'enable --now srnet-seller-watcher' "$T/r2/commands.log" || fail "watcher not enabled with a payout"
grep -qx 'BOARD_SUBMIT_URL=http://exitsrjnc83har7a17mdtjpgi1j3odgiphaffe9ejfq4wnbdxkio.sesh/submit' "$T/r2/$U/config/listing.env" || fail "default board url"
grep -qx 'SRNET_SESH_DNS=127.0.0.1:5354' "$T/r2/$U/config/listing.env" || fail "submit dns"
grep -qx 'listen=127.0.0.1:5354' "$T/r2/$U/config/submit.ini" || fail "submit router dns"
grep -qx "keyfile=/opt/srnet-seller/config/claim.key" "$T/r2/$U/config/submit.ini" || fail "submit router uses claim.key"
grep -q 'exit.key' "$T/r2/$U/config/submit.ini" && fail "submit router must not use the exit key"
[ "$(stat -c %a "$T/r2/$U/config/claim.key")" = 600 ] || fail "claim.key mode"
CS=$(python3 -c "import sys;sys.path.insert(0,'$HERE');import listing as L;print(L.sesh_of(bytes.fromhex(open(sys.argv[1]).read().strip())[32:]))" "$T/r2/$U/config/claim.key")
python3 -c "import json,sys;d=json.load(open(sys.argv[1]));assert d['listing']['claim_url']=='http://$CS/claim'" "$T/r2/$U/listing/listing.json" || fail "listing claim_url"
python3 "$T/r2/$U/pack/listing.py" verify "$T/r2/$U/listing/listing.json" >/dev/null || fail "listing with claim_url verifies"
grep -q -- '--host fd2e:7365:736a::1 --port 80' "$T/r2/etc/systemd/system/srnet-seller-claimd.service" || fail "claimd bind"
grep -qx 'User=srnet-claim' "$T/r2/etc/systemd/system/srnet-seller-claimd.service" || fail "claimd user"
grep -qx "InaccessiblePaths=/$U/config/issuer.key /$U/config/watcher.env /$U/run /$U/watch" "$T/r2/etc/systemd/system/srnet-seller-claimd.service" || fail "claimd can't see the issuer key"
grep -qx 'SupplementaryGroups=srnet-claims' "$T/r2/etc/systemd/system/srnet-seller-watcher.service" || fail "watcher shares claims group"
grep -q -- "--state /$U/watch/watcher-state.json" "$T/r2/etc/systemd/system/srnet-seller-watcher.service" || fail "watcher state outside claims"
grep -q 'useradd .*srnet-claim$' "$T/r2/commands.log" || fail "claim user created"
grep -q "chown srnet-claim:srnet-claim /$U/config/quote.key" "$T/r2/commands.log" || fail "quote key owner"
grep -q "chown srnet-watch:srnet-watch /$U/config/issuer.key" "$T/r2/commands.log" || fail "issuer key owner"
grep -q 'ufw allow in on sr-tun-submit to fd2e:7365:736a::1 port 80 proto tcp' "$T/r2/commands.log" || fail "claim ufw rule"
[ -x "$T/r2/$U/pack/claimd.py" ] || fail "claimd installed"
grep -q 'chown -R srnet-exit:srnet-exit /opt/srnet-seller/data$' "$T/r2/commands.log" || fail "router data not chowned recursively"
grep -q 'srnet-seller-submit.service' "$T/r2/commands.log" || fail "submit router not enabled"
grep -q '^listen=$' "$T/r2/$U/config/exit.ini" || fail "exit dns listener must stay off"
# test chains only with --testnets (and then no board); no --payout: draft listing, watcher not started
"$I" --binary "$T/fakebin" --chain anvil --testnets --dry-run --root "$T/r2t" --payout $PAY >/dev/null 2>&1 || fail "local chain refused"
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['chains']==[31337]" "$T/r2t/$U/config/seller-policy.json" || fail "local chain"
grep -q -- '--testnets' "$T/r2t/etc/systemd/system/srnet-seller-claimd.service" || fail "claimd testnets"
grep -qx 'BOARD_SUBMIT_URL=' "$T/r2t/$U/config/listing.env" || fail "test chains should not submit to the board"
"$I" --binary "$T/fakebin" --dry-run --root "$T/r2p" >/dev/null 2>&1 || fail "no payout refused"
grep -q 'enable --now srnet-seller-watcher' "$T/r2p/commands.log" && fail "watcher enabled without payout"
python3 -c "import json,sys;sys.path.insert(0,'$HERE');import listing as L;assert L.listing_error(json.load(open(sys.argv[1]))['listing'])=='bad or zero payout'" "$T/r2p/$U/listing/listing.json" || fail "draft listing should be unbuyable"
"$I" --binary "$T/fakebin" --dry-run --root "$T/r2n" --payout $PAY --board-submit-url none >/dev/null 2>&1 || fail "board none"
grep -qx 'BOARD_SUBMIT_URL=' "$T/r2n/$U/config/listing.env" || fail "--board-submit-url none"
grep -q 'srnet-seller-submit.service' "$T/r2n/commands.log" || fail "submit router carries claims even without a board"
ok
# 7. enable without ack -> exit 2
set +e; "$I" --binary "$T/fakebin" --enable-exit --dry-run --root "$T/r3" >/dev/null 2>&1; rc=$?; set -e
[ $rc = 2 ] || fail "enable without ack rc=$rc"; ok
# 8. enable with ack: web ports + DNS to the exit resolver only, forwarded DNS dropped, private ranges dropped, NAT
"$I" --binary "$T/fakebin" --enable-exit --i-accept-exit-operator-responsibility --dry-run --root "$T/r4" \
  --payout $PAY --board-submit-url http://127.0.0.1:8787/submit >/dev/null 2>&1
E="$T/r4/$U/config/exit.ini"
grep -q '^enable=true' "$E" || fail "not enabled"
[ "$(grep -c '^policy=' "$E")" = 4 ] || fail "policy count"
grep -qx 'policy=tcp/443' "$E" && grep -qx 'policy=tcp/80' "$E" || fail "web ports"
grep -qx 'policy=udp/53' "$E" && grep -qx 'policy=tcp/53' "$E" || fail "DNS to the exit resolver not in policy"
grep -q 'dport 53 drop' "$T/r4/commands.log" || fail "no DNS drop"
grep -q '169.254.0.0/16' "$T/r4/commands.log" || fail "metadata range not blocked"
grep -q 'masquerade' "$T/r4/commands.log" || fail "no NAT"
grep -qE 'chain (fwd|in|nat) ' "$T/r4/$U/config/nat.nft" && fail "nft chain name is a reserved word"
if command -v nft >/dev/null && [ "$(id -u)" = 0 ]; then nft -c -f "$T/r4/$U/config/nat.nft" >/dev/null || fail "nat.nft does not parse"; fi
grep -q 'route allow in on sr-tun-seller out on eth0 proto tcp to any port 80,443' "$T/r4/commands.log" || fail "no fwd rule"
grep -q 'net.ipv4.ip_forward=1' "$T/r4/etc/sysctl.d/90-srnet-seller.conf" || fail "sysctl"
grep -q 'enable --now srnet-seller-watcher' "$T/r4/commands.log" || fail "watcher not enabled with payout"
grep -q 'BOARD_SUBMIT_URL=http://127.0.0.1:8787/submit' "$T/r4/$U/config/listing.env" || fail "board url"
ok
# 8a. exit resolver: unbound recursive from the roots, no logs, tunnel only, rate limited, advertised
UB="$T/r4/etc/unbound/srnet-seller-dns.conf"; N="$T/r4/$U/config/nat.nft"
[ -f "$UB" ] || fail "no resolver config"
python3 "$HERE/dnscheck.py" --conf "$UB" || fail "resolver config fails its own check"
grep -qx '  interface: 10.195.0.53' "$UB" || fail "resolver interface"
grep -qx '  access-control: 0.0.0.0/0 refuse' "$UB" && grep -qx '  access-control: 10.193.0.0/16 allow' "$UB" || fail "resolver acl"
grep -qE '^ *(forward|stub)-' "$UB" && fail "resolver forwards"
grep -qx '  log-queries: no' "$UB" && grep -qx '  log-replies: no' "$UB" && grep -qx '  verbosity: 0' "$UB" || fail "resolver logs"
grep -qx '  qname-minimisation: yes' "$UB" && grep -q 'auto-trust-anchor-file' "$UB" || fail "qmin/dnssec"
grep -qx '  module-config: "validator iterator"' "$UB" || fail "module-config"
grep -q 'ip-ratelimit: 100' "$UB" || fail "resolver rate limit"
grep -q 'ip daddr 10.195.0.53 meta l4proto { tcp, udp } th dport 53 update @dns_rl4' "$N" || fail "nft dns rate limit"
grep -q 'iifname "sr-tun-seller" ip daddr 10.195.0.53 meta l4proto { tcp, udp } th dport 53 accept' "$N" || fail "nft dns accept"
grep -q 'ip daddr 10.195.0.53 iifname != { "sr-tun-seller", "lo" } drop' "$N" || fail "resolver reachable from outside the tunnel"
grep -q 'iifname "sr-tun-seller" meta l4proto { tcp, udp } th dport 53 drop' "$N" || fail "forwarded DNS not dropped"
grep -q 'ufw allow in on sr-tun-seller to 10.195.0.53 port 53 proto udp' "$T/r4/commands.log" || fail "ufw dns rule"
grep -q 'enable --now srnet-seller-dns.service srnet-seller-nat.service' "$T/r4/commands.log" || fail "dns unit not enabled"
grep -q 'ExecStart=/usr/sbin/unbound -d -p -c /etc/unbound/srnet-seller-dns.conf' "$T/r4/etc/systemd/system/srnet-seller-dns.service" || fail "dns unit"
grep -q 'apt-get install .*unbound dns-root-data' "$T/r4/commands.log" || fail "unbound not installed"
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['listing']['dns']=='10.195.0.53'" "$T/r4/$U/listing/listing.json" || fail "listing dns"
python3 "$HERE/listing.py" verify "$T/r4/$U/listing/listing.json" >/dev/null || fail "listing with dns verifies"
grep -q '"dns_resolver": "10.195.0.53"' "$T/r4/$U/config/seller-policy.json" || fail "policy dns"
grep -qx 'DNS=1' "$T/r4/$U/config/install.conf" || fail "install.conf dns"
# --no-dns: no resolver, no DNS in policy, no dns in the listing; exit off: no resolver either
"$I" --binary "$T/fakebin" --enable-exit --i-accept-exit-operator-responsibility --dry-run --root "$T/r4n" \
  --payout $PAY --no-dns >/dev/null 2>&1 || fail "--no-dns refused"
grep -q '^policy=.*53' "$T/r4n/$U/config/exit.ini" && fail "--no-dns: DNS in policy"
[ -f "$T/r4n/etc/unbound/srnet-seller-dns.conf" ] && fail "--no-dns: resolver config written"
grep -q '10.195.0.53' "$T/r4n/$U/config/nat.nft" && fail "--no-dns: nft dns rules"
python3 -c "import json,sys;assert 'dns' not in json.load(open(sys.argv[1]))['listing']" "$T/r4n/$U/listing/listing.json" || fail "--no-dns: listing dns"
[ -f "$T/r2/etc/unbound/srnet-seller-dns.conf" ] && fail "exit off: resolver config written"
python3 -c "import json,sys;assert 'dns' not in json.load(open(sys.argv[1]))['listing']" "$T/r2/$U/listing/listing.json" || fail "exit off: listing dns"
if command -v nft >/dev/null && [ "$(id -u)" = 0 ]; then nft -c -f "$N" >/dev/null || fail "nat.nft with dns does not parse"; fi
ok
# 8b. per-buyer speed cap: default 10 Mbit/s each way (1250 kB/s) in nat.nft and the listing; 0 = none
N4="$T/r4/$U/config/nat.nft"
grep -q 'update @cap_up4 { ip saddr limit rate over 1250 kbytes/second' "$N4" || fail "no upload cap"
grep -q 'update @cap_down4 { ip daddr limit rate over 1250 kbytes/second' "$N4" || fail "no download cap"
grep -q '"speed_cap_mbps": 10' "$T/r4/$U/config/seller-policy.json" || fail "policy cap"
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['listing']['speed_cap_mbps']==10" "$T/r4/$U/listing/listing.json" || fail "listing cap"
grep -qx 'SPEED_CAP=10' "$T/r4/$U/config/install.conf" || fail "install.conf cap"
"$I" --binary "$T/fakebin" --enable-exit --i-accept-exit-operator-responsibility --dry-run --root "$T/r4u" \
  --payout $PAY --speed-cap-mbps 0 >/dev/null 2>&1 || fail "cap 0 refused"
grep -q 'cap_' "$T/r4u/$U/config/nat.nft" && fail "cap rules with --speed-cap-mbps 0"
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['listing']['speed_cap_mbps']==0" "$T/r4u/$U/listing/listing.json" || fail "listing cap 0"
"$I" --binary "$T/fakebin" --dry-run --root "$T/r4x" --speed-cap-mbps 5x >/dev/null 2>&1 && fail "bad cap accepted"
"$I" --binary "$T/fakebin" --enable-exit --i-accept-exit-operator-responsibility --dry-run --root "$T/r4c" \
  --payout $PAY --speed-cap-mbps 25 >/dev/null 2>&1
grep -q 'limit rate over 3125 kbytes/second burst 781 kbytes' "$T/r4c/$U/config/nat.nft" || fail "cap 25"
if command -v nft >/dev/null && [ "$(id -u)" = 0 ]; then
  for f in "$N4" "$T/r4u/$U/config/nat.nft" "$T/r4c/$U/config/nat.nft"; do nft -c -f "$f" >/dev/null || fail "$f does not parse"; done
fi
grep -q '"price_per_hour_usdc": "1.00"' "$T/r4/$U/config/seller-policy.json" || fail "default price not 1.00"
ok
# 8c. country: detected (services agree), --country overrides, disagreement/failure -> --country required
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['listing']['country']=='US'" "$T/r4/$U/listing/listing.json" || fail "detected country"
grep -q '"country": "US"' "$T/r4/$U/config/seller-policy.json" || fail "policy country"
out=$("$I" --binary "$T/fakebin" --dry-run --root "$T/r7" --payout $PAY 2>&1) || fail "detect"
echo "$out" | grep -q 'country detected from public IPv4: US' || fail "detected country not printed"
SRNET_GEOIP_URLS="file://$T/geo1 file://$T/geo3" "$I" --binary "$T/fakebin" --dry-run --root "$T/r7b" --payout $PAY >/dev/null 2>&1 && fail "disagreeing services accepted"
SRNET_GEOIP_URLS="file://$T/geo1 file://$T/missing" "$I" --binary "$T/fakebin" --dry-run --root "$T/r7c" --payout $PAY >/dev/null 2>&1 && fail "failed lookup accepted"
SRNET_GEOIP_URLS="file://$T/geo1 file://$T/geo3" "$I" --binary "$T/fakebin" --dry-run --root "$T/r7d" --payout $PAY --country de >/dev/null 2>&1 || fail "--country override refused"
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['listing']['country']=='DE'" "$T/r7d/$U/listing/listing.json" || fail "--country override"
"$I" --binary "$T/fakebin" --dry-run --root "$T/r7e" --payout $PAY --country USA >/dev/null 2>&1 && fail "bad --country accepted"
ok
# 8d. apt-get update first; router libs only for a prebuilt/release binary; quiet shaper; board text
grep -q 'apt-get update -q' "$T/r4/commands.log" || fail "no apt-get update"
grep -q 'apt-get install .*libevent-core-2.1-7t64 .*libzstd1' "$T/r4/commands.log" || fail "router libs not installed for --binary"
grep -q 'ip link show sr-tun-seller >/dev/null 2>&1 || exit 0' "$T/r4/$U/bin/srnet-seller-shape" || fail "shaper not quiet without TUN"
out=$("$I" --binary "$T/fakebin" --dry-run --root "$T/r8" --payout $PAY 2>&1)
echo "$out" | grep -q 'published to the SRNet board automatically' || fail "end text"
ok
# 9. idempotent: issuer + exit identity + quote key kept on re-run
P1=$(cat "$T/r4/$U/config/issuer.pub"); K1=$(sha256sum < "$T/r4/$U/config/exit.key"); S1=$(sha256sum < "$T/r4/$U/config/quote.key")
"$I" --binary "$T/fakebin" --dry-run --root "$T/r4" --payout $PAY >/dev/null 2>&1
[ "$P1" = "$(cat "$T/r4/$U/config/issuer.pub")" ] || fail "issuer rotated"
[ "$K1" = "$(sha256sum < "$T/r4/$U/config/exit.key")" ] || fail "identity rotated"
[ "$S1" = "$(sha256sum < "$T/r4/$U/config/quote.key")" ] || fail "quote key rotated"
ok
# 10. prebuilt: unpublished -> refused; sha mismatch -> refused; match -> accepted
"$I" --prebuilt --dry-run --root "$T/r5" >/dev/null 2>&1 && fail "prebuilt without pin accepted"
"$I" --prebuilt "$T/fakebin" --prebuilt-sha256 $(printf '0%.0s' {1..64}) --dry-run --root "$T/r5" >/dev/null 2>&1 && fail "sha mismatch accepted"
"$I" --prebuilt "$T/fakebin" --prebuilt-sha256 "$(sha256sum "$T/fakebin" | cut -d' ' -f1)" --dry-run --root "$T/r5" >/dev/null 2>&1 || fail "matching prebuilt refused"
ok
# 11. default mode builds from source
"$I" --dry-run --root "$T/r6" >/dev/null 2>&1
grep -q 'build-session-router.sh --install-deps' "$T/r6/commands.log" || fail "source build not default"; ok
grep -q 'apt-get install .*libunbound8' "$T/r6/commands.log" && fail "router libs installed for a source build"; ok
# 12. uninstall (keeps keys) and purge
"$I" --uninstall --dry-run --root "$T/r4" >/dev/null 2>&1
grep -q 'disable --now srnet-seller-exit.service' "$T/r4/commands.log" || fail "uninstall exit"
grep -q 'nft delete table inet srnet_seller' "$T/r4/commands.log" || fail "uninstall nft"
grep -q 'ufw route delete allow' "$T/r4/commands.log" || fail "uninstall ufw route"
grep -q 'disable --now srnet-seller-dns.service' "$T/r4/commands.log" || fail "uninstall dns"
grep -q 'ufw delete allow in on sr-tun-seller to 10.195.0.53 port 53 proto udp' "$T/r4/commands.log" || fail "uninstall dns ufw"
grep -q "rm -rf $T/r4/$U\$" "$T/r4/commands.log" && fail "uninstall purged without --purge"
"$I" --uninstall --purge --dry-run --root "$T/r4" >/dev/null 2>&1
grep -q "rm -rf $T/r4/$U\$" "$T/r4/commands.log" || fail "purge"
ok
# R2: the claim desk gets its own env (cap only, no RPC URLs), the watcher reads the listing,
# a re-install keeps the replaced listing for the watcher, and health checks the clock.
C5="$T/r5r/etc/systemd/system/srnet-seller-claimd.service"; W5="$T/r5r/etc/systemd/system/srnet-seller-watcher.service"
grep -q "EnvironmentFile=/$U/config/claimd.env" "$C5" || fail "claimd env file"
grep -q "^EnvironmentFile=/$U/config/watcher.env" "$C5" && fail "claimd loads watcher.env"
grep -q "InaccessiblePaths=.*/$U/config/watcher.env" "$C5" || fail "claimd can read watcher.env"
grep -qx 'CAP_USDC=5' "$T/r5r/$U/config/claimd.env" || fail "claimd cap"
grep -q 'rpc\|https' "$T/r5r/$U/config/claimd.env" && fail "RPC settings in claimd.env"
grep -qx 'SRNET_CLAIM_DEBUG_SOURCES=0' "$T/r5r/$U/config/claimd.env" || fail "source-tag debug not off by default"
grep -q -- "--listing /$U/listing/listing.json --issuer-key" "$W5" || fail "watcher doesn't read the listing"
[ -e "$T/r5r/$U/listing/previous.json" ] && fail "previous listing on a first install"
"$I" --binary "$T/fakebin" --dry-run --root "$T/r5r" --payout $PAY --price-per-hour-usdc 2.00 >/dev/null 2>&1 || fail "re-install"
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['listing']['price_per_hour_usdc']=='1.00'" "$T/r5r/$U/listing/previous.json" || fail "previous listing not kept"
python3 -c "import json,sys;assert json.load(open(sys.argv[1]))['listing']['price_per_hour_usdc']=='2.00'" "$T/r5r/$U/listing/listing.json" || fail "new listing"
[ -e "$T/r5r/$U/listing/listing.json.new" ] && fail "temp listing left"
grep -q 'chk "clock synced" "timedatectl show -p NTPSynchronized --value | grep -qx yes"' "$I" || fail "no clock check"
ok
echo "install dry-run tests: OK ($n)"
