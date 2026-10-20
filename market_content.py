"""公共资讯独立缓存和不可变版本；不使用模拟银行时钟或账户库。"""
from __future__ import annotations
import json, os, sqlite3, hashlib, threading, uuid, re
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from decimal import Decimal
from market_sources import SOURCES, now_iso, REGISTRY, safe_url, readable_article, evidence_readable, source_brief_readable, source_verified_text_article, text_only_article, source_verified_text_event, source_verified_brief_event

CN=ZoneInfo('Asia/Shanghai')
SCHEMA=1
def packed(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,allow_nan=False)
def default_path():return Path(os.getenv('NAVREN_MARKET_DB',str(Path.home()/'Library/Application Support/Navren/market.sqlite3')))
def week_periods(count=2,now=None):
    now=(now or datetime.now(CN)).astimezone(CN)
    end=(now-timedelta(days=now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
    return [(end-timedelta(days=7*(i+1)),end-timedelta(days=7*i)) for i in range(count)]

def observation_change(start,end,kind,unit,identity):
    """行情端点必须相同身份、单位，利率百分点转基点，价格返回百分比。"""
    if not identity or not unit or start is None or end is None:raise ValueError('missing_observation')
    a,b=Decimal(str(start)),Decimal(str(end))
    if not a.is_finite() or not b.is_finite():raise ValueError('non_finite_observation')
    if kind=='rate' and unit=='percent':return {'value':str((b-a)*100),'unit':'basis_points','identity':identity}
    if kind in ('price','index','fx') and a>0:return {'value':str((b/a-1)*100),'unit':'percent','identity':identity}
    raise ValueError('unsupported_unit_or_kind')


WEEKLY_GENERATION_CONTRACT = 'weekly-generation-v3'

def _weekly_sha(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()

def _weekly_positive(value):
    return type(value) is int and value > 0

def weekly_materials(events):
    return [{k:v for k,v in e.items() if k in ('id','version','headline','summary','analysis','uncertainty','facts','published_at','identity','article_ids','article_versions')} for e in events]

def validate_weekly_event_inputs(events):
    if not isinstance(events,list) or not events:raise ValueError('no_weekly_evidence')
    seen=set(); articles={}
    for e in events:
        if not isinstance(e,dict) or not isinstance(e.get('id'),str) or not e['id'].strip() or e['id'] in seen:raise ValueError('invalid_weekly_event_identity')
        seen.add(e['id'])
        if not _weekly_positive(e.get('version')) or e.get('status')!='published':raise ValueError('invalid_weekly_event_version')
        ids=e.get('article_ids'); versions=e.get('article_versions')
        if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or not i.strip() for i in ids) or len(set(ids))!=len(ids):raise ValueError('invalid_weekly_article_identity')
        if not isinstance(versions,dict) or set(versions)!=set(ids) or any(not _weekly_positive(v) for v in versions.values()):raise ValueError('invalid_weekly_article_version')
        for i in ids:
            if i in articles and articles[i]!=versions[i]:raise ValueError('weekly_article_version_conflict')
            articles[i]=versions[i]
        if 'articles' in e:
            rows=e['articles']
            if not isinstance(rows,list) or len(rows)!=len(ids) or any(not isinstance(a,dict) for a in rows):raise ValueError('invalid_weekly_article_snapshot')
            if len({a.get('id') for a in rows})!=len(rows) or {a.get('id') for a in rows}!=set(ids):raise ValueError('invalid_weekly_article_snapshot')
            if any(not _weekly_positive(a.get('version')) or a['version']!=versions[a['id']] or a.get('status')!='active' for a in rows):raise ValueError('invalid_weekly_article_snapshot')
    return articles

def _weekly_generation_input(generation):
    return {k:generation[k] for k in ('contract','period_start','period_end_exclusive','event_bindings','article_bindings','source_permissions','materials_sha256')}

def validate_weekly_generation(generation, events, start, end):
    articles=validate_weekly_event_inputs(events)
    if not isinstance(generation,dict) or generation.get('contract')!=WEEKLY_GENERATION_CONTRACT:raise ValueError('missing_weekly_generation_snapshot')
    required={'contract','generation_id','started_at','period_start','period_end_exclusive','expected_weekly_version','event_bindings','article_bindings','source_permissions','materials_sha256','input_hash','snapshot_sha256'}
    if set(generation)!=required:raise ValueError('invalid_weekly_generation_schema')
    if not isinstance(generation['generation_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',generation['generation_id']):raise ValueError('invalid_weekly_generation_identity')
    if type(generation['expected_weekly_version']) is not int or generation['expected_weekly_version']<0:raise ValueError('invalid_weekly_head_version')
    if generation['period_start']!=start.isoformat() or generation['period_end_exclusive']!=end.isoformat():raise ValueError('weekly_generation_period_mismatch')
    if generation['materials_sha256']!=_weekly_sha(weekly_materials(events)):raise ValueError('weekly_generation_materials_mismatch')
    bindings=generation['event_bindings']
    if not isinstance(bindings,list) or len(bindings)!=len(events):raise ValueError('invalid_weekly_generation_events')
    for e,b in zip(events,bindings):
        if not isinstance(b,dict) or set(b)!={'event_id','event_version','article_ids','article_versions','event_body_sha256'}:raise ValueError('invalid_weekly_generation_events')
        if b['event_id']!=e['id'] or b['event_version']!=e['version'] or not _weekly_positive(b['event_version']) or b['article_ids']!=e['article_ids'] or b['article_versions']!=e['article_versions']:raise ValueError('weekly_generation_events_mismatch')
        if not isinstance(b['article_versions'],dict) or any(not _weekly_positive(v) for v in b['article_versions'].values()):raise ValueError('invalid_weekly_generation_article_version')
        if not isinstance(b['event_body_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',b['event_body_sha256']):raise ValueError('invalid_weekly_generation_events')
    abs_=generation['article_bindings']; permissions=generation['source_permissions']
    if not isinstance(abs_,list) or len(abs_)!=len(articles) or not isinstance(permissions,list) or not permissions:raise ValueError('invalid_weekly_generation_articles')
    seen=set(); source_ids=set()
    for a in abs_:
        if not isinstance(a,dict) or set(a)!={'article_id','version','article_body_sha256','source_id','permission_revision'}:raise ValueError('invalid_weekly_generation_articles')
        if a['article_id'] in seen or a['article_id'] not in articles or not _weekly_positive(a['version']) or a['version']!=articles[a['article_id']]:raise ValueError('weekly_generation_articles_mismatch')
        if not isinstance(a['source_id'],str) or not a['source_id'] or not _weekly_positive(a['permission_revision']) or not isinstance(a['article_body_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',a['article_body_sha256']):raise ValueError('invalid_weekly_generation_articles')
        seen.add(a['article_id']); source_ids.add(a['source_id'])
    permissions_by_source={}
    for p in permissions:
        if not isinstance(p,dict) or set(p)!={'source_id','enabled','revision','registry_sha256','definition_sha256'}:raise ValueError('invalid_weekly_generation_permissions')
        if not isinstance(p['source_id'],str) or p['source_id'] in permissions_by_source or p['enabled'] is not True or not _weekly_positive(p['revision']):raise ValueError('invalid_weekly_generation_permissions')
        if any(not isinstance(p[k],str) or not re.fullmatch(r'[0-9a-f]{64}',p[k]) for k in ('registry_sha256','definition_sha256')):raise ValueError('invalid_weekly_generation_permissions')
        permissions_by_source[p['source_id']]=p
    if set(permissions_by_source)!=source_ids or any(a['permission_revision']!=permissions_by_source[a['source_id']]['revision'] for a in abs_):raise ValueError('weekly_generation_permissions_mismatch')
    if generation['input_hash']!=_weekly_sha(_weekly_generation_input(generation)):raise ValueError('weekly_generation_input_hash_mismatch')
    snapshot={k:v for k,v in generation.items() if k not in ('snapshot_sha256','input_hash')}
    if generation['snapshot_sha256']!=_weekly_sha(snapshot):raise ValueError('weekly_generation_snapshot_hash_mismatch')
    return articles

def weekly_publication_sha(body):
    stable={k:v for k,v in body.items() if k not in ('generated_at','released_at','version','content_version','schema_version','publication_sha256','status')}
    if isinstance(stable.get('review'),dict):stable['review']={k:v for k,v in stable['review'].items() if k!='reviewed_at'}
    if isinstance(stable.get('reading'),list):stable['reading']=[{k:v for k,v in a.items() if k not in ('checked_at','fetched_at')} if isinstance(a,dict) else a for a in stable['reading']]
    return _weekly_sha(stable)


EVENT_GENERATION_CONTRACT = 'event-generation-v3'
SELECTION_ATTENTION_POLICY = 'daily-recency-v1-reviewed-attention'
EVENT_EDITOR_POLICY = 'event-editor-v4-reviewed-attention'
EVENT_REVIEW_TEXT_LIMIT = 14000

def event_materials(groups):
    return [{'event_id':g['id'],'articles':[{'id':a['id'],'version':a['version'],'title':a['title'],'publisher':a['publisher'],'origin_publisher':a.get('origin_publisher',a['publisher']),'identity':a['identity'],'published_at':a.get('published_at'),'read_scope':a['read_scope'],'text':a['evidence_text'][:EVENT_REVIEW_TEXT_LIMIT]} for a in g['articles']]} for g in groups]

def event_input_hash(group,mode='reviewed_ai',*,legacy=False):
    value={'editor':EVENT_GENERATION_CONTRACT,'mode':mode,'articles':[(a['id'],a['version'],a['fingerprint']) for a in group['articles']]}
    if not legacy:value['editor_policy']=EVENT_EDITOR_POLICY
    return _weekly_sha(value)

def event_primary_publication(body,articles):
    """Use the bound primary's first publication, never ingest/revision clocks."""
    primary=next((a for a in articles if a.get('id')==body.get('primary_article_id')),None)
    if primary is None:raise ValueError('event_primary_article_unbound')
    try:
        stamp=datetime.fromisoformat(primary['published_at'])
        if stamp.tzinfo is None:raise ValueError()
    except (KeyError,TypeError,ValueError):raise ValueError('event_primary_publication_invalid') from None
    return primary,stamp.astimezone(CN)

def selection_attention_time_context(body,articles,as_of):
    primary,published=event_primary_publication(body,articles)
    try:
        checked=datetime.fromisoformat(as_of)
        if checked.tzinfo is None:raise ValueError()
    except (TypeError,ValueError):raise ValueError('selection_attention_clock_invalid') from None
    return {'policy':SELECTION_ATTENTION_POLICY,'as_of':as_of,
        'primary_article_id':primary['id'],'first_published_at':primary['published_at'],
        'original_timezone':primary.get('original_timezone'),
        'published_precision':primary.get('published_precision'),
        'maximum_expires_at':(published+timedelta(hours=168)).isoformat()}

def validate_selection_attention(body,articles,*,mode='reviewed_ai'):
    """Validate reviewed record integrity; expiry is a Daily eligibility check."""
    attention=body.get('selection_attention')
    if attention is None:return None
    if mode!='reviewed_ai':raise ValueError('selection_attention_requires_independent_review')
    if (not isinstance(attention,dict) or set(attention)!={'important','reason','expires_at','evidence'}
            or attention['important'] is not True or not isinstance(attention['reason'],str)
            or not attention['reason'].strip() or len(attention['reason'])>400):raise ValueError('invalid_selection_attention')
    primary,published=event_primary_publication(body,articles)
    try:
        expires=datetime.fromisoformat(attention['expires_at'])
        if expires.tzinfo is None or not published<expires<=published+timedelta(hours=168):raise ValueError()
    except (TypeError,ValueError):raise ValueError('invalid_selection_attention_expiry') from None
    evidence=attention['evidence'];refs={a['id']:a for a in articles};seen=set()
    if not isinstance(evidence,list) or not 1<=len(evidence)<=8:raise ValueError('missing_selection_attention_evidence')
    for ref in evidence:
        if not isinstance(ref,dict) or set(ref)!={'article_id','version','quote'}:raise ValueError('invalid_selection_attention_evidence')
        identifier=ref['article_id'];version=ref['version'];quote=ref['quote']
        if not isinstance(identifier,str) or identifier not in refs or not _weekly_positive(version):raise ValueError('selection_attention_article_unbound')
        article=refs[identifier]
        if version!=article.get('version') or version!=body.get('article_versions',{}).get(identifier):raise ValueError('selection_attention_version_mismatch')
        if (not text_only_article(article) or article.get('read_scope')!='full_text' or article.get('evidence_truncated') is not False
                or not isinstance(quote,str) or not quote.strip() or len(quote)>200
                or quote not in article.get('evidence_text','')[:EVENT_REVIEW_TEXT_LIMIT]):raise ValueError('selection_attention_quote_unbound')
        key=(identifier,version,quote)
        if key in seen:raise ValueError('duplicate_selection_attention_evidence')
        seen.add(key)
    return expires.astimezone(CN)

def validate_event_groups(groups):
    if not isinstance(groups,list) or not groups:raise ValueError('missing_event_materials')
    ids=set(); articles={}
    for g in groups:
        if not isinstance(g,dict) or set(g)!={'id','articles'} or not isinstance(g['id'],str) or not g['id'].strip() or g['id'] in ids:raise ValueError('invalid_event_group_identity')
        ids.add(g['id'])
        if not isinstance(g['articles'],list) or not g['articles']:raise ValueError('missing_event_articles')
        own=set()
        for a in g['articles']:
            if not isinstance(a,dict) or not isinstance(a.get('id'),str) or not a['id'].strip() or a['id'] in own or not _weekly_positive(a.get('version')) or a.get('status')!='active':raise ValueError('invalid_event_article_identity')
            if not isinstance(a.get('fingerprint'),str) or not a['fingerprint'] or not isinstance(a.get('evidence_text'),str) or not a['evidence_text'] or a.get('evidence_truncated') is not False:raise ValueError('invalid_event_article_materials')
            own.add(a['id'])
            stable={k:v for k,v in a.items() if k not in ('status','checked_at')}
            if a['id'] in articles and articles[a['id']]!=stable:raise ValueError('event_shared_article_conflict')
            articles[a['id']]=stable
    return articles

def validate_event_generations(generations,groups,*,require_all=True):
    articles=validate_event_groups(groups)
    group_ids={g['id'] for g in groups}
    if not isinstance(generations,dict) or not generations or not set(generations)<=group_ids or (require_all and set(generations)!=group_ids):raise ValueError('missing_event_generation')
    materials_sha=_weekly_sha(event_materials(groups)); ids=[g['id'] for g in groups]
    expected_groups=[{'event_id':g['id'],'article_ids':[a['id'] for a in g['articles']],'article_versions':{a['id']:a['version'] for a in g['articles']}} for g in groups]
    for event_id,generation in generations.items():
        keys={'contract','generation_id','event_id','started_at','content_mode','group_bindings','article_bindings','source_permissions','materials_sha256','snapshot_sha256'}
        if not isinstance(generation,dict) or set(generation)!=keys or generation['contract']!=EVENT_GENERATION_CONTRACT or generation['event_id']!=event_id:raise ValueError('invalid_event_generation_schema')
        if not isinstance(generation['generation_id'],str) or not re.fullmatch(r'[0-9a-f]{32}',generation['generation_id']) or generation['content_mode'] not in ('reviewed_ai','source_verified_text','source_brief'):raise ValueError('invalid_event_generation_identity')
        bindings=generation['group_bindings']
        if not isinstance(bindings,list) or len(bindings)!=len(groups):raise ValueError('event_generation_groups_mismatch')
        for expected,b in zip(expected_groups,bindings):
            if not isinstance(b,dict) or set(b)!={'event_id','article_ids','article_versions','expected_event_version'} or any(b[k]!=expected[k] for k in expected):raise ValueError('event_generation_groups_mismatch')
            if type(b['expected_event_version']) is not int or b['expected_event_version']<0 or any(not _weekly_positive(v) for v in b['article_versions'].values()):raise ValueError('invalid_event_generation_versions')
        abs_=generation['article_bindings'];permissions=generation['source_permissions']
        if not isinstance(abs_,list) or len(abs_)!=len(articles) or not isinstance(permissions,list) or not permissions:raise ValueError('invalid_event_generation_articles')
        seen=set(); sources=set()
        for a in abs_:
            if not isinstance(a,dict) or set(a)!={'article_id','version','article_body_sha256','source_id','permission_revision'} or a['article_id'] not in articles or a['article_id'] in seen or not _weekly_positive(a['version']) or a['version']!=articles[a['article_id']]['version']:raise ValueError('event_generation_articles_mismatch')
            if not isinstance(a['source_id'],str) or not a['source_id'] or not _weekly_positive(a['permission_revision']) or not isinstance(a['article_body_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',a['article_body_sha256']):raise ValueError('invalid_event_generation_articles')
            seen.add(a['article_id']);sources.add(a['source_id'])
        permission_map={}
        for p in permissions:
            if not isinstance(p,dict) or set(p)!={'source_id','enabled','revision','registry_sha256','definition_sha256'} or not isinstance(p['source_id'],str) or p['source_id'] in permission_map or p['enabled'] is not True or not _weekly_positive(p['revision']):raise ValueError('invalid_event_generation_permissions')
            if any(not isinstance(p[k],str) or not re.fullmatch(r'[0-9a-f]{64}',p[k]) for k in ('registry_sha256','definition_sha256')):raise ValueError('invalid_event_generation_permissions')
            permission_map[p['source_id']]=p
        if set(permission_map)!=sources or any(a['permission_revision']!=permission_map[a['source_id']]['revision'] for a in abs_):raise ValueError('event_generation_permissions_mismatch')
        if generation['materials_sha256']!=materials_sha or generation['snapshot_sha256']!=_weekly_sha({k:v for k,v in generation.items() if k!='snapshot_sha256'}):raise ValueError('event_generation_materials_mismatch')
    return articles

def event_publication_sha(body):
    stable={k:v for k,v in body.items() if k not in ('generated_at','released_at','version','status','content_version','schema_version','publication_sha256')}
    if isinstance(stable.get('review'),dict):stable['review']={k:v for k,v in stable['review'].items() if k!='reviewed_at'}
    return _weekly_sha(stable)

def event_draft_sha(body):
    value={k:body.get(k) for k in ('id','headline','summary','analysis','uncertainty','facts')}
    # Missing/null legacy records retain their original approved draft hash.
    if body.get('selection_attention') is not None:value['selection_attention']=body['selection_attention']
    return _weekly_sha(value)

class MarketStore:
    def __init__(self,path=None):
        self.path=Path(path or default_path());self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS market_sources(id TEXT PRIMARY KEY, definition TEXT NOT NULL, status TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS market_source_permissions(source_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), revision INTEGER NOT NULL CHECK(revision>0));
              CREATE TABLE IF NOT EXISTS market_weekly_generations(generation_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS market_event_generations(generation_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS market_event_review_bindings(generation_id TEXT NOT NULL,event_id TEXT NOT NULL,kind TEXT NOT NULL,publication_sha256 TEXT NOT NULL,draft_sha256 TEXT NOT NULL,review_sha256 TEXT NOT NULL,PRIMARY KEY(generation_id,event_id));
              CREATE TABLE IF NOT EXISTS market_weekly_review_bindings(generation_id TEXT PRIMARY KEY, publication_sha256 TEXT NOT NULL, draft_sha256 TEXT NOT NULL, review_sha256 TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS market_articles(id TEXT NOT NULL, version INTEGER NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY(id,version));
              CREATE TABLE IF NOT EXISTS market_events(id TEXT NOT NULL, version INTEGER NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, released_at TEXT NOT NULL, PRIMARY KEY(id,version));
              CREATE TABLE IF NOT EXISTS market_claims(id TEXT PRIMARY KEY, event_id TEXT NOT NULL, event_version INTEGER NOT NULL, body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS market_observations(id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(id,version));
              CREATE TABLE IF NOT EXISTS market_weeklies(period TEXT NOT NULL, version INTEGER NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, released_at TEXT NOT NULL, PRIMARY KEY(period,version));
              CREATE TABLE IF NOT EXISTS market_ingest_runs(id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, body TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS market_article_current ON market_articles(id,version DESC);
            ''')
            for s in SOURCES:
                db.execute('INSERT INTO market_sources VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET definition=excluded.definition',
                           (s.id,packed(s.__dict__),packed({'status':'not_checked' if s.enabled else s.scope})))
                db.execute('INSERT OR IGNORE INTO market_source_permissions VALUES(?,?,1)',(s.id,int(s.enabled and s.scope in ('official_facts','public_media'))))
            for s in REGISTRY.values():
                if s.id=='cctv_tv' and s.scope=='source_brief_only':
                    db.execute('INSERT OR IGNORE INTO market_sources VALUES(?,?,?)',(s.id,packed(s.__dict__),packed({'status':'backfill_only'})))
                    db.execute('INSERT OR IGNORE INTO market_source_permissions VALUES(?,?,1)',(s.id,int(s.enabled)))
            from market_daily import DailyMarket
            DailyMarket.schema(db)
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:db.execute('PRAGMA journal_mode=WAL');yield db;db.commit()
        except Exception:db.rollback();raise
        finally:db.close()
    def source_status(self,id,data):
        if not isinstance(data,dict) or set(data)&{'enabled','outbound_allowed','permission','permission_revision','source_permission','source_permissions'}:raise ValueError('source_permission_update_requires_trusted_method')
        with self.db() as db:
            old=json.loads(db.execute('SELECT status FROM market_sources WHERE id=?',(id,)).fetchone()[0])
            if data['status']=='ok':data['last_success_at']=data['checked_at']
            elif old.get('last_success_at'):data['last_success_at']=old['last_success_at']
            db.execute('UPDATE market_sources SET status=? WHERE id=?',(packed(data),id))
    def source_states(self):
        with self.db() as db:rows=db.execute('SELECT s.*,p.enabled permission_enabled,p.revision permission_revision FROM market_sources s LEFT JOIN market_source_permissions p ON s.id=p.source_id').fetchall()
        rows=[{'id':r['id'],**{k:v for k,v in json.loads(r['definition']).items() if k in ('publisher','mode','scope')},**json.loads(r['status']),
            'enabled':bool(json.loads(r['definition']).get('enabled') is True and REGISTRY.get(r['id']) and REGISTRY[r['id']].enabled is True and r['permission_enabled']==1 and _weekly_positive(r['permission_revision'])),
            'permission':{'enabled':r['permission_enabled']==1,'revision':r['permission_revision']}} for r in rows]
        for s in rows:
            if not s['enabled']:s.update(status='disabled',reason='subscription_required' if s['id']=='ft' else s['scope'])
        return rows
    def save_article(self,a):
        with self.db() as db:
            old=db.execute('SELECT version,fingerprint,status FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(a['id'],)).fetchone()
            if old and old['fingerprint']==a['fingerprint'] and old['status']=='active':
                db.execute('UPDATE market_articles SET checked_at=? WHERE id=? AND version=?',(a['checked_at'],a['id'],old['version']))
                return False
            version=old['version']+1 if old else 1;a={**a,'version':version}
            db.execute('INSERT INTO market_articles VALUES(?,?,?,?,?,?)',(a['id'],version,a['fingerprint'],packed(a),'active',a['checked_at']))
            if old:self._invalidate(db,a['id'],'source_revised')
            return True
    def _invalidate(self,db,article_id,reason):
        for table in ('market_events','market_weeklies'):
            for row in db.execute('SELECT rowid,body FROM '+table+" WHERE status='published'").fetchall():
                body=json.loads(row['body'])
                if article_id in body.get('article_ids',[]):db.execute('UPDATE '+table+' SET status=? WHERE rowid=?',(reason,row['rowid']))
    def withdraw_article(self,id,reason='withdrawn'):
        with self.db() as db:
            db.execute('UPDATE market_articles SET status=?,checked_at=? WHERE id=?',(reason,now_iso(),id));self._invalidate(db,id,reason)
    def unavailable_article(self,id,reason='temporarily_unavailable'):
        """访问或全文核验失败不等于撤回原报道，保留版本与正文供恢复核验。"""
        with self.db() as db:
            db.execute('UPDATE market_articles SET status=?,checked_at=? WHERE id=? AND version=(SELECT MAX(version) FROM market_articles WHERE id=?)',
                ('temporarily_unavailable',now_iso(),id,id))
            self._invalidate(db,id,'temporarily_unavailable')
    def articles(self):
        with self.db() as db:rows=db.execute('SELECT a.* FROM market_articles a JOIN (SELECT id,MAX(version) v FROM market_articles GROUP BY id) b ON a.id=b.id AND a.version=b.v').fetchall()
        return [{**json.loads(r['body']),'status':r['status'],'checked_at':r['checked_at']} for r in rows]
    def article(self, id):
        from public_market_history import current_article_view
        with self.db() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(id,)).fetchone()
            if row is None:
                raise KeyError('article_not_found')
            return current_article_view(self,db,row)
    @staticmethod
    def public_article(a):
        from public_market_history import mask_public_article, article_content_available
        if not article_content_available(a):return mask_public_article(a,status=a.get('status') if a.get('status','active')!='active' else 'reading_unavailable')
        identity=a['identity']
        if a.get('content_mode')=='source_verified_text':
            available=source_verified_text_article(a)
            return {**{k:v for k,v in a.items() if k not in ('evidence_text','fingerprint')},
                'source_verified':available,'source_summary_quotes':a.get('source_check',{}).get('summary_quotes',[]) if available else [],
                'status':a.get('status','active') if available else 'reading_unavailable',
                'evidence_excerpt':a.get('evidence_text','')[:120] if available else '',
                'excerpt_notice':'摘要摘自文字原文，完整报道请查看原文。' if available else '原文标题、正文或发表时间绑定无法核对。'}
        if a.get('content_mode')=='source_brief':
            available=source_brief_readable(a)
            return {**{k:v for k,v in a.items() if k not in ('evidence_text','fingerprint')},
                'source_brief':a.get('evidence_text','') if available else '', 'source_verified':available,
                'status':a.get('status','active') if available else 'reading_unavailable',
                'evidence_excerpt':a.get('evidence_text','')[:120] if available else '',
                'excerpt_notice':'仅核实官方文字简介；未读取视频内容。' if available else '官方简介绑定无法核对。'}
        source=REGISTRY.get(a['source_id']);available=readable_article(a)
        if a['read_scope']=='full_text' and source and source.scope=='official_facts':
            identity='official_speech' if '/press/key/' in a['url'] else 'official_interview' if '/press/inter/' in a['url'] else 'official_interpretation' if a['source_id']=='nbs_analysis' else 'official_release'
        return {**{k:v for k,v in a.items() if k not in ('evidence_text','fingerprint')},
            'identity':identity,'access_status':'free_full_text' if available else a.get('access_status','unavailable'),
            'status':a.get('status','active') if available else 'reading_unavailable',
            'evidence_excerpt':a.get('evidence_text','')[:120] if available else '',
            'excerpt_notice':'此处展示短证据，完整报道请阅读原文。' if available else '当前原文未通过免费全文检查。'}

    def _source_permission(self,db,source_id):
        source=REGISTRY.get(source_id)
        row=db.execute('SELECT enabled,revision FROM market_source_permissions WHERE source_id=?',(source_id,)).fetchone()
        configured=db.execute('SELECT definition FROM market_sources WHERE id=?',(source_id,)).fetchone()
        if not source or source.enabled is not True or source.scope not in ('official_facts','public_media') or row is None or configured is None:raise ValueError('weekly_source_permission_unavailable')
        definition=json.loads(configured['definition'])
        if row['enabled']!=1 or not _weekly_positive(row['revision']) or definition.get('enabled') is not True or definition.get('scope') not in ('official_facts','public_media'):raise ValueError('weekly_source_permission_unavailable')
        return {'source_id':source_id,'enabled':True,'revision':row['revision'],'registry_sha256':_weekly_sha(source.__dict__),'definition_sha256':_weekly_sha(definition)}

    def set_source_permission(self,source_id,*,enabled,expected_revision):
        if not isinstance(source_id,str) or source_id not in REGISTRY or type(enabled) is not bool or not _weekly_positive(expected_revision):raise ValueError('invalid_source_permission_update')
        if enabled and (REGISTRY[source_id].enabled is not True or REGISTRY[source_id].scope not in ('official_facts','public_media','source_brief_only')):raise ValueError('source_not_eligible_for_text_permission')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT enabled,revision FROM market_source_permissions WHERE source_id=?',(source_id,)).fetchone()
            if old is None or old['revision']!=expected_revision:raise ValueError('source_permission_revision_conflict')
            if old['enabled']==int(enabled):return {'source_id':source_id,'enabled':enabled,'revision':old['revision'],'changed':False}
            revision=old['revision']+1
            db.execute('UPDATE market_source_permissions SET enabled=?,revision=? WHERE source_id=?',(int(enabled),revision,source_id))
            if not enabled:
                ids=[r['id'] for r in db.execute('SELECT id,body FROM market_articles').fetchall() if json.loads(r['body']).get('source_id')==source_id]
                for article_id in set(ids):self._invalidate(db,article_id,'source_permission_revoked')
            return {'source_id':source_id,'enabled':enabled,'revision':revision,'changed':True}

    def capture_weekly_generation(self,events,start,end,request_id=None):
        selected=deepcopy(events); validate_weekly_event_inputs(selected)
        if start.tzinfo is None or end.tzinfo is None or start>=end or end>datetime.now(CN):raise ValueError('invalid_weekly_generation_period')
        frozen=[]; event_bindings=[]; article_bindings={}; permissions={}; public_articles={}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for e in selected:
                row=db.execute('SELECT * FROM market_events WHERE id=? AND version=?',(e['id'],e['version'])).fetchone()
                latest=db.execute('SELECT version FROM market_events WHERE id=? ORDER BY version DESC LIMIT 1',(e['id'],)).fetchone()
                if row is None or latest is None or latest['version']!=e['version'] or row['status']!='published':raise ValueError('weekly_event_snapshot_unavailable')
                body=json.loads(row['body'])
                if body.get('id')!=e['id'] or body.get('version')!=row['version']:raise ValueError('weekly_event_snapshot_unavailable')
                body={**body,'status':row['status']}
                self._validate_event_publication(db,body['id'],body,body.get('input_hash'),require_publication_stamp=True)
                validate_weekly_event_inputs([body])
                if not start<=datetime.fromisoformat(body['published_at']).astimezone(CN)<end:raise ValueError('event_outside_report_period')
                if body['article_ids']!=e['article_ids'] or body['article_versions']!=e['article_versions']:raise ValueError('weekly_event_snapshot_mismatch')
                exact=[]
                for article_id in body['article_ids']:
                    version=body['article_versions'][article_id]
                    ar=db.execute('SELECT * FROM market_articles WHERE id=? AND version=?',(article_id,version)).fetchone()
                    current=db.execute('SELECT version FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(article_id,)).fetchone()
                    if ar is None or current is None or current['version']!=version or ar['status']!='active':raise ValueError('weekly_article_snapshot_unavailable')
                    article={**json.loads(ar['body']),'status':ar['status'],'checked_at':ar['checked_at']}
                    if article.get('id')!=article_id or article.get('version')!=version or not text_only_article(article):raise ValueError('weekly_article_snapshot_unavailable')
                    permission=self._source_permission(db,article['source_id']); permissions[article['source_id']]=permission
                    binding={'article_id':article_id,'version':version,'article_body_sha256':hashlib.sha256(ar['body'].encode()).hexdigest(),'source_id':article['source_id'],'permission_revision':permission['revision']}
                    if article_id in article_bindings and article_bindings[article_id]!=binding:raise ValueError('weekly_article_version_conflict')
                    article_bindings[article_id]=binding
                    public=self.public_article(article); exact.append(public); public_articles[article_id]=public
                frozen.append({**body,'articles':exact})
                event_bindings.append({'event_id':body['id'],'event_version':row['version'],'article_ids':list(body['article_ids']),'article_versions':dict(body['article_versions']),'event_body_sha256':hashlib.sha256(row['body'].encode()).hexdigest()})
            old=db.execute('SELECT version FROM market_weeklies WHERE period=? ORDER BY version DESC LIMIT 1',(start.date().isoformat(),)).fetchone()
            generation={'contract':WEEKLY_GENERATION_CONTRACT,'generation_id':uuid.uuid4().hex,'started_at':now_iso(),'period_start':start.isoformat(),'period_end_exclusive':end.isoformat(),'expected_weekly_version':old['version'] if old else 0,'event_bindings':event_bindings,'article_bindings':list(article_bindings.values()),'source_permissions':list(permissions.values()),'materials_sha256':_weekly_sha(weekly_materials(frozen))}
            generation['snapshot_sha256']=_weekly_sha(generation)
            generation['input_hash']=_weekly_sha(_weekly_generation_input(generation))
            validate_weekly_generation(generation,frozen,start,end)
            db.execute('INSERT INTO market_weekly_generations VALUES(?,?)',(generation['generation_id'],packed(generation)))
            return {'events':deepcopy(frozen),'generation':deepcopy(generation),'reading':[deepcopy(a) for a in list(public_articles.values())[:5]]}

    def _discard_unreviewed_weekly_generation(self,generation_id):
        if not isinstance(generation_id,str) or not re.fullmatch(r'[0-9a-f]{32}',generation_id):raise ValueError('invalid_weekly_generation_identity')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM market_weekly_review_bindings WHERE generation_id=?',(generation_id,)).fetchone() is not None:return False
            if any(json.loads(r['body']).get('generation',{}).get('generation_id')==generation_id for r in db.execute('SELECT body FROM market_weeklies').fetchall()):return False
            return db.execute('DELETE FROM market_weekly_generations WHERE generation_id=?',(generation_id,)).rowcount==1

    def _record_weekly_review(self,body):
        body=deepcopy(body)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            g=self._validate_weekly_publication(db,body['period'],body,body['input_hash'],require_review_record=False)
            identity=weekly_publication_sha(body)
            review_sha=_weekly_sha({k:v for k,v in body['review'].items() if k!='reviewed_at'})
            old=db.execute('SELECT * FROM market_weekly_review_bindings WHERE generation_id=?',(g['generation_id'],)).fetchone()
            if old is not None:
                if old['publication_sha256']!=identity or old['draft_sha256']!=body['review']['draft_sha256'] or old['review_sha256']!=review_sha:raise ValueError('weekly_review_record_conflict')
                return False
            db.execute('INSERT INTO market_weekly_review_bindings VALUES(?,?,?,?)',(g['generation_id'],identity,body['review']['draft_sha256'],review_sha))
            return True

    def _validate_weekly_publication(self,db,id,body,fingerprint,*,require_review_record=True,require_publication_stamp=False):
        g=body.get('generation'); start=datetime.fromisoformat(body['period_start']); end=datetime.fromisoformat(body['period_end_exclusive'])
        if id!=start.date().isoformat() or body.get('period')!=id or start.tzinfo is None or end.tzinfo is None or start>=end or end>datetime.now(CN):raise ValueError('weekly_generation_period_mismatch')
        events=[]
        if not isinstance(g,dict) or not isinstance(g.get('event_bindings'),list):raise ValueError('missing_weekly_generation_snapshot')
        captured=db.execute('SELECT snapshot FROM market_weekly_generations WHERE generation_id=?',(g.get('generation_id'),)).fetchone()
        if captured is None or json.loads(captured['snapshot'])!=g:raise ValueError('weekly_generation_not_captured')
        for b in g['event_bindings']:
            if not isinstance(b,dict):raise ValueError('invalid_weekly_generation_events')
            row=db.execute('SELECT * FROM market_events WHERE id=? AND version=?',(b.get('event_id'),b.get('event_version'))).fetchone()
            latest=db.execute('SELECT version FROM market_events WHERE id=? ORDER BY version DESC LIMIT 1',(b.get('event_id'),)).fetchone()
            if row is None or latest is None or latest['version']!=row['version'] or row['status']!='published' or hashlib.sha256(row['body'].encode()).hexdigest()!=b.get('event_body_sha256'):raise ValueError('weekly_event_snapshot_changed')
            e={**json.loads(row['body']),'status':row['status']}
            self._validate_event_publication(db,e['id'],e,e.get('input_hash'),require_publication_stamp=True)
            if e.get('version')!=row['version'] or not start<=datetime.fromisoformat(e['published_at']).astimezone(CN)<end:raise ValueError('weekly_event_snapshot_changed')
            events.append(e)
        article_versions=validate_weekly_generation(g,events,start,end)
        if body.get('input_hash')!=g['input_hash'] or fingerprint!=g['input_hash']:raise ValueError('weekly_generation_input_hash_mismatch')
        expected_citations=[{'event_id':e['id'],'event_version':e['version'],'heading':e['headline'],'article_ids':list(e['article_ids']),'article_versions':dict(e['article_versions'])} for e in events]
        if body.get('citations')!=expected_citations or body.get('article_ids')!=list(article_versions) or body.get('article_versions')!=article_versions:raise ValueError('weekly_generation_citations_mismatch')
        permissions={p['source_id']:p for p in g['source_permissions']}; exact_reading=[]
        for a in g['article_bindings']:
            row=db.execute('SELECT * FROM market_articles WHERE id=? AND version=?',(a['article_id'],a['version'])).fetchone()
            latest=db.execute('SELECT version FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(a['article_id'],)).fetchone()
            if row is None or latest is None or latest['version']!=row['version'] or row['status']!='active' or hashlib.sha256(row['body'].encode()).hexdigest()!=a['article_body_sha256']:raise ValueError('weekly_article_snapshot_changed')
            article={**json.loads(row['body']),'status':row['status'],'checked_at':row['checked_at']}
            if article.get('id')!=a['article_id'] or article.get('version')!=row['version'] or article.get('source_id')!=a['source_id'] or not text_only_article(article):raise ValueError('weekly_article_snapshot_changed')
            if self._source_permission(db,a['source_id'])!=permissions[a['source_id']]:raise ValueError('weekly_source_permission_changed')
            exact_reading.append(self.public_article(article))
        reading=body.get('reading')
        stable_reading=lambda values:[{k:v for k,v in a.items() if k not in ('checked_at','fetched_at')} for a in values]
        if not isinstance(reading,list) or any(not isinstance(a,dict) for a in reading) or stable_reading(reading)!=stable_reading(exact_reading[:5]):raise ValueError('weekly_generation_reading_mismatch')
        review=body.get('review',{})
        draft={k:body.get(k) for k in ('title','summary','changes','transmission')}
        if review.get('status')!='approved' or review.get('generation_snapshot_sha256')!=g['snapshot_sha256'] or review.get('materials_sha256')!=g['materials_sha256'] or review.get('draft_sha256')!=_weekly_sha(draft):raise ValueError('weekly_review_binding_mismatch')
        if not isinstance(body.get('changes'),list) or len(body['changes'])!=min(3,len(events)) or any(not isinstance(c,dict) or c.get('event_id') not in {e['id'] for e in events} for c in body['changes']):raise ValueError('weekly_reference_missing')
        if require_review_record:
            reviewed=db.execute('SELECT * FROM market_weekly_review_bindings WHERE generation_id=?',(g['generation_id'],)).fetchone()
            review_sha=_weekly_sha({k:v for k,v in review.items() if k!='reviewed_at'})
            if reviewed is None or reviewed['publication_sha256']!=weekly_publication_sha(body) or reviewed['draft_sha256']!=review['draft_sha256'] or reviewed['review_sha256']!=review_sha:raise ValueError('weekly_review_not_recorded')
        if require_publication_stamp and body.get('publication_sha256')!=weekly_publication_sha(body):raise ValueError('weekly_publication_stamp_mismatch')
        return g


    def _event_source_permission(self,db,source_id,mode):
        if mode!='source_brief':return self._source_permission(db,source_id)
        source=REGISTRY.get(source_id)
        row=db.execute('SELECT enabled,revision FROM market_source_permissions WHERE source_id=?',(source_id,)).fetchone()
        config=db.execute('SELECT definition FROM market_sources WHERE id=?',(source_id,)).fetchone()
        definition=json.loads(config['definition']) if config else None
        if source_id!='cctv_tv' or not source or source.enabled is not True or source.scope!='source_brief_only' or row is None or row['enabled']!=1 or not _weekly_positive(row['revision']) or not isinstance(definition,dict) or definition.get('enabled') is not True or definition.get('scope')!='source_brief_only':raise ValueError('event_source_permission_unavailable')
        return {'source_id':source_id,'enabled':True,'revision':row['revision'],'registry_sha256':_weekly_sha(source.__dict__),'definition_sha256':_weekly_sha(definition)}

    @staticmethod
    def _event_source_readable(article,mode):
        if mode=='source_verified_text':return source_verified_text_article(article)
        if mode=='source_brief':return source_brief_readable(article)
        return text_only_article(article) and article.get('read_scope')=='full_text' and article.get('evidence_truncated') is False

    def capture_event_generation(self,groups,*,content_mode='reviewed_ai'):
        selected=deepcopy(groups);validate_event_groups(selected)
        if content_mode not in ('reviewed_ai','source_verified_text','source_brief'):raise ValueError('invalid_event_content_mode')
        if content_mode!='reviewed_ai' and (len(selected)!=1 or len(selected[0]['articles'])!=1):raise ValueError('deterministic_event_requires_one_source')
        frozen=[];bindings=[];article_bindings={};permissions={}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for group in selected:
                exact=[]
                for supplied in group['articles']:
                    row=db.execute('SELECT * FROM market_articles WHERE id=? AND version=?',(supplied['id'],supplied['version'])).fetchone()
                    latest=db.execute('SELECT version FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(supplied['id'],)).fetchone()
                    if row is None or latest is None or latest['version']!=supplied['version'] or row['status']!='active':raise ValueError('event_article_snapshot_unavailable')
                    raw=json.loads(row['body']);article={**raw,'status':row['status'],'checked_at':row['checked_at']}
                    if raw.get('id')!=supplied['id'] or raw.get('version')!=row['version'] or {k:v for k,v in supplied.items() if k not in ('status','checked_at')}!={k:v for k,v in raw.items() if k not in ('status','checked_at')}:raise ValueError('event_article_snapshot_mismatch')
                    if not self._event_source_readable(article,content_mode):raise ValueError('event_article_not_readable')
                    permission=self._event_source_permission(db,article['source_id'],content_mode);permissions[article['source_id']]=permission
                    binding={'article_id':article['id'],'version':row['version'],'article_body_sha256':hashlib.sha256(row['body'].encode()).hexdigest(),'source_id':article['source_id'],'permission_revision':permission['revision']}
                    if article['id'] in article_bindings and article_bindings[article['id']]!=binding:raise ValueError('event_shared_article_conflict')
                    article_bindings[article['id']]=binding;exact.append(article)
                head=db.execute('SELECT version FROM market_events WHERE id=? ORDER BY version DESC LIMIT 1',(group['id'],)).fetchone()
                frozen.append({'id':group['id'],'articles':exact})
                bindings.append({'event_id':group['id'],'article_ids':[a['id'] for a in exact],'article_versions':{a['id']:a['version'] for a in exact},'expected_event_version':head['version'] if head else 0})
            generations={}
            for group in frozen:
                g={'contract':EVENT_GENERATION_CONTRACT,'generation_id':uuid.uuid4().hex,'event_id':group['id'],'started_at':now_iso(),'content_mode':content_mode,'group_bindings':deepcopy(bindings),'article_bindings':list(article_bindings.values()),'source_permissions':list(permissions.values()),'materials_sha256':_weekly_sha(event_materials(frozen))}
                g['snapshot_sha256']=_weekly_sha(g);generations[group['id']]=g
            validate_event_generations(generations,frozen)
            for g in generations.values():db.execute('INSERT INTO market_event_generations VALUES(?,?)',(g['generation_id'],packed(g)))
            return {'groups':deepcopy(frozen),'generations':deepcopy(generations)}

    def _validate_event_publication(self,db,id,body,fingerprint,*,require_record=True,require_publication_stamp=False):
        g=body.get('generation')
        if not isinstance(g,dict) or g.get('contract')!=EVENT_GENERATION_CONTRACT or g.get('event_id')!=id or body.get('id')!=id:raise ValueError('missing_event_generation')
        captured=db.execute('SELECT snapshot FROM market_event_generations WHERE generation_id=?',(g.get('generation_id'),)).fetchone()
        if captured is None or json.loads(captured['snapshot'])!=g:raise ValueError('event_generation_not_captured')
        mode=g['content_mode'];article_map={};permission_map={p['source_id']:p for p in g['source_permissions']}
        for binding in g['article_bindings']:
            row=db.execute('SELECT * FROM market_articles WHERE id=? AND version=?',(binding['article_id'],binding['version'])).fetchone()
            latest=db.execute('SELECT version FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(binding['article_id'],)).fetchone()
            if row is None or latest is None or latest['version']!=row['version'] or row['status']!='active' or hashlib.sha256(row['body'].encode()).hexdigest()!=binding['article_body_sha256']:raise ValueError('event_article_snapshot_changed')
            article={**json.loads(row['body']),'status':row['status'],'checked_at':row['checked_at']}
            if article.get('id')!=binding['article_id'] or article.get('version')!=row['version'] or article.get('source_id')!=binding['source_id'] or not self._event_source_readable(article,mode):raise ValueError('event_article_snapshot_changed')
            if self._event_source_permission(db,binding['source_id'],mode)!=permission_map[binding['source_id']]:raise ValueError('event_source_permission_changed')
            article_map[binding['article_id']]=article
        groups=[{'id':b['event_id'],'articles':[article_map[i] for i in b['article_ids']]} for b in g['group_bindings']]
        # Validate the genuine persisted per-event record against the complete batch.
        validate_event_generations({id:g},groups,require_all=False)
        own=next((b for b in g['group_bindings'] if b['event_id']==id),None)
        if own is None or body.get('article_ids')!=own['article_ids'] or body.get('article_versions')!=own['article_versions']:raise ValueError('event_article_binding_mismatch')
        own_articles=[article_map[i] for i in own['article_ids']];own_group={'id':id,'articles':own_articles}
        hashes={event_input_hash(own_group,mode)}
        if body.get('selection_attention') is None:hashes.add(event_input_hash(own_group,mode,legacy=True))
        if body.get('input_hash')!=fingerprint or fingerprint not in hashes:raise ValueError('event_input_hash_mismatch')
        if body.get('primary_article_id') not in own['article_ids']:raise ValueError('event_primary_article_unbound')
        facts=body.get('facts')
        if not isinstance(facts,list) or not facts or any(not isinstance(f,dict) or f.get('article_id') not in own['article_ids'] or not isinstance(f.get('quote'),str) or not f['quote'].strip() or f['quote'] not in article_map[f['article_id']]['evidence_text'] for f in facts):raise ValueError('event_fact_source_unbound')
        review=body.get('review',{})
        if mode=='reviewed_ai':
            if body.get('content_mode','reviewed_ai')!='reviewed_ai' or review.get('status')!='approved' or review.get('generation_snapshot_sha256')!=g['snapshot_sha256'] or review.get('materials_sha256')!=g['materials_sha256'] or review.get('draft_sha256')!=event_draft_sha(body):raise ValueError('event_review_binding_mismatch')
        elif mode=='source_verified_text':
            if not source_verified_text_event(body,own_articles):raise ValueError('event_deterministic_source_proof_invalid')
        elif not source_verified_brief_event(body,own_articles):raise ValueError('event_deterministic_source_proof_invalid')
        if validate_selection_attention(body,own_articles,mode=mode) is not None:
            if review.get('selection_attention_time_context')!=selection_attention_time_context(body,own_articles,g['started_at']):raise ValueError('selection_attention_review_context_unbound')
        if require_record:
            recorded=db.execute('SELECT * FROM market_event_review_bindings WHERE generation_id=? AND event_id=?',(g['generation_id'],id)).fetchone()
            review_sha=_weekly_sha({k:v for k,v in review.items() if k!='reviewed_at'})
            if recorded is None or recorded['kind']!=mode or recorded['publication_sha256']!=event_publication_sha(body) or recorded['draft_sha256']!=event_draft_sha(body) or recorded['review_sha256']!=review_sha:raise ValueError('event_review_not_recorded')
        if require_publication_stamp and body.get('publication_sha256')!=event_publication_sha(body):raise ValueError('event_publication_stamp_mismatch')
        return g,own,own_articles

    def _record_event_review(self,body):
        body=deepcopy(body)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            g,own,articles=self._validate_event_publication(db,body['id'],body,body['input_hash'],require_record=False)
            stable=event_publication_sha(body);draft=event_draft_sha(body);review=_weekly_sha({k:v for k,v in body['review'].items() if k!='reviewed_at'})
            old=db.execute('SELECT * FROM market_event_review_bindings WHERE generation_id=? AND event_id=?',(g['generation_id'],body['id'])).fetchone()
            if old is not None:
                if old['kind']!=g['content_mode'] or old['publication_sha256']!=stable or old['draft_sha256']!=draft or old['review_sha256']!=review:raise ValueError('event_review_record_conflict')
                return False
            db.execute('INSERT INTO market_event_review_bindings VALUES(?,?,?,?,?,?)',(g['generation_id'],body['id'],g['content_mode'],stable,draft,review))
            return True

    def register_source_verified_event(self,body):
        body=deepcopy(body);mode=body.get('content_mode')
        if mode not in ('source_verified_text','source_brief'):raise ValueError('unsupported_deterministic_event_mode')
        ids=body.get('article_ids');versions=body.get('article_versions')
        if not isinstance(ids,list) or len(ids)!=1 or not isinstance(versions,dict) or set(versions)!=set(ids) or not _weekly_positive(versions[ids[0]]):raise ValueError('deterministic_event_requires_exact_version')
        with self.db() as db:
            db.execute('BEGIN')
            row=db.execute('SELECT * FROM market_articles WHERE id=? AND version=?',(ids[0],versions[ids[0]])).fetchone()
            if row is None:raise ValueError('deterministic_event_article_missing')
            article={**json.loads(row['body']),'status':row['status'],'checked_at':row['checked_at']}
        snapshot=self.capture_event_generation([{'id':body['id'],'articles':[article]}],content_mode=mode)
        body={k:v for k,v in body.items() if k not in ('version','status','released_at','content_version','schema_version','publication_sha256','generation')}
        body['generation']=snapshot['generations'][body['id']];body['input_hash']=event_input_hash(snapshot['groups'][0],mode)
        self._record_event_review(body)
        return body

    def _event_is_reusable(self,body,fingerprint=None):
        try:
            if body.get('status')!='published':return False
            with self.db() as db:
                db.execute('BEGIN')
                row=db.execute('SELECT * FROM market_events WHERE id=? ORDER BY version DESC LIMIT 1',(body['id'],)).fetchone()
                if row is None or row['status']!='published' or row['version']!=body.get('version'):return False
                exact={**json.loads(row['body']),'status':row['status']}
                self._validate_event_publication(db,body['id'],exact,fingerprint or exact['input_hash'],require_publication_stamp=True)
            return True
        except (ValueError,KeyError,TypeError):return False

    def read_event_source(self,event_id,article_id):
        with self.db() as db:
            db.execute('BEGIN')
            row=db.execute('SELECT * FROM market_events WHERE id=? ORDER BY version DESC LIMIT 1',(event_id,)).fetchone()
            if row is None or row['status']!='published':raise ValueError('event_source_unavailable')
            body={**json.loads(row['body']),'status':row['status']}
            generation,binding,articles=self._validate_event_publication(db,event_id,body,body.get('input_hash'),require_publication_stamp=True)
            article=next((a for a in articles if a['id']==article_id),None)
            if article is None:raise ValueError('event_article_not_member')
            return {'event_id':event_id,'event_version':row['version'],'source_scope':article['read_scope'],'video_content_read':article.get('video_content_read',False),'article':self.public_article(article)}

    def publish(self,table,id,body,fingerprint):
        if table not in ('market_events','market_weeklies'):raise ValueError('unknown_content_type')
        key='id' if table=='market_events' else 'period'
        body=deepcopy(body)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            generation=self._validate_weekly_publication(db,id,body,fingerprint) if table=='market_weeklies' else None
            event_generation=self._validate_event_publication(db,id,body,fingerprint) if table=='market_events' else None
            old=db.execute('SELECT version,fingerprint,status,body FROM '+table+' WHERE '+key+'=? ORDER BY version DESC LIMIT 1',(id,)).fetchone()
            if generation is not None:
                identity=weekly_publication_sha(body)
                previous=json.loads(old['body']) if old else {}
                if old and previous.get('generation',{}).get('generation_id')==generation['generation_id']:
                    if previous.get('publication_sha256')!=weekly_publication_sha(previous) or identity!=previous.get('publication_sha256'):raise ValueError('weekly_generation_replay_conflict')
                    if old['status']!='published':raise ValueError('weekly_generation_replay_unavailable')
                    return False
                if (old['version'] if old else 0)!=generation['expected_weekly_version']:raise ValueError('weekly_head_version_conflict')
                body['publication_sha256']=identity
            elif event_generation is not None:
                captured,own,articles=event_generation;identity=event_publication_sha(body);previous=json.loads(old['body']) if old else {}
                if old and previous.get('generation',{}).get('generation_id')==captured['generation_id']:
                    if previous.get('publication_sha256')!=event_publication_sha(previous) or previous.get('publication_sha256')!=identity:raise ValueError('event_generation_replay_conflict')
                    if old['status']!='published':raise ValueError('event_generation_replay_unavailable')
                    return False
                if (old['version'] if old else 0)!=own['expected_event_version']:raise ValueError('event_head_version_conflict')
                body['publication_sha256']=identity
            version=old['version']+1 if old else 1;now=now_iso()
            body={**body,'version':version,'content_version':fingerprint[:16],'released_at':now,'schema_version':SCHEMA}
            db.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?,?)',(id,version,fingerprint,packed(body),'published',now))
            if table=='market_events':
                for i,claim in enumerate(body.get('facts',[])):
                    db.execute('INSERT INTO market_claims VALUES(?,?,?,?)',(f'{id}/{version}/{i}',id,version,packed(claim)))
        return True
    def versions(self,table):
        if table not in ('market_events','market_weeklies'):raise ValueError('unknown_content_type')
        key='id' if table=='market_events' else 'period'
        with self.db() as db:rows=db.execute('SELECT a.* FROM '+table+' a JOIN (SELECT '+key+',MAX(version) v FROM '+table+' GROUP BY '+key+') b ON a.'+key+'=b.'+key+' AND a.version=b.v').fetchall()
        return [{**json.loads(r['body']),'status':r['status']} for r in rows]
    def event(self,id):
        with self.db() as db:
            db.execute('BEGIN')
            row=db.execute('SELECT * FROM market_events WHERE id=? ORDER BY version DESC LIMIT 1',(id,)).fetchone()
            if row is None:raise KeyError('event_not_found')
            event={**json.loads(row['body']),'status':row['status']}
            try:
                if row['status']!='published':raise ValueError('event_not_published')
                generation,binding,articles=self._validate_event_publication(db,id,event,event['input_hash'],require_publication_stamp=True)
                return {**event,'articles':[self.public_article(a) for a in articles]}
            except (ValueError,KeyError,TypeError):
                # Unknown legacy generation is a visible migration gap, never latest fallback.
                metadata={k:v for k,v in event.items() if k in ('id','version','content_version','schema_version','released_at','published_at','article_ids','article_versions')}
                metadata.update(status='reading_unavailable',source_binding_status='event_source_binding_unavailable',summary='',analysis='',uncertainty='',facts=[],conflicts=[],articles=[])
                return metadata
    def status(self):
        from market_schedule import snapshot
        states=self.source_states()
        with self.db() as db:row=db.execute('SELECT body FROM market_ingest_runs ORDER BY started_at DESC LIMIT 1').fetchone()
        run=json.loads(row[0]) if row else {}
        ok=[s for s in states if s.get('enabled') and s.get('status')=='ok']
        return {'schema_version':SCHEMA,'checked_at':run.get('ended_at'),
            'checked_at_local':datetime.fromisoformat(run['ended_at']).astimezone(CN).isoformat() if run.get('ended_at') else None,
            'timezone':'Asia/Shanghai','sources':states,
            'state':'ready' if ok else 'unavailable','updating':run.get('status')=='running',
            'limitations':['官方原始发布与精选公开财经报道；原文逐篇检查，来源覆盖有限。','未接入可连续核验的行情端点；不计算用户组合涨幅。'],
            'schedule':snapshot(self)}
    def hotspots(self, now=None):
        from market_daily import DailyMarket
        return DailyMarket(self).hotspots(now=now)
    def history_index(self, from_date=None, to_date=None, category='all', now=None):
        from market_daily import DailyMarket
        return DailyMarket(self).index(from_date,to_date,category,now=now)
    def history_day(self, day, category='all', now=None):
        from market_daily import DailyMarket
        return DailyMarket(self).day(day,category,now=now)
    def history_event(self, day, id, revision=None, *, now=None, expected_version=None):
        from market_daily import DailyMarket
        return DailyMarket(self).event(day,id,revision,now=now,expected_version=expected_version)
    def route(self, path, query, now=None):
        from market_daily import DailyMarket
        return DailyMarket(self).route(path,query,now=now)
    def update_daily(self, *, run_id, valid_check, coverage=None, now=None):
        from market_daily import DailyMarket
        return DailyMarket(self).update(now=now,run_id=run_id,valid_check=valid_check,coverage=coverage)
    def reconstruct_daily(self, now=None, run_id='initial-history-backfill'):
        from market_daily import DailyMarket
        return DailyMarket(self).reconstruct(now=now,run_id=run_id)
    def seal_daily(self, now=None):
        from market_daily import DailyMarket
        return DailyMarket(self).seal(now=now)
    def weekly_publications(self):
        """只核验当前周报原件，不读取 coverage/status，供读取和调度共用。"""
        from public_market_history import current_article_view, mask_public_weekly
        with self.db() as db:
            db.execute('BEGIN')
            stored = db.execute('SELECT w.* FROM market_weeklies w JOIN (SELECT period,MAX(version) v FROM market_weeklies GROUP BY period) h ON w.period=h.period AND w.version=h.v').fetchall()
            rows = []
            for row in stored:
                report = {**json.loads(row['body']),'status':row['status']}
                status = report['status']
                if status == 'published':
                    try:
                        self._validate_weekly_publication(db,report['period'],report,row['fingerprint'],require_publication_stamp=True)
                    except (ValueError,KeyError,TypeError):
                        status = 'generation_binding_unavailable'
                articles = []
                for id in report.get('article_ids',[]):
                    article_row = db.execute('SELECT * FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(id,)).fetchone()
                    if article_row is None:
                        articles.append({'id':id,'status':'reading_unavailable','source_permission':{'source_id':None,'enabled':False,'revision':None,'reason':'article_not_found'}})
                    else:
                        articles.append(current_article_view(self,db,article_row))
                if status == 'published' and (not articles or any(a.get('status')!='active' or not evidence_readable(a) for a in articles)):
                    status = ('source_permission_revoked' if any(a.get('status')=='source_permission_revoked' for a in articles) else 'reading_unavailable')
                if status != 'published':
                    report = mask_public_weekly(report,articles,status=status)
                else:
                    report = {**report,'source_articles':articles,'source_permissions':[a['source_permission'] for a in articles]}
                rows.append(report)
            rows.sort(key=lambda w:w['period'],reverse=True)
        return rows

    def weekly(self, period='latest'):
        rows = self.weekly_publications()
        if period == 'latest':
            selected = next((w for w in rows if w['status']=='published'),None)
        else:
            selected = next((w for w in rows if w['period']==period),None)
        history = [{k:w[k] for k in ('period','version','title','status')} for w in rows]
        return {'schema_version':SCHEMA,'status':selected['status'] if selected else 'no_verified_weekly',
            'report':selected,'history':history,'coverage':self.status(),
            'expected_period':week_periods(1)[0][0].date().isoformat()}
    def record_run(self,id,data):
        with self.db() as db:db.execute('INSERT INTO market_ingest_runs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at,body=excluded.body',
                                     (id,data['started_at'],data.get('ended_at'),packed(data)))

    def history_article(self, id, version, *, reading_mode, db=None):
        from public_market_history import PublicMarketHistory
        return PublicMarketHistory(self).read_article(id,version,reading_mode=reading_mode,db=db)

    def history_weekly(self, period, version, *, reading_mode, db=None):
        from public_market_history import PublicMarketHistory
        return PublicMarketHistory(self).read_weekly(period,version,reading_mode=reading_mode,db=db)

    def assert_history_receipt(self, receipt, *, db=None):
        from public_market_history import PublicMarketHistory
        return PublicMarketHistory(self).assert_fresh(receipt,db=db)
