"""公共资讯独立缓存和不可变版本；不使用模拟银行时钟或账户库。"""
from __future__ import annotations
import json, os, sqlite3, hashlib, threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from decimal import Decimal
from market_sources import SOURCES, now_iso, REGISTRY, safe_url, readable_article, evidence_readable, source_brief_readable, source_verified_text_article

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

class MarketStore:
    def __init__(self,path=None):
        self.path=Path(path or default_path());self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS market_sources(id TEXT PRIMARY KEY, definition TEXT NOT NULL, status TEXT NOT NULL);
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
            from market_daily import DailyMarket
            DailyMarket.schema(db)
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:db.execute('PRAGMA journal_mode=WAL');yield db;db.commit()
        except Exception:db.rollback();raise
        finally:db.close()
    def source_status(self,id,data):
        with self.db() as db:
            old=json.loads(db.execute('SELECT status FROM market_sources WHERE id=?',(id,)).fetchone()[0])
            if data['status']=='ok':data['last_success_at']=data['checked_at']
            elif old.get('last_success_at'):data['last_success_at']=old['last_success_at']
            db.execute('UPDATE market_sources SET status=? WHERE id=?',(packed(data),id))
    def source_states(self):
        with self.db() as db:rows=db.execute('SELECT * FROM market_sources').fetchall()
        rows=[{'id':r['id'],**{k:v for k,v in json.loads(r['definition']).items() if k in ('publisher','mode','enabled','scope')},**json.loads(r['status'])} for r in rows]
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
    def article(self,id):
        item=next((a for a in self.articles() if a['id']==id),None)
        if item is None:raise KeyError('article_not_found')
        return self.public_article(item)
    @staticmethod
    def public_article(a):
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
    def publish(self,table,id,body,fingerprint):
        if table not in ('market_events','market_weeklies'):raise ValueError('unknown_content_type')
        key='id' if table=='market_events' else 'period'
        with self.db() as db:
            old=db.execute('SELECT version,fingerprint,status FROM '+table+' WHERE '+key+'=? ORDER BY version DESC LIMIT 1',(id,)).fetchone()
            if old and old['fingerprint']==fingerprint and old['status']=='published':return False
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
        e=next((e for e in self.versions('market_events') if e['id']==id),None)
        if e is None:raise KeyError('event_not_found')
        articles=[self.article(i) for i in e['article_ids']]
        if e['status']=='published' and any(a['status']!='active' or not evidence_readable(a) for a in articles):e={**e,'status':'reading_unavailable'}
        return {**e,'articles':articles}
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
    def weekly(self,period='latest'):
        rows=self.versions('market_weeklies')
        for w in rows:
            articles=[self.article(i) for i in w.get('article_ids',[])]
            if w['status']=='published' and any(a['status']!='active' or not evidence_readable(a) for a in articles):w['status']='reading_unavailable'
        if period=='latest':
            rows=sorted((r for r in rows if r['status']=='published'),key=lambda x:x['period'],reverse=True)
            e=rows[0] if rows else None
        else:e=next((r for r in rows if r['period']==period),None)
        if e:
            e={**e,'source_articles':[self.article(i) for i in e.get('article_ids',[])]}
        # rows在latest分支已筛选；历史状态需重新核对原文，不能沿用旧发布标记。
        history=[]
        for r in sorted(self.versions('market_weeklies'),key=lambda x:x['period'],reverse=True):
            articles=[self.article(i) for i in r.get('article_ids',[])]
            status='reading_unavailable' if r['status']=='published' and any(a['status']!='active' or not evidence_readable(a) for a in articles) else r['status']
            history.append({'period':r['period'],'version':r['version'],'title':r['title'],'status':status})
        return {'schema_version':SCHEMA,'status':e['status'] if e else 'no_verified_weekly','report':e,'history':history,
            'coverage':self.status(),'expected_period':week_periods(1)[0][0].date().isoformat()}
    def record_run(self,id,data):
        with self.db() as db:db.execute('INSERT INTO market_ingest_runs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at,body=excluded.body',
                                     (id,data['started_at'],data.get('ended_at'),packed(data)))
