# SRNet VPS Host Exit Pack

**What's new**

- No developer fee anywhere in SRNet.
- Simpler payments: buyers pay the seller's payout address straight from their own wallet, by
  the hour. No balance, no contract and no escrow.
- `srnet-exits` runs on its own, so you can use exits without SRNet-Desktop.

Full history: [CHANGELOG.md](CHANGELOG.md)

An exit is a server that sends your internet traffic out from its own IP address, like a VPN.
This repository lets you **buy** time on an exit and connect through it, or **sell** time on a
server of your own. Buyers pay sellers directly in USDC, by the hour.

This is beta software. Keep amounts small.

SRNet is an independent project. It is not affiliated with or endorsed by the Session Foundation
or the Session Technology Foundation.

## Buy exit time

You need:

- A computer with Ubuntu 24.04.
- A few USDC on Arbitrum One or Base in any wallet (a phone wallet works), plus a little ETH on
  the same network to pay the network fee.

If you already use SRNet-Desktop, skip to [Add exits to SRNet-Desktop](#add-exits-to-srnet-desktop).

**1. Install Session Router.** Session Router is the private network exits run on. Follow steps
1 and 2 of the [SRNet-Desktop install guide](https://github.com/SRNet-lab/SRNet-Desktop#1-session-router).
When you're done, `getent hosts localhost.sesh` prints an address starting with `fd2e:`.

**2. Install the exits client.** Run this as yourself, not with `sudo`:

```sh
cd ~
sudo apt install -y git python3-venv nftables
git clone https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack.git
SRNet-VPSHost-ExitPack/client/install.sh
```

It asks for your password once, to set up Session Router for exits. Then open a new terminal, so
the `srnet-exits` command is found.

**3. Check your setup.**

```sh
srnet-exits doctor
```

It ends with `all good` when everything is ready. A `FAIL` line says what is missing.

**4. Pick a seller.**

```sh
srnet-exits exits
```

You get a table of sellers with their country and hourly price. Copy the seller id (the long
name ending in `.sesh`) from the first column.

**5. Buy an hour.**

```sh
srnet-exits buy --seller <seller id> --hours 1
```

The first time, it shows what the exit can see about your traffic and asks you to type
`I UNDERSTAND`. Then it shows a QR code, the address and the exact amount to send. Send exactly
that amount from your wallet, on the network it names. Leave the terminal open: it waits for the
payment and says when the seller has confirmed it. This usually takes a minute or two.

**6. Connect.**

```sh
srnet-exits connect <seller id>
curl -s https://api.ipify.org; echo
```

`connect` asks for your password. The second line prints the exit's IP address instead of yours.

**7. Disconnect when you're done.**

```sh
srnet-exits disconnect
```

Your normal connection comes back. Paid time you haven't used stays, and `connect` works again
until it runs out. About 10 minutes before the end you get a warning. To add time, run `buy`
again.

`srnet-exits status` shows your paid time and whether you're connected. `srnet-exits ui` does all
of this in a page in your browser. More: [client/README.md](client/README.md).

### Add exits to SRNet-Desktop

```sh
~/SRNet-Desktop/install.sh --with-exits
```

Then use `srnet exits`, `srnet buy`, `srnet connect` and `srnet disconnect` the same way as
above, or the **Exits** tab in the app. `srnet` and `srnet-exits` share your paid time, so you
can have both.

To update the exits client: `cd ~/SRNet-VPSHost-ExitPack && git pull && client/install.sh`. To
update SRNet-Desktop and its exits add-on: `cd ~/SRNet-Desktop && git pull && ./install.sh`.

## Sell exit time

You need:

- A new Ubuntu 24.04 server (x86_64) from any VPS provider, with at least 2 vCPU, 4 GB RAM and a
  public IPv4 address. If the provider has its own firewall, allow UDP port 1193 in.
- A wallet address that can receive USDC on Arbitrum One and Base. Only the address goes on the
  server, never a private key.
- An hourly price in USDC, at least 0.10.

Running an exit sends other people's web traffic out from your server's IP address. You are
responsible for that traffic under your local law and your provider's terms. Only web traffic
(ports 80 and 443) goes out.

**1. Log in to the server** over SSH as a user who can use `sudo`.

**2. Download the pack.**

```sh
cd ~
sudo apt update && sudo apt install -y git
git clone https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack.git
cd SRNet-VPSHost-ExitPack
```

**3. Install.** Put in your own wallet address and price:

```sh
sudo seller/install.sh --payout 0xYourPayoutWallet --price-per-hour-usdc 1.00 \
  --enable-exit --i-accept-exit-operator-responsibility
```

The last two flags turn on forwarding and confirm that you accept the responsibility above.
The installer asks no questions. It builds Session Router, which takes about 12 minutes on a
2 vCPU server and longer on a slower one. At the end it prints your seller id (the
`seller .sesh:` line) and a health check that ends with `health: OK`.

Your listing goes on the public exit board within minutes, and buyers can pay you from then on.
Payments go straight to your wallet; there is nothing to claim.

To try it first without being listed, add `--board-submit-url none` to the install command.
Run the command again without it when you're ready.

**4. Check on it any time.**

```sh
sudo srnet-seller health
```

`sudo srnet-seller logs` shows recent activity. To change the price, run the install command
again with the new `--price-per-hour-usdc`.

Day-to-day commands, options and what the installer sets up: [QUICKSTART.md](QUICKSTART.md).
`seller/install.sh --help` lists every option.

## Details

### How paying works

1. `buy` asks the seller for a quote: its payout address, the network and an exact amount. The
   amount is the hourly price times the hours, plus a payment ID of under one cent that tells
   your payment apart from others.
2. You send that amount from any wallet. The client watches the chain and tells the seller when
   it sees the payment.
3. The seller checks the payment on chain and lets you in for the hours you paid.

A quote is good for about 15 minutes. A wrong amount or the wrong network is not matched.
Payments are final. One payment is limited to 20 USDC by default.

### Kill switch

`connect` turns on a kill switch. The kill switch blocks everything outside the tunnel except
DHCP lease renewal and Session Router's own UDP, which it needs. DNS outside the tunnel is
blocked. If connect can't tell which user Session Router runs as, it lets all UDP out and says so.

### Building and releasing

Build pins, patch order and release steps: [RELEASING.md](RELEASING.md).

### Layout

```
patches/            changes to upstream Session Router
board/              signed exit board: admin CLI, server, deploy script
contrib/exit-lease/ admission issuer and exit-lease-verify
scripts/            build and release scripts
docker/             pinned container build
seller/             installer, quotes, claim desk, payment watcher, listing
client/             the buyer client: srnet-exits on its own, or the SRNet client's add-on
contrib/legacy-cashout/  take out USDC left in the old SRNet balance (until 2027-01-18)
cloud-init/         user-data for installs without SSH
```

### Old SRNet balance

Payments used to go through an SRNet balance on Arbitrum One. That is gone. If you still have
USDC there, `contrib/legacy-cashout/` sends it back to your wallet; see its
[README](contrib/legacy-cashout/README.md). It stays in this repository until 2027-01-18.

## Related repositories

- [SRNet-Desktop](https://github.com/SRNet-lab/SRNet-Desktop): the SRNet client. With this
  add-on installed, buyers find, pay for and connect to exits from it.
- [Session-Router-SRNet](https://github.com/SRNet-lab/Session-Router-SRNet): the Session Router
  build the client runs, with exit mapping added.
- [SRNet-TypedSNApp-HostPack](https://github.com/SRNet-lab/SRNet-TypedSNApp-HostPack): run your
  own host for boards, stores, wikis, chat rooms, file drops, profiles, directories or mail.

## License

`patches/` and `contrib/exit-lease/verify_main.cpp` are GPL-3.0, like upstream
Session Router. See [LICENSE](LICENSE) for the rest.

## Donations

If SRNet is useful to you, donations are welcome at
`0xec4fEECa354C7eabbe8957a23c361Aa365E3b377` (USDC or ETH on Arbitrum One or Base).
