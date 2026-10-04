"""Offline regressions for review source dates and recommendation projection."""
import json
from datetime import datetime, timedelta
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from analysis import market_regime as mr


class ReviewDateEvidenceTests(unittest.TestCase):
    def test_activity_does_not_stamp_collection_day(self):
        with patch('fetchers.sector_data._fetch_json', return_value={
                'data': {'diff': [{'f104': 10, 'f105': 5, 'f62': 123456789}]}}):
            result = mr.fetch_market_activity()
        self.assertIsNone(result['evidence']['data_date'])
        self.assertEqual(result['evidence']['date_origin'], 'unknown')
        self.assertEqual(result['main_force_yi'], 1.2)
        self.assertEqual(result['main_force_raw_yi'], 1.23456789)

    def test_activity_requires_dates_for_every_region(self):
        from zoneinfo import ZoneInfo
        stamp = int(datetime(2026, 9, 30, 15, 30, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
        for timestamp, expected in ((stamp, '2026-09-30'), (None, None), (stamp - 86400, None)):
            with self.subTest(timestamp=timestamp), patch('fetchers.sector_data._fetch_json', return_value={
                    'data': {'diff': [{'f104': 10, 'f105': 5, 'f62': 0, 'f124': stamp},
                                      {'f104': 20, 'f105': 5, 'f62': 0, 'f124': timestamp}]}}):
                evidence = mr.fetch_market_activity()['evidence']
                self.assertEqual(evidence['data_date'], expected)
                self.assertEqual(evidence['date_origin'], 'provider' if expected else 'unknown')

    def test_request_day_is_not_limit_up_source_day(self):
        with patch.object(mr, 'HAS_AKSHARE', False):
            evidence = mr.fetch_zt_stats()['evidence']
        self.assertIsNone(evidence['data_date'])
        self.assertEqual(evidence['date_origin'], 'request')
        self.assertIsNotNone(evidence['requested_date'])

    def test_index_uses_last_valid_close_date(self):
        rows = [{'close': 100, 'trade_date': '2026-09-30'}] * 25
        rows.append({'close': 0, 'trade_date': '2026-10-04'})
        metrics = mr._index_metrics(rows)
        self.assertEqual(metrics['data_date'], '2026-09-30')

    def test_legacy_good_is_not_promoted_without_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ctx.json'
            for quality in ('good', 'partial', 'missing'):
                ctx = {'data_date': '2026-09-30', 'regime': {'score': 85,
                       'data_quality': quality}, 'components': {
                       k: {'score': 85, 'data_status': 'good'}
                       for k in mr.REGIME_COMPONENT_ORDER}}
                path.write_text(json.dumps(ctx))
                view = mr.load_recommendation_context(path, today='2026-09-30')
                self.assertEqual(view['data_quality'], 'partial' if quality == 'good' else quality)
                self.assertEqual(view['model_data_quality'], quality)
                self.assertEqual(json.loads(path.read_text()), ctx)

    def test_four_clocks_freeze_raw_inputs_and_reference_reports(self):
        end = datetime(2026, 9, 30)
        dates = sorted((end - timedelta(days=i)).strftime('%Y%m%d')
                       for i in range(45) if (end - timedelta(days=i)).weekday() < 5)
        rows = [{'trade_date': d, 'close': 100 + i, 'amount': (1000 + i) * 1e8}
                for i, d in enumerate(dates)]

        def fetch(code, **kwargs):
            kwargs['diagnostics'].update({'source': 'fixture', 'data_date': '2026-09-30'})
            return rows

        for clock in (datetime(2026, 9, 30, 7), datetime(2026, 9, 30, 10),
                      datetime(2026, 9, 30, 16), datetime(2026, 10, 4, 16)):
            with self.subTest(clock=clock), patch.object(mr, 'fetch_index_kline', side_effect=fetch), \
                    patch.object(mr, 'fetch_market_activity', return_value={'up': 3000, 'down': 2000, 'main_force_yi': 12.3}), \
                    patch.object(mr, 'fetch_zt_stats', return_value={'count': 60, 'streak_count': 10, 'max_streak': 3}), \
                    patch.object(mr, 'fetch_sector_rankings', return_value=[{'name': 'fixture', 'change_pct': 1}]), \
                    patch.object(mr, 'load_history', return_value={}):
                ctx = mr.collect_context(now=clock)
                self.assertIsNone(ctx['activity_evidence']['data_date'])
                self.assertFalse(ctx['conclusion_qualification']['eligible'])
                raw = ctx['detail_inputs']
                self.assertEqual(raw['breadth']['industry_up_count'], 1)
                self.assertEqual(raw['breadth']['industry_count'], 1)
                self.assertEqual(raw['volume']['history_sample_count'], 20)
                self.assertEqual(raw['volume']['history_average_yi'], sum(raw['volume']['history_amounts_yi']) / 20)
                self.assertIsNotNone(raw['index_trend']['indices'][0]['ma20'])
                self.assertEqual(raw['capital']['main_force_yi'], 12.3)
                md = mr.generate_report(ctx)
                html = mr._generate_html(ctx, clock.isoformat(), observation_state={'status': 'degraded', 'items': []})
                self.assertIn('参考评分', md)
                self.assertIn('参考评分', html)
                self.assertIn('证据不足，以下为模型提示', md)
                self.assertIn('证据不足，以下为模型提示', html)


if __name__ == '__main__':
    unittest.main()
