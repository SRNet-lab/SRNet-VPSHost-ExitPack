"""Netns lab for inbound.py + the kill switch (cases: full rpfilter metrics foreign legacy container).

Runs inside `unshare -rn` after setup.sh: this netns is the client (eth0 = WAN 10.0.0.2/24 +
2001:db8::2/64, sr-tun0 = a veth standing in for the tunnel); the remote netns (pid in argv[1])
is the WAN gateway and the internet (198.51.100.1/.2, 2001:db8:ff::1) and counts on which link
each client packet arrives. The real inbound.up()/down() and KillSwitch.arm()/disarm() run with
the real nft and ip; the tunnel routes are added the way Session Router adds them.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
os.environ["PATH"] = "/usr/sbin:/sbin:" + os.environ["PATH"]
from srnet_exits import inbound, killswitch  # noqa: E402

RP = open(sys.argv[1]).read().strip()
CASE = sys.argv[2]
FAILS = []
BIT = f"{inbound.MARK:#x}"


def sh(c, ns=False):
    if ns:
        c = f"nsenter -t {RP} -n " + c
    r = subprocess.run(c, shell=True, text=True, capture_output=True)
    return r.stdout + r.stderr


def check(ok, label, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


KIDS = []


def remote_py(code, bg=False, timeout=10, keep=False):
    c = ["nsenter", "-t", RP, "-n", sys.executable, "-c", code]
    if bg:
        p = subprocess.Popen(c, stdout=subprocess.PIPE if not keep else subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, text=True)
        KIDS.append(p)
        return p
    return subprocess.run(c, text=True, capture_output=True, timeout=timeout)


def _cleanup():
    for p in KIDS:
        try:
            p.kill()
        except OSError:
            pass


import atexit  # noqa: E402
atexit.register(_cleanup)


def counters():
    d = {}
    for o in json.loads(sh("nft -j list counters table inet mon", ns=True))["nftables"]:
        if "counter" in o:
            d[o["counter"]["name"]] = o["counter"]["packets"]
    return d


def delta(fn):
    a = counters()
    try:
        res = fn()
    except Exception as e:  # noqa: BLE001
        res = e
    time.sleep(0.3)
    b = counters()
    return res, {k: b[k] - a[k] for k in b if b[k] != a[k]}


# remote services: tcp :80 v4+v6, udp :9999 v4+v6, udp+tcp :53
remote_py("""
import socket,threading,time
def tcp(fam,addr,port):
    s=socket.socket(fam);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    fam==socket.AF_INET6 and s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1);s.bind((addr,port));s.listen()
    while True:
        c,_=s.accept();c.sendall(b'hi');c.close()
def udp(fam,addr,port):
    s=socket.socket(fam,socket.SOCK_DGRAM);fam==socket.AF_INET6 and s.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1);s.bind((addr,port))
    while True:
        d,a=s.recvfrom(999);s.sendto(b'ok',a)
for a in [(socket.AF_INET,'0.0.0.0',80),(socket.AF_INET6,'::',80),(socket.AF_INET,'0.0.0.0',53)]:
    threading.Thread(target=tcp,args=a,daemon=True).start()
for a in [(socket.AF_INET,'0.0.0.0',9999),(socket.AF_INET,'0.0.0.0',53),(socket.AF_INET6,'::',9999)]:
    threading.Thread(target=udp,args=a,daemon=True).start()
time.sleep(10000)
""", bg=True, keep=True)


def ssh_server(fam, addr):
    s = socket.socket(fam)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if fam == socket.AF_INET6:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.bind((addr, 22))
    s.listen()
    while True:
        c, _ = s.accept()

        def h(c=c):
            try:
                while True:
                    d = c.recv(99)
                    if not d:
                        break
                    c.sendall(b"SSH:" + d)
            except OSError:
                pass
        threading.Thread(target=h, daemon=True).start()


threading.Thread(target=ssh_server, args=(socket.AF_INET, "0.0.0.0"), daemon=True).start()
threading.Thread(target=ssh_server, args=(socket.AF_INET6, "::"), daemon=True).start()
time.sleep(0.5)


def remote_ssh(dst, fam="AF_INET", src=None):
    bind = f"s.bind(({src!r},0));" if src else ""
    r = remote_py(f"import socket;s=socket.socket(socket.{fam});s.settimeout(2);{bind}s.connect(({dst!r},22));"
                  "s.sendall(b'x');print(s.recv(9))")
    return (r.stdout or r.stderr).strip()


def tcp(dst, port=80, fam=socket.AF_INET):
    s = socket.socket(fam)
    s.settimeout(1.5)
    s.connect((dst, port))
    d = s.recv(9)
    s.close()
    return d


def udp(dst, port, fam=socket.AF_INET):
    s = socket.socket(fam, socket.SOCK_DGRAM)
    s.settimeout(1.5)
    s.sendto(b"q", (dst, port))
    return s.recvfrom(9)[0]


def tunnel_routes(add=True):
    op = "add" if add else "del"
    sh(f"ip route {op} 0.0.0.0/1 dev sr-tun0; ip route {op} 128.0.0.0/1 dev sr-tun0; "
       f"ip -6 route {op} ::/1 dev sr-tun0; ip -6 route {op} 8000::/1 dev sr-tun0")


def svm():
    return open("/proc/sys/net/ipv4/conf/all/src_valid_mark").read().strip()


def teardown_checks(ks=True, extra_rules=""):
    if ks:
        killswitch.KillSwitch().disarm()
    inbound.down()
    tunnel_routes(False)
    rs = sh("nft list ruleset")
    check("srnet" not in rs, "teardown: no srnet nft tables left", rs[:200])
    rules = sh("ip rule; ip -6 rule")
    check(BIT not in rules, "teardown: no fwmark rule left", rules)
    check(svm() == "0", "teardown: src_valid_mark back to 0", svm())


# ---------------------------------------------------------------------------------------------
if CASE == "full":
    pre = remote_py("""
