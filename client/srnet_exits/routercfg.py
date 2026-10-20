"""Two Session Router client settings that exits need, checked here and set by `srnet setup-router`
(which install.sh --with-exits runs; one sudo prompt).

- keyfile=...: a persistent client key. Without it the router picks a new random .sesh address at
  every restart (or reboot), and paid exit time is bound to the address it was bought for, so it
  would stop working after the next restart.
- blackhole-routes=false: with the default (true) the router adds "blackhole default" to the
  kernel's *local* routing table while an exit is mapped. The local table is looked up before
  everything else, so every non-local packet fails (EINVAL): replies to SSH/RDP sessions, the
  router's own relay traffic and the tunnel itself. The kill switch (nftables) is the leak
  protection instead.
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_INI = "/etc/session-router/client.ini"
KEY = "/var/lib/session-router/client/client.key"
FIX = "srnet setup-router"
SERVICE = "session-router-client"

def _text(p: Path, privileged: bool) -> str | None:
    try:
        return p.read_text()
    except PermissionError:
        if not privileged:
            return None
    except (OSError, UnicodeDecodeError):
        return None
    # the package installs it root:_session 0640; srnet connect asks sudo anyway
    from . import priv
    try:
        r = priv.run(["cat", str(p)], check=False, what="checking Session Router's settings")
    except priv.PrivError:
        return None
    return r.stdout if r.returncode == 0 else None


def read(path: str | Path = DEFAULT_INI, privileged: bool = False) -> dict | None:
    """{'keyfile': str|None, 'blackhole_routes': bool} or None when the file can't be read
    (privileged: read it through sudo when it is not readable as this user)."""
    p = Path(path)
    cp = configparser.ConfigParser(strict=False, interpolation=None, inline_comment_prefixes=("#", ";"))
    text = _text(p, privileged)
    if text is None:
        return None
    try:
        cp.read_string(text)
    except configparser.Error:
        return None
    net = cp["network"] if cp.has_section("network") else {}
    bh = str(net.get("blackhole-routes", "true")).strip().lower()
    return {"keyfile": (net.get("keyfile") or "").strip() or None,
            "blackhole_routes": bh not in ("false", "0", "no", "off")}


def problems(path: str | Path = DEFAULT_INI, privileged: bool = False) -> list[str] | None:
    """The problems, [] when fine, None when the file could not be read (or does not exist)."""
    r = read(path, privileged)
    if r is None:
        return None
    out = []
    if not r["keyfile"]:
        out.append("Session Router has no persistent key (keyfile= in [network]): it gets a new .sesh "
                   "address at every restart, and paid exit time only works for the address it was bought for")
    if r["blackhole_routes"]:
        out.append("Session Router's blackhole-routes is on: while connected it blackholes all traffic, "
                   "including your SSH/RDP sessions and the tunnel itself")
    return out


# ------------------------------------------------------------------ setting them (root)
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _active(line: str, name: str) -> str | None:
    """The value if `line` is an active (uncommented) `name=value` line."""
    m = re.match(r"^\s*" + re.escape(name) + r"\s*=\s*(.*?)\s*$", line)
    if not m:
        return None
    return re.split(r"\s[#;]", m.group(1), 1)[0].strip()


def edit_text(text: str, key: str = KEY) -> tuple[str, str, bool]:
    """client.ini text -> (new text, keyfile path in use, changed). Keeps every other line as it is:
    an existing non-empty keyfile= stays, blackhole-routes is set to false (in [network])."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    start = next((i for i, l in enumerate(lines) if (m := _SECTION.match(l)) and m.group(1).strip() == "network"), None)
    if start is None:
        add = ["\n" if lines and lines[-1].strip() else "", "[network]\n", f"keyfile={key}\n", "blackhole-routes=false\n"]
        return "".join(lines + [x for x in add if x]), key, True
    end = next((i for i in range(start + 1, len(lines)) if _SECTION.match(lines[i])), len(lines))
    keyfile, changed, insert = None, False, []
    k_idx = b_idx = None
    for i in range(start + 1, end):
        v = _active(lines[i], "keyfile")
        if v is not None:
            if v:
                keyfile = v
            k_idx = i
        if _active(lines[i], "blackhole-routes") is not None:
            b_idx = i
    if keyfile is None:
        keyfile = key
        if k_idx is not None:
            lines[k_idx] = f"keyfile={key}\n"
        else:
            insert.append(f"keyfile={key}\n")
        changed = True
    if b_idx is not None:
        if _active(lines[b_idx], "blackhole-routes").lower() not in ("false", "0", "no", "off"):
            lines[b_idx] = "blackhole-routes=false\n"
            changed = True
    else:
        insert.append("blackhole-routes=false\n")
        changed = True
    lines[start + 1:start + 1] = insert
    return "".join(lines), keyfile, changed


