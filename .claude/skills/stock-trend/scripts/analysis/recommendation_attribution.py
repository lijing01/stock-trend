import argparse, copy, dataclasses, json, math, os, tempfile
from datetime import date
from pathlib import Path
WINDOWS=(5,10,20,60); EVALUATOR_VERSION='recommendation-attribution/v1'
@dataclasses.dataclass(frozen=True)
class CostModel:
 buy_commission_bps:float=0; sell_commission_bps:float=0; buy_slippage_bps:float=0; sell_slippage_bps:float=0; sell_tax_bps:float=0
 def __post_init__(self):
  if any(not math.isfinite(x) or x<0 for x in dataclasses.astuple(self)): raise ValueError('cost bps must be finite and non-negative')
 @property
 def mode(self):
  return 'gross' if not any(dataclasses.astuple(self)) else 'explicit_cost'
def _dt(x): return date.fromisoformat(str(x)[:10])
def _row_date(row): return str(row.get('date',row.get('trade_date','')))[:10]
def _number(row,key,default=None):
 try:
  value=float(row.get(key,default)); return value if math.isfinite(value) else default
 except (TypeError,ValueError): return default
def _oneup(r): return float(r.get('pct_chg',0) or 0)>=9.5 and len({float(r.get(k,0) or 0) for k in ('open','high','low','close')})==1
def _ret(a,b): return None if a in (None,0) or b is None else b/a-1
def resolve_entry(plan, recommendation_date, market_sessions, stock_rows):
 sessions=sorted(str(x)[:10] for x in market_sessions if str(x)[:10]>recommendation_date)
 if not sessions:return {'status':'data_error','reason':'market_calendar_missing'}
 d=sessions[0]; rows={_row_date(r):r for r in stock_rows}; r=rows.get(d)
 if not r or _number(r,'vol',_number(r,'volume',0))<=0:return {'status':'unexecutable','reason':'t1_suspended','date':d}
 if _oneup(r):return {'status':'unexecutable','reason':'t1_one_price_limit_up','date':d}
 e=plan.get('entry',{}); low=_number(e,'low',_number(e,'price')); high=_number(e,'high',low); stop=_number(plan.get('stop_loss',{}),'price',-math.inf)
 if low is None or high is None or stop is None:return {'status':'data_error','reason':'trade_plan_invalid','date':d}
 if _number(r,'open',0)<stop:return {'status':'unexecutable','reason':'t1_open_below_stop','date':d}
 if _number(r,'low',0)>high or _number(r,'high',0)<low:return {'status':'unexecutable','reason':'t1_entry_zone_not_reached','date':d}
 return {'status':'executable','date':d,'price':min(high,max(low,_number(r,'open',low)))}