import socket,time
s=socket.create_connection(('10.0.0.2',22),timeout=3)
s.sendall(b'a');print('pre1',s.recv(99),flush=True)
time.sleep(4)
try:
    s.sendall(b'b');print('pre2',s.recv(99),flush=True)
except Exception as e: print('pre2 FAIL',e,flush=True)
""", bg=True)
    time.sleep(0.8)
    check(svm() == "0", "src_valid_mark starts at 0")
    inbound.up("eth0")
    killswitch.KillSwitch().arm("eth0", bootstrap_ips=[])
    tunnel_routes()
    check(svm() == "1", "src_valid_mark is 1 while connected")
    # debug: count packets leaving eth0 that are not ours to send (no mark bit, not ND)
    sh(f"""nft 'add table inet dbg; add chain inet dbg out {{ type filter hook output priority 100; }};
        add rule inet dbg out oifname "eth0" meta mark and {BIT} == 0 meta l4proto {{ tcp, udp }} counter'""")
    out = pre.communicate(timeout=10)[0]
    check("pre2 b'SSH:b'" in out, "SSH session opened before connect survives", out)
    time.sleep(1)   # let that session's FIN exchange (marked replies on the WAN) finish
    for label, fn, fam6 in [("local TCP", lambda: tcp("198.51.100.1"), False),
                            ("local UDP", lambda: udp("198.51.100.1", 9999), False),
                            ("local DNS udp/53", lambda: udp("198.51.100.1", 53), False),
                            ("local DNS tcp/53", lambda: tcp("198.51.100.1", 53), False),
                            ("local TCP6", lambda: tcp("2001:db8:ff::1", 80, socket.AF_INET6), True),
                            ("local UDP6", lambda: udp("2001:db8:ff::1", 9999, socket.AF_INET6), True)]:
        _, d = delta(fn)
        wan = {k: v for k, v in d.items() if k.startswith("wan") and k != "wan6_icmp"}
        check(not wan and d.get("tun_any", 0) > 0, f"{label} goes only into the tunnel", d)
    res, d = delta(lambda: remote_ssh("10.0.0.2"))
    check("SSH:x" in res and not d.get("tun_tcp"), "new inbound SSH v4 works, replies on the WAN", (res, d))
    res = remote_ssh("10.0.0.2", src="198.51.100.1")
    check("SSH:x" in res, "new inbound SSH v4 from an internet address works", res)
    res, d = delta(lambda: remote_ssh("2001:db8::2", "AF_INET6"))
    check("SSH:x" in res, "new inbound SSH v6 works", res)
    sh("ip -6 neigh flush all")
    sh("ip -6 neigh flush all", ns=True)
    res = remote_ssh("2001:db8::2", "AF_INET6")
    check("SSH:x" in res, "inbound SSH v6 after the neighbor caches were flushed (R5-1)", res)
    dbg = sh("nft list table inet dbg")
    check("packets 0 " in dbg, "no unmarked TCP/UDP left on eth0", dbg)
    sh("nft delete table inet dbg")
    teardown_checks()

elif CASE == "rpfilter":
    for i in ("all", "default", "eth0"):
        open(f"/proc/sys/net/ipv4/conf/{i}/rp_filter", "w").write("1")
    inbound.up("eth0")
    killswitch.KillSwitch().arm("eth0", bootstrap_ips=[])
    tunnel_routes()
    res = remote_ssh("10.0.0.2", src="198.51.100.1")
    check("SSH:x" in res, "strict rp_filter: inbound SSH from an internet address works (R5-3)", res)
    teardown_checks()
    check(open("/proc/sys/net/ipv4/conf/all/rp_filter").read().strip() == "1", "rp_filter itself untouched")

