"""Commands the exits add-on adds to `srnet` (and that `srnet-exits` offers on its own).

  srnet exits                          list sellers from the signed board
  srnet buy --seller ID --hours N      get a quote, pay it from any wallet, wait for the exit to admit you
  srnet paid [--quote ID] [--tx HASH]  carry on after `buy --no-wait` (or tell the seller a tx hash)
  srnet connect ID                     route your traffic through that exit (kill switch, exit DNS)
  srnet status                         paid time left, connection, kill switch, DNS
  srnet disconnect                     restore DNS, unmap, remove the kill switch

You pay the seller directly in USDC on Arbitrum or Base, from your own wallet, and pay the network
fee yourself. The amount is exact to the last digit: its last digits tell the seller which quote you
paid. Payments are final. Nothing renews by itself; about 10 minutes before the end you get a
reminder, and buying again adds time.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
import time
from pathlib import Path

import chains as C  # noqa: E402

from . import board as boardmod, config, inbound, listings, killswitch, pay, priv, reminder, routercfg, tunneldns, warning
from . import state as S
from .backend import LiveBackend
from .prog import P as _P


# ------------------------------------------------------------------ helpers
def load_listings(args, st, quiet: bool = False) -> list[dict]:
    """--listings FILE if given, else the signed board fetched over Session Router."""
    cfg = config.load(st.root)
    lp = getattr(args, "listings", None) or cfg.get("listings_path")
    if lp:
        return listings.load_board(Path(lp))
    good, info = boardmod.fetch(boardmod.sources(cfg), boardmod.pin_of(cfg), st.root, dns=config.sesh_dns(cfg))
    if not quiet:
        src = "cached copy" if info["source"] == "cache" else info["source"]
        print(f"board seq {info['seq']} from {src}; {len(info['dropped'])} listing(s) dropped", file=sys.stderr)
    return good


def own_sesh() -> str:
    """This machine's .sesh address: Session Router answers localhost.sesh with a CNAME to it."""
    import socket
    try:
        name = socket.getaddrinfo("localhost.sesh", 80, flags=socket.AI_CANONNAME)[0][3].rstrip(".")
    except OSError:
        return ""
    return name if name.endswith(".sesh") and name != "localhost.sesh" else ""


def _rpc(endpoint: str):
    from .rpc import Rpc
    return Rpc(endpoint)


def _left(sec: int) -> str:
    sec = max(0, int(sec))
    if sec >= 3600:
        return f"{sec // 3600} h {(sec % 3600) // 60:02d} min"
    return f"{sec // 60} min" if sec >= 60 else f"{sec} s"


def _hhmm(t: int) -> str:
    """This machine's local time with its zone (say 14:33 CDT), never bare UTC."""
    return time.strftime("%H:%M %Z", time.localtime(t)).strip()


def qr_text(data: str) -> str:
    """Terminal QR if the optional `qrcode` package is installed, else ''."""
    try:
        import io
        import qrcode  # type: ignore
        q = qrcode.QRCode(border=1)
        q.add_data(data)
        q.make(fit=True)
        buf = io.StringIO()
        q.print_ascii(out=buf, invert=True)
        return buf.getvalue()
    except Exception:
        return ""


def extra_text(i: dict) -> str:
    """The price and the payment ID part of the amount, so a larger extra under load is visible."""
    return (f"That is {i['price_text']} USDC for {i['hours']} h plus {i['extra_text']} USDC as a payment ID "
            "(it makes your payment unique; the seller keeps it).")


def payment_text(i: dict) -> str:
    qr = qr_text(i["uri"])
    return "\n".join(x for x in [
        f"Pay exactly {i['amount_text']} USDC on {i['chain']} to:",
        f"  {i['address']}",
        extra_text(i),
        "",
        qr.rstrip("\n") if qr else "",
        f"Payment link (most wallets): {i['uri']}",
        f"USDC contract on {i['chain']}: {i['token_address']}",
        f"Quote {i['id']}: {i['hours']} h, good until {_hhmm(i['expires'])} (about 15 min).",
        "Send that exact amount: a different amount or another chain can't be matched to you.",
        "You pay the network fee yourself. The payment is final.",
    ] if x is not None)