def evaluate_recommendation(recommendation,evaluation_as_of,market_sessions,stock_rows,hs300_rows=None,sector_rows=None,cost_model=None,windows=WINDOWS):
 c=cost_model or CostModel(); content=recommendation.get('content',recommendation); rd=content.get('recommendation_date'); cand=recommendation.get('candidate',recommendation)
 if not cand and content.get('candidates'): cand=content['candidates'][0]
 plan=cand.get('trade_plan') or {}; sessions=sorted(str(x)[:10] for x in market_sessions if rd<str(x)[:10]<=evaluation_as_of)
 if not sessions:
  return {'evaluator_version':EVALUATOR_VERSION,'recommendation_date':rd,'code':cand.get('code',''),'evaluation_as_of':evaluation_as_of,'execution':{'status':'pending','reason':'evaluation_cutoff_before_t1'},'cost_model':dataclasses.asdict(c),'windows':{str(w):{'status':'pending','required_session':w} for w in windows}}
 ex=resolve_entry(plan,rd,market_sessions,stock_rows) if plan else {'status':'unexecutable','reason':'trade_plan_missing'}
 result={'evaluator_version':EVALUATOR_VERSION,'recommendation_date':rd,'code':cand.get('code',''),'evaluation_as_of':evaluation_as_of,'execution':ex,'cost_model':dataclasses.asdict(c),'windows':{}}
 if ex.get('status')!='executable':
  for w in windows: result['windows'][str(w)]={'status':ex.get('status'),'reason':ex.get('reason')}
  return result
 entry=ex['date']; rows={_row_date(r):r for r in stock_rows}; entryrow=rows[entry]; ep=ex['price']
 close0=_number(entryrow,'close',ep); stop=_number(plan.get('stop_loss',{}),'price',-math.inf)
 target=_number(plan.get('targets',{}),'primary',_number(plan.get('target',{}),'price',math.inf))
 for w in windows:
  future=[d for d in sorted(str(x)[:10] for x in market_sessions) if d>=entry and d<=evaluation_as_of]
  if len(future)<w: result['windows'][str(w)]={'status':'pending','required_session':w}; continue
  path=future[:w]; mark=rows.get(path[-1]); prev=close0; mfe=-math.inf; mae=math.inf; exit_reason=None; exit_date=None; exit_price=None; carried=False
  for d in path:
   r=rows.get(d)
   if not r or _number(r,'vol',_number(r,'volume',0))<=0:
    carried=True; continue
   lo=_number(r,'low',prev); hi=_number(r,'high',prev); cl=_number(r,'close',prev); mfe=max(mfe,_ret(ep,hi) or 0); mae=min(mae,_ret(ep,lo) or 0)
   if exit_reason is None and lo<=stop: exit_reason='stop'; exit_date=d; exit_price=stop
   elif exit_reason is None and hi>=target: exit_reason='target'; exit_date=d; exit_price=target
   prev=cl
  mtm=_ret(ep,_number(mark,'close',prev) if mark else prev)
  path_return=_ret(ep,exit_price) if exit_price is not None else mtm
  cost_bps=sum(dataclasses.astuple(c))
  net=(path_return if path_return is not None else 0)-(cost_bps/10000)
  item={'status':'complete','mark_to_market_return':mtm,'plan_path_return':path_return,'gross_return':mtm,'net_return':net,'mfe':mfe if mfe!=-math.inf else None,'mae':mae if mae!=math.inf else None,'exit_reason':exit_reason,'exit_date':exit_date,'carried_suspension':carried}
  for label,series in (('hs300',hs300_rows),('sector',sector_rows)):
   if series:
    bm={str(r.get('date',''))[:10]:r for r in series}; a=bm.get(entry); z=bm.get(path[-1]); br=_ret(float(a.get('close')),float(z.get('close'))) if a and z else None
    item[label+'_return']=br; item[label+'_alpha']=mtm-br if br is not None else None
   else: item[label+'_return']=item[label+'_alpha']=None
  result['windows'][str(w)]=item
 return result

def merge_attribution(existing,incoming):
 if not existing:return copy.deepcopy(incoming)
 out=copy.deepcopy(existing)
 if 'items' in incoming or 'items' in out:
  old={str(x.get('code')):x for x in out.setdefault('items',[])}
  for item in incoming.get('items',[]):
   code=str(item.get('code')); previous=old.get(code)
   old[code]=merge_attribution(previous,item) if previous else copy.deepcopy(item)
  out['items']=[old[k] for k in sorted(old)]
  return out
 for ni,nw in incoming.get('windows',{}).items():
  ow=out.setdefault('windows',{}).get(ni)
  if not ow or ow.get('status') in ('pending','data_error'):
   out['windows'][ni]=copy.deepcopy(nw)
 return out
def write_sidecar(payload,path):
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.tmp-'); os.close(fd)
 try:
  with open(tmp,'w') as f: json.dump(payload,f,ensure_ascii=False,sort_keys=True); f.flush(); os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  try: os.unlink(tmp)
  except OSError: pass
 return path
