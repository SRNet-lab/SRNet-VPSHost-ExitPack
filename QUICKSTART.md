# Quickstart

Run an SRNet exit on a fresh server.

## Requirements

- Ubuntu 24.04 LTS x86_64 (Debian 12 is untested), and a user who can use sudo
- A public IPv4 address with udp/1193 reachable
- 2+ vCPU and 4 GB RAM (or swap) for a source build, which takes about 12 minutes on 2 vCPU
  and longer on slower servers. The release install uses a prebuilt router and needs less.
- A payout wallet address that can receive USDC on Arbitrum One and Base
- An hourly price in USDC (at least 0.10)

Buyers pay USDC straight to your payout address on Arbitrum One or Base (both by default;
`--chain arbitrum` or `--chain base` for one). Real USDC; keep amounts small.

## Install from a clone

```sh
cd ~
sudo apt update && sudo apt install -y git
git clone https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack.git
cd SRNet-VPSHost-ExitPack
sudo seller/install.sh --source --payout 0xYourPayoutWallet --price-per-hour-usdc 1.00 \
  --enable-exit --i-accept-exit-operator-responsibility
```

Your listing goes on the public SRNet board within minutes. To try the pack first without
being listed, add `--board-submit-url none`.

Exit forwarding stays off unless you pass both `--enable-exit` and
`--i-accept-exit-operator-responsibility`.

## Install from a release

No release is published on this repository yet. Each release will have an `ONE-LINER.txt` with
the exact command and hash, in this form:

```sh
curl -fsSLo srnet-bootstrap.sh https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack/releases/download/<tag>/bootstrap.sh \
  && echo "<sha256 from ONE-LINER.txt>  srnet-bootstrap.sh" | sha256sum -c - \
  && sudo bash srnet-bootstrap.sh
```

The bootstrap asks for the payout address and price, shows the operator notice and installs.

Without SSH, paste the release's `user-data.yaml` into your provider's user data
field and set `SRNET_PAYOUT`, `SRNET_PRICE_PER_HOUR_USDC` and
`SRNET_ACCEPT_EXIT_RESPONSIBILITY=yes`. The server installs itself on first
boot. Progress goes to the serial console and
`/var/log/srnet-seller-install.log`.

## What gets installed

- The patched Session Router, built from the pinned upstream commit.
- Forwarding for tcp/80 and tcp/443 only. DNS is never forwarded; buyers use the
  exit's own resolver (`srnet-seller-dns`: unbound on 10.195.0.53, tunnel only,
  no query logs). Private,
  link-local and cloud metadata ranges are blocked, and buyers can't reach
  services on the server itself.
- ufw: incoming traffic denied except SSH and udp/1193.
- systemd services, each under its own user:
  - `srnet-seller-exit`: the router, with only `CAP_NET_ADMIN`
  - `srnet-seller-claimd`: the claim desk; gives signed quotes and takes payment notices
  - `srnet-seller-watcher`: checks reported payments on chain and admits the buyer for the
    paid hours
  - `srnet-seller-listing.timer`: re-signs the listing every hour
  - `srnet-seller-submit.service`: client-only router used to reach the board
- A new exit identity key and a signed listing at
  `/opt/srnet-seller/listing/listing.json`.

The listing is re-signed every hour and sent to the SRNet board
(`exitsrjnc…sesh`) through `srnet-seller-submit`. It appears to buyers within minutes, with no
approval step. Pass `--board-submit-url none` to keep it local.

## Usage

```sh
sudo srnet-seller status
sudo srnet-seller health     # binary, keys, firewall, NAT, DNS drop, exit resolver, listing age
sudo srnet-seller listing
sudo srnet-seller logs
sudo srnet-seller held       # paid quotes waiting for you (their price matches no listing)
sudo srnet-seller admit <quote id>
```

Payments land in your payout wallet directly; there is nothing to claim. Each admitted
payment is logged by the watcher (`journalctl -u srnet-seller-watcher`).

To change the price, re-run the installer with the new `--price-per-hour-usdc` (and the
other flags you used). Don't edit `listing.json` by hand. If a
paid quote matches no listing, the watcher holds it until you run
`sudo srnet-seller admit <quote id>`. Answer within 8 days.

## Uninstall

```sh
sudo srnet-seller uninstall           # remove services, firewall rules and NAT; keep keys
sudo srnet-seller uninstall --purge   # also delete keys, listing, data and service users
```

## Options

Run `seller/install.sh --help` for the full list. The common ones:

```
--source | --prebuilt [URL] | --binary PATH
--chain NAME             arbitrum, base (repeatable; default both)
--rpc CHAINID=URL        your own RPC for a chain (default: public RPCs)
--cap-usdc N             largest single payment you will quote (default 20)
--quotes-per-hour N      new quotes per hour overall on the claim desk (default 150)
--max-hours N            most hours one quote can buy
--country XX             ISO 3166-1 alpha-2; default: detected from the public IPv4
                         (two geo-IP services must agree, else --country is required)
--region NAME            optional finer detail
--max-mbit N             total cap on the exit, default 100, 0 disables
--speed-cap-mbps N       per-buyer cap each way, default 10, 0 = unlimited (shown in the listing)
--listen-port N          default 1193
--board-submit-url URL   default: the SRNet board; none = local only
--no-firewall
--dry-run --root DIR     write files under DIR and log system commands instead of running them
```
