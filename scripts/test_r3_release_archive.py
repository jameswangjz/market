"""Offline archive contract and fail-closed regression tests."""

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import r3_release_archive as archive


REPO = Path(__file__).resolve().parents[1]
PROOFS = {
    "saas": "32-saas-refund-live-20261016010230.json",
    "native": "35-native-release-final-20261016011200.json",
    "file": "36-file-refund-pg-20261016011200.json",
    "monthly": "37-monthly-final-20261016011210.json",
    "review": "38-refund-review-20261016011530.json",
}


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.committed = archive.committed_sources(REPO)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {k: k + ".json" for k in archive.KINDS}
        self.raw = {k: (REPO / "docs/evidence" / p).read_bytes() for k, p in PROOFS.items()}
        self.sources = self.committed
        self.snapshot = {
            "status": "passed", "captured_at": "2026-10-16T00:20:00+00:00",
            "namespace": "market", "deployed_commit": archive.COMMIT,
            "image_tag": archive.TAG, "health": {"console": 200, "api": 200},
            "source_sha256": self.sources,
            "deployments": [{"name": name, "image": "market/app:" + archive.TAG,
                             "desired": 1, "ready": 1, "updated": 1, "available": 1,
                             "generation": 2, "observed_generation": 2}
                            for name in sorted(archive.DEPLOYMENTS)],
            "backup": {"path": "/private/not-to-be-opened.json", "sha256": "a" * 64,
                       "size_bytes": 123, "scope": "current_readonly_snapshot"},
        }
        self.write()
        self.mock_sources = patch.object(archive, "committed_sources", return_value=self.sources)
        self.mock_sources.start()
        self.addCleanup(self.mock_sources.stop)

    def write(self):
        for kind, raw in self.raw.items():
            (self.root / self.paths[kind]).write_bytes(raw)
        (self.root / self.paths["snapshot"]).write_text(json.dumps(self.snapshot))

    def run_archive(self):
        return archive.archive(self.root, self.paths, "output")

    def reject(self):
        with self.assertRaises((archive.ArchiveError, OSError)):
            self.run_archive()
        self.assertFalse((self.root / "output").exists())

    def finding(self):
        self.snapshot.update(status="passed_with_findings",
                             blockers=["api_charge_source_continuity_lost"],
                             affected_worker="api-charge-ledger-worker")
        for deployment in self.snapshot["deployments"]:
            if deployment["name"] == "api-charge-ledger-worker":
                deployment.update(ready=0, available=0)
        self.write()

    def test_actual_proofs_and_committed_sources(self):
        result = self.run_archive()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["release_ready"])
        self.assertFalse(result["all_business_release_passed"])
        self.assertFalse(result["task_states_changed"])
        self.assertEqual(list((self.root / "output").iterdir()), [self.root / "output/manifest.json"])
        manifest = (self.root / "output/manifest.json").read_text()
        self.assertNotIn("/private/", manifest)
        self.assertNotIn("/tmp/", manifest)
        for kind, raw in self.raw.items():
            self.assertEqual(result["evidence"][kind]["sha256"], hashlib.sha256(raw).hexdigest())

    def test_declared_ledger_failure_preserved(self):
        self.finding()
        result = self.run_archive()
        self.assertEqual(result["status"], "passed_with_findings")
        self.assertFalse(result["release_ready"])
        self.assertEqual(result["blockers"], ["api_charge_source_continuity_lost"])
        self.assertIn("explicit approval", result["remaining_condition"])

    def test_other_readiness_failure_not_excused(self):
        self.finding()
        next(d for d in self.snapshot["deployments"] if d["name"] == "market-api")["ready"] = 0
        self.write()
        self.reject()

    def test_missing_or_false_blocker_rejected(self):
        self.finding()
        self.snapshot["blockers"] = []
        self.write()
        self.reject()

    def test_snapshot_contract_failures(self):
        original = copy.deepcopy(self.snapshot)
        for field, value in (("health", {"console": 200, "api": 503}),
                             ("deployed_commit", "8ac01c9"), ("image_tag", "old"),
                             ("captured_at", "2026-10-16T00:00:00"),
                             ("source_sha256", {}), ("deployments", [])):
            with self.subTest(field=field):
                self.snapshot = copy.deepcopy(original)
                self.snapshot[field] = value
                self.write()
                self.reject()

    def test_source_mismatch(self):
        self.snapshot["source_sha256"] = {**self.sources, "main.py": "0" * 64}
        self.write()
        self.reject()

    def test_proof_status_and_review_sha_mismatch(self):
        for raw in (self.raw["native"].replace(b'"passed"', b'"failed"', 1),
                    self.raw["native"] + b"\n"):
            with self.subTest(raw=raw[-10:]):
                (self.root / "native.json").write_bytes(raw)
                self.reject()

    def test_review_bundle_mismatch(self):
        review = json.loads(self.raw["review"])
        review["evidence_bundle_sha256"] = "0" * 64
        (self.root / "review.json").write_text(json.dumps(review))
        self.reject()

    def test_secret_and_ambiguous_json(self):
        for raw in (b'{"status":"passed","password":"do-not-print"}',
                    b'{"status":"passed","note":"Bearer do-not-print"}',
                    b'{"status":"passed","status":"passed"}',
                    b'{"status":"passed","n":NaN}'):
            with self.subTest(raw=raw):
                (self.root / "native.json").write_bytes(raw)
                self.reject()

    def test_symlink_hardlink_and_parent_symlink(self):
        target = self.root / "native.json"
        target.unlink()
        target.symlink_to(self.root / "saas.json")
        self.reject()
        target.unlink()
        target.hardlink_to(self.root / "saas.json")
        self.reject()
        target.unlink()
        target.write_bytes(self.raw["native"])
        (self.root / "linked").symlink_to(self.root, target_is_directory=True)
        self.paths["native"] = "linked/native.json"
        self.reject()

    def test_traversal_and_private_paths(self):
        for path in ("../native.json", "/tmp/native.json", "private/native.json",
                     "a/../native.json", "a\\native.json", "./native.json"):
            with self.subTest(path=path):
                self.paths["native"] = path
                self.reject()

    def test_existing_output_not_overwritten(self):
        (self.root / "output").mkdir()
        sentinel = self.root / "output/keep"
        sentinel.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.run_archive()
        self.assertEqual(sentinel.read_text(), "keep")

    def test_mutation_between_validation_and_output(self):
        original = archive.validate
        def mutate(*args):
            original(*args)
            (self.root / "native.json").write_bytes(self.raw["native"] + b"\n")
        with patch.object(archive, "validate", side_effect=mutate):
            self.reject()

    def test_cli_fixed_diagnostic_no_secret(self):
        (self.root / "native.json").write_bytes(b'{"password":"do-not-print"}')
        argv = ["--root", str(self.root), "--output-dir", "output"]
        for kind, path in self.paths.items():
            argv.extend(["--" + kind, path])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(archive.main(argv), 1)
        self.assertNotIn("do-not-print", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