# ------------------------------------------------------------------ commands
def cmd_exits(args, st) -> int:
    cfg = config.load(st.root)
    try:
        board = load_listings(args, st)
    except boardmod.B.BoardError as e:
        print(f"board unavailable: {e}", file=sys.stderr)
        print("is session-router running? or pass --listings FILE", file=sys.stderr)
        return 1
    if not board:
        print("(no valid listings)")
        return 0
    print("Exits are Beta: they work but are not production-certified; keep amounts small.", file=sys.stderr)
    print(boardmod.table(board, time.time(), config.testnets(cfg)))
    if any(not listings.exit_dns(d["listing"]) for d in board):
        print("EXIT DNS NONE: that exit runs no resolver of its own. srnet refuses to buy from or connect "
              "to it unless you pass --system-dns, and then your DNS goes out your normal connection, "
              "outside the exit.", file=sys.stderr)
    return 0


def _events(kind, data):
    if kind == "waiting":
        near = data.get("near") or []
        extra = f" (seen other amounts: {', '.join(C.fmt_usdc(v) for v in near[:3])} USDC)" if near else ""
        print(f"  waiting for the payment, {_left(data['left'])} left on the quote{extra}", file=sys.stderr)
    elif kind == "seen":
        print(f"payment seen: {data['tx']} (block {data['block']}); telling the seller", file=sys.stderr)
    elif kind == "reported":
        print("seller notified; waiting for it to confirm", file=sys.stderr)
    elif kind == "confirming" and (data or {}).get("note") == "chain_unavailable":
        print("  the seller can't read the chain right now; it keeps checking by itself", file=sys.stderr)
    elif kind in ("rpc_error", "desk_error"):
        print(f"  note: {data}", file=sys.stderr)


def _finish(b: pay.Buyer, rec: dict, out: dict, st) -> int:
    s = out.get("status")
    if s == "admitted":
        lease = pay.active_lease(st, rec["seller"]) or {}
        end = int(lease.get("end") or out.get("lease_end") or 0)
        print(f"paid: the seller confirmed your exit time ({_left(end - time.time())})")
        print(f"connect: {_P()} connect {rec['seller']}")
        print(f"You're in: exit time until {_hhmm(end)}")
        try:
            reminder.start(st.root, rec["seller"])
        except OSError:
            pass
        return 0
    print(out.get("message") or out.get("reason") or f"status: {s}", file=sys.stderr)
    if s == "waiting":
        print(f"check again: {_P()} paid --quote {rec['id']}", file=sys.stderr)
    return 1


def cmd_buy(args, st) -> int:
    seller = args.seller or args.seller_pos
    if not seller:
        print(f"which seller? {_P()} buy --seller <id> --hours N (see: {_P()} exits)", file=sys.stderr)
        return 2
    warning.ensure_ack(st.root)
    if not _router_ok(config.load(st.root), need=("persistent key",), privileged=False):
        return 1
    client_sesh = args.client_sesh or own_sesh()
    if not client_sesh:
        print(f"could not read your .sesh address from Session Router (is .sesh DNS working? run {_P()} doctor); "
              "or pass --client-sesh ADDRESS.sesh", file=sys.stderr)
        return 1
    try:
        doc = listings.find_seller(load_listings(args, st, quiet=True), seller)
    except boardmod.B.BoardError as e:
        print(f"board unavailable: {e}", file=sys.stderr)
        return 1
    if not doc:
        print(f"seller not on the board (see: {_P()} exits)", file=sys.stderr)
        return 1
    if not listings.exit_dns(doc["listing"]) and not args.system_dns:
        print("this exit offers no DNS resolver of its own, so your DNS would leave outside the exit. "
              "Pick another exit, or buy knowingly with --system-dns", file=sys.stderr)
        return 1
    b = pay.Buyer(st)
    try:
        rec = b.quote(doc, args.hours, client_sesh, chain=args.chain, max_usdc=args.max_usdc)
    except pay.BuyError as e:
        print(f"no quote: {e}", file=sys.stderr)
        return 1
    print(payment_text(b.info(rec)))
    if args.no_wait:
        print(f"\nafter paying: {_P()} paid --quote {rec['id']}   (add --tx 0x... if you have the hash)")
        return 0
    print(f"\nwatching the chain for your payment (Ctrl-C is safe; carry on with: {_P()} paid)", file=sys.stderr)
    try:
        out = b.watch(rec, _events)
    except pay.BuyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return _finish(b, rec, out, st)


