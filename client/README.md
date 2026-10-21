# srnet-exits

The SRNet exits client. It runs on its own as `srnet-exits`, or as an add-on for the SRNet
client, where the same commands appear under `srnet`:

```
srnet exits                          list exits from the signed board
srnet buy --seller <id> --hours N    get a quote, pay it from any wallet, wait for the seller
srnet paid [--quote ID] [--tx 0x..]  carry on after paying (or give the transaction hash)
srnet connect <seller>               route traffic and DNS through the exit (Linux, asks for sudo)
srnet status [--json]                paid time left, connection, kill switch and DNS
srnet disconnect                     DNS and routes back, kill switch off
srnet setup-router                   once: persistent router key, no blackhole routes (asks for sudo)
srnet verify --chain base --tx 0x.. --to 0x.. --amount 3.50   check a shop payment on chain
```

As an add-on it also adds an **Exits** tab to `srnet ui`. Without it, the SRNet client has no
exit, wallet or payment code at all and those commands print how to install it.

SRNet is an independent project. It is not affiliated with or endorsed by the Session Foundation
or the Session Technology Foundation.

## Use exits without SRNet-Desktop

You need Ubuntu 24.04 with Session Router installed and running: steps 1 and 2 of the
[SRNet-Desktop install guide](https://github.com/SRNet-lab/SRNet-Desktop#1-session-router).
Then, as your own user (not with sudo):

```sh
cd ~
sudo apt install -y git python3-venv nftables
git clone https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack.git
SRNet-VPSHost-ExitPack/client/install.sh
```

The installer:

- makes its own Python environment in `~/.local/share/srnet-exits` and puts the `srnet-exits`
  command in `~/.local/bin` (it never touches an `srnet` command you may have);
- runs `srnet-exits setup-router` once (sudo asks for your password);
- adds one marked line to `~/.bashrc` (and `~/.zshrc` if you have one) so new terminals find
  `~/.local/bin`.

Run it again to update. `client/install.sh --uninstall` removes the command, its environment and
that one line; your paid time and settings in `~/.srnet` stay. It refuses while an exit is
connected. With `--force` it disconnects first.

Every command below works the same with `srnet-exits` in place of `srnet`, and
`srnet-exits doctor` checks Session Router, `.sesh` names, the clock and everything exits need.
`srnet-exits ui` opens the Exits page in your browser (on 127.0.0.1 only). With `--no-browser`
it prints the address for you to open.

## Add exits to the SRNet client

Into the same Python environment as the SRNet client:

```sh
~/SRNet-Desktop/install.sh --with-exits
```

or by hand, with the client's own Python:

```sh
python -m pip install "srnet-exits @ git+https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack"
```

`srnet doctor` lists the add-on and checks what it needs: `ip`, `nft` (kill switch),
systemd-resolved, and the Arbitrum One and Base RPCs.

You can have both. `srnet` and `srnet-exits` use the same `~/.srnet`: the same paid time, the same
buyer key and one connection, so either one can disconnect what the other connected.

## Buying exit time

```sh
srnet exits
srnet buy --seller <id> --hours 2
```

The seller answers with a quote bound to your `.sesh` address. The client prints the chain,
the payout address, the exact amount and a QR code (scan it with a phone wallet), with the
amount, chain, address and end time again below the code, then watches the chain. Send exactly
that amount of USDC on that chain, from any wallet; you pay the network fee. The amount is the
price plus a payment ID, usually under one cent (0.50 for an hour can be 0.505521).

Once the transfer is seen, the client tells the seller, the seller checks it on chain and
admits you for the hours you paid. Then:

```sh
srnet connect <id>
curl -s https://api.ipify.org; echo    # the exit's IP
srnet status
srnet disconnect
```

`status` shows whether you're connected, how much paid time is left and whether the kill switch
is on. `status --json` prints the full record.

`connect` again later works with no new purchase while paid time is left. About 10 minutes
before it ends you get a warning (a desktop notification if `notify-send` is there). Nothing
renews by itself: run `srnet buy` again to add time.

Good to know:

- A quote is good for about 15 minutes. If you close the terminal, `srnet paid` picks up the
  latest open quote; `srnet paid --tx 0x...` uses a transaction hash you already have.
- Wrong amount or wrong chain: the payment is not matched. Ask the seller directly.
- Payments are final. One payment is limited to 20 USDC by default. Change it with
  `--max-usdc` or `"exits": {"max_payment_usdc": "40"}` in `~/.srnet/config.json`.
  Sellers have their own limit too.
- Arbitrum One is used unless you pick Base (`--chain base`, or `"exits": {"chain": "base"}`).
  USDC only.
- Your own RPC: `"exits": {"rpc": {"42161": "https://...", "8453": "https://..."}}`.
- Exits from older seller packs show "seller needs to update" and can't be bought from.
- Your computer's clock has to be within 2 minutes of the real time.

## Connect details

`connect` arms an nftables kill switch and sends all DNS to the exit's own resolver through the
tunnel (`.sesh` stays on Session Router). `disconnect` puts your DNS settings back. It refuses exits that run no resolver unless you pass `--system-dns`, and refuses to
run without the kill switch unless you pass `--no-killswitch`. As a normal user `nft` runs
through sudo; the Exits tab can only connect if sudo needs no password, otherwise run
`srnet connect` in a terminal (as yourself, not `sudo srnet`).

The kill switch blocks everything outside the tunnel except UDP, which Session Router needs. Apps
that send UDP, such as QUIC in browsers and WebRTC calls, can still leave outside the exit if the
tunnel drops or they pick your normal connection themselves. DNS outside the tunnel is blocked.

On a remote machine (a VPS you reach over SSH or RDP), connecting keeps those sessions working.
None of it (kill switch, routes, rules) survives a reboot.

- Containers: published ports (Docker and the like) keep answering on your normal interface while
  connected, but the kill switch covers only this machine's own traffic, not traffic that
  containers or VMs start themselves.
- UDP servers bound to all addresses (0.0.0.0, e.g. mosh) answer from the tunnel address, so
  their replies go into the tunnel and the client never gets them; TCP services such as SSH and
  RDP are not affected. Bind such a server to your public IP, or disconnect while you need it.

Session Router needs two client settings for exits: a persistent key (`keyfile=`; paid time
belongs to your `.sesh` address, which otherwise changes at every restart) and
`blackhole-routes=false` (otherwise the router blackholes all traffic while an exit is mapped).
`srnet setup-router` sets both (one sudo prompt, and it keeps a copy of the original as
`client.ini.srnet-orig`); `client/install.sh` and the desktop's `install.sh --with-exits` run it.
`srnet buy` and `srnet connect` refuse until they are set.

## Tests

```sh
python3 -m unittest discover -s client/tests -t client/tests
client/tests/lab/run.sh     # the real nft/ip rules in network namespaces (needs nft + user namespaces)
```

The tests run without the SRNet client. If it is installed in the same environment, in
`$SRNET_CLIENT_DIR`, or in a sibling `SRNet-Desktop` checkout, they also check the add-on through
`srnet`. `test_e2e_local.py` runs the seller's claim desk and watcher and the buyer commands as
separate processes against a mock chain on loopback; no real funds.
