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

def run_recommendation_snapshot_tests():
 suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
 result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
 return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)
if __name__=='__main__':unittest.main()
