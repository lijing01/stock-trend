import copy
import unittest,sys,tempfile,json
from unittest import mock
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
from analysis.recommendation_attribution import *
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
  self.assertEqual(merged['items'][0]['windows']['10']['status'],'pending')

 def _plan(self):
  return {'entry':{'low':10,'high':12},'stop_loss':{'price':9},'target':{'price':20}}

 def _production_rows(self, days):
  return [{'trade_date':d,'open':11,'high':12,'low':8 if i == 1 else 10,
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