def cmd_paid(args, st) -> int:
    b = pay.Buyer(st)
    try:
        rec = b.find(args.quote, args.seller, with_expired=bool(args.tx))
        if rec["state"] == "admitted":
            return _finish(b, rec, {"status": "admitted"}, st)
        if rec["state"] in ("rejected", "expired") and not args.tx:
            print(rec.get("reason") or f"that quote is {rec['state']}", file=sys.stderr)
            return 1
        if args.tx and rec["state"] != "open" and rec.get("tx") != args.tx.lower():
            rec["state"] = "open"
        out = b.watch(rec, _events, tx=args.tx.lower() if args.tx else None)
    except pay.BuyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return _finish(b, rec, out, st)


def cmd_connect(args, st) -> int:
    warning.ensure_ack(st.root)
    cfg = config.load(st.root)
    lease = pay.active_lease(st, args.seller)
    if not lease:
        print(f"no paid time for this seller; buy some first: {_P()} buy --seller {args.seller} --hours 1",
              file=sys.stderr)
        return 1
    print(f"paid time left: {_left(lease['end'] - time.time())} (until {_hhmm(lease['end'])})")
    if not args.mock and not _router_ok(cfg):
        return 1
    tun = cfg.get("tun", killswitch.DEFAULT_TUN)
    dns_saved = dns_ip = None
    if not args.system_dns:
        dns_ip, why = exit_dns_for(args, st, lease, tun)
        if not dns_ip:
            print(why, file=sys.stderr)
            return 1
        if not args.mock and not tunneldns.resolved_active():
            print("cannot route system DNS through the exit: systemd-resolved is not running (resolvectl). "
                  "Without it DNS would leak outside the exit. Use systemd-resolved (Ubuntu default), or "
                  "connect knowingly with --system-dns", file=sys.stderr)
            return 1
        if not args.mock:
            try:
                dns_saved = dns_original(st, tun)
            except tunneldns.TunnelDNSError as e:
                print(f"cannot read the resolver settings of {tun}: {e} (is Session Router running?)", file=sys.stderr)
                return 1
    ks = killswitch.KillSwitch(tun=tun, overlay_udp_port=int(cfg.get("overlay_udp_port", 1090)))
    wan = args.wan_if or cfg.get("wan_if") or guess_wan(tun)
    # first, while the main table still has only the WAN routes: replies to connections that come
    # in on the WAN (SSH/RDP to this machine's public IP) keep going back out the WAN
    try:
        inbound.up(wan)
        print(f"inbound sessions on {wan} (SSH, RDP, ...) keep working; everything this machine starts "
              "goes through the exit")
    except Exception as e:
        print(f"could not keep inbound sessions on {wan} working: {e}", file=sys.stderr)
        print("not connecting: on a remote machine this would cut your SSH/RDP session. "
              "Pass --wan-if IFACE if the WAN interface is wrong.", file=sys.stderr)
        return 1
    armed = False
    if args.no_killswitch:
        print("kill switch: OFF (--no-killswitch): if the tunnel drops, traffic goes out your normal connection")
    else:
        try:
            ks.arm(wan, bootstrap_ips=cfg.get("bootstrap_ips") or [])
            armed = True
            print(f"kill switch armed on {wan} (tun={ks.tun})")
        except Exception as e:
            print(f"kill switch NOT armed: {e}", file=sys.stderr)
            if not args.allow_no_ks:
                print("not connecting without the kill switch. Fix the above, or connect knowingly "
                      f"without it: {_P()} connect {args.seller} --no-killswitch", file=sys.stderr)
                _inbound_down(quiet=True)
                return 1
            print("continuing WITHOUT the kill switch (--allow-no-ks)", file=sys.stderr)
    b = LiveBackend(_rpc(args.rpc_endpoint or cfg.get("omq") or config.OMQ), st)
    from lease_flow import connect as lease_connect
    result = lease_connect(b, args.seller, capable_timeout_s=float(args.timeout))
    if result.ok and dns_saved is not None:
        try:
            dns_up(st, dns_saved, dns_ip)
            print(f"DNS: every lookup now goes to the exit's own resolver ({dns_ip}) through the tunnel; "
                  ".sesh stays on Session Router. If the exit's resolver stops answering, lookups fail "
                  "(no fallback to your normal DNS).")
        except Exception as e:
            print(f"could not route DNS through the exit: {e}", file=sys.stderr)
            print("disconnecting so DNS does not leak outside the exit", file=sys.stderr)
            dns_down(st, quiet=True)
            b.unmap_exit(args.seller)
            if armed:
                print(f"the kill switch is still armed (clearnet blocked); remove it with: {_P()} disconnect",
                      file=sys.stderr)
            print(json.dumps({"ok": False, "reason": "tunnel_dns_failed", "killswitch": armed}))
            return 1
    elif result.ok:
        print("DNS: left as it is (--system-dns): lookups may go out your normal connection, outside the exit",
              file=sys.stderr)
    if result.ok:
        conn = {"seller": args.seller, "at": int(time.time()), "killswitch": armed,
                "dns": "tunnel" if dns_saved is not None else "system", "exit_dns": dns_ip,
                "lease_end": int(lease["end"])}
        S.update(st, lambda d: d.__setitem__("connected", conn))
        try:
            reminder.start(st.root, args.seller)
        except OSError:
            pass
        print(json.dumps({"ok": True, "killswitch": armed, "dns": st.data["connected"]["dns"],
                          "lease_end": int(lease["end"]), "steps": result.steps}))
        return 0
    print(json.dumps({"ok": False, "reason": result.reason, "steps": result.steps, "killswitch": armed}))
    if armed:
        print(f"the kill switch is still armed (clearnet blocked); remove it with: {_P()} disconnect", file=sys.stderr)
    return 1


