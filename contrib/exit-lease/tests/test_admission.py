import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admission as A

SELLER = "sellerfixture000000000000000000000000000000000001.sesh"
STRANGER = "clientfixture00000000000000000000000000000000001.sesh"
OTHER = "clientfixture00000000000000000000000000000000002.sesh"


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.key = Path(self.d.name) / "issuer.key"
        self.pub = A.keygen(self.key)
        self.store = Path(self.d.name) / "admissions.json"

    def tearDown(self):
        self.d.cleanup()

    def test_keyfile_private_and_no_overwrite(self):
        self.assertEqual(os.stat(self.key).st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            A.keygen(self.key)

    def test_issue_and_verify(self):
        rec = A.issue(self.key, self.store, SELLER, STRANGER, 3600, tid="t1")
        self.assertEqual(A.verify_record(rec, self.pub, SELLER, STRANGER), "admission")
        self.assertEqual(A.verify_record(rec, self.pub, SELLER, OTHER), "wrong_client")
        self.assertEqual(A.verify_record(rec, self.pub, OTHER, STRANGER), "wrong_seller")
        self.assertEqual(A.verify_record(rec, self.pub, SELLER, STRANGER, now=int(time.time()) + 7200), "expired")
        t = dict(rec, exp=rec["exp"] + 10)
        self.assertEqual(A.verify_record(t, self.pub, SELLER, STRANGER), "bad_sig")

    def test_duplicate_tid_rejected_revoke_prune(self):
        A.issue(self.key, self.store, SELLER, STRANGER, 3600, tid="t1")
        with self.assertRaises(KeyError):
            A.issue(self.key, self.store, SELLER, STRANGER, 3600, tid="t1")
        A.issue(self.key, self.store, SELLER, OTHER, -5, tid="old")
        self.assertEqual(A.Store(self.store).prune(), 1)
        self.assertTrue(A.Store(self.store).revoke("t1"))
        self.assertEqual(A.Store(self.store).records(), [])

    def test_cpp_verifier_accepts_python_records(self):
        exe = os.environ.get("EXIT_LEASE_VERIFY")
        if not exe:
            self.skipTest("EXIT_LEASE_VERIFY not set")
        A.issue(self.key, self.store, SELLER, STRANGER, 3600, tid="x")
        ok = subprocess.run([exe, str(self.store), self.pub, SELLER, STRANGER], capture_output=True, text=True)
        self.assertEqual(ok.stdout.strip(), "admission")
        self.assertEqual(ok.returncode, 0)
        bad = subprocess.run([exe, str(self.store), self.pub, SELLER, OTHER], capture_output=True, text=True)
        self.assertEqual(bad.stdout.strip(), "wrong_client")
        self.assertNotEqual(bad.returncode, 0)
        # Tamper on disk -> C++ says bad_sig.
        data = json.loads(self.store.read_text())
        data["admissions"]["x"]["exp"] += 100
        self.store.write_text(json.dumps(data))
        t = subprocess.run([exe, str(self.store), self.pub, SELLER, STRANGER], capture_output=True, text=True)
        self.assertEqual(t.stdout.strip(), "bad_sig")


if __name__ == "__main__":
    unittest.main()
