# exit-lease

Signed lease admission for Session Router exits. Applies on top of upstream
`afb98f959` plus `patches/0000-*` (see the top-level README).

Nothing here enables `[exit]`. Exits stay off by default.

## Tools

```
admission.py keygen|issue|revoke|prune|verify   seller-side issuer
exit-lease-verify STORE PUBKEY SELLER CLIENT    prints what the exit would decide
```

The seller pack's payment watcher calls `admission.py issue` for each paid
lease hour. `lease_flow.py` is the buyer-side connect order used by the client.

## How admission works

The exit admits a client when it is open, has a matching static token, is on
the whitelist, or holds a valid lease admission. A lease admission is a record
in the store file `SR_EXIT_LEASE_ADMISSIONS` with `"v": 2`, for this exit's
`.sesh` and the inbound client's `.sesh`, not expired, and signed by the Ed25519
key in `SR_EXIT_LEASE_ISSUER_PUBKEY`. The signed bytes are:

```
"srnet-exit-admission/v2\n" + seller + "\n" + client + "\n" + exp + "\n" + tid
```

If the store or the issuer key is missing, or the record is malformed, badly
signed, for the wrong seller or client, or expired, nobody is admitted. Unsigned
`"v": 1` records are refused. The code is in `src/auth/exit_lease_admission.hpp`.

## Connect order

The client connects only inside paid time: the seller's watcher has checked the
payment on chain and written an admission for the buyer's `.sesh` address until
the end of the paid hours. The client then calls MapExit and waits for
EXIT_CAPABLE. If mapping fails, the client unmaps. Payments are final; nothing
renews by itself.

## DNS

Exits never forward port 53 to other servers. Each exit runs its own recursive
resolver (unbound on 10.195.0.53, tunnel only, no query logs), advertised in the
listing as `"dns"`; the buyer client sends all system DNS there while connected
(.sesh stays on the buyer's own router). The router itself does not change DNS:
`set_dns_mode` stays commented out in `route_poker.hpp`.

## Tests

```sh
cmake -G Ninja <src> -DBUILD_STATIC_DEPS=OFF -DSROUTER_TESTS=ON -DSUBMODULE_CHECK=OFF
ninja test_exit_lease_admission exit-lease-verify && ./test/test_exit_lease_admission
EXIT_LEASE_VERIFY=$PWD/test/exit-lease-verify python3 -m unittest discover -s contrib/exit-lease/tests
```

The patch's `test/CMakeLists.txt` builds `exit-lease-verify` from
`verify_main.cpp`, which the patch itself doesn't add. Copy this directory into
the Session Router tree first (`build-session-router.sh` does this):

```sh
cp -r contrib/exit-lease <src>/contrib/exit-lease
```
