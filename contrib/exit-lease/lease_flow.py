"""Buyer-side connect flow with spend-after-map ordering (exit lease admission).

Nothing is relied on until the exit has actually admitted us. Ordering:

  1. reserve()   lease prepaid (SRNetBalance startLease: rate per hour x whole
                 hours, FINAL at start; each hour is the seller's as it begins). The
                 seller's watcher writes the signed admission, one paid hour at a time.
  2. map_exit()  MapExit full_tunnel to the seller's exit.
  3. wait_capable()  session live and EXIT_CAPABLE (exit admitted us).
  4. commit()    keep the paid hour (no transaction); the client renews hourly.

Any failure after reserve() -> unmap (best effort) + release(): there is NO buyer
cancel and no refund, so release() only stops hourly auto-renew. The loss is the
prepaid hour (the client buys 1 hour at a time by default). Only the seller can
stop a lease early, and then unused whole hours return to the buyer.
The CLI (`srnet connect`) drives this with real backends; tests use fakes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class Backend(Protocol):
    def reserve(self, seller: str) -> str: ...          # returns reservation/hold id
    def map_exit(self, seller: str) -> None: ...         # raises on failure
    def wait_capable(self, seller: str, timeout_s: float) -> bool: ...
    def unmap_exit(self, seller: str) -> None: ...
    def commit(self, hold_id: str) -> None: ...          # spend/settle
    def release(self, hold_id: str) -> None: ...         # stop renewing (no refund exists)


@dataclass
class ConnectResult:
    ok: bool
    hold_id: str | None
    reason: str
    steps: list[str] = field(default_factory=list)


def connect(b: Backend, seller: str, capable_timeout_s: float = 60.0) -> ConnectResult:
    steps: list[str] = []
    try:
        hold = b.reserve(seller)
    except Exception as e:  # nothing held, nothing to release
        return ConnectResult(False, None, f"reserve_failed:{e}", ["reserve!"])
    steps.append("reserve")

    def fail(reason: str) -> ConnectResult:
        try:
            b.unmap_exit(seller)
            steps.append("unmap")
        except Exception:
            steps.append("unmap!")
        try:
            b.release(hold)
            steps.append("release")
        except Exception:
            # best effort; nothing on chain to undo
            steps.append("release!")
        return ConnectResult(False, hold, reason, steps)

    try:
        b.map_exit(seller)
        steps.append("map")
    except Exception as e:
        return fail(f"map_failed:{e}")
    try:
        capable = b.wait_capable(seller, capable_timeout_s)
    except Exception as e:
        return fail(f"capable_error:{e}")
    if not capable:
        return fail("not_admitted_or_not_capable")
    steps.append("capable")
    try:
        b.commit(hold)
        steps.append("commit")
    except Exception as e:
        # Mapped but commit failed: tear down and stop renewing.
        return fail(f"commit_failed:{e}")
    return ConnectResult(True, hold, "connected", steps)
