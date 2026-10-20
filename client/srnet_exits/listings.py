"""Load signed exit listings; drop bad signatures."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import listing as L  # noqa: E402


def load_board(path: Path) -> list[dict]:
    """path is a JSON file: either one listing doc, or {"listings":[...]} / a JSONL file."""
    text = path.read_text()
    out: list[dict] = []
    if path.suffix == ".jsonl":
        out = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
        if isinstance(data, list):
            out = data
        elif isinstance(data, dict) and "listings" in data:
            out = data["listings"]
        elif isinstance(data, dict) and "listing" in data:
            out = [data]
        else:
            raise ValueError("unrecognized listings file shape")
    return [doc for doc in out if L.verify(doc)]


def exit_dns(body: dict) -> str | None:
    """The exit's own resolver from a listing body ("dns"), or None if it offers none.

    Only a private IPv4 address is accepted: it is reached inside the tunnel at the exit,
    never a public resolver."""
    v = (body or {}).get("dns")
    return str(ipaddress.IPv4Address(v)) if L.valid_dns(v) else None


def find_seller(board: list[dict], seller: str) -> dict | None:
    for doc in board:
        if doc.get("listing", {}).get("seller") == seller:
            return doc
    return None
