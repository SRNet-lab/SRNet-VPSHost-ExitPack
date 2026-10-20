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

setup-router also leaves a world-readable marker next to client.ini (srnet-exits.ok) with the
settings and client.ini's size, mtime and hash, so `srnet doctor` and `srnet buy` can confirm the
settings without root while client.ini (root:_session 0640) is unchanged since.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_INI = "/etc/session-router/client.ini"
KEY = "/var/lib/session-router/client/client.key"
FIX = "srnet setup-router"
SERVICE = "session-router-client"
MARKER = "srnet-exits.ok"
_FALSE = ("false", "0", "no", "off")


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
        r = priv.run(["cat", str(p)], check=False, what="checking Session Router's settings", cwd="/")
    except priv.PrivError:
        return None
    return r.stdout if r.returncode == 0 else None


# ------------------------------------------------------------------ one parser for check and edit
_SECTION = re.compile(r"^\s*\[\s*([^\]]*?)\s*\]\s*(?:[#;].*)?$")
_KV = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*[=:]\s*(.*?)\s*$")


def _kv(line: str) -> tuple[str, str] | None:
    """(lowercased key, value without an inline comment) for an active key=value line."""
    m = _KV.match(line)
    if not m:
        return None
    return m.group(1).lower(), re.split(r"\s[#;]", m.group(2), maxsplit=1)[0].strip()


def _scan(lines: list[str]):
    """Yield (index, in_network_section, kv-or-None, is_network_header)."""
    net = False
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m:
            net = m.group(1).strip().lower() == "network"
            yield i, net, None, net
            continue
        yield i, net, _kv(line), False


def values(text: str) -> dict:
    """{'keyfile': str|None, 'blackhole_routes': bool}: the last active value in any [network]
    section wins (keys and section names case-insensitive)."""
    kf, bh = None, None
    for _, net, kv, _ in _scan(text.splitlines()):
        if net and kv:
            if kv[0] == "keyfile":
                kf = kv[1]
            elif kv[0] == "blackhole-routes":
                bh = kv[1]
    return {"keyfile": kf or None, "blackhole_routes": (bh or "true").lower() not in _FALSE}


def _marker_path(ini: Path) -> Path:
    return ini.with_name(MARKER)


