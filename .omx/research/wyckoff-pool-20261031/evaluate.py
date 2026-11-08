"""Offline descriptive audit; no trading simulation or production mutation."""
import json, csv, hashlib, statistics
from pathlib import Path
from collections import defaultdict,Counter
ROOT=Path.cwd(); CACHE=ROOT/'.cache/stock-trend'; OUT=ROOT/'.omx/research/wyckoff-pool-20261031'
def date(x):return str(x).replace('-','')[:8]
runs=defaultdict(list)
for p in sorted((CACHE/'recommendation_history').glob('**/*.json')):
 d=json.loads(p.read_text()); s=d.get('snapshot',d);c=s.get('content',{});day=c.get('recommendation_date',''); stamp=s.get('generated_at','')
 if not ('2026-10-12'<=day<='2026-10-30') or stamp[:10]!=day or stamp[11:16]<'15:00' or c.get('snapshot_type')!='formal':continue
 if not c.get('buckets',{}).get('observation'):continue
 runs[day].append((stamp,p,s))
cal=json.loads((CACHE/'trading_calendar.json').read_text())
# use explicit weekdays from frozen weak-to-strong authoritative calendar
ix=json.loads(sorted((CACHE/'weak_to_strong/run_index/daily/2026-10-30').glob('*.json'))[-1].read_text())
frozen=json.loads((CACHE/'weak_to_strong/daily/2026-10-30'/ix['snapshot_name']).read_text())
calendar=[date(x) for x in frozen['calendar']['trading_dates'] if date(x)<='20261030']; rows=[]; manifests=[]
for day, choices in sorted(runs.items()):
 stamp,p,s=max(choices,key=lambda x:x[0]);items=sorted(s['content']['buckets']['observation'],key=lambda x:(-float(x.get('execution_priority_score',x.get('quality_adjusted_score',0)) or 0),x['code']))
 manifests.append({'date':day,'generated_at':stamp,'path':str(p.relative_to(ROOT)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'rows':len(items),'model':s['content'].get('model_version')})
 for rank,x in enumerate(items,1):
  w=x.get('wyckoff',{});st=w.get('short_term',{});t=w.get('entry_timing',{});h=w.get('event_health',{});ce=w.get('confirmed_event',{});code=x['code'];ts=x.get('ts_code') or code+('.SH' if code.startswith('6') else '.SZ');kp=CACHE/f'kline_{ts}_D_qfq.json'
  r={'date':day,'rank':rank,'code':code,'name':x['name'],'phase':st.get('phase'),'sub_phase':st.get('sub_phase'),'level':x.get('buy_point_level'),'score':x.get('execution_priority_score'),'wyckoff_score':w.get('score'),'confidence':st.get('confidence'),'age':st.get('signal_age_bars'),'state':st.get('current_state'),'timing':t.get('status'),'timing_score':t.get('entry_timing_score'),'extension_atr':t.get('trigger_extension_atr'),'long_phase':w.get('long_term',{}).get('phase'),'alignment':w.get('alignment',{}).get('status'),'event_date':st.get('event_date'),'confirmation_date':st.get('confirmation_date'),'range_id':w.get('signal',{}).get('range_id'),'variant':ce.get('variant'),'cache_path':str(kp.relative_to(ROOT))}
  bars={};
  if kp.exists():
   k=json.loads(kp.read_text());bars={date(b['trade_date']):b for b in k.get('data',[])};r['kline_sha256']=hashlib.sha256(kp.read_bytes()).hexdigest();r['kline_fetched']=k.get('meta',{}).get('fetch_time')
  di=date(day);r['outcome_status']='missing_signal_bar'
  if di in bars and di in calendar:
   i=calendar.index(di);close=bars[di]['close'];r['signal_close']=close;r['outcome_status']='available'
   for n in [1,3,5,10]:
    days=calendar[i+1:i+n+1]
    if len(days)!=n or any(d not in bars for d in days):continue
    r[f'return_{n}']=100*(bars[days[-1]]['close']/close-1)
    r[f'mae_{n}']=min(0,100*(min(bars[d]['low'] for d in days)/close-1))
    r[f'mfe_{n}']=max(0,100*(max(bars[d]['high'] for d in days)/close-1))
  rows.append(r)
def stats(rr):
 out={'rows':len(rr),'stocks':len({r['code'] for r in rr})}
 for n in [1,3,5,10]:
  a=[r for r in rr if f'return_{n}' in r];v=[r[f'return_{n}'] for r in a]
  out[str(n)]={'n':len(v),'dates':len({r['date'] for r in a}),'mean':round(statistics.mean(v),3) if v else None,'median':round(statistics.median(v),3) if v else None,'positive_pct':round(100*sum(z>0 for z in v)/len(v),1) if v else None,'mean_mae':round(statistics.mean(r[f'mae_{n}'] for r in a),3) if a else None}
 return out
# Approximate first observed event, not canonical cycle identity (rolling range ids may move).
seen=set();first=[]
for r in rows:
 key=(r['code'],r['sub_phase'],r['event_date'],r['confirmation_date'])
 if key not in seen:seen.add(key);first.append(r)
summary={'window':'2026-10-12..2026-10-30','cutoff':'2026-10-30','manifest':manifests,'all_rows':stats(rows),'approx_first_events':stats(first)}
for label,fn in [('sub_phase',lambda r:r['sub_phase'] or 'none'),('rank',lambda r:'top10' if r['rank']<=10 else '11-30'),('timing',lambda r:r['timing'] or 'unknown'),('alignment',lambda r:r['alignment'] or 'unknown'),('age',lambda r:'unknown' if r['age'] is None else '0' if r['age']==0 else '1-3' if r['age']<=3 else '4+'),('extension',lambda r:'unknown' if r['extension_atr'] is None else '<=0.5' if r['extension_atr']<=.5 else '0.5-1.5' if r['extension_atr']<=1.5 else '>1.5')]:
 groups=defaultdict(list)
 for r in first:groups[str(fn(r))].append(r)
 summary[label]={k:stats(v) for k,v in groups.items()}
fields=list(dict.fromkeys(k for r in rows for k in r))
with (OUT/'rows.csv').open('w') as f:
 w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
(OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
