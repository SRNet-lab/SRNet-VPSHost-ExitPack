"""claimd.py: quotes from the signed listing, open-quote limits, paid notices, HTTP surface."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from fixtures import CLIENT, OTHER, Q, T0, Seller, buyer_key, tx
import claimd as D


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.s = Seller(price="0.50", max_hours=48)
        self.clock = Clock()
        self.desk = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock)
        self.bk, self.bpub = buyer_key()

    def req(self, key=None, now=None, **kw):
        """A signed request (re-signed after kw changes, by key or the test buyer)."""
        key = key or self.bk
        r = Q.make_request(self.s.sesh, CLIENT, 4, 8453, key, now=self.clock() if now is None else now)
        r.update(kw)
        return Q.sign_request(r, key)

    def test_quote_follows_listing(self):
        code, doc = self.desk.quote(self.req())
        self.assertEqual(code, 200, doc)
        q = doc["quote"]
        self.assertEqual(q["payout"], self.s.listing["payout"])
        self.assertEqual(q["price_per_hour"], 500_000)
        self.assertTrue(2_000_000 < q["amount"] <= 2_000_000 + Q.PAY_ID_MAX)
        self.assertEqual(q["expires"], T0 + Q.QUOTE_TTL_S)
        self.assertIsNone(Q.quote_error(doc, listing=self.s.listing, req=self.req(), cap=20_000_000, now=T0))
        self.assertEqual(self.desk.quote(self.req(hours=49))[0], 400)
        self.assertEqual(self.desk.quote(self.req(chainid=1))[0], 400)

    def test_seller_cap_default_20(self):
        code, doc = self.desk.quote(self.req(hours=41))   # 20.50 + id > 20
        self.assertEqual(code, 403)
        self.assertIn("limit of 20.00", doc["error"])
        self.assertEqual(self.desk.quote(self.req(hours=39))[0], 200)

    def test_amounts_unique_among_open_quotes(self):
        amounts = set()
        for i in range(3):
            k, _ = buyer_key()
            c = [CLIENT, OTHER, "y" * 52 + ".sesh"][i]
            code, doc = self.desk.quote(self.req(client_sesh=c, key=k))
            self.assertEqual(code, 200)
            amounts.add(doc["quote"]["amount"])
        self.assertEqual(len(amounts), 3)

    def test_open_quote_limit_per_buyer_key(self):
        for _ in range(D.MAX_OPEN_PER_BUYER):
            self.assertEqual(self.desk.quote(self.req(), src="10.0.0.1")[0], 200)
        self.assertEqual(self.desk.quote(self.req(), src="10.0.0.2")[0], 429)
        self.clock.t += Q.QUOTE_TTL_S + 1
        self.assertEqual(self.desk.quote(self.req(), src="10.0.0.2")[0], 200)

    def test_naming_a_victims_address_does_not_lock_them_out(self):
        for i in range(D.MAX_OPEN_PER_SOURCE):
            k, _ = buyer_key()
            self.assertEqual(self.desk.quote(self.req(key=k), src="10.9.9.9")[0], 200)
        k, _ = buyer_key()
        self.assertEqual(self.desk.quote(self.req(key=k), src="10.9.9.9")[0], 429)  # attacker's source is full
        self.assertEqual(self.desk.quote(self.req(), src="10.0.0.1")[0], 200)              # the real buyer isn't

    def test_hourly_limits(self):
        old = D.QUOTES_PER_SOURCE, D.QUOTES_GLOBAL
        try:
            D.QUOTES_PER_SOURCE, D.QUOTES_GLOBAL = (2, 3600), (3, 3600)
            ok = lambda src: self.desk.quote(self.req(key=buyer_key()[0]), src=src)[0]  # noqa: E731
            self.assertEqual([ok("a"), ok("a"), ok("a")], [200, 200, 429])
            self.assertEqual([ok("b"), ok("c")], [200, 503])   # overall: busy
            self.clock.t += 3601
            self.assertEqual(ok("a"), 200)
        finally:
            D.QUOTES_PER_SOURCE, D.QUOTES_GLOBAL = old

    def test_overall_open_cap(self):
        old = D.MAX_OPEN
        try:
            D.MAX_OPEN = 2
            ok = lambda src: self.desk.quote(self.req(key=buyer_key()[0]), src=src)[0]  # noqa: E731
            self.assertEqual([ok("a"), ok("b"), ok("c")], [200, 200, 503])
        finally:
            D.MAX_OPEN = old

    def test_amount_never_reused_within_7_days_even_when_paid(self):
        self.desk.rng = lambda n: 41                       # every quote wants pay id 42
        amounts = []
        for i in range(3):
            code, doc = self.desk.quote(self.req(key=buyer_key()[0]), src=f"s{i}")
            self.assertEqual(code, 200)
            q = doc["quote"]
            amounts.append(q["amount"])
            (self.s.state / "status" / f"{q['id']}.json").write_text(json.dumps({"status": "admitted"}))
            self.clock.t += 2 * 86400                      # long expired, and paid
        self.assertEqual(amounts[0], 2_000_042)
        self.assertEqual(len(set(amounts)), 3)
        self.clock.t = T0 + Q.QUOTE_TTL_S + Q.RESERVE_S + 1   # the first one's reservation is over
        _, doc = self.desk.quote(self.req(key=buyer_key()[0]), src="s9")
        self.assertEqual(doc["quote"]["amount"], 2_000_042)

    def test_amounts_tracked_in_full_across_price_changes(self):
        self.desk.rng = lambda n: 4999
        _, doc = self.desk.quote(self.req(hours=2))          # 0.50 * 2 + 0.005000 = 1.005000
        self.assertEqual(doc["quote"]["amount"], 1_005_000)
        self.s.relist(price="0.501")                         # 2h = 1.002 + id: overlaps the first range
        self.desk._listing = None
        self.desk.rng = lambda n: 2999                       # 1.002000 + 0.003000 = 1.005000 again
        _, doc = self.desk.quote(self.req(hours=2, key=buyer_key()[0]), src="x")
        self.assertNotEqual(doc["quote"]["amount"], 1_005_000)

    def test_too_many_txs_per_quote(self):
        _, doc = self.desk.quote(self.req())
        q = doc["quote"]
        for n in range(D.MAX_TX_PER_QUOTE):
            self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(n + 1)))[0], 202)
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(1)))[0], 202)      # repeats are fine
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(99)))[0], 429)

    def test_prune_and_index(self):
        _, doc = self.desk.quote(self.req())
        qid = doc["quote"]["id"]
        (self.s.state / "status" / f"{qid}.json").write_text("{}")
        self.clock.t += Q.KEEP_QUOTES_S - 10
        self.assertEqual(self.desk.prune(), 0)
        self.assertIsNotNone(self.desk.load_quote(qid))
        self.clock.t += 20
        self.assertEqual(self.desk.prune(), 1)
        self.assertIsNone(self.desk.load_quote(qid))
        self.assertFalse((self.s.state / "status" / f"{qid}.json").exists())
        self.assertEqual(self.desk._count("SELECT COUNT(*) FROM quotes"), 0)

    def test_index_backfills_old_quote_files(self):
        _, doc = self.desk.quote(self.req())
        (self.s.state / "index.sqlite").unlink()
        for x in ("-wal", "-shm"):
            (self.s.state / ("index.sqlite" + x)).unlink(missing_ok=True)
        d2 = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock)
        self.assertEqual(d2._count("SELECT COUNT(*) FROM quotes WHERE amount = ?", doc["quote"]["amount"]), 1)

    def test_quote_files_are_fsynced(self):
        from unittest import mock
        calls = []
        real = D.os.fsync
        with mock.patch.object(D.os, "fsync", side_effect=lambda fd: (calls.append(fd), real(fd))):
            self.assertEqual(self.desk.quote(self.req())[0], 200)
        self.assertGreaterEqual(len(calls), 1)

    def test_paid_notice(self):
        _, doc = self.desk.quote(self.req())
        q = doc["quote"]
        self.assertEqual(self.desk.status(q["id"]), (200, {"status": "waiting"}))
        thief, _ = buyer_key()
        self.assertEqual(self.desk.paid(Q.make_paid(thief, q, tx(1)))[0], 403)
        self.assertEqual(self.desk.paid({**Q.make_paid(self.bk, q, tx(1)), "quote_id": "0" * 16})[0], 404)
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(1))), (202, {"status": "received"}))
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(1)))[0], 202)
        self.assertEqual(len(list((self.s.state / "spool").glob("*.json"))), 1)
        self.assertEqual(self.desk.status(q["id"])[1]["status"], "waiting")
        # a notice that arrives long after expiry is still taken (the block time decides)
        self.clock.t = q["expires"] + 3 * 86400
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(2)))[0], 202)
        self.clock.t = q["issued"] + D.KEEP_QUOTES_S + 1
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(3)))[0], 410)

    def test_unreported_quote_shows_expired(self):
        _, doc = self.desk.quote(self.req())
        q = doc["quote"]
        self.clock.t = q["expires"] + D.PAID_GRACE_S + 1
        self.assertEqual(self.desk.status(q["id"])[1]["status"], "expired")
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q, tx(1)))[0], 202)
        self.assertEqual(self.desk.status(q["id"])[1]["status"], "waiting")

    def test_listing_must_match_quote_key(self):
        other = Seller()
        desk = D.Desk(self.s.state, other.quote_key, self.s.listing_path)
        with self.assertRaises(RuntimeError):
            desk.quote(self.req())


    # ------------------------------------------------------------ signed requests (R2-2)
    def test_requests_in_someone_elses_name_are_refused(self):
        _, victim = buyer_key()
        thief, _ = buyer_key()
        forged = Q.sign_request({**self.req(), "buyer_key": victim}, thief)
        code, doc = self.desk.quote(forged)
        self.assertEqual((code, "not signed" in doc["error"]), (400, True))
        self.assertEqual(self.desk._count("SELECT COUNT(*) FROM quotes"), 0)

    def test_a_seen_request_cant_be_replayed_to_use_up_the_buyers_limit(self):
        seen = self.req()                     # e.g. kept by an earlier seller or a relay
        self.assertEqual(self.desk.quote(seen, src="10.0.0.1")[0], 200)
        for src in ("10.0.0.7", "10.0.0.8", "10.0.0.9"):
            code, doc = self.desk.quote(seen, src=src)
            self.assertEqual(code, 409, doc)
        self.assertEqual(self.desk._count("SELECT COUNT(*) FROM quotes WHERE buyer_key = ?", self.bpub), 1)
        # a request for another seller doesn't count here at all
        other = Seller(price="0.50")
        self.assertEqual(self.desk.quote(Q.make_request(other.sesh, CLIENT, 1, 8453, self.bk, now=T0))[0], 400)
        # an old request is refused by time, and the remembered signatures are pruned
        self.assertEqual(self.desk.quote(self.req(now=T0 - Q.REQUEST_SKEW_S - 1))[0], 400)
        self.clock.t += D.REPLAY_KEEP_S + 1
        self.desk.quote(self.req())
        self.assertEqual(self.desk._count("SELECT COUNT(*) FROM requests WHERE ts <= ?", T0), 0)

    def test_open_limit_per_buyer_is_five(self):
        self.assertEqual(D.MAX_OPEN_PER_BUYER, 5)

    def test_global_quote_limit_default_150_and_configurable(self):
        self.assertEqual(D.QUOTES_GLOBAL, (150, 3600))
        # the default can't fill the smallest amount pool inside the 7-day hold
        self.assertLess(D.QUOTES_GLOBAL[0] * (Q.RESERVE_S + Q.MAX_QUOTE_TTL_S) / 3600,
                        Q.POOL_BUSY * Q.extra_cap(D.L.MIN_PRICE_UNITS))     # cheapest listing, 1 hour
        self.assertGreaterEqual(D.SAFE_QUOTES_PER_HOUR, D.QUOTES_GLOBAL[0])
        d = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock, quotes_per_hour=2)
        self.assertEqual(d.quote(self.req(key=buyer_key()[0]), src="a")[0], 200)
        self.assertEqual(d.quote(self.req(key=buyer_key()[0]), src="b")[0], 200)
        code, doc = d.quote(self.req(key=buyer_key()[0]), src="c")
        self.assertEqual(code, 503)
        self.assertIn("busy", doc["error"])
        self.clock.t += 3601
        self.assertEqual(d.quote(self.req(key=buyer_key()[0]), src="d")[0], 200)

    def test_quotes_per_hour_flag(self):
        import contextlib
        import io
        from unittest import mock
        seen = {}

        class Stop(Exception):
            pass

        def fake_desk(*a, **kw):
            seen.update(kw)
            raise Stop
        base = ["--host", "127.0.0.1", "--dir", str(self.s.state), "--quote-key", str(self.s.quote_key),
                "--listing", str(self.s.listing_path)]
        with mock.patch.object(D, "Desk", fake_desk):
            with self.assertRaises(Stop):
                D.main(base)
            self.assertEqual(seen["quotes_per_hour"], 150)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(Stop):
                D.main(base + ["--quotes-per-hour", "400"])
            self.assertEqual(seen["quotes_per_hour"], 400)
            self.assertIn("over 267", err.getvalue())
            with self.assertRaises(SystemExit):
                D.main(base + ["--quotes-per-hour", "0"])

    # ------------------------------------------------------------ damaged index (R2-4)
    def test_damaged_index_is_moved_aside_and_rebuilt_with_reports(self):
        ids = []
        for i in range(3):
            _, doc = self.desk.quote(self.req(key=buyer_key()[0] if i else None), src=f"s{i}")
            ids.append(doc["quote"])
        bk2 = buyer_key()[0]
        q_pending, q_done, q_rejected = ids     # spool files for the last two are written directly below
        self.assertEqual(self.desk.paid(Q.make_paid(self.bk, q_pending, tx(1)))[0], 202)
        (self.s.state / "spool" / "done").mkdir(exist_ok=True)
        (self.s.state / "spool" / "rejected").mkdir(exist_ok=True)
        (self.s.state / "spool" / "done" / f"{q_done['id']}-a.json").write_text(
            json.dumps({"v": 1, "quote_id": q_done["id"], "tx": tx(2), "sig": "00"}))
        for n in (3, 4):
            (self.s.state / "spool" / "rejected" / f"{q_rejected['id']}-{n}.json").write_text(
                json.dumps({"v": 1, "quote_id": q_rejected["id"], "tx": tx(n), "sig": "00"}))
        self.desk.db.close()
        for x in ("-wal", "-shm"):
            (self.s.state / ("index.sqlite" + x)).unlink(missing_ok=True)
        (self.s.state / "index.sqlite").write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
        self.clock.t += Q.QUOTE_TTL_S + D.PAID_GRACE_S + 1
        d2 = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock)
        self.assertTrue(list(self.s.state.glob("index.sqlite.bad-*")))
        self.assertEqual(d2._count("SELECT COUNT(*) FROM quotes"), 3)
        self.assertEqual(d2._count("SELECT COUNT(*) FROM notices"), 4)
        self.assertEqual(d2.status(q_pending["id"]), (200, {"status": "waiting"}))   # still reported, not expired
        self.assertEqual(d2._count("SELECT COUNT(*) FROM notices WHERE qid = ?", q_rejected["id"]), 2)
        # amounts stay reserved after the rebuild
        taken = {r[0] for r in d2.db.execute("SELECT amount FROM quotes")}
        self.assertEqual(taken, {q["amount"] for q in ids})
        self.assertEqual(d2.quote(self.req(key=bk2))[0], 200)

    def test_held_reports_survive_a_rebuild_and_expire_with_retention(self):
        import os
        _, doc = self.desk.quote(self.req())
        q = doc["quote"]
        held = self.s.state / "spool" / "held"
        held.mkdir(parents=True, exist_ok=True)
        for n in (1, 2):
            (held / f"{q['id']}-{n}.json").write_text(json.dumps({"v": 1, "quote_id": q["id"], "tx": tx(n), "sig": "00"}))
        self.desk.db.close()
        for x in ("", "-wal", "-shm"):
            (self.s.state / ("index.sqlite" + x)).unlink(missing_ok=True)
        d2 = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock)
        self.assertEqual(d2._count("SELECT COUNT(*) FROM notices WHERE qid = ?", q["id"]), 2)   # tx count kept
        old = held / f"{q['id']}-1.json"
        t = old.stat().st_mtime - Q.KEEP_QUOTES_S - 60
        os.utime(old, (t, t))
        d2.prune()
        self.assertFalse(old.exists())
        self.assertTrue((held / f"{q['id']}-2.json").exists())

    def test_garbage_index_file_is_replaced(self):
        self.desk.db.close()
        (self.s.state / "index.sqlite").write_bytes(b"not a database at all" * 50)
        d2 = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock)
        self.assertEqual(d2.quote(self.req())[0], 200)

    # ------------------------------------------------------------ source tags (debug, off by default)
    def test_source_tags_logged_only_with_debug(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.desk.quote(self.req(), src="10.0.0.1")
        self.assertNotIn("source", err.getvalue())
        d = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path, clock=self.clock, debug_sources=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d.quote(self.req(key=buyer_key()[0]), src="10.0.0.1")
            d.quote(self.req(key=buyer_key()[0]), src="10.0.0.2")
        tags = [l.split()[-1] for l in err.getvalue().splitlines() if " source " in l]
        self.assertEqual(len(tags), 2)
        self.assertNotEqual(tags[0], tags[1])
        self.assertNotIn("10.0.0", err.getvalue())


class HttpTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        # The desk calls os._exit(1) on an index error, from a server thread. Keep the real one
        # out of reach for the server's whole lifetime so it can never end the test run.
        self.exits = []
        self._exit_patch = mock.patch.object(D.os, "_exit", side_effect=self.exits.append)
        self._exit_patch.start()
        self.s = Seller()
        self.desk = D.Desk(self.s.state, self.s.quote_key, self.s.listing_path)
        self.srv = D.serve("127.0.0.1", 0, self.desk)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._exit_patch.stop()

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data), timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_error_while_running_exits_for_a_restart(self):
        import io
        import sqlite3
        from unittest import mock
        bk, _ = buyer_key()
        import time
        err = io.StringIO()
        boom = mock.Mock(side_effect=sqlite3.DatabaseError("database disk image is malformed"))
        with mock.patch.object(D.sys, "stderr", err), \
                mock.patch.object(self.desk, "quote", boom), mock.patch.object(self.desk, "status", boom):
            code, doc = self.call("/claim/quote", Q.make_request(self.s.sesh, CLIENT, 1, 42161, bk))
            self.assertEqual(code, 503)
            self.assertEqual(self.call("/claim/status/" + "ab" * 8)[0], 503)
            for _ in range(250):              # the server thread exits after it has answered
                if len(self.exits) == 2:
                    break
                time.sleep(0.02)
        self.assertEqual(self.exits, [1, 1])
        self.assertIn("index error, exiting", err.getvalue())

    def test_flow(self):
        bk, bpub = buyer_key()
        req = Q.make_request(self.s.sesh, CLIENT, 1, 42161, bk)
        code, doc = self.call("/claim/quote", req)
        self.assertEqual(code, 200)
        qid = doc["quote"]["id"]
        self.assertEqual(self.call(f"/claim/status/{qid}"), (200, {"status": "waiting"}))
        self.assertEqual(self.call("/claim/paid", Q.make_paid(bk, doc["quote"], tx(9)))[0], 202)
        self.assertEqual(self.call("/claim", {"v": 1})[0], 410)
        self.assertEqual(self.call("/claim/status/zz")[0], 404)
        self.assertEqual(self.call("/claim/quote", {"pad": "x" * 3000})[0], 413)

    def test_refuses_public_bind(self):
        with self.assertRaises(SystemExit):
            D.serve("8.8.8.8", 0, self.desk)
        self.assertTrue(D.bind_ok("fd2e:7365:736a::1"))
        self.assertFalse(D.bind_ok("0.0.0.0"))

    def test_rate_limit(self):
        lim = D.Limiter(per_source=(2, 60), overall=(100, 60))
        self.assertTrue(lim.allow("a", 0) and lim.allow("a", 1))
        self.assertFalse(lim.allow("a", 2))
        self.assertTrue(lim.allow("b", 2))

    def test_source_flood_does_not_reset_the_global_limit(self):
        lim = D.Limiter(per_source=(100, 60), overall=(50, 60), max_sources=20)
        for i in range(50):
            self.assertTrue(lim.allow(f"s{i}", 1))
        self.assertFalse(lim.allow("new", 2))         # global window survives the eviction
        self.assertLessEqual(len(lim.hits), 21)
        self.assertTrue(lim.allow("new", 62))

    def test_socket_timeout_and_connection_cap(self):
        import socket
        self.assertEqual(D.make_handler(self.desk, D.Limiter()).timeout, D.SOCKET_TIMEOUT_S)
        self.srv.max_per_source = 2
        idle = [socket.create_connection(("127.0.0.1", self.srv.server_address[1])) for _ in range(2)]
        try:
            import time
            time.sleep(0.2)
            s3 = socket.create_connection(("127.0.0.1", self.srv.server_address[1]))
            s3.settimeout(3)
            self.assertEqual(s3.recv(10), b"")          # closed straight away
            s3.close()
        finally:
            for s in idle:
                s.close()
        import time
        time.sleep(0.3)
        self.assertEqual(self.call(f"/claim/status/{'0' * 16}")[0], 404)


if __name__ == "__main__":
    unittest.main()
