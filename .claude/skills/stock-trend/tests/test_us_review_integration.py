"""Independent US report context must preserve A-share scoring and updates."""

import copy
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bridge.us_review import collect_summary, resolve_anchor
from analysis import market_regime
from reporting import us_market_summary as reporting


class USReviewIntegrationTests(unittest.TestCase):
    def test_calendar_write_failure_only_degrades_us_collection(self):
        with tempfile.TemporaryDirectory() as root, patch(
                "bridge.us_review.atomic_write_text", side_effect=OSError("calendar disk failure")), patch(
                "fetchers.sector_data._load_authoritative_trading_dates",
                return_value={"2026-09-30", "2026-10-09"}):
            result = collect_summary("2026-09-30", anchor_mode="completed", cache_dir=root)
        self.assertEqual(result["data_quality"], "unavailable")
        self.assertIn("calendar disk failure", result["errors"]["collection"])

    def test_calendar_preparation_error_is_visible_and_score_preserved(self):
        ctx = {"data_date": "2026-09-30", "generated_at": "2026-10-04 16:00:00",
               "regime": {"score": 50, "label": "弱势"}, "components": {},
               "amount_yi": 10000, "zt": {}, "top_sectors": [], "bottom_sectors": []}
        summary = {"schema_version": "us-market-summary/v2", "basis_date": "2026-09-30",
                   "status": "unavailable", "data_quality": "unavailable", "errors": {}}
        original = copy.deepcopy(ctx)
        with patch.object(sys, "argv", ["market_regime.py", "--json", "--no-html"]), patch(
                "bridge.us_review.resolve_anchor", side_effect=OSError("calendar disk failure")), patch(
                "bridge.us_review.collect_summary", return_value=summary), patch.object(
                market_regime, "collect_context", return_value=ctx), patch.object(
                market_regime, "should_save_history", return_value=False), patch.object(
                market_regime, "save_context"), redirect_stdout(io.StringIO()):
            market_regime.main()
        self.assertEqual(ctx, original)
        self.assertEqual(summary["errors"]["calendar_preparation"], "OSError: calendar disk failure")

    def test_completed_anchor_morning_and_cache_contract(self):
        config = {"config_sha256": "fixture", "indices": [
            {"symbol": "SPY", "name": "SPY", "sector": "index"}], "sectors": [], "stocks": []}
        payload = {"provider": "fixture", "rows": {"SPY": [
            {"date": "2026-09-28", "adj_close": 100},
            {"date": "2026-09-29", "adj_close": 102}]}, "errors": {}}
        capture = datetime.fromisoformat("2026-09-29T16:00:00+08:00")
        cutoff = datetime.fromisoformat("2026-09-30T09:00:00+08:00")
        with tempfile.TemporaryDirectory() as root:
            with patch("bridge.us_review._observed_now", return_value=capture), patch(
                    "fetchers.sector_data._load_authoritative_trading_dates",
                    return_value={"2026-09-28", "2026-09-29", "2026-09-30", "2026-10-09"}):
                resolve_anchor(capture, root)
            with patch("analysis.us_market_summary.load_watchlist", return_value=config), patch(
                    "bridge.us_review._observed_now", return_value=cutoff), patch(
                    "fetchers.us_market.fetch_us_market", return_value=payload), patch(
                    "fetchers.sector_data._load_authoritative_trading_dates", side_effect=AssertionError("network")):
                result = collect_summary("2026-09-30", as_of=cutoff, cache_dir=root,
                                         anchor_mode="completed", calendar_prepared=True)
            self.assertEqual(result["basis_date"], "2026-09-30")
            self.assertEqual(result["a_share_anchor_date"], "2026-09-29")
            self.assertEqual(result["latest_completed_session"], "2026-09-29")
            self.assertEqual(result["groups"]["indices"][0]["daily_pct"], 2)
            with patch("analysis.us_market_summary.load_watchlist", return_value=config), patch(
                    "fetchers.us_market.fetch_us_market", side_effect=AssertionError("network")), patch(
                    "fetchers.sector_data._load_authoritative_trading_dates", side_effect=AssertionError("network")):
                cached = collect_summary("2026-09-30", as_of=cutoff.replace(hour=10),
                                         cache_dir=root, no_refresh=True, anchor_mode="completed")
                self.assertEqual(cached["source_status"], "cached")
                after_close = collect_summary("2026-09-30", as_of=cutoff.replace(hour=16),
                                         cache_dir=root, no_refresh=True, anchor_mode="completed")
                self.assertEqual(after_close["data_quality"], "unavailable")
                pointer = next((Path(root) / "us_market").glob("*.json"))
                stored = json.loads(pointer.read_text())
                for field, value in (("schema_version", "us-market-summary/v1"),
                                     ("frozen_at", "2026-10-01T00:00:00+08:00")):
                    pointer.write_text(json.dumps(dict(stored, **{field: value})))
                    rejected = collect_summary("2026-09-30", as_of=cutoff.replace(hour=10),
                                         cache_dir=root, no_refresh=True, anchor_mode="completed")
                    self.assertEqual(rejected["data_quality"], "unavailable")

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
            with patch("bridge.us_review._observed_now", return_value=cutoff), patch("fetchers.us_market.fetch_us_market", return_value=payload) as fetch:
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
