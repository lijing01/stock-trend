"""Independent US report context must preserve A-share scoring and updates."""

import copy
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bridge.us_review import collect_summary
from analysis import market_regime
from reporting import us_market_summary as reporting


class USReviewIntegrationTests(unittest.TestCase):
    def test_observation_and_us_updates_do_not_lose_each_other(self):
        entered = threading.Event()
        release = threading.Event()
        failures = []
        summary = {"basis_date": "2026-09-30", "data_quality": "unavailable", "groups": {}}
        original_render = reporting.render_html

        def delayed_render(value):
            entered.set()
            if not release.wait(3):
                raise AssertionError("writer not released")
            return original_render(value)

        def run(function):
            try:
                function()
            except Exception as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text('<h1>今日复盘 2026-09-30</h1>' +
                            market_regime.render_observation_list_html(pending=True) + '<footer>原内容</footer>')
            with patch.object(reporting, "render_html", side_effect=delayed_render), patch.object(
                market_regime, "load_observation_analysis",
                return_value={"status": "unavailable", "reason": "观察区块更新完成"}
            ):
                writer = threading.Thread(target=run, args=(lambda: reporting.update_reports(path, summary),))
                observer = threading.Thread(target=run, args=(lambda: market_regime.update_observation_list_html(
                    path, data_date="2026-09-30"),))
                writer.start()
                self.assertTrue(entered.wait(3))
                observer.start()
                release.set()
                writer.join(5)
                observer.join(5)
                self.assertFalse(writer.is_alive() or observer.is_alive())
            self.assertEqual(failures, [])
            text = path.read_text()
            self.assertEqual(text.count(reporting.HTML_BLOCK_START), 1)
            self.assertEqual(text.count(market_regime.OBSERVATION_BLOCK_START), 1)
            self.assertIn("观察区块更新完成", text)
            self.assertIn("原内容", text)

    def test_no_refresh_never_fetches_and_rejects_future_cache(self):
        payload = {"provider": "fixture", "fetched_at": "2026-10-03T17:00:00+08:00",
                   "rows": {}, "errors": {}}
        cutoff = datetime(2026, 10, 3, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as directory:
            with patch("fetchers.us_market.fetch_us_market", return_value=payload) as fetch:
                result = collect_summary("2026-09-30", as_of=cutoff, cache_dir=directory)
                self.assertNotEqual(result.get("errors", {}).get("collection"), "no_qualified_us_cache")
                fetch.assert_called_once()
            with patch("fetchers.us_market.fetch_us_market", side_effect=AssertionError("network")):
                cached = collect_summary("2026-09-30", as_of=cutoff, cache_dir=directory, no_refresh=True)
                self.assertEqual(cached.get("source_status"), "cached")
                earlier = cutoff.replace(hour=16)
                rejected = collect_summary("2026-09-30", as_of=earlier, cache_dir=directory, no_refresh=True)
                self.assertEqual(rejected["data_quality"], "unavailable")
                wrong_basis = collect_summary("2026-09-29", as_of=cutoff, cache_dir=directory, no_refresh=True)
                self.assertEqual(wrong_basis["data_quality"], "unavailable")
            artifact = Path(result["artifact_path"])
            self.assertEqual(json.loads(artifact.read_text())["basis_date"], "2026-09-30")

    def test_optional_us_context_changes_only_rendering(self):
        ctx = {"data_date": "2026-09-30", "generated_at": "2026-10-03 16:44:58",
               "regime": {"score": 34.2, "label": "弱势"}, "components": {},
               "amount_yi": 14380, "zt": {}, "top_sectors": [], "bottom_sectors": []}
        original = copy.deepcopy(ctx)
        base_md = market_regime.generate_report(ctx)
        summary = {"basis_date": "2026-09-30", "as_of": "2026-10-03T17:00:00+08:00",
                   "data_quality": "unavailable", "status": "unavailable", "groups": {},
                   "errors": {"collection": "fixture"}}
        render_ctx = {**ctx, "us_market_summary": summary}
        md = market_regime.generate_report(render_ctx)
        html = market_regime._generate_html(render_ctx, "20261003-170000",
                                           observation_state={"status": "unavailable"})
        self.assertEqual(ctx, original)
        self.assertNotIn("美股区间概要", base_md)
        self.assertIn("美股区间概要", md)
        self.assertIn("us-market-summary", html)
        self.assertLess(html.index('id="us-market-summary"'), html.index('id="observation-list"'))
        self.assertIn("34.2 / 100", html)
        self.assertIn("2026-10-03 16:44:58", html)


if __name__ == "__main__":
    unittest.main()
