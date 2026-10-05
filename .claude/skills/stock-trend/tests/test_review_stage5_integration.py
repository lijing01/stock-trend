"""Stage 5 optional evidence is shared by report formats and remains offline."""
import copy
import io
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack, redirect_stdout
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from analysis import market_regime as market
from test_market_detail_links import report_context


class Stage5IntegrationTests(unittest.TestCase):
    def test_adjacent_date_uses_verified_calendar_not_weekdays(self):
        with patch.object(market, '_review_calendar', return_value={
                'trading_dates': ['2026-09-29', '2026-09-30', '2026-10-09']}):
            self.assertEqual(market._previous_observation_session('2026-10-09', as_of=datetime.fromisoformat('2026-10-09T16:00:00+08:00')), '2026-09-30')
            self.assertIsNone(market._previous_observation_session('2026-10-04'))
            self.assertIsNone(market._previous_observation_session('2026-09-29'))

    def test_missing_calendar_degrades_without_network_fallback(self):
        with patch('bridge.us_review.resolve_anchor', side_effect=OSError('offline')) as resolve:
            self.assertIsNone(market._previous_observation_session('2026-09-30'))
        self.assertTrue(resolve.call_args.kwargs['no_refresh'])

    def test_dual_updater_loads_one_state(self):
        state = {'status': 'degraded', 'items': [], 'data_date': '2026-09-30'}
        with patch.object(market, 'load_observation_analysis', return_value=state) as load, patch(
                'reporting.observation_report_update.update_observation_reports', return_value={'status': 'completed'}) as update:
            market.update_observation_list_reports('/tmp/fixture.html', '/tmp/fixture.md', data_date='2026-09-30')
        load.assert_called_once()
        self.assertIs(update.call_args.kwargs['state'], state)

    def test_sector_failure_does_not_modify_model(self):
        ctx = {'data_date': '2026-09-30', 'regime': {'score': 60}, 'components': {}}
        original = copy.deepcopy(ctx)
        with patch.object(market, '_review_calendar', side_effect=OSError('disk failure')):
            result = market.collect_sector_persistence(ctx, no_refresh=False)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(ctx, original)

    def test_json_html_workflow_creates_both_pending_reports(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            ctx = report_context()
            stack.enter_context(patch.object(sys, 'argv', ['market_regime.py', '--json', '--html', '--observation-status', 'pending']))
            stack.enter_context(patch.object(market, 'REPORTS_DIR', Path(root)))
            stack.enter_context(patch.object(market, 'collect_context', return_value=ctx))
            stack.enter_context(patch.object(market, 'save_context'))
            stack.enter_context(patch.object(market, 'should_save_history', return_value=False))
            stack.enter_context(patch.object(market, 'collect_review_comparison', return_value=None))
            stack.enter_context(patch.object(market, 'collect_sector_persistence', return_value={'status': 'missing', 'items': []}))
            stack.enter_context(patch('bridge.us_review.resolve_anchor'))
            stack.enter_context(patch('bridge.us_review.collect_summary', return_value=None))
            with redirect_stdout(io.StringIO()) as output:
                market.main()
            html = list(Path(root).glob('*.html'))
            md = list(Path(root).glob('*.md'))
            self.assertEqual(len(html), 1)
            self.assertEqual(len(md), 1)
            self.assertEqual(html[0].stem, md[0].stem)
            self.assertIn('MD:', output.getvalue())
            for path in (html[0], md[0]):
                self.assertIn('分析进行中', path.read_text())

    def test_offline_sector_binding_never_reads_current_yaml_or_calendar(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'sector_persistence' / '2026-09-30' / 'frozen.json'
            binding = {'basis_date': '2026-09-30', 'artifact_path': str(path),
                       'sectors': [], 'trading_dates': ['2026-09-29', '2026-09-30'],
                       'calendar_evidence': {'source': 'fixture'}}
            ctx = {'data_date': '2026-09-30', 'sector_persistence_binding': binding}
            with patch.object(market, 'CACHE_DIR', Path(root)), patch.object(
                    market, '_review_calendar', side_effect=AssertionError('offline calendar read')), patch.object(
                    market, 'load_observation_analysis', side_effect=AssertionError('current YAML read')), patch(
                    'analysis.sector_snapshot_job.freeze_sector_persistence', return_value={'status': 'complete', 'items': []}) as freeze:
                result = market.collect_sector_persistence(ctx, no_refresh=True)
            self.assertEqual(result['status'], 'complete')
            self.assertFalse(freeze.call_args.kwargs['refresh'])
            self.assertEqual(freeze.call_args.args[3], path)

    def test_current_yaml_and_self_validated_historical_artifact_integrate(self):
        from test_observation_list_analysis import _v2_row, _v2_payload
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            yaml_path = root / 'observations.yaml'
            yaml_path.write_text("observation_list:\n  - {code: '600519', date: '2026-09-21', entry_phase: 吸筹}\n")
            current_row = _v2_row('2026-09-30')
            current_row['wyckoff'] = {'event_health': {'state': 'structure_invalidated'}}
            previous_row = _v2_row('2026-09-29')
            previous_row['wyckoff'] = {'event_health': {'state': 'confirmed_holding'}}
            current = _v2_payload(current_row, generated='2026-09-30T16:00:00+08:00')
            current['status'] = 'ready'
            current['config_sha256'] = hashlib.sha256(yaml_path.read_bytes()).hexdigest()
            previous = _v2_payload(previous_row)
            previous['config_sha256'] = 'an-old-yaml-digest'
            (root/'2026-09-30.json').write_text(json.dumps(current))
            (root/'2026-09-29.json').write_text(json.dumps(previous))
            with patch.object(market, '_review_calendar', return_value={
                    'trading_dates': ['2026-09-29', '2026-09-30']}):
                state = market.load_observation_analysis('2026-09-30', root/'2026-09-30.json',
                    yaml_path, as_of=datetime.fromisoformat('2026-09-30T17:00:00+08:00'))
            self.assertEqual(state['comparison']['status'], 'ready')
            self.assertEqual(state['comparison']['events'][0]['type'], 'structure_invalidated')
            self.assertEqual(state['comparison']['rows'][0]['score_status'], 'comparable')
            for renderer in (market.render_observation_list_html, market.render_observation_list_markdown):
                self.assertIn('结构失效', renderer(state))

    def test_refresh_and_offline_share_exact_run_with_observed_sector(self):
        from test_sector_persistence import sector, records, DATES
        with tempfile.TemporaryDirectory() as root:
            ctx = {'generated_at': '2026-09-21T16:00:00+08:00', 'data_date': DATES[-1],
                   'top_sectors': [sector()], 'bottom_sectors': []}
            observed = {'status': 'ready', 'items': [{'sector_memberships': [{
                'code': 'BK0002', 'name': '观察行业', 'sector_type': 'industry',
                'membership_provider': 'eastmoney', 'membership_data_date': DATES[-1],
                'membership_quality': 'historical_verified'}]}]}
            cutoff = datetime.fromisoformat('2026-09-21T16:00:00+08:00')
            with patch.object(market, 'CACHE_DIR', Path(root)), patch.object(
                    market, '_review_calendar', return_value={'trading_dates': DATES, 'source': 'fixture'}), patch(
                    'analysis.sector_snapshot_job._bounded_fetch', return_value=({'BK0001': records()}, False, '')):
                result = market.collect_sector_persistence(ctx, observation_state=observed, as_of=cutoff)
            self.assertIn('run_binding', result)
            self.assertEqual([s['code'] for s in result['run_binding']['sectors']], ['BK0001', 'BK0002'])
            ctx['sector_persistence_binding'] = result['run_binding']
            with patch.object(market, 'CACHE_DIR', Path(root)), patch.object(
                    market, '_review_calendar', side_effect=AssertionError('offline calendar')), patch.object(
                    market, 'load_observation_analysis', side_effect=AssertionError('changed YAML')):
                replay = market.collect_sector_persistence(ctx, no_refresh=True)
            self.assertEqual(replay['items'], result['items'])
            self.assertEqual(replay['status'], result['status'])

    def test_current_uncompleted_basis_is_not_adjacent_comparison(self):
        with patch.object(market, '_review_calendar', return_value={
                'trading_dates': ['2026-09-29', '2026-09-30']}):
            for clock in ('07:00', '10:00', '15:05'):
                cutoff = datetime.fromisoformat(f'2026-09-30T{clock}:00+08:00')
                self.assertIsNone(market._previous_observation_session('2026-09-30', as_of=cutoff))
            cutoff = datetime.fromisoformat('2026-09-30T15:10:00+08:00')
            self.assertEqual(market._previous_observation_session('2026-09-30', as_of=cutoff), '2026-09-29')

    def test_shared_supplementary_values_preserve_scores_for_four_clocks(self):
        from analysis.sector_persistence import analyze_sector_series
        from analysis.observation_comparison import compare_observation_artifacts
        from test_sector_persistence import sector, records, DATES
        from test_observation_comparison import _artifact, _row
        persistence = {'basis_date': DATES[-1], 'items': [analyze_sector_series(
            sector(), records(), trading_dates=DATES, basis_date=DATES[-1])]}
        comparison = compare_observation_artifacts(
            _artifact('2026-09-30', [_row('600519', state='structure_invalidated')]),
            _artifact('2026-09-29', [_row('600519', state='confirmed_holding')]),
            expected_previous_date='2026-09-29')
        for clock, intraday in (('morning', False), ('intraday', True), ('close', False), ('holiday', False)):
            ctx = report_context()
            ctx['intraday'] = intraday
            original = copy.deepcopy(ctx['regime'])
            ctx['sector_persistence'] = persistence
            state = {'status': 'ready', 'items': [], 'comparison': comparison}
            html = market._generate_html(ctx, clock, observation_state=state)
            md = market.generate_report(ctx, observation_state=state)
            for content in (html, md):
                self.assertIn('结构失效', content)
                self.assertIn('+20.00%', content)
                self.assertIn('连续上涨', content)
            self.assertEqual(ctx['regime'], original)


if __name__ == '__main__':
    unittest.main()
