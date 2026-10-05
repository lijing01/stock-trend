"""Comparison collection is optional, offline and isolated from model/history."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from analysis import market_regime as market
from analysis.review_comparison import build_review_snapshot, persist_review_snapshot, prepare_comparison
from test_review_comparison import context, calendar


class ComparisonIntegrationTests(unittest.TestCase):
    def test_four_clocks_share_html_md_and_preserve_model(self):
        with tempfile.TemporaryDirectory() as root:
            for day in ('2026-09-28', '2026-09-29'):
                persist_review_snapshot(build_review_snapshot(context(day), frozen_at=day + 'T16:00:00+08:00'), root)
            sessions = calendar('2026-09-28', '2026-09-29', '2026-09-30', '2026-10-09')
            for clock, basis, intraday in (
                ('2026-09-30T07:00:00+08:00', '2026-09-29', False),
                ('2026-09-30T10:00:00+08:00', '2026-09-30', True),
                ('2026-09-30T16:00:00+08:00', '2026-09-30', False),
                ('2026-10-04T16:00:00+08:00', '2026-09-30', False),
            ):
                with self.subTest(clock):
                    ctx = context(basis, intraday=intraday)
                    ctx.update(zt={}, top_sectors=[], bottom_sectors=[])
                    original = copy.deepcopy(ctx)
                    comparison = prepare_comparison(ctx, sessions, as_of=clock, cache_dir=root)
                    self.assertEqual(ctx, original)
                    ctx['review_comparison'] = comparison
                    md = market.generate_report(ctx)
                    html = market._generate_html(ctx, 'fixture', observation_state={'status': 'unavailable'})
                    self.assertIn(comparison['prior_session_date'], md)
                    self.assertIn(comparison['prior_session_date'], html)
                    self.assertEqual(market.build_agent_output(ctx)['review_comparison'], comparison)
                    self.assertEqual(ctx['regime'], original['regime'])

    def test_collection_is_offline_and_does_not_mutate_scores(self):
        ctx = {'data_date': '2026-09-30', 'regime': {'score': 60}, 'components': {}}
        original = copy.deepcopy(ctx)
        cutoff = datetime.fromisoformat('2026-10-04T16:00:00+08:00')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'calendar.json'
            calendar = {'schema': 'review-time/v1', 'trading_dates': ['2026-09-29', '2026-09-30']}
            path.write_text(json.dumps(calendar))
            with patch('bridge.us_review.resolve_anchor', return_value={'evidence_path': str(path)}) as anchor, patch(
                    'analysis.review_comparison.prepare_comparison', return_value={'status': 'unavailable'}) as prepare, patch.object(
                    market, 'load_history', return_value={}):
                result = market.collect_review_comparison(ctx, as_of=cutoff, persist=False)
            self.assertEqual(result['status'], 'unavailable')
            self.assertTrue(anchor.call_args.kwargs['no_refresh'])
            self.assertEqual(prepare.call_args.args[1], calendar)
            self.assertEqual(prepare.call_args.kwargs['as_of'], cutoff)
            self.assertIsNotNone(prepare.call_args.kwargs['frozen_at'].tzinfo)
        self.assertEqual(ctx, original)

    def test_optional_failure_is_explicit(self):
        with patch('bridge.us_review.resolve_anchor', side_effect=OSError('fixture disk failure')):
            result = market.collect_review_comparison({}, as_of=datetime.fromisoformat('2026-10-04T16:00:00+08:00'))
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('fixture disk failure', result['reason'])


if __name__ == '__main__':
    unittest.main()
