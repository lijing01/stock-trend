"""Static import boundaries for the incremental package migration."""

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = SKILL_ROOT / "stock_trend"
SCRIPTS_ROOT = SKILL_ROOT / "scripts"


def imports_for(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class ArchitectureTests(unittest.TestCase):
    def test_domain_has_no_upward_dependencies(self):
        forbidden = ("stock_trend.providers", "stock_trend.services",
                     "stock_trend.reporting", "scripts", "scans", "backtesting",
                     "reporting", "providers", "fetchers", "core", "analysis")
        for path in PACKAGE_ROOT.joinpath("domain").rglob("*.py"):
            with self.subTest(path=path):
                self.assertFalse(any(name == prefix or name.startswith(prefix + ".")
                                     for name in imports_for(path) for prefix in forbidden))

    def test_analytics_has_no_cli_or_reporting_dependencies(self):
        forbidden = ("scripts", "scans", "reporting", "stock_trend.reporting")
        for path in PACKAGE_ROOT.joinpath("analytics").rglob("*.py"):
            with self.subTest(path=path):
                self.assertFalse(any(name == prefix or name.startswith(prefix + ".")
                                     for name in imports_for(path) for prefix in forbidden))

    def test_backtesting_does_not_import_scan_workflows(self):
        for path in SCRIPTS_ROOT.joinpath("backtesting").glob("*.py"):
            with self.subTest(path=path):
                imports = imports_for(path)
                self.assertFalse(any(name == "scans" or name.startswith("scans.")
                                     for name in imports))

    def test_sys_path_insert_allowlist_does_not_grow(self):
        allowlist = {
            str(path.relative_to(SCRIPTS_ROOT))
            for path in SCRIPTS_ROOT.rglob("*.py")
            if path.is_file() and "stock_trend" not in path.parts
            and "sys.path.insert" in path.read_text(encoding="utf-8")
        }
        expected = {
            "analysis/evolution_job.py", "analysis/evolution_proposals.py", "analysis/market_regime.py",
            "analysis/market_style.py", "analysis/market_theme.py", "analysis/recommendation_attribution.py",
            "analysis/recommendation_diagnostics.py", "analysis/scores.py", "analysis/sector_snapshot_job.py",
            "analysis/technical.py", "backtesting/engine.py", "backtesting/recommendation_experiments.py",
            "backtesting/wyckoff_backtest.py", "bridge/run_today.py", "bridge/today_background.py",
            "core/base_fetcher.py", "core/cache_utils.py", "core/resolve_code.py", "fetchers/capital_flow.py",
            "fetchers/etf_data.py", "fetchers/fundamental.py", "fetchers/futures_data.py",
            "fetchers/index_valuation.py", "fetchers/kline.py", "fetchers/kline_eastmoney.py",
            "fetchers/macro_snapshot.py", "fetchers/sector_data.py", "fetchers/sector_kline.py",
            "fetchers/sector_mapper.py", "pipeline/runner.py", "portfolio/manager.py",
            "reporting/report.py", "scans/daily_candidates.py", "scans/etf_scanner.py", "scans/stock_scanner.py",
        }
        self.assertEqual(allowlist, expected)

    def test_etf_replay_scores_match_scanner_fixture(self):
        program = '''
import json
from scans.etf_scanner import score_momentum as scanner_momentum, score_volume as scanner_volume, score_shares_trend as scanner_shares
from stock_trend.analytics.etf_scoring import score_momentum, score_volume, score_shares_trend
kline = [{"close": 100 + index * 0.4, "vol": 1000 + index * 11, "amount": 1_100_000_000} for index in range(80)]
shares = {"recent_flows": [{"shares_billion": 10}, {"shares_billion": 10.8}]}
print(json.dumps({"scanner": [scanner_momentum(kline), scanner_volume(kline), scanner_shares(shares)], "replay": [score_momentum(kline), score_volume(kline), score_shares_trend(shares)]}))
'''
        env = {**os.environ, "PYTHONPATH": str(SCRIPTS_ROOT)}
        result = subprocess.run(
            [sys.executable, "-c", program], env=env, text=True,
            capture_output=True, check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["replay"], payload["scanner"])


if __name__ == "__main__":
    unittest.main()
