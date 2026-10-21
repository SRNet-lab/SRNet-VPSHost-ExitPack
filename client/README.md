# srnet-exits

The exits client: find a seller, pay for exit time from your own wallet and send your traffic
through their exit. It runs on its own as `srnet-exits`, or as an add-on for the SRNet client,
where the same commands appear under `srnet`.

SRNet is an independent project. It is not affiliated with or endorsed by the Session Foundation
or the Session Technology Foundation.

## Install on its own

On Ubuntu 24.04:

**1. Install Session Router** with steps 1 and 2 of the
[SRNet-Desktop install guide](https://github.com/SRNet-lab/SRNet-Desktop#1-session-router).

**2. Install the client.** Run this as yourself, not with `sudo`:

```sh
cd ~
sudo apt install -y git python3-venv nftables
git clone https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack.git
SRNet-VPSHost-ExitPack/client/install.sh
```

It asks for your password once, to set up Session Router for exits, and puts the `srnet-exits`
command in `~/.local/bin`.

**3. Open a new terminal** and check your setup:

```sh
srnet-exits doctor
```

It ends with `all good` when everything is ready.

Run `client/install.sh` again to update. `client/install.sh --uninstall` removes the client and
keeps your paid time and settings in `~/.srnet`.

## Install into SRNet-Desktop

```sh
~/SRNet-Desktop/install.sh --with-exits
```

This adds the commands below to `srnet` and an **Exits** tab to the app. You can have both:
`srnet` and `srnet-exits` share the same paid time and connection.

## Buy time and connect

The commands are the same with `srnet` or `srnet-exits`.

**1. List sellers** and copy a seller id (the long `.sesh` name in the first column):

```sh
srnet-exits exits
```

**2. Buy an hour:**

```sh
srnet-exits buy --seller <seller id> --hours 1
```

The first time, type `I UNDERSTAND` after reading what the exit can see. Then send exactly the
amount shown, on the network shown, from any wallet. You pay the network fee in ETH. Leave the
terminal open until it says the seller confirmed your time.

**3. Connect** (asks for your password):

```sh
srnet-exits connect <seller id>
```

**4. Disconnect** when you're done:

```sh
srnet-exits disconnect
```

`srnet-exits status` shows your paid time and connection. `srnet-exits ui` opens the same thing
as a page in your browser.

About 10 minutes before your time runs out you get a warning. Nothing renews by itself: run
`buy` again to add time. If you closed the terminal before the payment went through,
`srnet-exits paid` picks it up again.

## All commands

```
srnet exits                          list exits from the signed exit board
srnet buy --seller <id> --hours N    get a quote, pay it from any wallet, wait for the seller
srnet paid [--quote ID] [--tx 0x..]  carry on after paying (or give the transaction hash)
srnet connect <seller>               route traffic and DNS through the exit (Linux, asks for sudo)
srnet status [--json]                paid time left, connection, kill switch and DNS
srnet disconnect                     DNS and routes back, kill switch off
srnet setup-router                   once: persistent router key, no blackhole routes (asks for sudo)
srnet verify --chain base --tx 0x.. --to 0x.. --amount 3.50   check a shop payment on chain
```

`srnet-exits` also has `doctor` and `ui`.

## Details

### Payments

- The amount is the price plus a payment ID of under one cent (0.50 for an hour can be
  0.505521). It is how the seller tells your payment apart from others.
- A quote is good for about 15 minutes. A wrong amount or the wrong network is not matched; ask
  the seller directly.
- Payments are final. One payment is limited to 20 USDC by default. Change it with
  `--max-usdc` or `"exits": {"max_payment_usdc": "40"}` in `~/.srnet/config.json`.
- Arbitrum One is used unless you pick Base (`--chain base`, or `"exits": {"chain": "base"}`).
- Your own RPC: `"exits": {"rpc": {"42161": "https://...", "8453": "https://..."}}`.
- Exits from older seller packs show "seller needs to update" and can't be bought from.
- Your computer's clock has to be within 2 minutes of the real time.

### Connecting

`connect` turns on a kill switch (nftables) and sends all DNS to the exit's own resolver through
the tunnel (`.sesh` names stay on Session Router). `disconnect` puts your DNS settings back. It
refuses exits that run no resolver unless you pass `--system-dns`, and refuses to run without the
kill switch unless you pass `--no-killswitch`. The Exits tab can only connect if sudo needs no
password; otherwise run `connect` in a terminal.

The kill switch blocks everything outside the tunnel except DHCP lease renewal and Session
Router's own UDP, which it needs. DNS outside the tunnel is blocked. Connect finds Session
Router's user from the running router, or else the `_session` user its package creates. If the
router runs as root or as your own user, other programs running as that user can still send UDP
outside the exit, and connect says so. If connect can't tell which user Session Router runs as,
it lets all UDP out and says so.

On a remote machine (a VPS you reach over SSH or RDP), connecting keeps those sessions working.
None of it (kill switch, routes, rules) survives a reboot.

- Containers: published ports (Docker and the like) keep answering on your normal interface while
  connected, but the kill switch covers only this machine's own traffic, not traffic that
  containers or VMs start themselves.
- UDP servers bound to all addresses (0.0.0.0, e.g. mosh) answer from the tunnel address, so
  their replies go into the tunnel and the client never gets them. TCP services such as SSH and
  RDP are not affected. Bind such a server to your public IP, or disconnect while you need it.

### Session Router settings

Exits need two Session Router settings: a persistent key (`keyfile=`; paid time belongs to your
`.sesh` address, which otherwise changes at every restart) and `blackhole-routes=false`.
`srnet setup-router` sets both with one sudo prompt and keeps a copy of the original as
`client.ini.srnet-orig`. Both installers run it for you. `buy` and `connect` refuse until they
are set.

### Tests

```sh
python3 -m unittest discover -s client/tests -t client/tests
client/tests/lab/run.sh     # the real nft/ip rules in network namespaces (needs nft + user namespaces)
```

The tests run without the SRNet client. If it is installed in the same environment, in
`$SRNET_CLIENT_DIR`, or in a sibling `SRNet-Desktop` checkout, they also check the add-on through
`srnet`. `test_e2e_local.py` runs the seller side and the buyer commands as separate processes
against a mock chain on loopback; no real funds.
