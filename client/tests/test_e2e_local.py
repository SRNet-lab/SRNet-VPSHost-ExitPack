"""Local end-to-end on loopback, no real funds: a mock EVM JSON-RPC chain (Anvil's id 31337 and its
mock USDC address), the seller's claim desk and watcher as separate processes, and the buyer's
`srnet-exits buy` / `paid` commands as separate processes.

    buy --no-wait  -> quote bound to this client, exact amount
    (pay on the mock chain)
    paid           -> client finds the transfer, reports it, the watcher checks it and admits us
    wrong amount   -> the seller rejects it with a plain "No matching payment yet"
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import helpers
from helpers import ME

F = helpers.seller_fixtures()
import admission as A  # noqa: E402
import chains as C  # noqa: E402
import evm  # noqa: E402

CHAIN = 31337
SELLER_DIR = helpers.REPO / "seller"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class MockChain:
    """Just enough JSON-RPC for the client and the watcher: chain id, blocks, logs, receipts."""

    def __init__(self):
        self.lock = threading.Lock()
        self.head, self.logs, self.receipts, self.times = 1, [], {}, {1: int(time.time())}

    def tick(self):
        with self.lock:
            self.head += 1
            self.times[self.head] = int(time.time())

    def pay(self, tx, to, value, token=None):
        token = token or C.usdc(CHAIN, True)
        with self.lock:
            self.head += 1
            self.times[self.head] = int(time.time())
            log = {"address": token, "topics": [evm.TRANSFER_TOPIC, evm.addr_topic("0x" + "11" * 20),
                                                evm.addr_topic(to)],
                   "data": "0x" + f"{int(value):064x}", "transactionHash": tx, "blockNumber": hex(self.head),
                   "logIndex": "0x0"}
            self.logs.append(log)
            self.receipts[tx] = {"status": "0x1", "blockNumber": hex(self.head), "logs": [log]}
        return tx

    def handle(self, method, params):
        with self.lock:
            if method == "eth_chainId":
                return hex(CHAIN)
            if method == "eth_blockNumber":
                return hex(self.head)
            if method == "eth_getBlockByNumber":
                n = self.head if params[0] == "latest" else int(params[0], 16)
                return {"number": hex(n), "timestamp": hex(self.times.get(n, int(time.time())))}
            if method == "eth_getTransactionReceipt":
                return self.receipts.get(params[0])
            if method == "eth_getLogs":
                f = params[0]
                lo = int(f.get("fromBlock", "0x0"), 16)
                hi = self.head if f.get("toBlock") in (None, "latest") else int(f["toBlock"], 16)
                want = [t.lower() if isinstance(t, str) else t for t in f.get("topics") or []]
                out = []
                for lg in self.logs:
                    if lg["address"].lower() != str(f.get("address", "")).lower():
                        continue
                    if not lo <= int(lg["blockNumber"], 16) <= hi:
                        continue
                    if any(w is not None and lg["topics"][i].lower() != w for i, w in enumerate(want)):
                        continue
                    out.append(lg)
                return out
        raise ValueError(method)

    def serve(self):
        chain = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    body = {"jsonrpc": "2.0", "id": req.get("id"), "result": chain.handle(req["method"], req.get("params") or [])}
                except Exception as e:
                    body = {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32601, "message": str(e)}}
                b = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.srv.server_address[1]}"


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.procs = []
        self.chain = MockChain()
        self.rpc = self.chain.serve()
        threading.Thread(target=self._miner, daemon=True).start()
        self.s = F.Seller(price="0.50", chains=(CHAIN,), dns="10.195.0.53")
        self.port = free_port()
        self.spawn([sys.executable, str(SELLER_DIR / "claimd.py"), "--host", "127.0.0.1", "--port", str(self.port),
                    "--dir", str(self.s.state), "--quote-key", str(self.s.quote_key), "--listing",
                    str(self.s.listing_path), "--testnets"])
        self.spawn([sys.executable, str(SELLER_DIR / "watcher.py"), "--dir", str(self.s.state), "--seller", self.s.sesh,
                    "--listing", str(self.s.listing_path), "--issuer-key", str(self.s.issuer), "--store", str(self.s.store), "--rpc", f"{CHAIN}={self.rpc}",
                    "--testnets", "--interval", "1", "--confirmations", "2"])
        self.home = Path(tempfile.mkdtemp())
        (self.home / "visibility.ack").write_text("I UNDERSTAND\n")
        (self.home / "config.json").write_text(json.dumps({"exits": {"rpc": {str(CHAIN): self.rpc}}}))
        self.board = self.home / "board.json"
        self.board.write_text(json.dumps([self.s.listing_doc]))
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    def _miner(self):
        while not getattr(self, "done", False):
            time.sleep(0.5)
            self.chain.tick()

    def spawn(self, cmd):
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             cwd=str(SELLER_DIR))
        self.procs.append(p)
        return p

    def tearDown(self):
        self.done = True
        for p in self.procs:
            p.terminate()
            try:
                p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.chain.srv.shutdown()
        self.chain.srv.server_close()

    def client(self, *args, timeout=90):
        pp = str(helpers.CLIENT)  # srnet-exits on its own: no SRNet client on the path
        env = dict(os.environ, PYTHONPATH=pp, SRNET_EXITS_TESTNETS="1",
                   SRNET_EXITS_TEST_CLAIM_URL=f"http://127.0.0.1:{self.port}/claim")
        r = subprocess.run([sys.executable, "-m", "srnet_exits.cli", "--home", str(self.home), *args],
                           capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr

    def quotes(self):
        return json.loads((self.home / "state.json").read_text())["exits"]["quotes"]

    def buy(self, hours):
        rc, out, err = self.client("buy", "--seller", self.s.sesh, "--hours", str(hours), "--client-sesh", ME,
                                   "--listings", str(self.board), "--no-wait")
        self.assertEqual(rc, 0, out + err)
        rec = max(self.quotes().values(), key=lambda r: r["doc"]["quote"]["issued"])
        return rec["doc"]["quote"], out

    def test_buy_pay_admit_then_wrong_amount(self):
        q, out = self.buy(2)
        self.assertEqual((q["chainid"], q["hours"], q["client_sesh"]), (CHAIN, 2, ME))
        self.assertTrue(1_000_000 < q["amount"] <= 1_000_000 + 9999)
        self.assertIn(C.fmt_usdc(q["amount"]), out)
        self.assertIn("ethereum:", out)

        self.chain.pay("0x" + "ab" * 32, q["payout"], q["amount"])
        rc, out, err = self.client("paid", "--quote", q["id"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("You're in: exit time until ", out)
        st = json.loads((self.home / "state.json").read_text())
        lease = st["exits"]["leases"][self.s.sesh]
        self.assertAlmostEqual(lease["end"] - time.time(), 2 * 3600, delta=300)
        recs = [r for r in A.Store(self.s.store).records() if r["client_sesh"] == ME]
        self.assertEqual(len(recs), 1)

        # same transaction again for a new quote: refused (one payment buys once)
        q2, _ = self.buy(1)
        rc, out, err = self.client("paid", "--quote", q2["id"], "--tx", "0x" + "ab" * 32)
        self.assertEqual(rc, 1, out + err)

        # wrong amount: a plain "no matching payment yet"
        q3, _ = self.buy(1)
        tx = self.chain.pay("0x" + "cd" * 32, q3["payout"], q3["amount"] - 1)
        rc, out, err = self.client("paid", "--quote", q3["id"], "--tx", tx)
        self.assertEqual(rc, 1, out + err)
        self.assertIn("No matching payment yet", err)
        self.assertIn(f"Send exactly {C.fmt_usdc(q3['amount'])} USDC", err)
        self.assertEqual(len([r for r in A.Store(self.s.store).records() if r["client_sesh"] == ME]), 1)


if __name__ == "__main__":
    unittest.main()
