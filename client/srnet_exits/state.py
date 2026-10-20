# Copyright (c) 2026 SRNet contributors.
# SPDX-License-Identifier: Apache-2.0
# From SRNet-Desktop client-cli/srnet/state.py, so srnet-exits runs without the SRNet client.
# Same files and format: the SRNet client and srnet-exits share one ~/.srnet.
"""Persistent client state under ~/.srnet or SRNET_HOME.

state.json is shared: the SRNet client, its exits add-on and srnet-exits (each possibly in its own
process, like the app's payment watch next to a `connect` in a terminal) all write it. Writes go
through update(fn): it takes an exclusive lock on state.lock, reads the file again, applies fn to
that fresh copy and replaces the file atomically, so no writer can drop another's change.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def home() -> Path:
    return Path(os.environ.get("SRNET_HOME", Path.home() / ".srnet"))


DEFAULT = {"pending_buy": None, "connected": None, "listings_path": None}


@contextlib.contextmanager
def locked(root: Path):
    """Exclusive lock on root/state.lock (blocks until it is free)."""
    fd = os.open(str(Path(root) / "state.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        yield
    finally:
        os.close(fd)  # closing drops the lock


def write(path: Path, data: dict) -> None:
    """Write data to path atomically: a private temp file in the same dir, then os.replace."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class State:
    def __init__(self, root: Path | None = None):
        self.root = root or home()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "spool").mkdir(exist_ok=True)
        self.path = self.root / "state.json"
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return dict(DEFAULT)

    def reload(self) -> dict:
        self.data = self._load()
        return self.data

    def update(self, fn):
        """Lock, re-read state.json, fn(fresh data), write it back. self.data becomes that fresh copy.
        Returns what fn returns. Use this for every change: it only changes what fn changes."""
        with locked(self.root):
            data = self._load()
            out = fn(data)
            write(self.path, data)
            self.data = data
            return out

    def save(self) -> None:
        """Write the whole in-memory copy (under the lock). Prefer update(): this one overwrites
        whatever another process changed since this copy was read."""
        with locked(self.root):
            write(self.path, self.data)


def update(st, fn):
    """st.update(fn) for any state object with .root and .data, also one from an SRNet client
    that has no update() yet: same lock, same file, same atomic write."""
    if callable(getattr(st, "update", None)):
        return st.update(fn)
    with locked(st.root):
        p = Path(st.root) / "state.json"
        data = json.loads(p.read_text()) if p.exists() else dict(DEFAULT)
        out = fn(data)
        write(p, data)
        st.data = data
        return out
