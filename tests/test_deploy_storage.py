"""assay2chip/hosting: SigV4, the object stores, signed URLs, workspace sync. Offline.

    python -m unittest tests.test_deploy_storage -v
"""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assay2chip.hosting import sigv4  # noqa: E402
from assay2chip.hosting.storage import LocalStore, StoreError, UrlSigner  # noqa: E402
from assay2chip.hosting.workspace import Workspaces, is_bulk  # noqa: E402

# The worked examples in the AWS S3 documentation ("Signature Version 4: examples").
AK = "AKIAIOSFODNN7EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
WHEN = datetime(2013, 5, 24, tzinfo=timezone.utc)
HOST = "examplebucket.s3.amazonaws.com"


class SigV4Test(unittest.TestCase):
    def test_header_signature_matches_aws_example(self):
        h = sigv4.sign_headers("GET", HOST, "/test.txt", region="us-east-1", access_key=AK,
                               secret_key=SK, headers={"Range": "bytes=0-9"}, now=WHEN)
        self.assertTrue(h["Authorization"].endswith(
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))

    def test_presigned_url_matches_aws_example(self):
        q = sigv4.presign("GET", HOST, "/test.txt", region="us-east-1", access_key=AK,
                          secret_key=SK, expires_s=86400, now=WHEN)
        self.assertIn("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157"
                      "751f604d404", q)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = LocalStore(Path(tempfile.mkdtemp(prefix="a2c_s_")))

    def test_round_trip_and_list(self):
        self.store.put_bytes("runs/a/x.json", b"{}")
        self.assertEqual(self.store.get_bytes("runs/a/x.json"), b"{}")
        self.assertEqual([o.key for o in self.store.list("runs/a/")], ["runs/a/x.json"])
        self.assertTrue(self.store.exists("runs/a/x.json"))

    def test_keys_cannot_escape(self):
        for bad in ("../x", "/etc/passwd", "runs/../../x", "a\\b", ""):
            with self.assertRaises(StoreError):
                self.store.put_bytes(bad, b"x")

    def test_signed_urls(self):
        signer = UrlSigner(b"0" * 32)
        s = signer.sign("runs/a/x.json", 60, now=1000)
        self.assertTrue(signer.verify("runs/a/x.json", s["exp"], s["sig"], now=1000))
        self.assertFalse(signer.verify("runs/a/y.json", s["exp"], s["sig"], now=1000))
        self.assertFalse(signer.verify("runs/a/x.json", s["exp"] + 1, s["sig"], now=1000))
        self.assertFalse(signer.verify("runs/a/x.json", s["exp"], s["sig"], now=2000),
                         "expired")
        self.assertFalse(signer.verify("../x", s["exp"], s["sig"], now=1000))


class WorkspaceTest(unittest.TestCase):
    def test_bulk_rules(self):
        self.assertTrue(is_bulk("assay2chip/work/r/foam/jobs/c/step_1/foam/c/all/constant/"
                                "polyMesh/points"))
        self.assertTrue(is_bulk("assay2chip/work/r/foam/jobs/c/step_1/foam/c/all/0.5/U"))
        self.assertTrue(is_bulk("assay2chip/work/r/foam/c/all/processor0/U"))
        self.assertFalse(is_bulk("assay2chip/work/r/foam/render/c.png"))
        self.assertFalse(is_bulk("assay2chip/work/r/report.md"))

    def test_copy_excludes_and_sync_uploads_only_changes(self):
        src = Path(tempfile.mkdtemp(prefix="a2c_src_"))
        (src / "chip").mkdir()
        (src / "chip" / "hand_made.json").write_text("{}")
        (src / ".venv").mkdir()
        (src / ".venv" / "big").write_text("x")
        (src / "private").mkdir()
        (src / "private" / "secret.md").write_text("x")
        (src / ".env").write_text("KEY=x")
        (src / "assay2chip" / "work" / "old").mkdir(parents=True)
        (src / "assay2chip" / "work" / "old" / "raw.json").write_text("{}")
        ws = Workspaces(Path(tempfile.mkdtemp(prefix="a2c_ws_")), src)
        root = ws.ensure("r")
        for gone in (".venv", "private", ".env", "assay2chip/work/old"):
            self.assertFalse((root / gone).exists(), gone)
        self.assertTrue((root / "chip" / "hand_made.json").exists())
        store = LocalStore(Path(tempfile.mkdtemp(prefix="a2c_st_")))
        self.assertEqual(ws.sync("r", store), [], "nothing new yet")
        (root / "chip" / "r_c1.json").write_text("{}")
        (root / "assay2chip/work/r/foam/c/all/constant/polyMesh").mkdir(parents=True)
        (root / "assay2chip/work/r/foam/c/all/constant/polyMesh/points").write_text("()")
        records = ws.sync("r", store)
        self.assertEqual([r["path"] for r in records], ["chip/r_c1.json"])
        self.assertEqual(ws.sync("r", store), [], "already synced")


if __name__ == "__main__":
    unittest.main()