def exit_dns_for(args, st, lease: dict, tun: str) -> tuple[str | None, str]:
    """The seller's exit resolver: recorded at buy, else read from the seller's current listing."""
    ip = lease.get("dns")
    if not ip:
        try:
            doc = listings.find_seller(load_listings(args, st, quiet=True), args.seller)
        except Exception as e:
            return None, (f"cannot find this exit's DNS resolver: the board is unavailable ({e}). Not connecting, "
                          "so DNS cannot leak outside the exit; or connect knowingly with --system-dns")
        ip = listings.exit_dns(doc["listing"]) if doc else None
    if not ip:
        return None, ("refusing to connect: this exit offers no DNS resolver of its own, so your DNS would go out "
                      f"your normal connection, outside the exit. Use another exit ({_P()} exits), or connect "
                      "knowingly with --system-dns")
    for net in tunneldns.tun_networks(tun):
        if ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(net):
            return None, (f"refusing to connect: the exit's resolver {ip} lies inside your own tunnel range {net}; "
                          "set a different [network] ifaddr in Session Router, or use --system-dns")
    return ip, ""


def dns_original(st, tun: str) -> dict:
    """The tunnel link's resolver settings to restore later. A record left by an earlier connect
    (never disconnected) is the original; never snapshot our own settings as the original."""
    rec = st.data.get("tunnel_dns") or {}
    if rec.get("saved"):
        return rec["saved"]
    snap = tunneldns.snapshot(tun)
    if tunneldns.is_ours(snap):
        snap = {"tun": tun, "dns": [d for d in snap["dns"] if d != tunneldns.LISTEN] or ["127.3.2.1"],
                "domains": [d for d in snap["domains"] if d != "~."], "default_route": False}
    return snap


def dns_up(st, saved: dict, upstream: str) -> None:
    rec = {"saved": saved, "at": int(time.time()), "exit_dns": upstream}
    # saved first: a crash after this point can still be undone by disconnect
    S.update(st, lambda d: d.__setitem__("tunnel_dns", rec))
    pid = tunneldns.start_proxy(st.root, saved["tun"], upstream)

    def with_pid(d):
        if d.get("tunnel_dns"):
            d["tunnel_dns"]["forwarder_pid"] = pid
    S.update(st, with_pid)
    tunneldns.apply(saved)


