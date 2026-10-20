#!/usr/bin/env python3
"""SRNet exit-seller board: operator CLI.

  board.py init    --state DIR --key BOARD.key        # BOARD.key = session-router keyfile (128 hex)
  board.py submit  --state DIR listing.json ...       # verify + publish (also what the server's /submit does)
  board.py ingest  --state DIR                        # pull everything from DIR/inbox/
  board.py list    --state DIR
  board.py ban     --state DIR <seller.sesh> --reason TEXT   # the only manual control
  board.py unban   --state DIR <seller.sesh>
  board.py sign    --state DIR --key BOARD.key --out WWW [--valid-hours 24] [--hide-after-hours 3]
  board.py tick    --state DIR --key BOARD.key --out WWW [--publish URL ...]
                                                      # ingest + sign + mirror; run hourly
  board.py publish --out WWW URL ...                  # push the signed board to mirrors
  board.py verify  WWW --pin <board.sesh>
  board.py config  --state DIR [--min-exit-version 0.1.0 | --min-exit-version off] [--testnets on|off]

Mirrors run board_server.py --pin <board.sesh> and accept POST /publish of a newer signed board.

Listings are published automatically: a v3 listing with a valid seller signature, supported
chains (Arbitrum One, Base), sane fields and a non-zero payout goes live at the next sign (every
tick). Changes to any field, including payout / chains / country, go live the same way. Banned
sellers are refused. Old v2 listings stay listed for two weeks after the board is upgraded
(clients show them as "seller needs to update"), then are left out. Sellers re-sign and resubmit every hour; a listing not refreshed for 3 hours
is left out of the signed board and comes back on its next valid refresh. With min_exit_version
set (config), listings below it are left out.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boardfmt as B  # noqa: E402
from boardfmt import L  # noqa: E402

MAX_LISTING_BYTES = 16 * 1024


def _load(state_dir: Path) -> dict:
    p = state_dir / "state.json"
    if not p.exists():
        raise SystemExit(f"no board state at {p}; run init")
    return json.loads(p.read_text())


def _save(state_dir: Path, st: dict) -> None:
    tmp = state_dir / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.replace(tmp, state_dir / "state.json")


def _log(state_dir: Path, event: dict) -> None:
    with open(state_dir / "audit.jsonl", "a") as f:
        f.write(json.dumps({"t": int(time.time()), **event}, sort_keys=True) + "\n")


def legacy_until(st: dict, now: float) -> int:
    """End of the v2 grace period; starts the first time this board version runs."""
    if "legacy_until" not in st:
        st["legacy_until"] = int(now) + B.LEGACY_GRACE_S
    return int(st["legacy_until"])


def check_listing(body: dict, st: dict | None = None, now: float | None = None) -> str | None:
    """Why a (signature-valid) listing can't go live, or None if it can."""
    why = L.listing_error(body, testnets=bool((st or {}).get("testnets")))
    if why == L.NEEDS_UPDATE and st is not None:
        now = time.time() if now is None else now
        if now >= legacy_until(st, now):
            return "old v2 listing, grace period over"
        return L.old_listing_error(body)
    return why


def submit_doc(state_dir: Path, st: dict, doc: dict, now: float | None = None) -> str:
    """Returns the resulting status: live | refreshed | rejected:<why> | ignored:<why>."""
    now = time.time() if now is None else now
    if len(json.dumps(doc)) > MAX_LISTING_BYTES:
        return "rejected:too large"
    if not L.verify(doc):
        return "rejected:bad signature"
    body = doc["listing"]
    max_age = st.get("max_listing_age_s", B.DEFAULT_MAX_LISTING_AGE_S)
    if not B.listing_live(doc, now, max_age):
        return "rejected:expired or future-dated"
    why = check_listing(body, st, now)
    if why:
        return f"rejected:{why}"
    seller = body["seller"]
    if seller in st.setdefault("banned", {}):
        return "rejected:banned"
    ent = st["sellers"].get(seller)
    if ent and int(ent["doc"]["listing"]["issued"]) >= int(body["issued"]):
        return "ignored:not newer than current"
    changed = sorted(f for f in B.AUDIT_FIELDS if ent and ent["doc"]["listing"].get(f) != body.get(f))
    was_hidden = bool(ent) and not B.listing_live(ent["doc"], now, max_age)
    st["sellers"][seller] = {"status": "live", "doc": doc, "refreshed": int(now),
                             "first_seen": (ent or {}).get("first_seen", (ent or {}).get("submitted", int(now)))}
    _log(state_dir, {"ev": "refresh" if ent else "new", "seller": seller, "issued": body["issued"],
                     "reshown": was_hidden, "changed": changed})
    return "refreshed" if ent else "live"


def cmd_init(a) -> int:
    d = Path(a.state)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    (d / "inbox").mkdir(exist_ok=True)
    if (d / "state.json").exists():
        print("already initialised", file=sys.stderr)
        return 1
    key = L.load_identity(a.key)
    st = {"v": 1, "board": L.sesh_of(B.pub_raw(key)), "seq": 0, "sellers": {},
          "max_listing_age_s": B.DEFAULT_MAX_LISTING_AGE_S}
    _save(d, st)
    _log(d, {"ev": "init", "board": st["board"]})
    print(st["board"])
    return 0


