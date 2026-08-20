import copy, hashlib, json, os, tempfile
from datetime import date
from pathlib import Path
from .cache_utils import CACHE_DIR

SCHEMA_VERSION = 'recommendation-snapshot/v1'
DEFAULT_ROOT = Path(CACHE_DIR) / 'recommendation_history'
class SnapshotValidationError(ValueError): pass
class SnapshotConflict(RuntimeError): pass
class SnapshotResult:
    def __init__(self, status, path=None, content_sha256=None):
        self.status = status
        self.path = path
        self.content_sha256 = content_sha256
def canonical_json(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
def content_sha256(v): return hashlib.sha256(canonical_json(v)).hexdigest()
def _d(s):
    try: return date.fromisoformat(s)
    except Exception: raise SnapshotValidationError('invalid recommendation_date')
def _validate(src):
    if not isinstance(src, dict): raise SnapshotValidationError('source must be object')
    rd=src.get('recommendation_date'); _d(rd)
    if not src.get('generated_at') or not src.get('model_version'):
        raise SnapshotValidationError('missing envelope metadata')
    if src.get('snapshot_type') not in ('formal','provisional'): raise SnapshotValidationError('invalid snapshot_type')
    pol=src.get('policy') or {}
    if src.get('snapshot_type')=='formal' and pol.get('provisional'): raise SnapshotValidationError('formal provisional')
    for key in ('market_regime','sectors','candidates','buckets','scan_status'):
        if key not in src: raise SnapshotValidationError('missing '+key)
    if not isinstance(src['buckets'], dict): raise SnapshotValidationError('invalid buckets')
    required_buckets = ('actionable', 'waiting_trigger', 'next_day_confirmation', 'observation')
    if any(name not in src['buckets'] or not isinstance(src['buckets'][name], list)
           for name in required_buckets):
        raise SnapshotValidationError('invalid bucket contract')
    def dates(x, path=''):
        if isinstance(x, dict):
            for k,v in x.items():
                if isinstance(v,str) and ('date' in k.lower() or k in ('as_of','basis_date')):
                    try:
                        is_plan_date = 'trade_plan' in path or k in ('valid_until', 'event_date') and 'plan' in path
                        if not is_plan_date and _d(v)>_d(rd):
                            raise SnapshotValidationError('future evidence: '+path+k)
                    except SnapshotValidationError: raise
                    except Exception: pass
                dates(v,path+k+'.')
        elif isinstance(x,list):
            for i,v in enumerate(x): dates(v,path+str(i)+'.')
    dates(src)
    for c in (src.get('buckets') or {}).get('actionable',[]):
        if c.get('trade_plan_status') not in ('complete',): raise SnapshotValidationError('actionable trade plan incomplete')
def build_snapshot(source):
    s=copy.deepcopy(source); _validate(s)
    content={k:s[k] for k in ('recommendation_date','snapshot_type','model_version','policy','market_regime','sectors','candidates','buckets','scan_status')}
    return {'schema_version':SCHEMA_VERSION,'generated_at':s.get('generated_at',''),'content_sha256':content_sha256(content),'content':content}
def save_official_snapshot(snapshot, root=DEFAULT_ROOT):
    _validate_snapshot_envelope(snapshot)
    root=Path(root); root.mkdir(parents=True,exist_ok=True); c=snapshot['content']; target=root/(c['recommendation_date']+'.json')
    payload=canonical_json(snapshot)+b'\n'; fd,tmp=tempfile.mkstemp(dir=root,prefix='.tmp-'); os.close(fd)
    try:
        with open(tmp,'wb') as f: f.write(payload); f.flush(); os.fsync(f.fileno())
        try: os.link(tmp,target)
        except FileExistsError:
            old=load_official_snapshot(target)
            if old['content_sha256']==snapshot['content_sha256']:
                return SnapshotResult('unchanged', target, snapshot['content_sha256'])
            raise SnapshotConflict(str(target))
        try:
            dir_fd = os.open(root, os.O_RDONLY)
            try: os.fsync(dir_fd)
            finally: os.close(dir_fd)
        except OSError:
            pass
        return SnapshotResult('created', target, snapshot['content_sha256'])
    finally:
        try: os.unlink(tmp)
        except OSError: pass
def save_snapshot_if_official(source, root=DEFAULT_ROOT):
    if source.get('snapshot_type')=='provisional' or (source.get('policy') or {}).get('provisional'):
        return SnapshotResult('skipped_provisional')
    return save_official_snapshot(build_snapshot(source),root)
def load_official_snapshot(path):
    p=Path(path); obj=json.loads(p.read_text());
    _validate_snapshot_envelope(obj)
    if p.stem != obj.get('content',{}).get('recommendation_date'): raise SnapshotValidationError('filename mismatch')
    return obj

def _validate_snapshot_envelope(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('schema_version') != SCHEMA_VERSION:
        raise SnapshotValidationError('unknown schema')
    content = snapshot.get('content')
    if not isinstance(content, dict):
        raise SnapshotValidationError('missing content')
    if content_sha256(content) != snapshot.get('content_sha256'):
        raise SnapshotValidationError('digest mismatch')
    source = copy.deepcopy(content)
    source['generated_at'] = snapshot.get('generated_at')
    _validate(source)
def iter_official_snapshots(root=DEFAULT_ROOT, through_date=None):
    out=[]; rejected=[]
    for p in sorted(Path(root).glob('*.json')):
        try:
            o=load_official_snapshot(p)
            if through_date and o['content']['recommendation_date']>through_date: continue
            out.append(o)
        except Exception as e: rejected.append({'path':str(p),'error':str(e)})
    return out,rejected
