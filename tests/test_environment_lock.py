"""Offline invariants for dependency locks; no registry or market access."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "lock_environment.py"
SPEC = importlib.util.spec_from_file_location("environment_lock", MODULE_PATH)
lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lock)


class EnvironmentLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "pyproject.toml"
        self.manifest.write_text('[project]\nname = "example"\n')
        self.path = self.root / "requirements.lock"
        self.report = {
            "version": "1", "pip_version": "26.1.1",
            "install": [{
                "metadata": {"name": "NumPy", "version": "2.2.6"},
                "download_info": {
                    "url": "https://example.test/numpy.whl",
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                },
            }],
        }

    def write_lock(self):
        self.path.write_text(lock.render_lock(
            self.report, lock.manifest_digest(self.root), "core"))

    def test_valid_lock_and_manifest_change(self):
        self.write_lock()
        lock.check_lock(self.path, self.root, "core")
        self.manifest.write_text('[project]\nname = "changed"\n')
        with self.assertRaisesRegex(ValueError, "pyproject-sha256"):
            lock.check_lock(self.path, self.root, "core")

    def test_wrong_target_or_profile_is_rejected(self):
        self.write_lock()
        with self.assertRaisesRegex(ValueError, "profile"):
            lock.check_lock(self.path, self.root, "all")
        self.path.write_text(self.path.read_text().replace(
            json.dumps(lock.target(), sort_keys=True), '{}'))
        with self.assertRaisesRegex(ValueError, "target"):
            lock.check_lock(self.path, self.root, "core")

    def test_missing_hash_vcs_and_unknown_report_are_rejected(self):
        item = self.report["install"][0]
        item["download_info"]["archive_info"]["hashes"] = {}
        with self.assertRaisesRegex(ValueError, "sha256"):
            lock.render_lock(self.report, "digest", "core")
        item["download_info"]["vcs_info"] = {"vcs": "git"}
        with self.assertRaisesRegex(ValueError, "registry archives"):
            lock.render_lock(self.report, "digest", "core")
        self.report["version"] = "future"
        with self.assertRaisesRegex(ValueError, "report version"):
            lock.render_lock(self.report, "digest", "core")

    def test_unpinned_entry_and_empty_resolution_are_rejected(self):
        self.write_lock()
        self.path.write_text(self.path.read_text().replace("numpy==2.2.6", "numpy>=2"))
        with self.assertRaisesRegex(ValueError, "exact versions"):
            lock.check_lock(self.path, self.root, "core")
        self.report["install"] = []
        with self.assertRaisesRegex(ValueError, "Empty"):
            lock.render_lock(self.report, "digest", "core")

    def test_declared_dependency_cannot_be_missing_or_outside_version_range(self):
        self.manifest.write_text('[project]\nname = "example"\ndependencies = ["numpy>=3"]\n')
        self.write_lock()
        with self.assertRaisesRegex(ValueError, "incompatible declared dependency"):
            lock.check_lock(self.path, self.root, "core")
        self.manifest.write_text('[project]\nname = "example"\ndependencies = ["pandas>=2"]\n')
        self.write_lock()
        with self.assertRaisesRegex(ValueError, "incompatible declared dependency"):
            lock.check_lock(self.path, self.root, "core")

    def test_all_checks_self_referenced_extras(self):
        self.manifest.write_text(
            '[project]\nname = "example"\n[project.optional-dependencies]\n'
            'market = ["pandas>=2"]\nall = ["example[market]"]\n')
        self.path.write_text(lock.render_lock(
            self.report, lock.manifest_digest(self.root), "all"))
        with self.assertRaisesRegex(ValueError, "pandas"):
            lock.check_lock(self.path, self.root, "all")


if __name__ == "__main__":
    unittest.main()
