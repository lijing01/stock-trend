import copy, tempfile, unittest, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
from core.recommendation_snapshot import *
def src():
 c={'code':'X','trade_plan_status':'complete','data_quality':{'kline':{'data_date':'2026-08-20'}}}
 return {'recommendation_date':'2026-08-20','generated_at':'2026-08-20T15:00:00','snapshot_type':'formal','model_version':'v1','policy':{'provisional':False},'market_regime':{'data_date':'2026-08-20'},'sectors':[],'candidates':[c],'buckets':{'actionable':[c]},'scan_status':'complete'}
class T(unittest.TestCase):
 def test_build_detached(self):
  s=src(); x=build_snapshot(s); s['candidates'][0]['code']='Y'; self.assertEqual(x['content']['candidates'][0]['code'],'X'); self.assertEqual(x['content_sha256'],content_sha256(x['content']))
 def test_write_idempotent_conflict(self):
  with tempfile.TemporaryDirectory() as d:
   x=build_snapshot(src()); self.assertEqual(save_official_snapshot(x,d).status,'created'); self.assertEqual(save_official_snapshot(x,d).status,'unchanged'); y=copy.deepcopy(x); y['content']['model_version']='v2'; y['content_sha256']=content_sha256(y['content']); self.assertRaises(SnapshotConflict,save_official_snapshot,y,d)
 def test_provisional(self):
  s=src(); s['snapshot_type']='provisional'; s['policy']['provisional']=True
  with tempfile.TemporaryDirectory() as d:self.assertEqual(save_snapshot_if_official(s,d).status,'skipped_provisional')

def run_recommendation_snapshot_tests():
 suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
 result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
 return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)
if __name__=='__main__':unittest.main()