def dns_down(st, quiet: bool = False) -> bool:
    """Restore the saved resolver settings and stop the forwarder. True if nothing is left to undo."""
    rec = st.data.get("tunnel_dns") or {}
    ok = True
    if rec.get("saved"):
        try:
            cur = tunneldns.snapshot(rec["saved"]["tun"])
        except tunneldns.TunnelDNSError:
            cur = None
        if cur is not None and cur != rec["saved"]:
            try:
                tunneldns.restore(rec["saved"])
                if not quiet:
                    sv = rec["saved"]
                    dns = " ".join(sv["dns"]) or "(none)"
                    doms = " ".join(sv["domains"]) or "(none)"
                    sys.stderr.flush()
                    print(f"DNS restored on {sv['tun']}: servers {dns}, domains {doms}, "
                          f"default route {'yes' if sv['default_route'] else 'no'}", flush=True)
            except tunneldns.TunnelDNSError as e:
                ok = False
                print(f"DNS NOT restored: {e}", file=sys.stderr)
    tunneldns.stop_proxy(st.root)
    if ok:
        S.update(st, lambda d: d.__setitem__("tunnel_dns", None))
    return ok


def cmd_setup_router(args, st) -> int:
    """Set keyfile= and blackhole-routes=false in Session Router's client.ini (one sudo call;
    everything else in the file stays), make the key, restart the router. Safe to run again."""
    cfg = config.load(st.root)
    ini = args.ini or cfg.get("router_ini") or routercfg.DEFAULT_INI
    if not Path(ini).exists():
        print(f"{ini} not found: install Session Router first (SRNet-Desktop README, Install, steps 1 and 2), "
              f"then run: {_P()} setup-router", file=sys.stderr)
        return 1
    # -I: no current directory or PYTHON* variables on sys.path for this root process; cwd="/"
    cmd = [sys.executable, "-I", "-m", "srnet_exits.routercfg", "apply", "--ini", ini, "--prog", _P()]
    if args.no_restart:
        cmd.append("--no-restart")
    try:
        r = priv.run(cmd, check=False, what="setting up Session Router for exits", cwd="/")
    except priv.PrivError as e:
        print(f"could not set up Session Router: {e}", file=sys.stderr)
        return 1
    out = (r.stdout or "").strip()
    if out:
        print(out)
    if r.returncode != 0:
        print((r.stderr or "").strip() or "setup-router failed", file=sys.stderr)
    return r.returncode


def _router_ok(cfg: dict, need=("persistent key", "blackhole-routes"), privileged: bool = True) -> bool:
    """Refuse (with the one-time fix) when Session Router's client.ini would break exits."""
    path = cfg.get("router_ini") or routercfg.DEFAULT_INI
    found = routercfg.problems(path, privileged=privileged)
    if found is None:
        # buy (no sudo) stays quiet here: connect checks again through sudo before anything changes
        if privileged and Path(path).exists():
            print(f"note: could not read {path} to check Session Router's settings for exits "
                  f"(to check and fix it: {_P()} setup-router)", file=sys.stderr)
        return True
    probs = [p for p in found if any(n in p for n in need)]
    if not probs:
        return True
    for p in probs:
        print(p + ".", file=sys.stderr)
    print(f"Fix it once, then try again: {routercfg.fix()}  (asks sudo; keeps the rest of client.ini)",
          file=sys.stderr)
    return False


def _inbound_down(quiet: bool = False) -> None:
    try:
        inbound.down()
    except Exception as e:
        if not quiet:
            print(f"note: could not remove the inbound-session routing (harmless, gone at reboot): {e}",
                  file=sys.stderr)


def guess_wan(tun: str = killswitch.DEFAULT_TUN) -> str:
    """The interface of the default route that is not the tunnel (also right while connected)."""
    for cmd in (["ip", "-o", "route", "show", "default"], ["ip", "-o", "route", "get", "1.1.1.1"]):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        for line in out.splitlines():
            parts = line.split()
            if "dev" in parts:
                dev = parts[parts.index("dev") + 1]
                if dev != tun:
                    return dev
    return "eth0"


def ks_word(v):
    if v is None:
        return (f"unknown (checking needs root; {_P()} connect/disconnect ask sudo themselves; to look: "
                "sudo nft list table inet srnet_ks)" if killswitch.find_nft() else "unavailable (nftables not installed)")
    return "armed" if v else "off"