elif CASE == "metrics":
    sh("ip route del default; ip route add default via 10.0.0.1 metric 100; "
       "ip route add default via 10.0.0.254 metric 200")
    inbound.up("eth0")
    t = sh("ip route show table 5352")
    check("metric 100" in t and "metric 200" in t, "both WAN defaults copied with their metrics (R5-8)", t)
    g = sh(f"ip route get 198.51.100.1 mark {BIT}")
    check("via 10.0.0.1 " in g, "marked replies use the primary gateway", g)
    teardown_checks(ks=False)

elif CASE == "foreign":
    sh("ip rule add fwmark 0x1 lookup 5352 priority 5352; ip route add 192.0.2.0/24 dev eth0 table 5352; "
       "ip route add 192.0.2.0/24 dev eth0 table 5353")
    before = sh("ip rule; ip route show table 5352; ip route show table 5353")
    inbound.up("eth0")
    rules = sh("ip rule")
    check(f"fwmark {BIT}/{BIT} lookup 5354" in rules, "skips a foreign rule/table at 5352 and a used table 5353 (R5-7)", rules)
    check(sh("ip route show table 5352").strip() == "192.0.2.0/24 dev eth0 scope link",
          "foreign table 5352 untouched while connected")
    inbound.down()
    after = sh("ip rule; ip route show table 5352; ip route show table 5353")
    check(before == after, "foreign rule and tables untouched after disconnect", after)
    # existing marks of other software keep their bits
    sh("nft 'add table inet other; add chain inet other pre { type filter hook prerouting priority -200; }; "
       "add rule inet other pre ct mark set 0x1 meta mark set 0x1'")
    inbound.up("eth0")
    tunnel_routes()
    res = remote_ssh("10.0.0.2")
    ct = sh("cat /proc/net/nf_conntrack 2>/dev/null; conntrack -L 2>/dev/null")
    check("SSH:x" in res, "inbound SSH works while other software sets marks", res)
    if "mark=" in ct:
        check(f"mark={0x1 | inbound.MARK}" in ct, "conntrack mark keeps the other bit (0x1|bit)", ct[:300])
    else:
        print("info conntrack table not readable here; mark bits checked by the unit tests")
    inbound.down()
    tunnel_routes(False)

elif CASE == "legacy":
    # connected with an earlier version (whole-mark rule 0x5352), then updated and disconnected
    sh("ip rule add fwmark 0x5352 lookup 5352 priority 5352; ip route add default via 10.0.0.1 table 5352; "
       "ip rule add fwmark 0x5352 lookup 100 priority 5000")
    inbound.down()
    rules = sh("ip rule")
    check("lookup 5352" not in rules and sh("ip route show table 5352").strip() == "",
          "the earlier version's rule and table are removed", rules)
    check("lookup 100" in rules, "a similar rule that is not exactly ours stays", rules)

elif CASE == "container":
    # a "container" netns behind a bridge-like veth, port 8080 on eth0 published to it (DNAT)
    cp = subprocess.Popen(["unshare", "-n", "sleep", "100000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    KIDS.append(cp)
    time.sleep(0.3)
    C = f"nsenter -t {cp.pid} -n "
    sh(f"ip link add docker0 type veth peer name ceth0; ip link set ceth0 netns {cp.pid}; "
       "ip addr add 172.17.0.1/16 dev docker0; ip link set docker0 up")
    sh(C + "ip link set lo up; " + C + "ip addr add 172.17.0.2/16 dev ceth0; " + C + "ip link set ceth0 up; "
       + C + "ip route add default via 172.17.0.1")
    open("/proc/sys/net/ipv4/ip_forward", "w").write("1")
    sh("nft 'add table ip nat; add chain ip nat pre { type nat hook prerouting priority dstnat; }; "
       "add rule ip nat pre iifname \"eth0\" tcp dport 8080 dnat to 172.17.0.2:80'")
    srv = subprocess.Popen(C.split() + [sys.executable, "-c",
                           "import socket\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
                           "s.bind(('0.0.0.0',80));s.listen()\nwhile True:\n c,_=s.accept();c.sendall(b'ctr');c.close()"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    KIDS.append(srv)
    time.sleep(0.5)
    inbound.up("eth0")
    killswitch.KillSwitch().arm("eth0", bootstrap_ips=[])
    tunnel_routes()
    r, d = delta(lambda: remote_py("import socket;s=socket.socket();s.settimeout(2);s.bind(('198.51.100.1',0));"
                                   "s.connect(('10.0.0.2',8080));print(s.recv(9))"))
    out = (r.stdout or r.stderr).strip()
    check("ctr" in out and d.get("wan_tcp", 0) > 0 and not d.get("tun_tcp"),
          "published container port answers on the WAN while connected, nothing via the tunnel (R5-6)", (out, d))
    teardown_checks()
    srv.kill()
    cp.kill()
else:
    sys.exit(f"unknown case {CASE}")

print("FAILED: " + "; ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
