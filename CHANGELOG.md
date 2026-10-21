# Changelog

## [0.4.0] - 2026-10-20

### Added

- `srnet-exits` runs on its own, without SRNet-Desktop. It has every exits command plus `doctor`
  and `ui`, and shares its paid time and settings with the add-on.
- `client/install.sh` installs `srnet-exits` in its own environment, puts it on your PATH for new
  terminals and sets up Session Router with one sudo prompt. `--uninstall` removes only what it
  added.
- `srnet-exits ui` opens the Exits page in your browser on 127.0.0.1, with a new port and access
  token on every run.
- README: using exits without SRNet-Desktop, and a link to the typed SNApp host pack, which has
  its own repository:
  [SRNet-TypedSNApp-HostPack](https://github.com/SRNet-lab/SRNet-TypedSNApp-HostPack).

### Changed

- `status` prints a short summary. `status --json` prints the full record.
- The payment details show again below the QR code, and the buy dialog explains the small
  payment ID in the exact amount.
- Seller setup in the README and QUICKSTART uses `sudo` on every step and says that a new
  listing goes on the public exit board. Add `--board-submit-url none` to try the pack without
  being listed.
- `srnet-seller` tells you to run it with `sudo` instead of showing a permission error, and its
  hints say `sudo srnet-seller`.

### Fixed

- `client/install.sh --uninstall --force` while connected now disconnects fully first: DNS,
  routes and the kill switch go back to normal. Your paid time is kept.
- Uninstall puts tunnel DNS back, stops the page, the lease reminder and the DNS forwarder, and
  won't remove a folder it didn't install.
- Shared state is written atomically and changed through a locked update, so the page and a
  terminal can no longer overwrite each other's connection or DNS state.
- A stored quote's state never moves backwards.
- The claim desk notices every listing change, and listings are written atomically.
- The page's reuse check never sends its token, and a bad Content-Length gets a clear error.

## [0.3.0] - 2026-10-20

### Added

- The buyer add-on, `srnet-exits`, for the SRNet client: `srnet exits`, `srnet buy`,
  `srnet connect` and the Exits tab.
- Hourly leases: `srnet buy --seller <id> --hours N` buys one or more hours at once.
- A small tool in `contrib/legacy-cashout` to take out old SRNet balances.

### Changed

- No developer fee.
- Buyers pay the seller's payout address straight from their own wallet in USDC on Arbitrum One
  or Base. Each quote is signed, has its own exact amount, and is checked on chain against the
  signed listing.
- The exit board takes the new listing format and marks old listings as needing an update.
- Connecting keeps SSH and RDP to your own machine working. The kill switch, inbound routing and
  router setup are hardened.

### Removed

- Payments through an SRNet balance, and the contract and escrow behind them.

## [0.2.0] - 2026-10-17

First public release: patches, build scripts and an installer for running a paid, lease-gated
Session Router exit on Ubuntu 24.04, paid through an SRNet balance on Arbitrum One.
