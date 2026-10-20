# Copyright (c) 2026 SRNet contributors.
# SPDX-License-Identifier: Apache-2.0
# From SRNet-Desktop client-cli/srnet/state.py, so srnet-exits runs without the SRNet client.
# Same files and format: the SRNet client and srnet-exits share one ~/.srnet.
"""Persistent client state under ~/.srnet or SRNET_HOME."""
from __future__ import annotations

import json
import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("SRNET_HOME", Path.home() / ".srnet"))


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
        return {"pending_buy": None, "connected": None, "listings_path": None}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)