def cmd_submit(a) -> int:
    d = Path(a.state)
    st = _load(d)
    rc = 0
    for f in a.files:
        try:
            doc = json.loads(Path(f).read_text())
        except (OSError, ValueError) as e:
            print(f"{f}: rejected:unreadable ({e})")
            rc = 1
            continue
        res = submit_doc(d, st, doc)
        print(f"{f}: {res}")
        rc |= res.startswith("rejected")
    _save(d, st)
    return rc


def cmd_ingest(a) -> int:
    d = Path(a.state)
    st = _load(d)
    done = d / "inbox" / "processed"
    done.mkdir(parents=True, exist_ok=True)
    for f in sorted((d / "inbox").glob("*.json")):
        try:
            res = submit_doc(d, st, json.loads(f.read_text()))
        except ValueError:
            res = "rejected:unreadable"
        print(f"{f.name}: {res}")
        os.replace(f, done / f.name)
    _save(d, st)
    return 0


def cmd_list(a) -> int:
    st = _load(Path(a.state))
    now = time.time()
    print(f"board {st['board']}  seq={st['seq']}  min_exit_version={st.get('min_exit_version') or 'off'}")
    for seller, e in sorted(st["sellers"].items()):
        b = e["doc"]["listing"]
        exp = L.expires_at(b, st["max_listing_age_s"])
        age_h = (now - int(b.get("issued", 0))) / 3600
        flag = "" if exp > now else f"  (HIDDEN: no refresh for {age_h:.1f} h)"
        price = (f"price/h={b['price_per_hour_usdc']}" if "price_per_hour_usdc" in b
                 else f"price={b.get('price_usdc')} period={b.get('period_s')}s")
        if not L.meets_min_version(b, st.get("min_exit_version")):
            flag += f"  (HIDDEN: exit_version below {st['min_exit_version']})"
        why = check_listing(b, st, now)
        if why:
            flag += f"  (HIDDEN: {why})"
        elif b.get("v") != L.LISTING_VERSION:
            flag += f"  ({L.NEEDS_UPDATE})"
        status = "banned" if seller in st.get("banned", {}) else "live"
        chains = ",".join(str(c) for c in b.get("chains") or [b.get("chainid")])
        print(f"{status:7} {seller}  {price} country={L.country_text(b)} region={b.get('region','')} chains={chains} "
              f"exit_version={b.get('exit_version') or 'unknown'} speed_cap={L.speed_cap_text(b)} "
              f"last-refresh={time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(int(b.get('issued', 0))))}"
              f"{flag}")
    for seller, ban in sorted(st.get("banned", {}).items()):
        print(f"ban     {seller}  reason={ban.get('reason', '')}")
    return 0


def cmd_ban(a) -> int:
    d = Path(a.state)
    st = _load(d)
    L.pub_of_sesh(a.seller)  # must be a valid .sesh
    st.setdefault("banned", {})[a.seller] = {"reason": a.reason, "t": int(time.time())}
    _save(d, st)
    _log(d, {"ev": "ban", "seller": a.seller, "reason": a.reason})
    print(f"banned {a.seller} (takes effect at next sign; clients drop it once they refresh)")
    return 0


def cmd_unban(a) -> int:
    d = Path(a.state)
    st = _load(d)
    if st.setdefault("banned", {}).pop(a.seller, None) is None:
        print("not banned", file=sys.stderr)
        return 1
    _save(d, st)
    _log(d, {"ev": "unban", "seller": a.seller})
    print(f"unbanned {a.seller} (its next valid listing goes live)")
    return 0


def build_body(st: dict, now: float, valid_hours: float) -> dict:
    max_age = st["max_listing_age_s"]
    minv = st.get("min_exit_version")
    banned = st.get("banned", {})
    live = [e["doc"] for seller, e in sorted(st["sellers"].items())
            if seller not in banned and check_listing(e["doc"]["listing"], st, now) is None
            and B.listing_live(e["doc"], now, max_age)
            and L.meets_min_version(e["doc"]["listing"], minv)]
    return {"v": 1, "board": st["board"], "seq": st["seq"] + 1, "issued": int(now),
            "expires": int(now + valid_hours * 3600), "max_listing_age_s": max_age, "listings": live}


def cmd_sign(a) -> int:
    d = Path(a.state)
    st = _load(d)
    if a.hide_after_hours:
        st["max_listing_age_s"] = int(a.hide_after_hours * 3600)
    elif st.get("max_listing_age_s", 0) > B.LISTING_HIDE_AFTER_S:
        # Older state files used a 30-day listing age; liveness hides after 3 h without a refresh.
        st["max_listing_age_s"] = B.LISTING_HIDE_AFTER_S
    key = L.load_identity(a.key)
    if L.sesh_of(B.pub_raw(key)) != st["board"]:
        print("key does not match this board", file=sys.stderr)
        return 1
    now = time.time()
    body = build_body(st, now, a.valid_hours)
    sig = B.sign_board(key, body)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    # write sig + body atomically-ish: body first to tmp, then both renamed
    (out / "board.json.tmp").write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
    (out / "board.sig.tmp").write_text(sig + "\n")
    os.replace(out / "board.json.tmp", out / "board.json")
    os.replace(out / "board.sig.tmp", out / "board.sig")
    st["seq"] = body["seq"]
    st["last_hash"] = B.body_hash(body)
    _save(d, st)
    _log(d, {"ev": "sign", "seq": body["seq"], "n": len(body["listings"]), "hash": st["last_hash"]})
    print(f"signed board seq={body['seq']} listings={len(body['listings'])} "
          f"expires={time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(body['expires']))}")
    return 0


