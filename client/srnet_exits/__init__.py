"""SRNet Exits: buy time on SRNet exits with a direct USDC payment, and connect through them.

An add-on for the SRNet client (srnet-client). It plugs into the `srnet` command and the desktop
app through the "srnet.addons" entry point; without it, SRNet has no exit or payment code at all.
"""
import sys as _sys
from pathlib import Path as _Path

__version__ = "0.3.0"

# Shared pack modules (chains, quote, listing, boardfmt, evm, seshdns, lease_flow) are imported by
# bare name. In a checkout they live in ../seller, ../board and ../contrib/exit-lease; after
# `pip install` they ship inside this package as _seller, _board and _lease.
_here = _Path(__file__).resolve().parent
_repo = _here.parents[1]
for _d in (_here / "_lease", _here / "_board", _here / "_seller",
           _repo / "contrib" / "exit-lease", _repo / "board", _repo / "seller"):
    if (_d / "listing.py").exists() or (_d / "boardfmt.py").exists() or (_d / "lease_flow.py").exists():
        if str(_d) not in _sys.path:
            _sys.path.insert(0, str(_d))
