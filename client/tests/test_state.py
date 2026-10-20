"""Shared ~/.srnet: many writers at once lose nothing, and a stale copy (the app's payment watch,
say) never wipes a connection made meanwhile in another process."""
import json
import multiprocessing as mp
import os
import tempfile
import time
import unittest
from pathlib import Path

import helpers
from helpers import ME, SELLER
from srnet_exits import pay, state as S


def _worker(root, i, n, old_style):
    st = S.State(Path(root))
    if old_style:   # an object like an older SRNet client's State: root and data, no update()
        class Old:
            pass
        o = Old()
        o.root, o.data = Path(root), {}
        st = o
    for k in range(n):
        def fn(d, k=k):
            d["n"] = d.get("n", 0) + 1
            d.setdefault("by", {})[str(i)] = k + 1
        S.update(st, fn)


class Concurrent(unittest.TestCase):
    def test_many_processes_lose_no_update(self):
        root = tempfile.mkdtemp()
        S.State(Path(root))
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_worker, args=(root, i, 40, i % 3 == 0)) for i in range(12)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(120)
            self.assertEqual(p.exitcode, 0)
        d = json.loads((Path(root) / "state.json").read_text())
        self.assertEqual(d["n"], 12 * 40)
        self.assertEqual(d["by"], {str(i): 40 for i in range(12)})
        self.assertEqual([p.name for p in Path(root).iterdir() if p.name.endswith(".tmp")], [])

    def test_reader_never_sees_a_half_written_file(self):
        root = Path(tempfile.mkdtemp())
        S.State(root).update(lambda d: None)
        ctx = mp.get_context("spawn")
        w = ctx.Process(target=_worker, args=(str(root), 0, 300, False))
        w.start()
        reads = 0
        while w.is_alive():
            json.loads((root / "state.json").read_text())   # raises if it ever is partial
            reads += 1
        w.join()
        self.assertGreater(reads, 0)

    def test_save_is_atomic_and_private(self):
        root = Path(tempfile.mkdtemp())
        st = S.State(root)
        st.data["x"] = 1
        st.save()
        self.assertEqual(json.loads((root / "state.json").read_text())["x"], 1)
        self.assertEqual((root / "state.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((root / "state.tmp").exists())


class StaleCopy(unittest.TestCase):
    """The page's watch holds a copy for minutes; a connect in a terminal must survive it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "visibility.ack").write_text("I UNDERSTAND\n")
        self.page = S.State(self.root)               # the page's copy, read now
        q = {"id": "00112233aabbccdd", "seller": SELLER, "state": "open", "scan": {},
             "doc": {"quote": {"id": "00112233aabbccdd", "seller": SELLER, "client_sesh": ME, "chainid": 42161,
                               "amount": 1000123, "hours": 1, "expires": int(time.time()) + 900}},
             "listing": {"dns": "10.195.0.53"}}
        S.update(self.page, lambda d: pay._exd(d)["quotes"].__setitem__(q["id"], q))
        self.rec = self.page.data["exits"]["quotes"][q["id"]]
        # meanwhile, another process connects
        other = S.State(self.root)
        conn = {"seller": SELLER, "at": 1, "killswitch": True, "dns": "tunnel", "exit_dns": "10.195.0.53",
                "lease_end": 2}
        tdns = {"saved": {"tun": "sr-tun0", "dns": ["127.3.2.1"], "domains": ["~sesh"], "default_route": False},
                "at": 1, "exit_dns": "10.195.0.53", "forwarder_pid": 4242}
        other.update(lambda d: d.update(connected=conn, tunnel_dns=tdns))
        self.conn, self.tdns = conn, tdns

    def disk(self):
        return json.loads((self.root / "state.json").read_text())

    def test_watch_progress_keeps_the_connection(self):
        b = pay.Buyer(self.page, cfg={})
        self.rec["scan"]["next"] = 123
        b._put(self.rec)
        d = self.disk()
        self.assertEqual(d["connected"], self.conn)
        self.assertEqual(d["tunnel_dns"], self.tdns)
        self.assertEqual(d["exits"]["quotes"][self.rec["id"]]["scan"]["next"], 123)

    def test_admission_keeps_the_connection_and_adds_the_lease(self):
        b = pay.Buyer(self.page, cfg={})
        b._settle(self.rec, {"status": "admitted", "lease_end": 9_999_999_999})
        d = self.disk()
        self.assertEqual(d["connected"], self.conn)
        self.assertEqual(d["tunnel_dns"]["forwarder_pid"], 4242)
        self.assertEqual(d["exits"]["leases"][SELLER]["end"], 9_999_999_999)
        self.assertEqual(d["exits"]["quotes"][self.rec["id"]]["state"], "admitted")

    def test_page_paid_keeps_the_connection(self):
        from srnet_exits import ui
        S.update(self.page, lambda d: d["exits"]["quotes"][self.rec["id"]].__setitem__("tx", "0x" + "11" * 32))
        stale = S.State(self.root)
        stale.data["connected"] = None                  # pretend this copy predates the connect
        be = ui.Backend(stale)
        be._start_watch = lambda *a, **k: None
        be.paid({"id": self.rec["id"], "tx": "0x" + "22" * 32})
        d = self.disk()
        self.assertEqual(d["connected"], self.conn)
        self.assertEqual(d["exits"]["quotes"][self.rec["id"]]["state"], "open")

    def test_disconnect_from_a_stale_copy_clears_only_what_it_owns(self):
        S.update(self.page, lambda d: d.__setitem__("pending_buy", "keep me"))
        stale = S.State(self.root)
        S.update(stale, lambda d: d.__setitem__("connected", None))
        d = self.disk()
        self.assertIsNone(d["connected"])
        self.assertEqual(d["tunnel_dns"], self.tdns)
        self.assertEqual(d["pending_buy"], "keep me")
        self.assertIn(self.rec["id"], d["exits"]["quotes"])


if __name__ == "__main__":
    unittest.main()