PUBLISH_TIMEOUT_S = 60


def publish(www: Path, url: str) -> str:
    """POST the signed board in WWW to a mirror's /publish. Returns the mirror's reply."""
    msg = {"body": (www / "board.json").read_text(), "sig": (www / "board.sig").read_text().strip()}
    if not url.endswith("/publish"):
        url = url.rstrip("/") + "/publish"
    req = urllib.request.Request(url, data=json.dumps(msg).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=PUBLISH_TIMEOUT_S) as r:  # noqa: S310
        return r.read(200).decode(errors="replace").strip()


def publish_all(www: Path, urls: list[str]) -> int:
    bad = 0
    for u in urls or []:
        try:
            print(f"mirror {u}: {publish(www, u)}")
        except Exception as e:  # one dead mirror must not stop the others
            print(f"mirror {u}: failed ({e})", file=sys.stderr)
            bad += 1
    return 1 if bad else 0


def cmd_publish(a) -> int:
    return publish_all(Path(a.out), a.urls)


def cmd_tick(a) -> int:
    """Hourly job: pull refreshed listings from the inbox, re-sign, push to mirrors."""
    rc = cmd_ingest(a) or cmd_sign(a)
    if rc:
        return rc
    publish_all(Path(a.out), a.publish)  # mirror failures are reported, not fatal
    return 0


def cmd_verify(a) -> int:
    w = Path(a.www)
    try:
        body, good, dropped = B.verify_board((w / "board.json").read_bytes(), (w / "board.sig").read_text(), a.pin)
    except B.BoardError as e:
        print(f"BOARD INVALID: {e}")
        return 1
    print(f"board OK seq={body['seq']} listings={len(good)} dropped={len(dropped)}")
    for r in dropped:
        print("  dropped", r)
    return 0


def cmd_config(a) -> int:
    d = Path(a.state)
    st = _load(d)
    if a.min_exit_version is not None:
        v = a.min_exit_version.strip()
        if v.lower() in ("", "off", "none"):
            st.pop("min_exit_version", None)
        elif L.version_key(v) is None:
            print("--min-exit-version must look like 0.1.0 (or 'off')", file=sys.stderr)
            return 1
        else:
            st["min_exit_version"] = v
        _save(d, st)
        _log(d, {"ev": "config", "min_exit_version": st.get("min_exit_version")})
    if a.testnets is not None:
        st["testnets"] = a.testnets == "on"
        _save(d, st)
        _log(d, {"ev": "config", "testnets": st["testnets"]})
    print(f"min_exit_version={st.get('min_exit_version') or 'off'} testnets={'on' if st.get('testnets') else 'off'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="board", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--state", required=True); p.add_argument("--key", required=True)
    p = sub.add_parser("submit"); p.add_argument("--state", required=True); p.add_argument("files", nargs="+")
    p = sub.add_parser("ingest"); p.add_argument("--state", required=True)
    p = sub.add_parser("list"); p.add_argument("--state", required=True)
    p = sub.add_parser("ban"); p.add_argument("--state", required=True); p.add_argument("seller")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("unban"); p.add_argument("--state", required=True); p.add_argument("seller")
    for name in ("sign", "tick"):
        p = sub.add_parser(name); p.add_argument("--state", required=True); p.add_argument("--key", required=True)
        p.add_argument("--out", required=True); p.add_argument("--valid-hours", type=float, default=24)
        p.add_argument("--hide-after-hours", type=float,
                       help="hide listings not refreshed for this long (default 3)")
        p.add_argument("--publish", action="append", default=[], metavar="URL",
                       help="mirror base URL to push the board to (tick only; repeatable)")
    p = sub.add_parser("publish"); p.add_argument("--out", required=True); p.add_argument("urls", nargs="+")
    p = sub.add_parser("verify"); p.add_argument("www"); p.add_argument("--pin", required=True)
    p = sub.add_parser("config"); p.add_argument("--state", required=True)
    p.add_argument("--min-exit-version", help="hide listings below this exit_version; 'off' clears (default off)")
    p.add_argument("--testnets", choices=("on", "off"), help="accept listings for test chains (test boards only)")
    a = ap.parse_args(argv)
    return {"init": cmd_init, "submit": cmd_submit, "ingest": cmd_ingest, "list": cmd_list,
            "ban": cmd_ban, "unban": cmd_unban, "sign": cmd_sign, "tick": cmd_tick, "publish": cmd_publish, "verify": cmd_verify,
            "config": cmd_config}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
