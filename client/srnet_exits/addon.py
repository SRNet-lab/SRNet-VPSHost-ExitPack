"""The object the SRNet client loads through the "srnet.addons" entry point."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


class ExitsAddon:
    name = "exits"
    title = "Exits"
    version = None

    def __init__(self):
        from . import __version__
        self.version = __version__

    def register(self, sub) -> dict:
        from . import cli
        cmds = cli.register(sub)
        v = sub.add_parser("verify", help="check a shop payment on chain: tx, address, exact USDC amount")
        v.add_argument("--chain", required=True, help="arbitrum or base")
        v.add_argument("--tx", required=True)
        v.add_argument("--to", required=True, help="the address that should have been paid")
        v.add_argument("--amount", required=True, help="exact USDC amount, e.g. 3.50")
        cmds["verify"] = cmd_verify
        return cmds

    def doctor(self, st, line) -> None:
        from . import config, killswitch, pay, tunneldns, watch
        import chains as C
        import evm
        cfg = config.load(st.root)
        line(True, "exits add-on", f"srnet-exits {self.version}")
        for mod, pkg in (("cryptography", "cryptography"), ("zmq", "pyzmq")):
            try:
                __import__(mod)
                line(True, pkg)
            except ImportError:
                line(False, pkg, "not installed; reinstall srnet-exits")
        line(True if shutil.which("ip") else False, "ip",
             "" if shutil.which("ip") else "not found (needed by connect); install: sudo apt install iproute2")
        nft = killswitch.find_nft()
        line(True if nft else False, "nft (kill switch)",
             nft or "not found: srnet connect refuses to run without its kill switch. "
                    "Install: sudo apt install nftables  (or connect knowingly with --no-killswitch)")
        rs = tunneldns.resolved_active()
        line(True if rs else False, "systemd-resolved (sends DNS to the exit's resolver while connected)",
             "active" if rs else "not running: srnet connect refuses, since DNS would leak outside the exit")
        t = config.testnets(cfg)
        for cid in C.DEFAULT_CHAINS:
            url = C.rpc_url(cid, config.rpc_overrides(cfg), t)
            try:
                got = evm.JsonRpc(url, timeout=10).chain_id()
                line(got == cid or False, f"{C.label(cid)} RPC", url if got == cid else f"{url} is chain {got}")
            except evm.RpcError as e:
                line(None, f"{C.label(cid)} RPC", f"{e} (used to watch for your payment)")
        line(True, "payment limit", f"{C.fmt_usdc(config.max_payment(cfg))} USDC per payment")
        line(True if pay.key_path(st.root).exists() else None, "buyer key",
             str(pay.key_path(st.root)) if pay.key_path(st.root).exists() else "made on your first purchase")

    def start_ui_backend(self, st) -> str:
        from . import ui
        return ui.start_background(st)

    def ui_manifest(self) -> dict:
        from . import ui
        return {"name": self.name, "title": self.title, "tab": True, "slow": list(ui.SLOW_CALLS)}


def cmd_verify(args, st) -> int:
    import json
    import chains as C
    import evm
    from . import config, shopcheck, watch
    cfg = config.load(st.root)
    t = config.testnets(cfg)
    try:
        cid = C.by_name(args.chain, t)
        amount = C.parse_usdc(args.amount)
    except C.ChainError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        out = shopcheck.check(watch.rpc_for(cid, config.rpc_overrides(cfg), t), cid, args.tx, args.to, amount, t)
    except evm.RpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0 if out.get("paid") else 1


ADDON = ExitsAddon()
