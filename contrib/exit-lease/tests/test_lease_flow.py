import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lease_flow import connect


class Fake:
    def __init__(self, map_ok=True, capable=True, commit_ok=True, reserve_ok=True):
        self.map_ok, self.capable, self.commit_ok, self.reserve_ok = map_ok, capable, commit_ok, reserve_ok
        self.log = []
        self.spent = False
        self.held = False

    def reserve(self, s):
        if not self.reserve_ok:
            raise RuntimeError("no funds")
        self.held = True; self.log.append("reserve"); return "h1"

    def map_exit(self, s):
        self.log.append("map")
        if not self.map_ok:
            raise RuntimeError("map refused")

    def wait_capable(self, s, t):
        self.log.append("capable?"); return self.capable

    def unmap_exit(self, s):
        self.log.append("unmap")

    def commit(self, h):
        self.log.append("commit")
        if not self.commit_ok:
            raise RuntimeError("settle reverted")
        self.spent = True; self.held = False

    def release(self, h):
        self.log.append("release"); self.held = False


class FlowTests(unittest.TestCase):
    def test_success_spends_only_after_map_and_capable(self):
        f = Fake()
        r = connect(f, "S")
        self.assertTrue(r.ok)
        self.assertEqual(f.log, ["reserve", "map", "capable?", "commit"])
        self.assertTrue(f.spent)

    def test_failed_map_does_not_spend(self):
        f = Fake(map_ok=False)
        r = connect(f, "S")
        self.assertFalse(r.ok)
        self.assertFalse(f.spent)
        self.assertFalse(f.held)
        self.assertNotIn("commit", f.log)
        self.assertTrue(r.reason.startswith("map_failed"))

    def test_not_admitted_does_not_spend_and_unmaps(self):
        f = Fake(capable=False)
        r = connect(f, "S")
        self.assertFalse(r.ok)
        self.assertFalse(f.spent)
        self.assertEqual(f.log[-2:], ["unmap", "release"])

    def test_commit_failure_tears_down(self):
        f = Fake(commit_ok=False)
        r = connect(f, "S")
        self.assertFalse(r.ok)
        self.assertIn("unmap", f.log)
        self.assertFalse(f.spent)

    def test_reserve_failure_touches_nothing(self):
        f = Fake(reserve_ok=False)
        r = connect(f, "S")
        self.assertFalse(r.ok)
        self.assertEqual(f.log, [])


if __name__ == "__main__":
    unittest.main()
