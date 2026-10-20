# Netns lab. Runs as root inside `unshare -rn` (this ns = the client machine);
# creates the "remote" ns (the internet + the WAN gateway) and links eth0 (WAN) and sr-tun0 (stand-in tunnel).
set -e
export PATH=/usr/sbin:/sbin:$PATH
unshare -n sleep 100000 & RP=$!; sleep 0.3; echo $RP > "$LABRP"
R="nsenter -t $RP -n"
ip link set lo up
ip link add eth0 type veth peer name peer0
ip link add sr-tun0 type veth peer name exitpeer
ip link set peer0 netns $RP; ip link set exitpeer netns $RP
ip addr add 10.0.0.2/24 dev eth0; ip -6 addr add 2001:db8::2/64 dev eth0 nodad
ip link set eth0 up
ip addr add 10.9.0.2/24 dev sr-tun0; ip -6 addr add fd00:9::2/64 dev sr-tun0 nodad; ip link set sr-tun0 up
$R ip link set lo up
$R ip addr add 198.51.100.1/32 dev lo; $R ip addr add 198.51.100.2/32 dev lo; $R ip -6 addr add 2001:db8:ff::1/128 dev lo
$R ip addr add 10.0.0.1/24 dev peer0; $R ip -6 addr add 2001:db8::1/64 dev peer0 nodad; $R ip -6 addr add fe80::1/64 dev peer0 nodad
$R ip link set peer0 up
$R ip addr add 10.9.0.1/24 dev exitpeer; $R ip -6 addr add fd00:9::1/64 dev exitpeer nodad; $R ip link set exitpeer up
# remote routes back to the client: its public IP via the WAN link; the exit path is the other link
$R ip route add default via 10.0.0.2 2>/dev/null || true
$R sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0
for i in all default peer0 exitpeer; do $R sysctl -qw net.ipv4.conf.$i.rp_filter=0; done
# like a real gateway: ARP asks from its own on-link address, not from the internet source being forwarded
$R sysctl -qw net.ipv4.conf.all.arp_announce=2 net.ipv4.conf.peer0.arp_announce=2
sleep 1
ip route add default via 10.0.0.1 dev eth0
ip -6 route add default via fe80::1 dev eth0
# remote: count which link the client's packets arrive on
$R nft -f - <<'NFT'
table inet mon {
  chain pre { type filter hook prerouting priority -300; policy accept;
    iifname "peer0" ip saddr 10.0.0.2 ip protocol tcp counter name wan_tcp
    iifname "peer0" ip saddr 10.0.0.2 ip protocol udp counter name wan_udp
    iifname "peer0" ip saddr 10.0.0.2 ip protocol icmp counter name wan_icmp
    iifname "peer0" ip6 saddr 2001:db8::2 meta l4proto tcp counter name wan6_tcp
    iifname "peer0" ip6 saddr 2001:db8::2 meta l4proto udp counter name wan6_udp
    iifname "peer0" meta l4proto ipv6-icmp counter name wan6_icmp
    iifname "exitpeer" counter name tun_any
    iifname "exitpeer" meta l4proto tcp counter name tun_tcp
  }
  counter wan_tcp {}
  counter wan_udp {}
  counter wan_icmp {}
  counter wan6_tcp {}
  counter wan6_udp {}
  counter wan6_icmp {}
  counter tun_any {}
  counter tun_tcp {}
}
NFT
