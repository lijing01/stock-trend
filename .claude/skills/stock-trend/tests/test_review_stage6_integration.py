"""Cross-market context is supplementary, frozen, and shared by report formats."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from analysis import market_regime as market
from test_market_detail_links import report_context


class Stage6IntegrationTests(unittest.TestCase):
    def test_formats_and_json_share_state_without_changing_scores(self):
        from reporting.cross_market_review import prepare, render
        ctx = report_context()
        original = copy.deepcopy(ctx['regime'])
        for clock in ('morning', 'intraday', 'close', 'holiday'):
            ctx['us_market_summary'] = {'basis_date': ctx['data_date'], 'groups': {},
                                        'status': 'unavailable'}
            ctx['cross_market_inputs'], ctx['cross_market_observation'] = prepare(ctx, None)
            html = market._generate_html(ctx, clock, observation_status='pending')
            md = market.generate_report(ctx, observation_status='pending')
            for content in (html, md):
                self.assertIn('美股与A股观察关联', content)
                self.assertIn('CROSS_MARKET_INPUTS:', content)
            self.assertEqual(market.build_agent_output(ctx)['cross_market_observation'],
                             ctx['cross_market_observation'])
            self.assertEqual(ctx['regime'], original)

    def test_background_update_uses_frozen_mapping_without_reload(self):
        from reporting.cross_market_review import prepare, render
        from analysis import cross_market_observation as analysis
        ctx = report_context()
        frozen, state = prepare(ctx, None)
        ctx['cross_market_inputs'] = frozen
        ctx['cross_market_observation'] = state
        with tempfile.TemporaryDirectory() as root:
            html, md = Path(root) / 'review.html', Path(root) / 'review.md'
            html.write_text(market._generate_html(ctx, 'fixture', observation_status='pending'))
            md.write_text(market.generate_report(ctx, observation_status='pending'))
            observation = {'status': 'ready', 'data_date': ctx['data_date'], 'items': []}
            with patch.object(market, 'load_observation_analysis', return_value=observation), patch.object(
                    analysis, 'load_mapping', side_effect=AssertionError('mutable mapping read')):
                first = market.update_observation_list_reports(html, md, data_date=ctx['data_date'])
                second = market.update_observation_list_reports(html, md, data_date=ctx['data_date'])
            self.assertEqual(first['status'], 'completed')
            self.assertFalse(second['files']['html']['changed'])
            self.assertFalse(second['files']['markdown']['changed'])
            for path in (html, md):
                self.assertEqual(path.read_text().count('<!-- CROSS_MARKET_OBSERVATION:START -->'), 1)
                self.assertIn('CROSS_MARKET_INPUTS:', path.read_text())

    def test_analysis_failure_does_not_block_report(self):
        from reporting.cross_market_review import prepare
        ctx = report_context()
        with patch('analysis.cross_market_observation.build_cross_market_observation',
                   side_effect=ValueError('broken fixture')):
            _, state = prepare(ctx, None)
        self.assertEqual(state['status'], 'unavailable')
        self.assertIn('ValueError', state['reason'])

    def test_four_actual_clock_cutoffs_preserve_observation_qualification(self):
        from analysis.cross_market_observation import build_cross_market_observation
        from test_cross_market_observation import summary, sector_state, observed_items, mapping_document, mapping
        scenes = (
            ('2026-09-30T07:00:00+08:00', '2026-09-29', '2026-09-29T16:00:00+08:00', True),
            ('2026-09-30T10:00:00+08:00', '2026-09-30', '2026-09-30T15:30:00+08:00', False),
            ('2026-09-30T16:00:00+08:00', '2026-09-30', '2026-09-30T15:30:00+08:00', True),
            ('2026-10-03T09:00:00+08:00', '2026-09-30', '2026-09-30T15:30:00+08:00', True),
        )
        for cutoff, basis, generated, qualified in scenes:
            market_summary = summary(requested=cutoff)
            market_summary['basis_date'] = basis
            market_summary['latest_completed_session'] = '2026-09-29'
            market_summary['groups']['sectors'][0].update(
                latest_completed_session='2026-09-29', actual_end_session='2026-09-29')
            sectors = sector_state()
            sectors['basis_date'] = basis
            sectors['items'][0]['basis_date'] = basis
            observation = observed_items(generated=generated)
            observation.update(data_date=basis, evidence_cutoff_date=basis)
            for item in observation['items']:
                item['data_date'] = basis
                for membership in item['sector_memberships']:
                    membership['membership_data_date'] = basis
            state = build_cross_market_observation(market_summary, sectors, observation,
                mappings=mapping_document(mapping()))
            self.assertEqual(bool(state['rows'][0]['items']), qualified, cutoff)
            self.assertEqual(state['report_cutoff'], cutoff)

    def test_non_finite_frozen_inputs_degrade_without_blocking_reports(self):
        from reporting.cross_market_review import prepare, render
        ctx = report_context()
        ctx['us_market_summary'] = {'groups': {'stocks': [{'daily_pct': float('nan')}]}}
        frozen, state = prepare(ctx, None)
        ctx.update(cross_market_inputs=frozen, cross_market_observation=state)
        for fmt in ('html', 'markdown'):
            content = render(ctx, fmt)
            self.assertIn('美股与A股观察关联', content)
            encoded = content.split('<!-- CROSS_MARKET_INPUTS:', 1)[1].split(' -->', 1)[0]
            self.assertIsNone(json.loads(encoded)['summary']['groups']['stocks'][0]['daily_pct'])

    def test_embedded_inputs_cannot_terminate_comment(self):
        from reporting.cross_market_review import prepare, render
        ctx = report_context()
        ctx['us_market_summary'] = {'reason': '--><script>alert(1)</script>'}
        frozen, state = prepare(ctx, None)
        ctx.update(cross_market_inputs=frozen, cross_market_observation=state)
        for fmt in ('html', 'markdown'):
            content = render(ctx, fmt)
            encoded = content.split('<!-- CROSS_MARKET_INPUTS:', 1)[1].split(' -->', 1)[0]
            self.assertNotIn('<', encoded)
            self.assertEqual(json.loads(encoded)['summary'], ctx['us_market_summary'])


if __name__ == '__main__':
    unittest.main()
