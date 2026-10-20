"""lease_flow.Backend wiring for `srnet connect`: check paid time -> map -> exit admits us.
Payments are final: a failed exit is unmapped and nothing is refunded."""
from __future__ import annotations

import time

from . import pay
from .prog import P as _P


class LiveBackend:
    """reserve = we hold paid time for this seller (recorded when the seller confirmed the payment);
    map via Session Router's OMQ RPC; commit/release only touch local state."""

    def __init__(self, rpc, state, clock=time.time, sleep=time.sleep):
        self.rpc, self.state, self.clock, self.sleep = rpc, state, clock, sleep

    def reserve(self, seller: str) -> str:
        lease = pay.active_lease(self.state, seller, self.clock())
        if not lease:
            raise RuntimeError(f"no paid time for this seller; buy some: {_P()} buy --seller {seller} --hours 1")
        return str((lease.get("quotes") or ["lease"])[-1])

    def map_exit(self, seller: str) -> None:
        r = self.rpc.map_exit(seller, full_tunnel=True)
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(r["error"])

    def wait_capable(self, seller: str, timeout_s: float) -> bool:
        deadline = self.clock() + timeout_s
        while self.clock() < deadline:
            r = self.rpc.list_exits()
            for e in (r.get("result") or []):
                if e.get("address") == seller and e.get("exit_capable") and e.get("session_live"):
                    return True
            self.sleep(2)
        return False

    def unmap_exit(self, seller: str) -> None:
        try:
            self.rpc.unmap_exit(seller)
        except Exception:
            pass

    def commit(self, hold_id: str) -> None:
        pass

    def release(self, hold_id: str) -> None:
        pass
