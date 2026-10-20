"""Chains and tokens SRNet exits accept. Shared by the buyer add-on, the seller pack and the board.

Only USDC, only on the chains below. The token address and the default RPC come from this
table, never from a listing or a quote, so a listing can't point buyers at a look-alike token.
Addresses are Circle's native USDC (developers.circle.com/stablecoins/usdc-contract-addresses).

Test chains are off unless the caller turns them on (testnets=True). 31337 is a local Anvil chain
where the test harness deploys MockUSDC first from Anvil's default account, which always lands at
0x5FbDB2315678afecb367f032d93F642f64180aa3.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

USDC_DECIMALS = 6
USDC_UNIT = 10 ** USDC_DECIMALS
TOKEN = "USDC"

MAINNETS = {
    42161: {"name": "arbitrum", "label": "Arbitrum One", "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "rpc": "https://arb1.arbitrum.io/rpc", "explorer": "https://arbiscan.io/tx/"},
    8453: {"name": "base", "label": "Base", "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
           "rpc": "https://mainnet.base.org", "explorer": "https://basescan.org/tx/"},
}
TESTNETS = {
    421614: {"name": "arbitrum-sepolia", "label": "Arbitrum Sepolia", "usdc": "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",
             "rpc": "https://sepolia-rollup.arbitrum.io/rpc", "explorer": "https://sepolia.arbiscan.io/tx/"},
    84532: {"name": "base-sepolia", "label": "Base Sepolia", "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "rpc": "https://sepolia.base.org", "explorer": "https://sepolia.basescan.org/tx/"},
    31337: {"name": "anvil", "label": "Anvil (local test)", "usdc": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
            "rpc": "http://127.0.0.1:8545", "explorer": ""},
}
DEFAULT_CHAINS = (42161, 8453)
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class ChainError(ValueError):
    pass


def table(testnets: bool = False) -> dict:
    return {**MAINNETS, **TESTNETS} if testnets else dict(MAINNETS)


def supported(chainid, testnets: bool = False) -> bool:
    return isinstance(chainid, int) and not isinstance(chainid, bool) and chainid in table(testnets)


def info(chainid: int, testnets: bool = False) -> dict:
    if not supported(chainid, testnets):
        raise ChainError(f"chain {chainid} is not supported (supported: {names(testnets)})")
    return table(testnets)[chainid]


def usdc(chainid: int, testnets: bool = False) -> str:
    return info(chainid, testnets)["usdc"].lower()


def label(chainid) -> str:
    t = table(True)
    return t[chainid]["label"] if chainid in t else f"chain {chainid}"


def names(testnets: bool = False) -> str:
    return ", ".join(f"{v['label']} ({k})" for k, v in table(testnets).items())


def by_name(name: str, testnets: bool = False) -> int:
    """'base', 'arbitrum', '8453' -> chain id."""
    n = str(name).strip().lower()
    for k, v in table(testnets).items():
        if n in (v["name"], str(k), v["label"].lower()):
            return k
    raise ChainError(f"unknown chain {name!r} (supported: {names(testnets)})")


def rpc_url(chainid: int, overrides: dict | None = None, testnets: bool = False) -> str:
    """The user's RPC for this chain if set ({"8453": "https://..."}), else the default one."""
    o = (overrides or {}).get(str(chainid)) or (overrides or {}).get(chainid)
    return o or info(chainid, testnets)["rpc"]


def parse_usdc(s) -> int:
    """'1.50' -> 1500000 base units. At most 6 decimals, > 0."""
    try:
        d = Decimal(str(s).strip())
    except InvalidOperation:
        raise ChainError(f"bad USDC amount {s!r}") from None
    if d <= 0 or d != d.quantize(Decimal(1).scaleb(-USDC_DECIMALS)) or d > 10 ** 9:
        raise ChainError(f"bad USDC amount {s!r} (more than 0, at most 6 decimals)")
    return int(d * USDC_UNIT)


def fmt_usdc(units: int) -> str:
    """1500123 -> '1.500123'; whole cents keep two decimals: 1500000 -> '1.50'."""
    units = int(units)
    s = f"{units // USDC_UNIT}.{units % USDC_UNIT:06d}"
    whole, frac = s.split(".")
    frac = frac.rstrip("0")
    return f"{whole}.{frac.ljust(2, '0')}"


def eip681(chainid: int, payout: str, amount: int, testnets: bool = False) -> str:
    """Payment link most wallets understand: an ERC-20 transfer of `amount` USDC to `payout`."""
    if not ADDR_RE.match(payout):
        raise ChainError("bad payout address")
    return f"ethereum:{info(chainid, testnets)['usdc']}@{chainid}/transfer?address={payout}&uint256={int(amount)}"


def tx_link(chainid: int, tx: str) -> str:
    e = table(True).get(chainid, {}).get("explorer")
    return e + tx if e else tx