def dns_word(st) -> str:
    rec = st.data.get("tunnel_dns") or {}
    if not rec:
        return "system (normal resolver)"
    pid = tunneldns.proxy_pid(st.root)
    return (f"through the exit's own resolver {rec.get('exit_dns') or '?'} (forwarder pid {pid})" if pid
            else f"through the exit, but the forwarder is not running: lookups fail until {_P()} disconnect")


def status_info(st, cfg: dict, endpoint: str | None = None, rpc_probe: bool = True) -> dict:
    now = time.time()
    ex = st.data.get("exits") or {}
    open_q = [{"id": r["id"], "seller": r["seller"], "state": r["state"],
               "amount": C.fmt_usdc(r["doc"]["quote"]["amount"]), "chain": C.label(r["doc"]["quote"]["chainid"]),
               "expires": r["doc"]["quote"]["expires"]}
              for r in (ex.get("quotes") or {}).values() if r["state"] in ("open", "reported")]
    info = {"leases": [l for l in pay.leases(st, now) if l["left_s"] > 0], "quotes": open_q,
            "connected": st.data.get("connected"), "dns": dns_word(st),
            "visibility_ack": (st.root / "visibility.ack").exists()}
    if rpc_probe:
        ks = killswitch.KillSwitch(tun=cfg.get("tun", killswitch.DEFAULT_TUN))
        try:
            info["killswitch"] = ks_word(ks.status())
        except Exception as e:
            info["killswitch"] = f"unknown ({e})"
        try:
            info["list_exits"] = _rpc(endpoint or cfg.get("omq") or config.OMQ).list_exits()
        except Exception as e:
            info["list_exits_error"] = str(e)
        conn = info.get("connected") or {}
        mapped = (info.get("list_exits") or {}).get("result")
        if conn and isinstance(mapped, list) and not mapped:
            # say after a reboot: the record is still here, the tunnel is not (nothing of it survives)
            info["connected_note"] = (f"not actually connected: Session Router has no exit mapped (say after a "
                                      f"reboot). Reconnect: {_P()} connect {conn.get('seller')}  or clear this "
                                      f"record: {_P()} disconnect")
    return info


def cmd_status(args, st) -> int:
    cfg = config.load(st.root)
    info = status_info(st, cfg, args.rpc_endpoint)
    for l in info["leases"]:
        tail = "  (ending soon: buy more to keep going)" if l["ending"] else ""
        print(f"paid time: {l['seller']} until {_hhmm(l['end'])}, {_left(l['left_s'])} left{tail}", file=sys.stderr)
    print(json.dumps(info, indent=2))
    return 0


def cmd_disconnect(args, st) -> int:
    cfg = config.load(st.root)
    conn = st.data.get("connected") or {}
    seller = args.seller or conn.get("seller")
    dns_ok = dns_down(st)  # first, so no lookup goes out the normal link while the tunnel is still up
    unmapped = True
    if seller:
        try:
            _rpc(args.rpc_endpoint or cfg.get("omq") or config.OMQ).unmap_exit(seller)
        except Exception as e:
            unmapped = False
            print(f"unmap: {e}", file=sys.stderr)
    ks = killswitch.KillSwitch(tun=cfg.get("tun", killswitch.DEFAULT_TUN))
    ks_ok = dns_ok
    if args.keep_killswitch:
        print(f"kill switch kept armed (--keep-killswitch): clearnet stays blocked until {_P()} disconnect")
    elif conn.get("killswitch") is False:
        print("kill switch was not armed for this connection")
    else:
        try:
            print("kill switch disarmed" if ks.disarm() else "kill switch was not armed")
        except Exception as e:
            if conn:
                ks_ok = False
                print(f"kill switch NOT removed: {e}", file=sys.stderr)
            else:
                print(f"note: could not check for a leftover kill switch: {e}", file=sys.stderr)
    # after the tunnel routes are gone: the WAN reply routing is not needed any more. If the unmap
    # failed the tunnel routes may still be there, and removing it now would cut SSH/RDP.
    if not args.keep_killswitch:
        if unmapped:
            _inbound_down()
        else:
            print("kept the routing that keeps SSH/RDP sessions to this machine working, because Session "
                  "Router did not confirm the exit is unmapped (its routes may still send traffic into the "
                  f"tunnel). Run {_P()} disconnect again once Session Router answers; a reboot also clears it.",
                  file=sys.stderr)
    if unmapped:   # else kept, so the next disconnect tries the unmap again
        S.update(st, lambda d: d.__setitem__("connected", None))
    out = {"ok": ks_ok, "unmapped": seller if unmapped else None}
    lease = pay.active_lease(st, seller) if seller else None
    if lease:
        out["reconnect"] = f"{_P()} connect {seller}  (works until {_hhmm(lease['end'])}; no new purchase)"
    print(json.dumps(out))
    return 0 if ks_ok else 1


