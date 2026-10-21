# Old SRNet balance: take it out

Earlier versions of the SRNet client kept USDC in an SRNet balance on Arbitrum One
(`0x7E0d01B7dd916BdAB96B0971ab92D160E1664431`). The current client doesn't use it. If you still
have USDC there, this script sends it back to your SRNet wallet. It stays here until 2027-01-18.

Run these on the computer that has your old SRNet wallet (`~/.srnet/keystore.json`), from the
`SRNet-VPSHost-ExitPack` folder.

**1. See what's left.** This only reads:

```sh
python3 contrib/legacy-cashout/cashout.py check
```

To check another address, add `--address 0x...`.

**2. Add a little ETH on Arbitrum One** to that wallet for the network fee. A few cents is
enough; `send` tells you if it's short.

**3. Send it back to your wallet:**

```sh
sudo apt install -y python3-venv
python3 -m venv ~/cashout-env
~/cashout-env/bin/pip install "eth-account>=0.11"
~/cashout-env/bin/python3 contrib/legacy-cashout/cashout.py send
```

It asks for your wallet passphrase, then for `y` to confirm. It takes out both the unused
balance and any seller earnings, back to the same wallet. To move the money on from there,
import the keystore file into a wallet app that reads JSON keystores.

Set `SRNET_ARB_RPC` to use your own Arbitrum One RPC.
