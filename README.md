# SRNet VPS Host Exit Pack

Patches, build scripts and a seller installer for running a paid, lease-gated
Session Router exit on Ubuntu 24.04.

The default is Arbitrum One (42161) and the live SRNetBalance contract, with
real USDC. It is unaudited, so keep amounts small. Other chain ids are refused. The buyer client is
[SRNet-Desktop](https://github.com/SRNet-lab/SRNet-Desktop).

Install from a clone (below). Release builds (a prebuilt router tarball, the seller pack and a
pinned `bootstrap.sh`, made with `scripts/make-release.sh`) go on
[GitHub Releases](https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack/releases) when published.
Building the router from source takes 20 to 60 minutes.

## Contract

SRNetBalance is live on Arbitrum One (42161) at `0x7E0d01B7dd916BdAB96B0971ab92D160E1664431`.
The previous deployment (`0x07bbe156083EbBcc9435C721393Edd36a383E648`) remains on-chain; new listings must use the live address above.
(tx `0xfb514a4f94049d1679b0c10ed3983be07b21151774b8a14257efa1432584637c`, block 514036941; source verified on Sourcify). The installer uses it by
default; no `--contract` is needed. Unaudited; keep amounts small.

## Install

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
seller/             installer, payment watcher, claim endpoint, listing, payout helper
cloud-init/         user-data for installs without SSH
```

## Release

```sh
scripts/build-release.sh --version v0.1.0            # container build, or --dist DIR to reuse one
scripts/make-release.sh --version v0.1.0 \
  --router-tarball release/v0.1.0/session-router-v0.1.0-linux-x86_64.tar.gz \
  [--contract 0x...] [--board-submit-url URL]
```

`build-release.sh` builds the pinned upstream commit with `patches/` in the
pinned container and packs `session-router`, `exit-lease-verify` and
`BUILDINFO` into `session-router-<version>-linux-x86_64.tar.gz` with
`SHA256SUMS`. `make-release.sh` then writes the seller pack tarball,
`bootstrap.sh`, `user-data.yaml`, `SHA256SUMS` and `ONE-LINER.txt` into
`release/v0.1.0/`. `bootstrap.sh` pins both the router tarball and the binary
inside it, downloads the tarball from GitHub Releases, checks both hashes and
builds from source if anything is missing or does not match.

## Tests

```sh
(cd seller && python3 -m unittest discover -s tests && bash tests/test_install.sh && bash tests/test_bootstrap.sh)
(cd contrib/exit-lease && python3 -m unittest discover -s tests)
(cd board && python3 -m unittest discover -s tests)
seller/tests/test_container.sh dist/session-router   # needs podman or docker
```

## Lease claims

Buyers send their signed lease claim to the seller's claim endpoint
(`claim_url` in the listing). It runs on the small client-only router the
seller also uses to reach the board, listens only on that router's tun
address, and accepts small, well-formed, signed claims at a limited rate. The
watcher checks each claim against the chain before it admits anyone. Claims
dropped into `/opt/srnet-seller/spool` by hand still work.

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

## License

`patches/` and `contrib/exit-lease/verify_main.cpp` are GPL-3.0, like upstream
Session Router. Everything else is Apache-2.0. See [LICENSE](LICENSE).
