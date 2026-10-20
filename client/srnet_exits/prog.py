"""The command name shown in hints: `srnet` when this runs as the SRNet client's add-on,
`srnet-exits` when it runs on its own (srnet-exits sets SRNET_EXITS_PROG for itself and the
helpers it starts)."""
from __future__ import annotations

import os

NAMES = ("srnet", "srnet-exits")


def P() -> str:
    v = os.environ.get("SRNET_EXITS_PROG", "")
    return v if v in NAMES else "srnet"
