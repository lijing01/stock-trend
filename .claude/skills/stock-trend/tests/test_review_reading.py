"""Offline reading contracts for the daily-review home page."""
import copy
import io
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from analysis import market_regime as market
from test_market_detail_links import report_context
from test_us_market_reporting import sample_summary


class ReadingTests(unittest.TestCase):
    def test_home_order_and_markdown_contents(self):
        ctx = report_context()
        ctx['us_market_summary'] = sample_summary()
        ctx['review_comparison'] = {'status': 'unavailable', 'basis_date': ctx['data_date']}
        original = copy.deepcopy(ctx)
        state = {'status': 'unavailable', 'reason': 'fixture'}
        html = market._generate_html(ctx, 'fixture', observation_state=state)
        md = market.generate_report(ctx, observation_state=state)
        for content in (html, md):
            order = [content.index(target) for target in (
                '参考评分', 'REVIEW_COMPARISON:START', 'id="market-component-summary"',
                'id="market-sectors"', 'US_MARKET_SUMMARY:START', 'OBSERVATION_LIST:START')]
            self.assertEqual(order, sorted(order))
            self.assertIn('2026-09-30', content)
            self.assertIn(market.DISCLAIMER, content)
            self.assertLess(content.index('OBSERVATION_LIST:END'), content.index('MARKET_DETAIL_LINKS:START'))
        self.assertIn('[五项简表](#market-component-summary)', md)
        self.assertIn('[五项详情](#market-component-details)', md)
        for target in ('market-component-summary', 'market-sectors', 'us-market-summary',
                       'observation-list', 'market-component-details'):
            self.assertEqual(md.count(f'id="{target}"'), 1)
        self.assertEqual(ctx, original)

    def test_status_precedes_score(self):
        for intraday in (False, True):
            ctx = report_context()
            ctx['intraday'] = intraday
            html = market._generate_html(ctx, 'fixture', observation_state={'status': 'unavailable'})
            status = '盘中临时' if intraday else '收盘口径'
            self.assertLess(html.index(status), html.index('参考评分'))

    def test_observation_formats_share_frozen_values_and_pending_state(self):
        state = {'status': 'degraded', 'data_date': '2026-09-30', 'provisional': True,
                 'items': [{'name': '测试股票', 'code': '600519', 'date': '2026-09-01',
                            'raw_dimensions': {'momentum': 67.5}, 'composite_score': 61,
                            'quality_adjusted_score': 55, 'reasons': ['冻结证据<缺失>|未核验']}]}
        original = copy.deepcopy(state)
        for content in (market.render_observation_list_html(state),
                        market.render_observation_list_markdown(state)):
            for value in ('2026-09-30', '2026-09-01', '600519', '67.5', '61', '55', '盘中临时'):
                self.assertIn(value, content)
            self.assertIn('&lt;缺失&gt;', content)
        self.assertEqual(state, original)
        for renderer in (market.render_observation_list_html, market.render_observation_list_markdown):
            self.assertIn('分析进行中', renderer(state, pending=True))

    def test_markdown_only_collects_ready_state_and_no_refresh_stays_offline(self):
        for offline in (False, True):
            with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
                ctx = report_context()
                argv = ['market_regime.py', '--no-html'] + (['--no-refresh'] if offline else [])
                stack.enter_context(patch.object(sys, 'argv', argv))
                stack.enter_context(patch.object(market, 'REPORTS_DIR', Path(root)))
                stack.enter_context(patch.object(market, 'collect_context', return_value=ctx))
                stack.enter_context(patch.object(market, 'load_context', return_value=ctx))
                stack.enter_context(patch.object(market, 'save_context'))
                stack.enter_context(patch.object(market, 'should_save_history', return_value=False))
                stack.enter_context(patch.object(market, 'collect_review_comparison', return_value=None))
                stack.enter_context(patch('bridge.us_review.resolve_anchor'))
                stack.enter_context(patch('bridge.us_review.collect_summary', return_value=None))
                analyze = stack.enter_context(patch('analysis.observation_list_analysis.analyze_observation_list'))
                stack.enter_context(patch.object(market, 'load_observation_analysis', return_value={
                    'status': 'unavailable', 'reason': 'fixture状态'}))
                with redirect_stdout(io.StringIO()):
                    market.main()
                self.assertEqual(analyze.call_count, 0 if offline else 1)
                paths = list(Path(root).glob('*.md'))
                self.assertEqual(len(paths), 1)
                self.assertIn('fixture状态', paths[0].read_text())


if __name__ == '__main__':
    unittest.main()
