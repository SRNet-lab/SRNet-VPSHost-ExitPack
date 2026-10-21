# Old SRNet balance: take it out

Before direct payments, the SRNet client kept USDC in an SRNet balance on Arbitrum One
(`0x7E0d01B7dd916BdAB96B0971ab92D160E1664431`). The new client doesn't use it. If you still
have USDC there, this script sends it back to your SRNet wallet. It stays here until 2027-01-18.

```sh
python3 contrib/legacy-cashout/cashout.py check            # read-only; uses ~/.srnet/keystore.json
python3 contrib/legacy-cashout/cashout.py check --address 0x...
pip install "eth-account>=0.11"
python3 contrib/legacy-cashout/cashout.py send             # asks for the passphrase, then "y"
```

`send` takes out both the unused balance and any seller earnings. Your wallet pays the network
fee, so it needs a little ETH on Arbitrum One first (a few cents is enough); the script tells you
if it's short. The money goes back to the same wallet. To move it
on from there, import the keystore file into a wallet app that reads JSON keystores.

Set `SRNET_ARB_RPC` to use your own Arbitrum One RPC.
