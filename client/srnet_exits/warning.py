"""First-run exit-visibility warning (required before connect/buy)."""
from __future__ import annotations

from pathlib import Path

WARNING = """\
SRNet exit visibility warning
=============================
Exits are Beta: they work but are not production-certified.
Keep amounts small.

When you connect through an exit, the exit OPERATOR can see:
  • the destinations you reach (IP addresses and ports)
  • connection timing and approximate transfer sizes
  • TLS SNI (the hostname on most HTTPS connections)

They cannot see the contents of properly encrypted traffic (HTTPS), but SNI
and destination IPs are enough to learn which sites you visit.

While connected, every DNS lookup goes through the tunnel to the exit's own
resolver, which resolves it itself (no public resolver involved). The exit
operator can therefore see the names you look up, as well as your
destinations; .sesh names stay on Session Router. The local nftables kill switch blocks clearnet TCP and DNS if
the tunnel drops; other UDP to the internet stays open for the overlay, so apps using
QUIC or WebRTC may bypass the exit.

You pay the seller directly in USDC from your own wallet, and you pay the
network fee yourself. Payments are FINAL: there is no refund, even if the
exit does not work. Nothing renews by itself; buy again to add time.

Type exactly:  I UNDERSTAND
"""


def acked(state_dir: Path) -> bool:
    ack = Path(state_dir) / "visibility.ack"
    return ack.exists() and ack.read_text().strip() == "I UNDERSTAND"


def write_ack(state_dir: Path) -> None:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    ack = state_dir / "visibility.ack"
    ack.write_text("I UNDERSTAND\n")
    ack.chmod(0o600)


def ensure_ack(state_dir: Path) -> None:
    if acked(state_dir):
        return
    print(WARNING)
    try:
        ans = input("> ").strip()
    except EOFError:
        ans = ""
    if ans != "I UNDERSTAND":
        raise SystemExit("refusing to continue without the visibility acknowledgement")
    write_ack(state_dir)