def _write_like(path: Path, text: str) -> None:
    """Replace the file atomically, keeping its owner and mode."""
    st = path.stat()
    tmp = path.with_name(f".{path.name}.srnet-tmp")
    tmp.write_text(text)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, path)


def apply(ini: str | Path = DEFAULT_INI, key: str = KEY, restart: bool = True,
          run=subprocess.run, log=print) -> int:
    """As root: make the key file if it is missing, set keyfile= and blackhole-routes=false in
    client.ini (a copy of the original is kept once as client.ini.srnet-orig), restart the router
    if anything changed. Running it again changes nothing."""
    ini = Path(ini)
    if not ini.exists():
        log(f"{ini} not found: install Session Router first (README, Install, step 1), then run srnet setup-router")
        return 1
    new, keyfile, changed = edit_text(ini.read_text(), key)
    kp = Path(keyfile)
    made_key = False
    if not kp.exists():
        kp.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = run(["session-router-config", "-k", str(kp)], capture_output=True, text=True)
            err = (r.stderr or r.stdout or "").strip() if (r.returncode != 0 or not kp.exists()) else None
        except OSError as e:
            err = f"{e} (session-router-config comes with the session-router-client package)"
        if err is not None:
            log(f"could not make the key file {kp}: {err}")
            return 1
        d = kp.parent.stat()
        os.chown(kp, d.st_uid, d.st_gid)   # the router's data dir owner (_session)
        os.chmod(kp, 0o600)
        made_key = True
        log(f"made a persistent Session Router key: {kp}")
    if changed:
        orig = ini.with_name(ini.name + ".srnet-orig")
        if not orig.exists():
            _write_like_copy(ini, orig)
        _write_like(ini, new)
        log(f"set keyfile={keyfile} and blackhole-routes=false in {ini} (original kept as {orig.name})")
    if not (changed or made_key):
        log("Session Router is already set up for exits (persistent key, no blackhole routes)")
        return 0
    if restart:
        r = run(["systemctl", "restart", SERVICE], capture_output=True, text=True)
        if r.returncode != 0:
            log(f"restart {SERVICE} yourself: sudo systemctl restart {SERVICE}  ({(r.stderr or '').strip()})")
            return 1
        log(f"restarted {SERVICE}")
    return 0


def _write_like_copy(src: Path, dst: Path) -> None:
    st = src.stat()
    dst.write_text(src.read_text())
    os.chown(dst, st.st_uid, st.st_gid)
    os.chmod(dst, st.st_mode & 0o7777)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m srnet_exits.routercfg")
    ap.add_argument("action", choices=["apply"])
    ap.add_argument("--ini", default=DEFAULT_INI)
    ap.add_argument("--key", default=KEY)
    ap.add_argument("--no-restart", action="store_true")
    a = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("run through: srnet setup-router (it asks sudo)", file=sys.stderr)
        return 2
    return apply(a.ini, a.key, restart=not a.no_restart)


if __name__ == "__main__":
    sys.exit(main())
