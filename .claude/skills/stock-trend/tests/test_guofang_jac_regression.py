"""Offline regression for the 2026-11-12 Guofang JAC classification."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "scripts"))
from analysis.wyckoff import analyze_kline_dict


class GuofangJacRegression(unittest.TestCase):
    def test_old_range_does_not_produce_current_jac(self):
        fixture = json.loads((ROOT / "fixtures" /
                              "guofang_601086_20261112_qfq.json").read_text())
        self.assertEqual(fixture["meta"]["data_source"], "tencent_a")
        self.assertEqual(fixture["data"][-1]["trade_date"], "20261112")
        result = analyze_kline_dict(fixture)
        signal = result.get("signal", {})
        self.assertFalse(
            signal.get("event") == "sos"
            and signal.get("status") == "confirmed"
            and signal.get("event_date") == "20261102",
            "a later rally above the old range must not become a new JAC",
        )


if __name__ == "__main__":
    unittest.main()
