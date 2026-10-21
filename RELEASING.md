# Building and releasing

For maintainers. Users install from a `git clone` of main; see the [README](README.md).

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

`dist/BUILDINFO` records the inputs and hash; `dist/RUNTIME-DEPS` lists the Ubuntu packages the
binary needs.

## Release

```sh
scripts/build-release.sh --version v0.1.0            # container build, or --dist DIR to reuse one
scripts/make-release.sh --version v0.1.0 \
  --router-tarball release/v0.1.0/session-router-v0.1.0-linux-x86_64.tar.gz \
  [--board-submit-url URL]
```

`build-release.sh` packs `session-router`, `exit-lease-verify` and `BUILDINFO` into
`session-router-<version>-linux-x86_64.tar.gz` with `SHA256SUMS`. `make-release.sh` then writes
the seller pack tarball, `bootstrap.sh`, `user-data.yaml`, `SHA256SUMS` and `ONE-LINER.txt` into
`release/v0.1.0/`.
