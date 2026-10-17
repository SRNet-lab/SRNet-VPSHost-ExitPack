# board

The signed list of exit sellers that `srnet exits` shows.

- `board.py`: operator CLI (init, list, ban, unban, sign, tick, publish, verify)
- `board_server.py`: serves `board.json` + `board.sig`; the primary takes `POST /submit`,
  mirrors take `POST /publish` of a newer board signed by the pinned key
- `deploy/install-board.sh`: one host, three Session Router names, systemd units

The board is served on three .sesh names. Clients try them in order:

```
exitsrjnc83har7a17mdtjpgi1j3odgiphaffe9ejfq4wnbdxkio.sesh   primary (submit here)
exitstaf5qs8so5g4ykzodhiyh79zjy3gqmszyzyhhm6r4szpwco.sesh   mirror
exitsdr3hzeqcwon7mqa7z87h8rn1j786uabdq5wzjw11us1nroo.sesh   mirror
```

Board key: `qbhfd6kc4qijpdpjkd8nfcq55r377m1ff3o6ontfyikygsycywfo.sesh` (pinned in the client).

## Format

`board.json` is `{v, board, seq, issued, expires, max_listing_age_s, listings[]}`.
`board.sig` is a hex Ed25519 signature over `"srnet-exit-board/v1\n"` + canonical JSON.
Every listing keeps its own seller signature. Clients refuse a wrong key, a bad signature, an
expired board, a lower seq than they've seen, or the same seq with different content.

## Listings

- No approval step. A listing with a valid seller signature, the Arbitrum One chain id and live
  contract, sane fields and a non-zero payout goes live at the next `tick`. Changes to any
  field (payout, vendor_id, country, price, ...) go live the same way; they are logged in
  `audit.jsonl`.
- `ban <seller> --reason TEXT` is the only manual control: banned sellers are dropped and their
  submissions refused. `unban` lifts it.
- `country` is ISO 3166-1 alpha-2 (`unknown` if absent); `region` is optional detail.
- Sellers re-sign and resubmit every hour. A listing not refreshed for 3 hours is hidden and
  comes back on its next valid refresh.
- `exit_version` is the seller's self-reported pack version (`unknown` if absent). It can change
  on a refresh. `config --min-exit-version 0.1.0` leaves older or unknown
  versions out of the signed board; `--min-exit-version off` (the default) shows all.
- `speed_cap_mbps` (per-buyer cap, 0 = unlimited, `unknown` if absent) is shown.
- `tick` (every 15 min) ingests submissions, re-signs and pushes to the mirrors.

## Run

```
board.py init    --state STATE --key board.key
board.py list    --state STATE
board.py ban     --state STATE <seller>.sesh --reason TEXT
board.py unban   --state STATE <seller>.sesh
board.py config  --state STATE [--min-exit-version 0.1.0|off]
board.py tick    --state STATE --key board.key --out WWW --publish http://<mirror>/ ...
board_server.py  --www WWW [--inbox STATE/inbox] [--pin <board>.sesh] --host <tun addr> --port 80
```

`board_server.py` binds loopback or private addresses only.

Tests: `python3 -m unittest discover -s tests`
