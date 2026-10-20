# SRNet VPS Host Exit Pack

Everything for SRNet exits in one place:

- **Seller pack** (`seller/`): patches, build scripts and an installer for running a paid,
  lease-gated Session Router exit on Ubuntu 24.04.
- **Buyer add-on** (`client/`, the `srnet-exits` package): plugs into the SRNet client
  ([SRNet-Desktop](https://github.com/SRNet-lab/SRNet-Desktop)) and adds `srnet exits`,
  `srnet buy`, `srnet connect` and the Exits tab.
- **Board** (`board/`): the signed list of exits that buyers browse.

Buyers pay sellers directly in USDC on Arbitrum One or Base, from any wallet. There is no
contract, no balance to top up and no middleman: the seller's payout address gets the payment,
and the buyer pays the network fee. This is beta software; keep amounts small.

SRNet is an independent project. It is not affiliated with or endorsed by the Session Foundation
or the Session Technology Foundation.

Install from a clone (below). Release builds (a prebuilt router tarball, the seller pack and a
pinned `bootstrap.sh`, made with `scripts/make-release.sh`) go on
[GitHub Releases](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack/releases) when published.
Building the router from source takes 20 to 60 minutes.

## How paying works

1. The buyer runs `srnet buy --seller <id> --hours N` (or presses Buy in the Exits tab).
2. The seller's claim desk answers with a signed quote: payout address, chain, the official USDC
   contract, and an exact amount (the hourly price times N plus a few millionths of a USDC as a
   payment ID, so no two quotes in 7 days share an amount). The payment ID is usually under one
   cent. When many quotes are out it can grow, but never past 0.01 USDC or 2% of the price,
   whichever is larger, and the payment screen shows it. The quote is bound to
   the buyer's `.sesh` address and expires after about 15 minutes. One payment is capped at
   20 USDC by default, and the price is at least 0.01 USDC an hour.
3. The client shows the address, the amount and a QR code (EIP-681), then watches the chain.
4. When the exact transfer shows up, the client tells the seller "quote X is paid, tx Y".
   The seller's watcher checks that transaction on chain itself (read-only) and admits the
   buyer for the quoted hours once it is a few blocks deep (Arbitrum 20, Base 5, a few seconds).
   The block time has to fall inside the quote's 15 minutes (with 5 minutes of slack for a
   seller clock that runs fast). A transaction can only be used once.
5. About 10 minutes before the time runs out the client warns. Nothing renews by itself;
   to keep going, buy again.

If the amount or the chain is wrong, nothing is matched and the client says so plainly.
Payments are final.

A payment made in time is never lost because someone came back late or an RPC was down. The
seller takes the report for 8 days and keeps checking while its RPC is down. If the client
couldn't read the chain, it says so and tells you not to pay again; `srnet paid --tx 0x...`
reports a hash you already have.

## Buyer: install the add-on

The add-on goes into the same Python environment as the SRNet client:

```sh
~/SRNet-Desktop/install.sh --with-exits
```

or, by hand, `<the client's python> -m pip install "srnet-exits @ git+https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack"`.
`srnet doctor` shows whether it is loaded. Details: [client/README.md](client/README.md).

## Seller: install

On a fresh Ubuntu 24.04 x86_64 server, as root:

```sh
cd ~
apt update && apt install -y git
git clone https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack.git
cd SRNet-VPSHost-ExitPack
seller/install.sh --source --payout 0xYourPayoutWallet --price-per-hour-usdc 1.00 \
  --enable-exit --i-accept-exit-operator-responsibility
```

Once a release is published, its `ONE-LINER.txt` has the exact `bootstrap.sh` command and hash;
the bootstrap falls back to a source build if the router tarball is missing or its hash doesn't
match.

See [QUICKSTART.md](QUICKSTART.md) for requirements, what gets installed and
day-to-day commands. `seller/install.sh --help` lists every flag.

## Build

The router is built from public upstream Session Router at
`afb98f959f4f7e0ef996aef02fa21a6caf26b1e9` (dev, v1.0.2-126) with the patches in
`patches/` applied. Submodule pins and apply order:

```
url:      https://github.com/session-foundation/session-router.git
  branch:   dev
  commit:   afb98f959f4f7e0ef996aef02fa21a6caf26b1e9
  describe: v1.0.2-126-gafb98f959  ("Merge pull request #59 from jagerman/embedded-path-stats")

Submodules: the public pins recorded in that commit (no forks, .gitmodules unchanged).
  cmake/session-deps      65a7618f72367e659bae0620be350e420d04f0e6
  external/CLI11          bfffd37e1f804ca4fae1caae106935791696b6a9
  external/mlkem-native   ebed0f388fb4a8a4ff99ef7c32e19ac36b0cdba3
  external/nlohmann       9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03
  external/oxen-libquic   c1835882a09cce6ff120ad7cba29b7d050359f5e
  external/oxen-mq        5117440a87a4199f14da8956247c695ed21d1c2f
  external/pybind11       3e9dfa2866941655c56877882565e7577de6fc7b
  external/sqlite_orm     ff7c878af872f7987b62d825284fb25d6043af10
  (gui and test/Catch2 are not needed for the daemon; Catch2 is fetched for tests)

Apply, in order:
  1. patches/0000-linux-base-exit-client-prereqs.patch
  2. patches/0001-exit-lease-admission-and-policy.patch
     (patches/series/0001-0005 are the same change split per commit; use either
      the combined 0001 file or the five series files, not both)
  3. patches/session-deps/0001-libngtcp2-find-system-gnutls.patch inside every vendored
     session-deps copy (cmake/session-deps and external/oxen-libquic/cmake/session-deps)
     (backport of public session-deps d74f5b1376 so the static ngtcp2 that libquic builds
      on Ubuntu 24.04 finds the system gnutls)
  4. cp -r contrib/exit-lease <src>/contrib/exit-lease   (needed by the exit-lease-verify target)

scripts/build-session-router.sh does all of this and checks every hash.
```

```sh
scripts/build-session-router.sh --install-deps --tests --out dist
```

Or in a container:

```sh
podman build --network host -f docker/Dockerfile \
  --build-arg APT_SNAPSHOT=20261011T000000Z --target out -o dist .
sha256sum dist/session-router
```

The build stops if the upstream commit doesn't match, a patch doesn't apply, or
the binary is missing the admission code. `dist/BUILDINFO` records the inputs
and hash; `dist/RUNTIME-DEPS` lists the Ubuntu packages the binary needs.

## Layout

```
patches/            changes to upstream Session Router
board/              signed seller board: operator CLI, server, deploy script
contrib/exit-lease/ admission issuer and exit-lease-verify
scripts/            build and release scripts
docker/             pinned container build
seller/             installer, quotes, claim desk, payment watcher, listing
client/             the buyer add-on for the SRNet client (srnet-exits)
contrib/legacy-cashout/  take out USDC left in the old SRNet balance (until 2027-01-18)
cloud-init/         user-data for installs without SSH
```

## Release

```sh
scripts/build-release.sh --version v0.1.0            # container build, or --dist DIR to reuse one
scripts/make-release.sh --version v0.1.0 \
  --router-tarball release/v0.1.0/session-router-v0.1.0-linux-x86_64.tar.gz \
  [--board-submit-url URL]
```

`build-release.sh` builds the pinned upstream commit with `patches/` in the
pinned container and packs `session-router`, `exit-lease-verify` and
`BUILDINFO` into `session-router-<version>-linux-x86_64.tar.gz` with
`SHA256SUMS`. `make-release.sh` then writes the seller pack tarball,
`bootstrap.sh`, `user-data.yaml`, `SHA256SUMS` and `ONE-LINER.txt` into
`release/v0.1.0/`. `bootstrap.sh` pins both the router tarball and the binary
inside it, downloads the tarball from GitHub Releases, checks both hashes and
builds from source if anything is missing or does not match.

## Claim desk

The claim desk (`srnet-seller-claimd`) answers `/claim/quote`, `/claim/paid` and
`/claim/status/<id>` at the `claim_url` in the listing. It runs on the small client-only
router the seller also uses to reach the board, listens only on that router's tun address,
and takes small, signed requests at a limited rate. It never holds a key that can move
funds. The watcher (`srnet-seller-watcher`) checks each reported payment on chain before it
admits anyone.

The desk runs as its own user (`srnet-claim`). It holds the quote key, but it can't read the
watcher's admission key, its RPC settings or its record of used payments. The watcher doesn't
take the desk's word for a quote either: it reads the signed listing itself and checks every
quote's payout address, chain, token, price and hours against it before it looks at the chain.

Limits per connection and overall (requests a minute, quotes an hour, open quotes, 3
transaction hashes per quote, 10 second socket timeouts) keep one requester from wearing the
desk down. Open quotes are counted per buyer key and per connection, never by the `.sesh`
address a request names. Quote requests are signed by the buyer key with the current time,
and each signed request is taken once, so another seller who has seen your requests can't
send them again to use up your limit. One buyer key per install is enough for that, so the
client doesn't keep one per seller.

Known beta limit: someone who keeps restarting Session Router to get new addresses can use up
the desk's hourly quote limit (or, for one cheap purchase, its 7-day pool of payment IDs), and
new buyers then get "busy" for a while; no money is at risk.

If the desk's index (`claims/index.sqlite`) is damaged, the desk moves it aside and rebuilds it
from its files on the next start. To check that two buyers come in on different sessions, set
`SRNET_CLAIM_DEBUG_SOURCES=1` in `config/claimd.env` and restart the desk: it then logs a salted
tag for each quote's connection (never the address).

## Old SRNet balance

Payments used to go through an SRNet balance on Arbitrum One. That is gone. If you still have
USDC there, `contrib/legacy-cashout/` sends it back to your wallet; see its
[README](contrib/legacy-cashout/README.md). It stays in this repository until 2027-01-18.

## Operating an exit

An exit sends other people's traffic out from your IP address. You are
responsible for it under your local law and your host's terms. Only tcp/80 and
tcp/443 are forwarded to the internet. DNS is never forwarded: buyers' lookups
are answered by the exit's own recursive resolver (unbound on 10.195.0.53, reachable
only through the tunnel). It resolves from the root servers itself (no forwarding
to any public resolver), validates DNSSEC, uses QNAME minimisation, keeps no query
logs and is rate limited per buyer. The listing advertises it as `"dns"`. Turn it
off with `--no-dns` (buyers' clients then refuse to connect unless they opt out of
tunnel DNS).

## Related repositories

- [SRNet-Desktop](https://github.com/SRNet-lab/SRNet-Desktop): the SRNet client. With this
  add-on installed, buyers find, pay for and connect to exits from it.
- [Session-Router-SRNet](https://github.com/SRNet-lab/Session-Router-SRNet): the Session Router
  build the client runs, with exit mapping added.

## License

`patches/` and `contrib/exit-lease/verify_main.cpp` are GPL-3.0, like upstream
Session Router. See [LICENSE](LICENSE) for the rest.

## Donations

If SRNet is useful to you, donations are welcome at
`0xec4fEECa354C7eabbe8957a23c361Aa365E3b377` (USDC or ETH on Arbitrum One or Base).