def read_sidecar(path):
 try:
  with open(path,encoding='utf-8') as f:return json.load(f)
 except (OSError,ValueError):return None
def sidecar_path(root,recommendation_date):
 return Path(root)/(str(recommendation_date)[:10]+'.json')
def summarize_attribution(items, minimum_dates=20, minimum_mature=100):
 completed=[]; pending=unexecutable=errors=0
 for item in items:
  for window in (item.get('windows') or {}).values():
   status=window.get('status')
   if status=='complete': completed.append(window)
   elif status=='pending': pending+=1
   elif status=='unexecutable': unexecutable+=1
   elif status=='data_error': errors+=1
 mature=len(completed)
 out={'official_dates':minimum_dates,'mature_observations':mature,
      'pending':pending,'unexecutable':unexecutable,'errors':errors,
      'status':'evidence_insufficient' if minimum_dates<20 or mature<100 else 'ready'}
 if completed:
  vals=[x.get('net_return') for x in completed if x.get('net_return') is not None]
  out['mean_net_return']=sum(vals)/len(vals) if vals else None
 return out
def track_attribution(snapshot, series_loader, evaluation_as_of, root=None,
                      cost_model=None, windows=WINDOWS):
 """Evaluate one immutable snapshot and merge its mutable date sidecar.

 ``series_loader`` receives ``(code, candidate)`` and returns a mapping with
 market_sessions, stock_rows, hs300_rows and sector_rows.  Keeping the loader
 injectable makes the evaluator deterministic and lets callers isolate one
 provider failure without losing other candidates.
 """
 content=snapshot.get('content',snapshot); buckets=content.get('buckets') or {}
 candidates=[]
 for item in buckets.get('actionable',[]):
  candidates.append(item)
 results=[]
 for candidate in candidates:
  try:
   series=series_loader(candidate.get('code'),candidate) or {}
   result=evaluate_recommendation(
    {'recommendation_date':content['recommendation_date'],'candidate':candidate},
    evaluation_as_of, series.get('market_sessions',[]),
    series.get('stock_rows',[]), series.get('hs300_rows'),
    series.get('sector_rows'), cost_model=cost_model, windows=windows)
  except Exception as exc:
   result={'evaluator_version':EVALUATOR_VERSION,'recommendation_date':content['recommendation_date'],
           'code':candidate.get('code',''),'evaluation_as_of':evaluation_as_of,
           'execution':{'status':'data_error','reason':type(exc).__name__},
           'windows':{str(w):{'status':'data_error','reason':type(exc).__name__} for w in windows}}
  results.append(result)
 payload={'evaluator_version':EVALUATOR_VERSION,
          'recommendation_date':content['recommendation_date'],
          'evaluation_as_of':evaluation_as_of,'items':results}
 if root is not None:
  path=sidecar_path(root,content['recommendation_date'])
  prior=read_sidecar(path)
  payload=merge_attribution(prior,payload) if prior else payload
  write_sidecar(payload,path)
 return payload
def main(argv=None):
 p=argparse.ArgumentParser(); p.add_argument('--through'); p.add_argument('--history',type=int,default=120); p.add_argument('--windows',default='5,10,20,60'); p.add_argument('--json',action='store_true'); p.add_argument('--buy-commission-bps',type=float,default=0); p.add_argument('--sell-commission-bps',type=float,default=0); p.add_argument('--buy-slippage-bps',type=float,default=0); p.add_argument('--sell-slippage-bps',type=float,default=0); p.add_argument('--sell-tax-bps',type=float,default=0); args=p.parse_args(argv)
 out={'evaluator_version':EVALUATOR_VERSION,'through':args.through,'windows':[int(x) for x in args.windows.split(',')],'status':'evidence_insufficient'}
 print(json.dumps(out,ensure_ascii=False) if args.json else 'evidence_insufficient'); return 0
if __name__=='__main__': main()