def _ini_facts(ini: Path, text: str | None = None) -> dict:
    st = ini.stat()
    out = {"ini_size": st.st_size, "ini_mtime_ns": st.st_mtime_ns}
    if text is not None:
        out["ini_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return out


def _from_marker(ini: Path) -> dict | None:
    """The settings setup-router recorded, if client.ini is unchanged since (size + mtime)."""
    try:
        m = json.loads(_marker_path(ini).read_text())
        facts = _ini_facts(ini)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or any(m.get(k) != v for k, v in facts.items()):
        return None
    if not isinstance(m.get("keyfile"), (str, type(None))) or not isinstance(m.get("blackhole_routes"), bool):
        return None
    return {"keyfile": m.get("keyfile") or None, "blackhole_routes": m["blackhole_routes"], "source": "marker"}


def read(path: str | Path = DEFAULT_INI, privileged: bool = False) -> dict | None:
    """{'keyfile': str|None, 'blackhole_routes': bool} or None when it can't be told. Reads
    client.ini when this user can, else setup-router's marker (if client.ini is unchanged since),
    else (privileged) through sudo."""
    p = Path(path)
    try:
        return values(p.read_text())
    except (PermissionError, UnicodeDecodeError):
        pass
    except OSError:
        return None
    m = _from_marker(p)
    if m is not None:
        return m
    if not privileged:
        return None
    text = _text(p, True)
    return values(text) if text is not None else None


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
def edit_text(text: str, key: str = KEY) -> tuple[str, str, bool]:
    """client.ini text -> (new text, keyfile path in use, changed). Keeps every other line as it
    is: an existing non-empty keyfile= stays, empty keyfile= lines get `key`, every active
    blackhole-routes in [network] becomes false, missing ones go right after the first [network]."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    changed = False
    first_net, has_kf, has_bh = None, False, False
    for i, net, kv, is_hdr in list(_scan(lines)):
        if is_hdr and first_net is None:
            first_net = i
        if not (net and kv):
            continue
        if kv[0] == "keyfile":
            has_kf = True
            if not kv[1]:
                lines[i] = f"keyfile={key}\n"
                changed = True
        elif kv[0] == "blackhole-routes":
            has_bh = True
            if kv[1].lower() not in _FALSE:
                lines[i] = "blackhole-routes=false\n"
                changed = True
    add = ([] if has_kf else [f"keyfile={key}\n"]) + ([] if has_bh else ["blackhole-routes=false\n"])
    if add:
        changed = True
        if first_net is None:
            lines += (["\n"] if lines and lines[-1].strip() else []) + ["[network]\n"] + add
        else:
            lines[first_net + 1:first_net + 1] = add
    new = "".join(lines)
    return new, values(new)["keyfile"] or key, changed


def _write_like(path: Path, text: str, like: Path | None = None) -> None:
    """Replace `path` atomically with `text`, owner and mode taken from `like` (default: path)."""
    st = (like or path).stat()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_marker(ini: Path, text: str) -> None:
    v = values(text)
    m = {"v": 1, "keyfile": v["keyfile"], "blackhole_routes": v["blackhole_routes"], **_ini_facts(ini, text)}
    mp = _marker_path(ini)
    fd, tmp = tempfile.mkstemp(dir=str(ini.parent), prefix=f".{MARKER}.")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(m, sort_keys=True) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, mp)


def make_key(kp: Path, run=subprocess.run) -> str | None:
    """Create the key file without ever following a link in the router's own directory: generate
    it as root in a private temp dir, then create kp with O_CREAT|O_EXCL|O_NOFOLLOW relative to an
    fd of its directory, owned by that directory's owner, mode 0600. Returns an error or None."""
    d = kp.parent
    try:
        dfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        return (f"{d} is missing ({e.strerror}); start Session Router once "
                f"(sudo systemctl start {SERVICE}) and run srnet setup-router again")
    try:
        dst = os.fstat(dfd)
        tmpd = tempfile.mkdtemp(prefix="srnet-key-")
        try:
            tk = os.path.join(tmpd, "client.key")
            try:
                r = run(["session-router-config", "-k", tk], capture_output=True, text=True, cwd="/")
            except OSError as e:
                return f"{e} (session-router-config comes with the session-router-client package)"
            if r.returncode != 0 or not os.path.isfile(tk):
                return (r.stderr or r.stdout or f"exit {r.returncode}").strip()
            with open(tk, "rb") as f:
                data = f.read()
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
        try:
            fd = os.open(kp.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        except FileExistsError:
            return f"{kp} appeared meanwhile (or is a link); not touching it"
        try:
            os.write(fd, data)
            os.fchown(fd, dst.st_uid, dst.st_gid)   # the router's data dir owner (_session)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return None


def apply(ini: str | Path = DEFAULT_INI, key: str = KEY, restart: bool = True,
          run=subprocess.run, log=print) -> int:
    """As root: make the key file if it is missing, set keyfile= and blackhole-routes=false in
    client.ini (a copy of the original is kept once as client.ini.srnet-orig), restart the router
    if anything changed, and (re)write the marker. Running it again changes nothing."""
    ini = Path(ini)
    if not ini.exists():
        log(f"{ini} not found: install Session Router first (README, Install, step 1), then run srnet setup-router")
        return 1
    text = ini.read_text()
    new, keyfile, changed = edit_text(text, key)
    kp = Path(keyfile)
    made_key = False
    if os.path.islink(kp):
        log(f"{kp} is a symbolic link; refusing to touch it. Remove it and run srnet setup-router again")
        return 1
    if not kp.exists():
        err = make_key(kp, run)
        if err is not None:
            log(f"could not make the key file {kp}: {err}")
            return 1
        made_key = True
        log(f"made a persistent Session Router key: {kp}")
    if changed:
        orig = ini.with_name(ini.name + ".srnet-orig")
        if not orig.exists():
            _write_like(orig, text, like=ini)
        _write_like(ini, new)
        log(f"set keyfile={keyfile} and blackhole-routes=false in {ini} (original kept as {orig.name})")
    _write_marker(ini, new)
    if not (changed or made_key):
        log("Session Router is already set up for exits (persistent key, no blackhole routes)")
        return 0
    if restart:
        r = run(["systemctl", "restart", SERVICE], capture_output=True, text=True, cwd="/")
        if r.returncode != 0:
            log(f"restart {SERVICE} yourself: sudo systemctl restart {SERVICE}  ({(r.stderr or '').strip()})")
            return 1
        log(f"restarted {SERVICE}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -I -m srnet_exits.routercfg")
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