# ------------------------------------------------------------------ wiring
def register(sub) -> dict:
    """Add the exits commands to an argparse subparsers object. Returns {name: fn(args, st)}."""
    e = sub.add_parser("exits", help="exits (Beta): list sellers from the signed board")
    e.add_argument("--listings", help="offline: read listings.json / .jsonl instead of the board")

    b = sub.add_parser("buy", help="exits: buy time on an exit with a direct USDC payment (final, no refund)")
    b.add_argument("seller_pos", nargs="?", metavar="SELLER", help=argparse.SUPPRESS)
    b.add_argument("--seller", help=f"seller .sesh id (see: {_P()} exits)")
    b.add_argument("--hours", type=int, default=1, help="whole hours (default 1, up to the seller's limit)")
    b.add_argument("--chain", help="arbitrum or base (default: arbitrum if the seller takes it)")
    b.add_argument("--max-usdc", help="largest payment you allow this time (default 20, or \"max_payment_usdc\")")
    b.add_argument("--client-sesh", help="your own .sesh address (default: asked from Session Router)")
    b.add_argument("--listings", help="offline: listings file instead of the board")
    b.add_argument("--no-wait", action="store_true", help=f"show the payment details and stop; finish with {_P()} paid")
    b.add_argument("--system-dns", action="store_true",
                   help="buy even if the exit runs no resolver (you will need connect --system-dns)")

    p = sub.add_parser("paid", help="exits: carry on after paying a quote (watch the chain, tell the seller)")
    p.add_argument("--quote", help="quote id (default: your latest open quote)")
    p.add_argument("--seller", help="pick the latest open quote for this seller")
    p.add_argument("--tx", help="transaction hash of your payment, if the watch didn't find it")

    c = sub.add_parser("connect", help="exits: route your traffic through an exit you've paid for")
    c.add_argument("seller")
    c.add_argument("--rpc-endpoint", help="session-router OMQ URL")
    c.add_argument("--timeout", type=float, default=90)
    c.add_argument("--wan-if")
    c.add_argument("--listings", help=argparse.SUPPRESS)
    c.add_argument("--no-killswitch", action="store_true", help="connect without the nftables kill switch")
    c.add_argument("--allow-no-ks", action="store_true", help="try the kill switch, but connect anyway if it fails")
    c.add_argument("--system-dns", action="store_true",
                   help="leave system DNS alone (lookups may go out your normal connection, outside the exit)")
    c.add_argument("--mock", action="store_true", help=argparse.SUPPRESS)

    s = sub.add_parser("status", help="exits: paid time left, connection, kill switch, DNS")
    s.add_argument("--rpc-endpoint")

    d = sub.add_parser("disconnect", help="exits: restore DNS, unmap, remove the kill switch")
    d.add_argument("--seller")
    d.add_argument("--rpc-endpoint")
    d.add_argument("--keep-killswitch", action="store_true")

    r = sub.add_parser("setup-router", help="exits: give Session Router a persistent key and turn off its "
                       "blackhole routes (asks sudo once; install.sh --with-exits runs it)")
    r.add_argument("--ini", default=None, help=f"client.ini (default {routercfg.DEFAULT_INI})")
    r.add_argument("--no-restart", action="store_true", help="don't restart session-router-client")

    return {"exits": cmd_exits, "buy": cmd_buy, "paid": cmd_paid, "connect": cmd_connect,
            "status": cmd_status, "disconnect": cmd_disconnect, "setup-router": cmd_setup_router}


def main(argv=None) -> int:
    """`python -m srnet_exits.cli` (the Exits tab runs connect / disconnect this way): same as srnet-exits."""
    from . import standalone
    return standalone.main(argv)


if __name__ == "__main__":
    sys.exit(main())
