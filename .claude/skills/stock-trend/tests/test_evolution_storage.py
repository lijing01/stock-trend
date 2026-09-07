import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analysis.recommendation_diagnostics import load_primary_research_snapshots
from core.candidate_research_snapshot import build_research_snapshot
from core.evolution_storage import input_manifest, load_research_snapshot, storage_root


class T(unittest.TestCase):
    def test_storage_areas_are_scoped_under_evolution_root(self):
        # The combined suite may redirect CACHE_DIR; the storage contract only
        # owns the evolution namespace below whichever cache root is active.
        self.assertTrue(str(storage_root("research")).endswith("evolution/research"))
        with self.assertRaisesRegex(ValueError, "unknown_evolution"):
            storage_root("recommendation_history")

    def test_new_research_embeds_reproducible_credential_free_input_manifest(self):
        snapshot = build_research_snapshot(
            [{"code": "600000", "composite_score": 80}], {}, "2026-09-07", {},
            {"score": 80}, ["BK0001"], 50,
        )
        manifest = snapshot["content"]["input_manifest"]
        self.assertEqual(manifest["archive_mode"], "embedded_or_immutable_reference")
        self.assertTrue(manifest["input_sha256"])
        self.assertEqual(manifest["credential_policy"], "no_model_credentials_stored")
        self.assertEqual(manifest["inputs"]["candidate_records"], snapshot["content"]["records"])

    def test_legacy_unwrapped_sample_loads_read_only_without_rewrite(self):
        legacy = {"recommendation_date": "2026-08-01", "records": [{"code": "600000"}]}
        loaded = load_research_snapshot(legacy)
        self.assertEqual(loaded["compatibility"]["status"], "legacy_read_only")
        self.assertEqual(loaded["content"]["records"][0]["code"], "600000")
        self.assertEqual(legacy.get("schema_version"), None)

    def test_primary_loader_accepts_legacy_v1_envelope(self):
        legacy = {"schema_version": "candidate-research-snapshot/v0", "content": {
            "recommendation_date": "2026-08-01", "records": [],
        }}
        with tempfile.TemporaryDirectory() as root:
            formal = Path(root) / "2026-08-01" / "formal"
            formal.mkdir(parents=True)
            (formal / "primary.json").write_text("legacy", encoding="utf-8")
            (formal / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
            loaded = load_primary_research_snapshots(root)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["compatibility"]["source_schema_version"], "candidate-research-snapshot/v0")

    def test_manifest_is_content_addressed(self):
        self.assertEqual(input_manifest(a={"x": 1})["input_sha256"], input_manifest(a={"x": 1})["input_sha256"])


def run_evolution_storage_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
    return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)


if __name__ == "__main__":
    unittest.main()
