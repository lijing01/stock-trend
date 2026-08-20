import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'scripts'))
from analysis.recommendation_attribution import *
class T(unittest.TestCase):
 def test_cost(self): self.assertEqual(CostModel().mode, 'gross')
 def test_entry_and_pending(self):
  plan={'entry':{'low':10,'high':12},'stop_loss':{'price':8},'target':{'price':15}}
  rec={'recommendation_date':'2026-08-20','code':'X','trade_plan':plan}; days=[f'2026-08-{d:02d}' for d in range(20,30)]; rows=[{'date':d,'open':11,'high':12,'low':10,'close':11+(i%3),'vol':1} for i,d in enumerate(days)]
  x=evaluate_recommendation(rec,'2026-08-24',days,rows,windows=(5,)); self.assertEqual(x['windows']['5']['status'],'pending')

def run_recommendation_attribution_tests():
 suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
 result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
 return result.testsRun - len(result.failures) - len(result.errors), len(result.failures) + len(result.errors)
if __name__=='__main__':unittest.main()
