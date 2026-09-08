import copy
import unittest,sys,tempfile,json
from datetime import date, timedelta
from unittest import mock
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
from analysis.recommendation_attribution import *
from core.candidate_research_snapshot import build_research_snapshot
class T(unittest.TestCase):
 def test_cost(self): self.assertEqual(CostModel().mode, 'gross')
 def test_entry_and_pending(self):
  plan={'entry':{'low':10,'high':12},'stop_loss':{'price':8},'target':{'price':15}}
  rec={'recommendation_date':'2026-08-20','code':'X','trade_plan':plan}; days=[f'2026-08-{d:02d}' for d in range(20,30)]; rows=[{'date':d,'open':11,'high':12,'low':10,'close':11+(i%3),'vol':1} for i,d in enumerate(days)]
  x=evaluate_recommendation(rec,'2026-08-24',days,rows,stock_meta={'adj':'qfq'},windows=(5,)); self.assertEqual(x['windows']['5']['status'],'pending')
 def test_sidecar_merge_preserves_completed_window(self):
  old={'items':[{'code':'X','windows':{'5':{'status':'complete','net_return':.1}}}]}
  new={'items':[{'code':'X','windows':{'5':{'status':'complete','net_return':.9},'10':{'status':'pending'}}}]}
  merged=merge_attribution(old,new)
  self.assertEqual(merged['items'][0]['windows']['5']['net_return'],.1)
  self.assertEqual(len(merged['items'][0]['conflicts']),1)
  self.assertEqual(merged['items'][0]['conflicts'][0]['window'],'5')
  self.assertEqual(merged['items'][0]['windows']['10']['status'],'pending')

 def test_evaluation_contract_id_ignores_per_date_population_identity(self):
  first=build_evaluation_contract((5,10,20), {},
   population_identity={'research_run_id':'run-a','record_ids':['a']})
  second=build_evaluation_contract((5,10,20), {},
   population_identity={'research_run_id':'run-b','record_ids':['b']})
  self.assertEqual(first['contract_id'],second['contract_id'])
  self.assertNotEqual(first['population_identity'],second['population_identity'])
  gap=build_evaluation_contract((5,10,20), {}, population_kind='research_population_gap')
  self.assertNotEqual(first['contract_id'],gap['contract_id'])

 def _plan(self):
  return {'entry':{'low':10,'high':12},'stop_loss':{'price':9},'target':{'price':20}}

 def _production_rows(self, days):
  return [{'trade_date':d,'open':11,'high':12,'low':8 if i == 2 else 10,
           'close':11+(i%2),'vol':1} for i,d in enumerate(days)]

 def test_production_trade_dates_mature_and_match_benchmarks(self):
  days=['20260820','20260821','20260822','20260825','20260826','20260827']
  rows=[{'trade_date':d,'open':11,'high':12,'low':10,'close':11,'vol':1} for d in days]
  result=evaluate_recommendation(
   {'recommendation_date':'2026-08-20','code':'X','trade_plan':self._plan()},
   '2026-08-27',days,rows,
   hs300_rows=[{'trade_date':d,'close':100+i} for i,d in enumerate(days)],
   stock_meta={'adj':'qfq'},windows=(5,))
  window=result['windows']['5']
  self.assertEqual(window['status'],'complete')
  self.assertIsNotNone(window['hs300_return'])
  self.assertIsNotNone(window['hs300_alpha'])

 def test_non_qfq_series_is_data_error(self):
  snapshot={'content':{'recommendation_date':'2026-08-20','buckets':{
   'actionable':[{'code':'X','trade_plan':self._plan()}]}}}
  days=['2026-08-20','2026-08-21','2026-08-22','2026-08-25','2026-08-26','2026-08-27']
  rows=[{'date':d,'open':11,'high':12,'low':10,'close':11,'vol':1} for d in days]
  def loader(code,candidate):
   return {'market_sessions':days,'stock_rows':rows,'stock_meta':{'adj':'hfq'}}
  result=track_attribution(snapshot,loader,'2026-08-27',windows=(5,))
  item=result['items'][0]
  self.assertEqual(item['execution']['status'],'data_error')
  self.assertEqual(item['execution']['reason'],'wrong_adjustment')

 def test_candidate_signal_performance_does_not_need_trade_plan(self):
  days=['2026-08-20','2026-08-21','2026-08-22','2026-08-25','2026-08-26','2026-08-27']
  rows=[{'date':d,'close':10+i,'vol':1} for i,d in enumerate(days)]
  result=evaluate_candidate_signal({'recommendation_date':'2026-08-20','code':'X'},
   '2026-08-27',days,rows,stock_meta={'adj':'qfq'},windows=(5,))
  self.assertEqual(result['measurement']['trade_plan_required'],False)
  self.assertEqual(result['windows']['5']['status'],'complete')
  self.assertAlmostEqual(result['windows']['5']['signal_return'],4/11)
  self.assertAlmostEqual(result['windows']['5']['mae'],0)

 def test_candidate_readiness_requires_finite_alpha_evidence(self):
  # Complete holding endpoints alone are not usable candidate research when
  # the benchmark alpha is absent. Keep 100 events across 20 dates to prove
  # the status gate is tied to valid alpha, not merely maturity counts.
  days=[]; cursor=date(2026,1,5)
  while len(days) < 20:
   if cursor.weekday() < 5: days.append(cursor.isoformat())
   cursor += timedelta(days=1)
  items=[]
  for number in range(100):
   day=days[number // 5]
   code=str(600000 + number)
   items.append({'recommendation_date':day,'code':code,'windows':{'20':{
    'status':'complete','entry_date':day,'exit_date':day,'hs300_alpha':None,
    'signal_return':.01}}})
  summary=summarize_candidate_performance(items, minimum_dates=20, minimum_mature=100)
  self.assertEqual(summary['status'], 'evidence_insufficient')
  self.assertEqual(summary['valid_alpha_events'], 0)
  self.assertEqual(summary['valid_alpha_dates'], 0)
  self.assertEqual(candidate_research_readiness(summary)['status'], 'evidence_insufficient')

 def test_research_population_evaluates_truncated_and_keeps_hard_exclusion(self):
  def candidate(code, **extra):
   value={'code':code,'ts_code':code+'.SH','composite_score':80,
          'quality_adjusted_score':80,'data_quality':{'eligible':True}}
   value.update(extra)
   return value
  research=build_research_snapshot(
   [candidate('A'), candidate('B'), candidate('C', research_terminal_status='phase2_filtered',
    research_terminal_reason='phase2_no_eligible_buy_point_or_data_error')],
   {'actionable':[candidate('A')]}, '2026-08-20', {}, {}, [], 50,
   official_tracking={'status':'created'}, model_version='test')
  days=['2026-08-20','2026-08-21','2026-08-22','2026-08-25','2026-08-26','2026-08-27']
  def loader(code,candidate,*args):
   if code=='B': raise RuntimeError('retryable')
   return {'market_sessions':days,
    'stock_rows':[{'date':d,'close':10+i,'low':10+i} for i,d in enumerate(days)],
    'hs300_rows':[{'date':d,'close':100+i} for i,d in enumerate(days)],
    'stock_meta':{'adj':'qfq'}}
  result=track_attribution(
   {'content':{'recommendation_date':'2026-08-20','buckets':{'actionable':[candidate('A')]},
               'candidates':[candidate('A')]}}, loader, '2026-08-27', windows=(5,), research_snapshot=research)
  by_code={item['code']:item for item in result['candidate_signal_items']}
  self.assertEqual(set(by_code), {'A','B','C'})
  self.assertEqual(by_code['A']['windows']['5']['status'],'complete')
  self.assertEqual(by_code['B']['windows']['5']['status'],'data_error')
  self.assertEqual(by_code['C']['windows']['5']['status'],'excluded')
  self.assertEqual(result['population_kind'],'frozen_investable_research_population')

 def test_failed_population_member_can_be_retried_in_same_v2_sidecar(self):
  candidate={'code':'B','ts_code':'B.SH','composite_score':80,
             'quality_adjusted_score':80,'data_quality':{'eligible':True}}
  research=build_research_snapshot([candidate], {'actionable':[]}, '2026-08-20', {}, {}, [], 50,
      official_tracking={'status':'created'}, model_version='test')
  days=['2026-08-20','2026-08-21','2026-08-22','2026-08-25','2026-08-26','2026-08-27']
  calls=[]
  def loader(code,candidate,*args):
   calls.append(len(calls))
   if len(calls)==1: raise RuntimeError('retryable')
   return {'market_sessions':days,
    'stock_rows':[{'date':d,'close':10+i,'low':10+i} for i,d in enumerate(days)],
    'hs300_rows':[{'date':d,'close':100+i} for i,d in enumerate(days)],
    'stock_meta':{'adj':'qfq'}}
  snapshot={'content':{'recommendation_date':'2026-08-20','buckets':{'actionable':[]},'candidates':[]}}
  with tempfile.TemporaryDirectory() as root:
   first=track_attribution(snapshot,loader,'2026-08-27',root=root,windows=(5,),research_snapshot=research)
   second=track_attribution(snapshot,loader,'2026-08-27',root=root,windows=(5,),research_snapshot=research)
  self.assertEqual(first['candidate_signal_items'][0]['windows']['5']['status'],'data_error')
  self.assertEqual(second['candidate_signal_items'][0]['windows']['5']['status'],'complete')

 def test_cutoff_uses_versioned_evaluation_path(self):
  snapshot={'content':{'recommendation_date':'2026-08-20','buckets':{'actionable':[]},'candidates':[]}}
  with tempfile.TemporaryDirectory() as root:
   result=track_attribution(snapshot,lambda code,candidate: {},'2026-08-27',root=root,windows=(5,))
   path=Path(root)/result['evaluation_contract']['contract_id']/'v2'/'2026-08-27'/'2026-08-20.json'
   self.assertTrue(path.exists())
   self.assertEqual(result['evaluation_identity']['evaluation_as_of'],'2026-08-27')
   self.assertEqual(result['evaluation_contract']['evaluation_version'],'v2')
   self.assertIn('input_manifest', result)

 def test_research_link_gap_does_not_emit_legacy_signal_population(self):
  snapshot={'content':{'recommendation_date':'2026-08-20',
                       'buckets':{'actionable':[]},'candidates':[]}}
  result=track_attribution(snapshot,lambda code,candidate: {},'2026-08-27',windows=(5,),
                           research_link_status='mismatch')
  self.assertEqual(result['candidate_signal_items'],[])
  self.assertEqual(result['population_kind'],'research_population_gap')
  self.assertEqual(result['research_link_status'],'mismatch')

 def test_primary_window_deduplicates_overlapping_same_code_events(self):
  def event(day, entry, exit):
   return {'recommendation_date':day,'code':'X','windows':{
    '5':{'status':'complete','net_return':.01},
    '10':{'status':'complete','net_return':.02},
    '20':{'status':'complete','net_return':.03,'entry_date':entry,'exit_date':exit}}}
  summary=summarize_attribution([
   event('2026-08-20','2026-08-21','2026-09-17'),
   event('2026-08-28','2026-08-31','2026-09-25'),
  ],minimum_dates=20,minimum_mature=1)
  self.assertEqual(summary['primary_window'],20)
  self.assertEqual(summary['raw_mature_records'],2)
  self.assertEqual(summary['deduplicated_mature_events'],1)
  self.assertEqual(summary['duplicate_primary_records'],1)
  self.assertEqual(summary['mature_observations'],1)
  self.assertAlmostEqual(summary['mean_net_return'],.03)
  self.assertEqual(summary['by_window']['5']['mature_observations'],2)

 def test_candidate_research_readiness_is_not_blocked_by_trade_plan(self):
  candidate={'status':'ready'}
  trade={'status':'evidence_insufficient'}
  readiness=calibration_readiness(candidate,trade)
  self.assertEqual(readiness['status'],'eligible_for_walk_forward_review')
  self.assertTrue(readiness['candidate_signal_ready'])
  self.assertFalse(readiness['trade_simulation_ready'])

 def test_contract_change_writes_a_separate_sidecar_directory(self):
  snapshot={'content':{'recommendation_date':'2026-08-20','buckets':{'actionable':[]},'candidates':[]}}
  with tempfile.TemporaryDirectory() as root:
   track_attribution(snapshot,lambda code,candidate: {},'2026-08-27',root=root,windows=(5,10,20,60))
   track_attribution(snapshot,lambda code,candidate: {},'2026-08-27',root=root,
                     cost_model=CostModel(sell_tax_bps=10),windows=(5,10,20,60))
   directories=[path for path in Path(root).iterdir() if path.is_dir()]
   self.assertEqual(len(directories),2)

 def test_stop_path_uses_one_gross_return(self):
  days=['2026-08-20','2026-08-21','2026-08-22','2026-08-25','2026-08-26','2026-08-27']
  rows=self._production_rows(days)
  result=evaluate_recommendation(
   {'recommendation_date':'2026-08-20','code':'X','trade_plan':self._plan()},
   '2026-08-27',days,rows,stock_meta={'adj':'qfq'},
   cost_model=CostModel(sell_commission_bps=10),windows=(5,))
  window=result['windows']['5']
  self.assertEqual(window['exit_reason'],'stop')
  self.assertEqual(window['gross_return'],window['plan_path_return'])
  self.assertAlmostEqual(window['net_return'],window['gross_return']-.001)

 def test_limit_rule_uses_security_board_not_fixed_95_percent(self):
  days=['2026-08-20','2026-08-21']
  rows=[{'date':days[0],'open':10,'high':10,'low':10,'close':10,'vol':1},
        {'date':days[1],'open':11,'high':11,'low':11,'close':11,'vol':1}]
  plan=self._plan()
  main=resolve_entry(plan, days[0], days, rows, code='600000')
  growth=resolve_entry(plan, days[0], days, rows, code='300001')
  self.assertEqual(main['reason'],'t1_one_price_limit_up')
  self.assertEqual(growth['status'],'executable')

 def test_unknown_one_price_rule_is_not_assumed_executable(self):
  days=['2026-08-20','2026-08-21']
  rows=[{'date':days[0],'open':10,'high':10,'low':10,'close':10,'vol':1},
        {'date':days[1],'open':11,'high':11,'low':11,'close':11,'vol':1}]
  result=resolve_entry(self._plan(), days[0], days, rows, code='X')
  self.assertEqual(result['reason'],'t1_limit_rule_unknown')

 def test_t_plus_one_and_gap_stop_are_conservative(self):
  days=['2026-08-20','2026-08-21','2026-08-22','2026-08-25','2026-08-26','2026-08-27']
  rows=[{'date':days[0],'open':11,'high':12,'low':8,'close':9,'vol':1},
        {'date':days[1],'open':11,'high':12,'low':10,'close':11,'vol':1},
        {'date':days[2],'open':8,'high':9,'low':7,'close':8,'vol':1},
        *[{'date':d,'open':8,'high':9,'low':7,'close':8,'vol':1} for d in days[3:]]]
  result=evaluate_recommendation({'recommendation_date':days[0],'code':'600000','trade_plan':self._plan()},
   days[-1],days,rows,stock_meta={'adj':'qfq'},windows=(5,))
  window=result['windows']['5']
  self.assertEqual(window['exit_date'],days[2])
  self.assertEqual(window['exit_reason'],'stop')
  self.assertEqual(window['plan_path_return'],8/11-1)
  self.assertTrue(window['execution_assumptions']['t_plus_one_sale'])

 def test_sidecar_merge_updates_run_metadata(self):
  old={'evaluator_version':'recommendation-attribution/v1','evaluation_as_of':'2026-08-25',
       'cost_model':{'sell_tax_bps':10},'items':[{'code':'X','evaluation_as_of':'2026-08-25',
       'windows':{'5':{'status':'pending'}}}]}
  new={'evaluator_version':'recommendation-attribution/v1','evaluation_as_of':'2026-08-27',
       'cost_model':{'sell_tax_bps':10},'items':[{'code':'X','evaluation_as_of':'2026-08-27',
       'execution':{'status':'executable'},'windows':{'5':{'status':'complete'}}}]}
  merged=merge_attribution(old,new)
  self.assertEqual(merged['evaluation_as_of'],'2026-08-27')
  self.assertEqual(merged['cost_model']['sell_tax_bps'],10)
  self.assertEqual(merged['items'][0]['evaluation_as_of'],'2026-08-27')
  self.assertEqual(merged['items'][0]['execution']['status'],'executable')

 def test_sidecar_merge_rejects_mixed_cost_models(self):
  old={'cost_model':{'sell_tax_bps':0},'items':[{'code':'X','windows':{}}]}
  new={'cost_model':{'sell_tax_bps':10},'items':[{'code':'X','windows':{}}]}
  with self.assertRaises(ValueError): merge_attribution(old,new)

 def test_corrupt_sidecar_is_not_treated_as_empty(self):
  with tempfile.TemporaryDirectory() as root:
   path=Path(root)/'2026-08-20.json'; path.write_text('{bad',encoding='utf-8')
   with self.assertRaises(ValueError): read_sidecar(path)
   self.assertEqual(path.read_text(encoding='utf-8'),'{bad')

 def test_history_limit_uses_latest_official_snapshots(self):
  snapshots=[{'content':{'recommendation_date':f'2026-08-{day:02d}','buckets':{'actionable':[]}}}
             for day in (18,19,20)]
  with tempfile.TemporaryDirectory() as root:
   with mock.patch('analysis.recommendation_attribution.iter_official_snapshots', return_value=(snapshots, [])):
    result=track_official_history(
     history_root=root, attribution_root=Path(root)/'attr',
     evaluation_as_of='2026-08-27', history=2)
   self.assertEqual(result['summary']['snapshots'],2)

def run_recommendation_attribution_tests():
 suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
 result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
 return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)
if __name__=='__main__':unittest.main()
