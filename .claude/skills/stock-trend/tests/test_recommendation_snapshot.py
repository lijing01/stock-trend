import copy, tempfile, unittest, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
from core.recommendation_snapshot import *
def src():
 c={'code':'X','trade_plan_status':'complete','data_quality':{'kline':{'data_date':'2026-08-20'}},
    'trade_plan':{'basis_date':'2026-08-20','validity':{'valid_until':'2026-08-25'}}}
 return {'recommendation_date':'2026-08-20','generated_at':'2026-08-20T15:00:00','snapshot_type':'formal','model_version':'v1','policy':{'provisional':False},'market_regime':{'data_date':'2026-08-20'},'sectors':[],'candidates':[c],'buckets':{'actionable':[c],'waiting_trigger':[],'next_day_confirmation':[],'observation':[]},'scan_status':'complete'}
class T(unittest.TestCase):
 def test_build_detached(self):
  s=src(); x=build_snapshot(s); s['candidates'][0]['code']='Y'; self.assertEqual(x['content']['candidates'][0]['code'],'X'); self.assertEqual(x['content_sha256'],content_sha256(x['content']))
 def test_write_idempotent_conflict(self):
  with tempfile.TemporaryDirectory() as d:
   x=build_snapshot(src()); self.assertEqual(save_official_snapshot(x,d).status,'created'); self.assertEqual(save_official_snapshot(x,d).status,'unchanged'); y=copy.deepcopy(x); y['content']['model_version']='v2'; y['content_sha256']=content_sha256(y['content']); self.assertRaises(SnapshotConflict,save_official_snapshot,y,d)
 def test_provisional(self):
  s=src(); s['snapshot_type']='provisional'; s['policy']['provisional']=True
  with tempfile.TemporaryDirectory() as d:self.assertEqual(save_snapshot_if_official(s,d).status,'skipped_provisional')

 def test_future_plan_validity_is_not_future_evidence(self):
  snapshot=build_snapshot(src())
  self.assertEqual(snapshot['content']['candidates'][0]['trade_plan']['validity']['valid_until'],'2026-08-25')

 def test_save_rejects_invalid_unverified_snapshot(self):
  invalid={'content':{'recommendation_date':'2026-08-20'},'content_sha256':'bad'}
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(SnapshotValidationError): save_official_snapshot(invalid,d)

 def test_loader_rechecks_business_contract_after_digest_update(self):
  snapshot=build_snapshot(src())
  snapshot['content']['buckets'].pop('actionable')
  snapshot['content_sha256']=content_sha256(snapshot['content'])
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'2026-08-20.json'; path.write_text(json.dumps(snapshot),encoding='utf-8')
   with self.assertRaises(SnapshotValidationError): load_official_snapshot(path)

 def test_nonfinite_values_are_normalized_and_audited(self):
  s=src(); s['market_regime']['score']=float('nan')
  snapshot=build_snapshot(s)
  self.assertIsNone(snapshot['content']['market_regime']['score'])
  self.assertTrue(snapshot['normalization_warnings'])
  with tempfile.TemporaryDirectory() as d:
   result=save_official_snapshot(snapshot,d)
   self.assertEqual(result.status,'created')
   loaded=load_official_snapshot(Path(d)/'2026-08-20.json')
   self.assertIsNone(loaded['content']['market_regime']['score'])

 def test_runtime_telemetry_does_not_change_decision_digest(self):
  first=src(); first['candidates'][0]['fetched_at']='2026-08-20T15:00:00'
  first['candidates'][0]['cache_hits']=1
  second=copy.deepcopy(first)
  second['candidates'][0]['fetched_at']='2026-08-20T15:01:00'
  second['candidates'][0]['cache_hits']=99
  self.assertEqual(build_snapshot(first)['content_sha256'],
                   build_snapshot(second)['content_sha256'])

 def test_build_normalizes_compact_nested_dates(self):
  source = src()
  source['candidates'][0]['wyckoff'] = {'trigger_date': '20260820'}
  snapshot = build_snapshot(source)
  self.assertEqual(
      snapshot['content']['candidates'][0]['wyckoff']['trigger_date'],
      '2026-08-20',
  )

 def test_build_normalizes_compact_recommendation_date(self):
  source = src()
  source['recommendation_date'] = '20260820'
  snapshot = build_snapshot(source)
  self.assertEqual(snapshot['content']['recommendation_date'],
                   '2026-08-20')

 def test_build_rejects_impossible_compact_date(self):
  source = src()
  source['candidates'][0]['wyckoff'] = {'trigger_date': '20260230'}
  with self.assertRaises(SnapshotValidationError):
   build_snapshot(source)

 def test_build_allows_missing_optional_date_value(self):
  source = src()
  source['candidates'][0]['data_quality']['capital'] = {'data_date': ''}
  snapshot = build_snapshot(source)
  self.assertEqual(
      snapshot['content']['candidates'][0]['data_quality']['capital']['data_date'],
      '',
  )

def run_recommendation_snapshot_tests():
 suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
 result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
 return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)
if __name__=='__main__':unittest.main()
