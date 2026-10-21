# Seller guide

How to sell exit time from a server of your own: buyers connect through your server and pay you
in USDC by the hour, straight to your wallet.

## What you need

- A new Ubuntu 24.04 server (x86_64) with a public IPv4 address. 2 vCPU and 4 GB RAM is enough
  to build Session Router from source, which takes about 12 minutes on 2 vCPU.
- UDP port 1193 open to the internet. If your VPS provider has its own firewall, allow it there.
- A wallet address that can receive USDC on Arbitrum One and Base. Only the address goes on the
  server, never a private key.
- An hourly price in USDC, at least 0.10.

Running an exit sends other people's web traffic out from your server's IP address. You are
responsible for that traffic under your local law and your provider's terms. Only ports 80 and
443 are forwarded. Forwarding stays off unless you pass both `--enable-exit` and
`--i-accept-exit-operator-responsibility`.

## Install

All of these steps run on the server.

**1. Log in** over SSH as a user who can use `sudo`.

**2. Download the pack.**

```sh
cd ~
sudo apt update && sudo apt install -y git
git clone https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack.git
cd SRNet-VPSHost-ExitPack
```

**3. Install it** with your own wallet address and price:

```sh
sudo seller/install.sh --payout 0xYourPayoutWallet --price-per-hour-usdc 1.00 \
  --enable-exit --i-accept-exit-operator-responsibility
```

It asks no questions. When it finishes it prints your seller id (`seller .sesh:`), your listing
and a health check that ends with `health: OK`.

Your listing goes on the public exit board within minutes. To try the pack without being listed,
add `--board-submit-url none`, and run the command again without it when you're ready.

**4. See what a buyer sees.** On any computer with `srnet-exits` installed (see the
[README](README.md#buy-exit-time)):

```sh
srnet-exits exits
srnet-exits buy --seller <your seller id> --hours 1 --no-wait
```

Your server shows up in the list. The second command shows the payment screen with your payout
address and stops. Nothing is paid.

## Day to day

```sh
sudo srnet-seller health     # is everything working?
sudo srnet-seller status     # the services
sudo srnet-seller listing    # your signed listing
sudo srnet-seller logs       # recent activity
```

Payments land in your wallet directly; there is nothing to claim.

**Change the price:** run the install command from step 3 again with the new
`--price-per-hour-usdc`. Don't edit `listing.json` by hand.

**Update the pack:** `cd ~/SRNet-VPSHost-ExitPack && git pull`, then run the install command from
step 3 again with the same options. It builds Session Router again, so it takes as long as the
first install. Your keys and seller id are kept.

**Held payments:** if a buyer paid a quote that no longer matches your listing (for example right
after a price change), it is held. `sudo srnet-seller held` lists them and
`sudo srnet-seller admit <quote id>` lets the buyer in. Answer within 8 days.

**Uninstall:**

```sh
sudo srnet-seller uninstall           # remove services, firewall rules and NAT; keep keys
sudo srnet-seller uninstall --purge   # also delete keys, listing, data and service users
```

## Options

`seller/install.sh --help` lists them all. The common ones:

```
--chain NAME             arbitrum or base (repeatable; default both)
--rpc CHAINID=URL        your own RPC for a chain (default: public RPCs)
--cap-usdc N             largest single payment you will quote (default 20)
--max-hours N            most hours one quote can buy
--country XX             two-letter country code; default: detected from the public IPv4
--region NAME            optional, shown to buyers
--max-mbit N             total speed cap on the exit, default 100, 0 = off
--speed-cap-mbps N       per-buyer cap each way, default 10, 0 = unlimited
--ssh-port N             your SSH port, if the installer can't read it from sshd
--board-submit-url URL   default: the exit board; none = not listed
--dry-run --root DIR     write files under DIR and log system commands instead of running them
```

## Details

What the installer sets up:

- The patched Session Router, built from the pinned upstream commit, listening on udp/1193.
- Forwarding for tcp/80 and tcp/443 only. Private, link-local and cloud metadata ranges are
  blocked, and buyers can't reach services on the server itself.
- A DNS resolver for buyers (`srnet-seller-dns`: unbound on 10.195.0.53, reachable only through
  the tunnel, no query logs). Other DNS is never forwarded. `--no-dns` turns it off; buyers'
  clients then refuse to connect unless they opt out of tunnel DNS.
- ufw: incoming traffic denied except SSH and udp/1193.
- systemd services, each under its own user:
  - `srnet-seller-exit`: the router
  - `srnet-seller-claimd`: gives buyers signed quotes and takes payment notices
  - `srnet-seller-watcher`: checks each payment on chain and lets the buyer in for the paid hours
  - `srnet-seller-listing.timer`: re-signs the listing every hour
  - `srnet-seller-submit.service`: the small router used to send the listing to the board
- An exit identity key and a signed listing at `/opt/srnet-seller/listing/listing.json`. The
  listing is sent to the exit board every hour, with no approval step.
