"""每日精选不可变快照；公共资讯时钟独立于银行业务日期。"""
from __future__ import annotations
import hashlib, json, re, asyncio, threading
from decimal import Decimal
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from pathlib import Path
from market_content import CN, packed, event_primary_publication
from market_sources import readable_article,cluster_key,evidence_readable,article_reported_at,source_verified_brief_event,source_verified_text_event,text_only_article

from market_source_attribution import first_publication_verified
from market_statistical_identity import innovation_release_alias, source_release_aliases
from market_semantic_checks import compound_announcement_issues

RULE_VERSION = 'daily-selection-v14-single-announcement-card'
_publication_clock_scope = ContextVar('navren_daily_publication_clock_scope',default=None)

def _clock_execution_owner():
    try:task=asyncio.current_task()
    except RuntimeError:task=None
    return threading.get_ident(),id(task) if task is not None else None

def _uses_live_publication_clock(daily):
    scope=_publication_clock_scope.get()
    return scope is not None and scope[0] is daily and scope[1]==_clock_execution_owner()
CATEGORIES = [
    {'id':'household','name':'民生金融','name_en':'Household finance'},
    {'id':'macro_policy','name':'宏观政策','name_en':'Macro policy'},
    {'id':'investment','name':'投资市场','name_en':'Investment markets'},
    {'id':'fintech','name':'金融科技','name_en':'Financial technology'},
    {'id':'international','name':'国际市场','name_en':'International markets'},
    {'id':'general','name':'综合金融','name_en':'General finance'},
]
CATEGORY_IDS = {x['id'] for x in CATEGORIES}
WITHDRAWN = {'withdrawn','retracted','removed','source_withdrawn','404','410'}

class MarketDailyError(ValueError):
    def __init__(self, code, status=400):
        self.code=code; self.status=status; super().__init__(code)

def instant(value):
    dt=datetime.fromisoformat(value)
    # Published date-only material is Shanghai local and retains its precision.
    return (dt.replace(tzinfo=CN) if dt.tzinfo is None else dt).astimezone(CN)

def classify(event, articles=()):
    """主要对象决定分类，不以机构名称推断；规则版本随快照保存。"""
    known=event.get('category')
    if known in CATEGORY_IDS: return known
    text=' '.join([event.get('headline',''),event.get('summary','')]+[a.get('title','') for a in articles]).lower()
    if re.search(r'跨境|汇率|外汇|海外市场|欧元|美元|exchange rate|cross.border|(?:欧央行|欧洲央行|美联储|ecb|federal reserve).{0,35}(?:利率|抵押|货币|信贷|通胀|rate|collateral|monetary|inflation)',text):return 'international'
    if re.search(r'购房|住房|房贷|缴费|消费信贷|个人.*权益|养老|社保|mortgage|pension',text):return 'household'
    if re.search(r'货币政策|财政|监管|利率|采购经理|pmi|宏观|通胀|统计|再贷款|inflation|monetary|policy rate',text):return 'macro_policy'
    if re.search(r'数字支付|金融科技|金融ai|银行技术|人工智能|支付技术|fintech|digital payment|banking technology',text):return 'fintech'
    if re.search(r'股票|股市|股指|债券|基金|证券|交易所|stock|bond|equity|fund market',text):return 'investment'
    return 'general'

def theme(event, category):
    selected=event.get('theme') or event.get('topic_id')
    if selected:return selected
    if category=='macro_policy':
        # 主题按报道主要对象判定；CPI、PMI等统计数据不是利率政策。
        headline=' '.join([event.get('headline','')]+[a.get('title','') for a in event.get('articles',[])])
        if re.search(r'降息|加息|降准|存款准备金|政策利率|基准利率|贷款市场报价利率|(?:调整|下调|上调|维持).{0,12}利率|利率.{0,12}(?:调整|下调|上调|决定)|\b(?:interest rate|policy rate|rate cut|rate hike|lpr)\b',headline,re.I):return 'rates'
    return {
        'household':'household','macro_policy':'macro','international':'international',
        'investment':'investment','fintech':'fintech','general':'macro'}[category]

def _china_gdp_release_alias(event, primary):
    """同一次GDP发布的跨标题解读只占一格，不按宽泛经济主题合并。"""
    heading=' '.join([event.get('headline',''),primary.get('title','')])
    if re.search(r'预测|预计|预期|目标|展望|宣布|决定|降息|加息|签署',heading):return None
    if re.search(r'美国|日本|英国|德国|法国|印度|欧元区|欧盟|俄罗斯|新加坡',heading):return None
    if not re.search(r'GDP|国内生产总值|经济(?:运行|三季报|半年报|年报)',heading,re.I):return None
    summary=event.get('summary','')
    if not re.search(r'中国|我国|国家统计局',heading+' '+summary.split('。')[0]):return None
    periods={'前三季度':('q1-q3',10),'前3季度':('q1-q3',10),'上半年':('h1',7),
             '第一季度':('q1',4),'一季度':('q1',4),'第二季度':('q2',7),'二季度':('q2',7),
             '第三季度':('q3',10),'三季度':('q3',10),'第四季度':('q4',13),'四季度':('q4',13),
             '全年':('year',13)}
    text=heading+' '+summary
    period=re.search(r'(?:(20\d{2})年)?('+ '|'.join(periods)+')',text)
    if not period:return None
    name,first_release_month=periods[period[2]]
    published=instant(article_reported_at(primary))
    # 只有当年季度已结束时，省略年份的当期报道才可用发表年；
    # 年初谈上一年、预测及不能确定期间的材料不推断身份。
    if period[1]:year=period[1]
    elif published.month>=first_release_month:year=str(published.year)
    else:return None
    growth_pattern=r'(?:GDP|国内生产总值)[^。；;！？]{0,100}?(?:同比(?:增长)?|增速(?:为)?|增长)\s*(\d+(?:\.\d+)?)\s*%'
    growth=re.search(growth_pattern,text,re.I)
    if not growth:return None
    value=format(Decimal(growth[1]).normalize(),'f')
    quotes=[primary.get('evidence_excerpt','')]+[f.get('quote','') for f in event.get('facts',[])
                                               if f.get('article_id')==primary['id']]
    supported=[format(Decimal(m[1]).normalize(),'f') for quote in quotes
               for m in re.finditer(growth_pattern,quote,re.I)]
    if value not in supported:return None
    return 'cn_gdp_release:'+year+':'+name+':'+value

def event_aliases(event):
    """显式身份、同文转载和同场开幕综述合并；共同主题不等于同一事件。"""
    primary=next(a for a in event['articles'] if a['id']==event['primary_article_id'])
    aliases={'article:'+a['id'] for a in event['articles']}
    aliases.add('cluster:'+cluster_key(primary))
    for field in ('canonical_event_id','event_group_id'):
        if event.get(field):aliases.add('canonical:'+str(event[field]))
    title=re.sub(r'新华社消息[丨|：:]|[\s，,。！!？?·丨|—－-]','',primary.get('title','')).lower()
    origin=primary.get('origin_publisher') or primary.get('publisher')
    if len(title)>=8 and origin:
        aliases.add('exact_title:'+str(origin)+':'+instant(article_reported_at(primary)).date().isoformat()+':'+title)
    day=instant(article_reported_at(primary)).date().isoformat()
    # 换发布机构或换文章 ID，不能让同一标题的转载获得第二个位置。
    if len(title)>=8:aliases.add('same_day_title:'+day+':'+title)
    headline=re.sub(r'[\s，,。！!？?·丨|—－-]','',event.get('headline','')).lower()
    if len(headline)>=8:aliases.add('same_day_headline:'+day+':'+headline)
    statistical_release=_china_gdp_release_alias(event,primary)
    if statistical_release:aliases.add(statistical_release)
    innovation_release=innovation_release_alias(event,primary)
    if innovation_release:aliases.add(innovation_release)
    aliases.update(source_release_aliases(event, primary))
    # 不同标题的开幕综述也会重复同场会议。只识别开幕/召开报道，
    # 不把论坛中的降息、签署等具体新决定按共同会议名称合并。
    opening=event.get('headline','')
    if not re.search(r'宣布|决定|下调|上调|调整|降准|降息|加息|签署',opening):
        opening=re.sub(r'第[零〇一二三四五六七八九十百千0-9]+届|20\d{2}年?','',opening)
        meeting=re.search(r'([\u4e00-\u9fffA-Za-z·]{2,30}(?:论坛|峰会|大会)(?:年会)?).{0,8}?(?:开幕|启幕|召开|举行|举办)',opening)
        if meeting:aliases.add('same_day_meeting_opening:'+day+':'+meeting[1])
    # A shared numeric quote can be an effective-date boilerplate for different policies.
    # It is not an identity unless an independently reviewed canonical ID links the objects.
    return aliases


def distinct_event_count(events):
    """Count source-bound identity groups without changing stored snapshots."""
    groups=[]
    try:
        for event in events:
            aliases=event_aliases(event)
            linked=[i for i,group in enumerate(groups) if aliases & group]
            for i in reversed(linked):aliases |= groups.pop(i)
            groups.append(aliases)
    except (KeyError,TypeError,ValueError,AttributeError):
        return None
    return len(groups)

class DailyMarket:
    def __init__(self, store): self.store=store

    @staticmethod
    def schema(db):
        db.executescript('''
          CREATE TABLE IF NOT EXISTS market_daily_editions(
            edition_date TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
            timezone TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('observed','reconstructed')),
            period_state TEXT NOT NULL CHECK(period_state IN ('updating','sealed')),
            selected_at TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT NOT NULL,
            rule_version TEXT NOT NULL, item_count INTEGER NOT NULL CHECK(item_count BETWEEN 0 AND 6),
            fingerprint TEXT NOT NULL, coverage TEXT NOT NULL,
            PRIMARY KEY(edition_date,revision));
          CREATE TABLE IF NOT EXISTS market_daily_items(
            edition_date TEXT NOT NULL, revision INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 6),
            event_id TEXT NOT NULL, event_version INTEGER NOT NULL,
            primary_article_id TEXT NOT NULL, article_versions TEXT NOT NULL,
            category TEXT NOT NULL, theme TEXT NOT NULL, display_version TEXT NOT NULL,
            selection_reason TEXT NOT NULL, snapshot TEXT NOT NULL,
            PRIMARY KEY(edition_date,revision,position), UNIQUE(edition_date,revision,event_id),
            FOREIGN KEY(edition_date,revision) REFERENCES market_daily_editions(edition_date,revision));
          CREATE INDEX IF NOT EXISTS market_daily_current ON market_daily_editions(edition_date,revision DESC);
          CREATE TABLE IF NOT EXISTS market_daily_checks(
            run_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, edition_date TEXT NOT NULL,
            valid INTEGER NOT NULL, coverage TEXT NOT NULL);
          CREATE TRIGGER IF NOT EXISTS market_daily_same_day_publication
          BEFORE INSERT ON market_daily_items
          WHEN (SELECT origin FROM market_daily_editions
                WHERE edition_date=NEW.edition_date AND revision=NEW.revision)='observed'
            AND NOT EXISTS(
              SELECT 1 FROM json_each(NEW.snapshot,'$.articles') article
              WHERE json_extract(article.value,'$.id')=NEW.primary_article_id
                AND COALESCE(json_extract(article.value,'$.reported_time_kind'),'published_at')!='source_page_updated_at'
                AND date(json_extract(article.value,'$.published_at'),'+8 hours')=NEW.edition_date
                AND julianday(json_extract(article.value,'$.published_at'))<=
                    (SELECT julianday(selected_at) FROM market_daily_editions
                     WHERE edition_date=NEW.edition_date AND revision=NEW.revision)
            )
          BEGIN SELECT RAISE(ABORT,'market_daily_requires_same_day_publication'); END;
        ''')

    def clock(self, now=None):
        now=now or datetime.now(CN)
        return (now.replace(tzinfo=CN) if now.tzinfo is None else now).astimezone(CN)
    def window(self, now=None):
        today=self.clock(now).date(); return today-timedelta(days=29),today
    def validate_date(self, value, now=None):
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise MarketDailyError('invalid_date')
        try: parsed=date.fromisoformat(value)
        except ValueError:raise MarketDailyError('invalid_date')
        start,today=self.window(now)
        if parsed>today:raise MarketDailyError('future_date')
        if parsed<start:raise MarketDailyError('outside_history_window')
        return parsed
    @staticmethod
    def validate_category(value):
        value=value or 'all'
        if value!='all' and value not in CATEGORY_IDS:raise MarketDailyError('unknown_category')
        return value
    @staticmethod
    def validate_revision(value):
        if value is None:return None
        if isinstance(value,bool) or not re.fullmatch(r'[1-9]\d{0,8}',str(value)):raise MarketDailyError('invalid_revision')
        return int(value)
    def metadata(self, now=None):
        now=self.clock(now);start,today=self.window(now)
        return {'schema_version':1,'timezone':'Asia/Shanghai','server_today':today.isoformat(),'server_now':now.isoformat(),
            'window_start':start.isoformat(),'window_end':today.isoformat(),'categories':CATEGORIES}

    @staticmethod
    def row_body(row): return {**json.loads(row['body']),'status':row['status']}
    @staticmethod
    def event_row(db, id, version=None):
        if version is None:return db.execute('SELECT * FROM market_events WHERE id=? ORDER BY version DESC LIMIT 1',(id,)).fetchone()
        return db.execute('SELECT * FROM market_events WHERE id=? AND version=?',(id,version)).fetchone()
    @staticmethod
    def article_row(db, id, version=None):
        if version is None:return db.execute('SELECT * FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(id,)).fetchone()
        return db.execute('SELECT * FROM market_articles WHERE id=? AND version=?',(id,version)).fetchone()

    def bound_articles(self, db, event):
        ids=event.get('article_ids',[]); versions=event.get('article_versions',{})
        if not ids or set(ids)!=set(versions):return None
        rows=[self.article_row(db,i,versions[i]) for i in ids]
        if any(r is None for r in rows):return None
        return [self.row_body(r) for r in rows]

    def trusted_event(self,db,row,snapshot=None):
        self.read_snapshot(db)
        try:
            if row is None or row['status']!='published':return False
            event=self.row_body(row)
            self.store._validate_event_publication(db,event['id'],event,event.get('input_hash'),require_publication_stamp=True)
            if snapshot is not None:
                fields=('generation','review','summary','analysis','uncertainty','facts','conflicts','source_check','source_supported_summary','agent_editorial_check','selection_attention')
                if snapshot.get('id')!=event['id'] or snapshot.get('version')!=row['version'] or any(snapshot.get(k)!=event.get(k) for k in fields):return False
                if snapshot.get('primary_article_id')!=event.get('primary_article_id'):return False
                if event.get('content_mode')!='source_brief':
                    primary,published=event_primary_publication(event,self.bound_articles(db,event) or [])
                    if snapshot.get('reported_at')!=primary['published_at'] or snapshot.get('reported_precision')!=primary.get('published_precision'):return False
            return True
        except (ValueError,KeyError,TypeError,AttributeError):return False

    def source_brief_snapshot(self, db, row):
        if not self.trusted_event(db,row):return None
        event=self.row_body(row);articles=self.bound_articles(db,event)
        if row['status']!='published' or not articles or not source_verified_brief_event(event,articles):return None
        if any(a['status']!='active' or self.article_row(db,a['id'])['version']!=a['version'] for a in articles):return None
        main=articles[0];category=classify(event,articles)
        snapshot={k:v for k,v in event.items() if k!='input_hash'}
        snapshot.update(articles=[self.store.public_article(main)],category=category,theme=theme(event,category),
            primary_article_id=main['id'],reported_at=main['reported_at'],reported_time_kind='source_page_updated_at',
            reported_precision=main.get('reported_precision','minute'),display_title=main['title'],display_summary=event['summary'],
            display_fact=None,display_conditions='官方文字简介；未读取视频内容',card_template='source_brief',
            display_version='brief:'+hashlib.sha256(packed([event['id'],event['version'],event['article_versions'],event['summary'],event['reported_at']]).encode()).hexdigest()[:16],
            cover_status='source_verified_brief',cover_reason='official_page_title_brief_date_binding',
            cover_review={'production_approved':False,'new_cloud_review':'not_run'})
        return snapshot

    def source_text_snapshot(self, db, row):
        if not self.trusted_event(db,row):return None
        event=self.row_body(row);articles=self.bound_articles(db,event)
        if row['status']!='published' or not articles or not source_verified_text_event(event,articles):return None
        if any(a['status']!='active' or self.article_row(db,a['id'])['version']!=a['version'] for a in articles):return None
        main=articles[0];category=classify(event,articles)
        snapshot={k:v for k,v in event.items() if k!='input_hash'}
        snapshot.update(articles=[self.store.public_article(main)],category=category,theme=theme(event,category),
            primary_article_id=main['id'],reported_at=main['published_at'],reported_time_kind='published_at',
            reported_precision=main.get('published_precision'),display_title=main['title'],display_summary=event['summary'],
            display_fact=None,display_conditions='原文文字摘要',card_template='source_verified_text',
            display_version='text:'+hashlib.sha256(packed([event['id'],event['version'],event['article_versions'],event['summary'],event['reported_at']]).encode()).hexdigest()[:16],
            cover_status='source_verified_text',cover_reason='official_page_title_text_date_binding',
            cover_review={'production_approved':False,'new_cloud_review':'not_run'})
        return snapshot

    def approved_snapshot(self, db, row, display):
        if not self.trusted_event(db,row):return None
        event=self.row_body(row)
        if event.get('content_mode')=='source_brief':return None
        if event.get('content_mode')=='source_verified_text':return self.source_text_snapshot(db,row)
        if row['status']!='published' or event.get('review',{}).get('status')!='approved':return None
        articles=self.bound_articles(db,event)
        if not articles or any(a['status']!='active' or not text_only_article(a) for a in articles):return None
        if compound_announcement_issues(event,articles):return None
        if any(self.article_row(db,a['id'])['version']!=a['version'] for a in articles):return None
        primary=event.get('primary_article_id') or event['article_ids'][0]
        if primary not in event['article_ids']:return None
        main=next(a for a in articles if a['id']==primary)
        category=classify(event,articles)
        # Raw body stays in the public store; frontend snapshots never carry the source full text.
        public=[self.store.public_article(a) for a in articles]
        snapshot={k:v for k,v in event.items() if k not in ('input_hash',)}
        snapshot.update(articles=public,category=category,theme=theme(event,category),
            primary_article_id=primary,reported_at=main['published_at'],reported_precision=main.get('published_precision'))
        short=display.get(event['id'])
        if short and short.get('event_version')==event['version'] and short.get('article_versions')==event['article_versions'] and short.get('content_version')==event.get('content_version'):
            snapshot.update({k:v for k,v in short.items() if k in (
                'display_title','display_summary','display_fact','display_conditions','display_version','card_template',
                'display_title_en','display_summary_en','display_conditions_en')})
            if not snapshot.get('display_version'):snapshot['display_version']='event:'+str(event['version'])
        else:
            snapshot.update(display_title=event['headline'],display_summary=event.get('summary',''),
                display_version='event:'+str(event['version']))
        return snapshot

    @staticmethod
    def display_pack():
        try:
            data=json.loads((Path(__file__).resolve().parent/'assets/market_display.json').read_text())
            if data.get('review',{}).get('status')!='approved':return {}
            return {e['id']:e for e in data.get('events',[])}
        except (OSError,ValueError,KeyError):return {}

    @staticmethod
    def read_snapshot(db):
        # Reuse an existing read/write transaction; never commit a caller transaction.
        if not db.in_transaction:db.execute('BEGIN')

    def selection_eligibility(self,db,event,now):
        try:
            articles=self.bound_articles(db,event) or []
            if compound_announcement_issues(event,articles):return None
            primary,published=event_primary_publication(event,articles)
            now=self.clock(now)
            age=(now-published).total_seconds()/3600
            if not first_publication_verified(primary):return None
            if primary.get('reported_time_kind')=='source_page_updated_at':return None
            if age<0 or published.date()!=now.date():return None
            return {'freshness_layer':0,'age_hours':age,'publication_date':published.date().isoformat()}
        except (ValueError,KeyError,TypeError,AttributeError):return None

    def candidates(self, db, now, reconstructed_date=None):
        self.read_snapshot(db)
        rows=db.execute('SELECT e.* FROM market_events e JOIN (SELECT id,MAX(version) version FROM market_events GROUP BY id) latest ON e.id=latest.id AND e.version=latest.version').fetchall()
        display=self.display_pack();out=[]
        for row in rows:
            try:
                event=self.approved_snapshot(db,row,display)
                if event is None:continue
                primary,main=event_primary_publication(event,event['articles'])
                if not first_publication_verified(primary):continue
                age=(now-main).total_seconds()/3600
            except (ValueError,KeyError,TypeError,AttributeError):continue
            if reconstructed_date:
                if main.date()!=reconstructed_date:continue
                end=datetime.combine(reconstructed_date+timedelta(days=1),time(),CN)
                # A later supporting article invalidates reconstruction of the combined summary.
                try:
                    if any(datetime.fromisoformat(article_reported_at(a)).tzinfo is None or instant(article_reported_at(a))>=end for a in event['articles']):continue
                    late_revision=any(a.get('content_updated_at') and instant(a['content_updated_at'])>=end for a in event['articles'])
                except (ValueError,KeyError,TypeError):continue
                event['after_revision_material']=bool(late_revision or event['version']>1 or any(a['version']>1 for a in event['articles']))
                event['revision_confirmed_at']=max((a.get('content_updated_at') for a in event['articles'] if a.get('content_updated_at')),default=None)
            else:
                eligibility=self.selection_eligibility(db,event,now)
                if eligibility is None:continue
            reason=event.get('ranking_reason',{})
            importance=float(reason.get('importance') if reason.get('importance') is not None else event.get('score') or 0)
            household=1 if event['category']=='household' else .65 if event['category']=='macro_policy' else .45
            if reconstructed_date:
                age=(datetime.combine(reconstructed_date+timedelta(days=1),time(),CN)-main).total_seconds()/3600
            novelty=float(reason.get('new_fact',.5));recency=max(0,1-max(0,age)/24)
            score=round(.35*importance+.25*household+.25*novelty+.15*recency,6)
            event['selection_reason']={'rule_version':RULE_VERSION,'score':score,'impact':importance,
                'personal_finance_relevance':household,'new_information':novelty,'recency':recency,
                'same_day_publication':True,'weights_are_design_parameters':True,
                'evidence_refs':[{'article_id':a['id'],'version':a['version']} for a in event['articles']]}
            event['selection_reason'].update({'freshness_layer':0} if reconstructed_date else eligibility)
            event['selection_reason']['recency_policy']='primary_original_publication_same_shanghai_day'
            out.append(event)
        # 已有完整六条不能让后续时段的新报道永远被旧分数压住；
        # 新原发先替换，之后保留仍有效的当日卡片，避免下一次检查又回滚。
        prior=self.edition(db,now.date().isoformat())
        if not reconstructed_date and prior and prior['item_count']==6:
            pins={(r['event_id'],r['event_version']):r['position'] for r in db.execute('SELECT event_id,event_version,position FROM market_daily_items WHERE edition_date=? AND revision=?',(prior['edition_date'],prior['revision']))}
            incumbents=[e for e in out if (e['id'],e['version']) in pins]
            if incumbents:
                watermark=max(instant(e['reported_at']) for e in incumbents)
                fresh={e['id'] for e in out if instant(e['reported_at'])>watermark}
                for e in out:
                    position=pins.get((e['id'],e['version']))
                    layer=(0 if e['id'] in fresh else position if position else 7) if fresh else (position-1 if position else 6)
                    e['selection_reason'].update(freshness_layer=layer,refresh_policy='verified_new_publication_then_retained_six')
        out.sort(key=lambda e:(e['selection_reason']['freshness_layer'],-e['selection_reason']['score'],-instant(e['reported_at']).timestamp(),e['id']))
        parents=list(range(len(out)));seen={};links={}
        def find(i):
            while parents[i]!=i:parents[i]=parents[parents[i]];i=parents[i]
            return i
        for i,event in enumerate(out):
            for alias in sorted(event_aliases(event)):
                if alias in seen:
                    a,b=find(i),find(seen[alias]);parents[max(a,b)]=min(a,b)
                    links.setdefault(i,[]).append(alias)
                else:seen[alias]=i
        groups={}
        for i,event in enumerate(out):groups.setdefault(find(i),[]).append((i,event))
        unique=[]
        for entries in groups.values():
            chosen=entries[0][1]
            primary=next(a for a in chosen['articles'] if a['id']==chosen['primary_article_id'])
            chosen['selection_reason']['event_group']=chosen.get('canonical_event_id') or chosen.get('event_group_id') or cluster_key(primary)
            for i,event in entries[1:]:
                chosen.setdefault('related_event_refs',[]).append({'id':event['id'],'version':event['version'],
                    'primary_article_id':event['primary_article_id'],'article_versions':event['article_versions']})
                chosen['selection_reason'].setdefault('group_evidence',[]).extend(links.get(i,[]))
            unique.append(chosen)
        out=unique
        picks=[]
        # Diversity only breaks near-score ties; never promotes lower quality material.
        while out and len(picks)<6:
            near=[e for e in out if e['selection_reason']['freshness_layer']==out[0]['selection_reason']['freshness_layer'] and out[0]['selection_reason']['score']-e['selection_reason']['score']<=.025]
            category_counts={e['category']:sum(p['category']==e['category'] for p in picks) for e in near}
            def key(e):
                publisher=e['articles'][0].get('origin_publisher') or e['articles'][0].get('publisher')
                count=sum((p['articles'][0].get('origin_publisher') or p['articles'][0].get('publisher'))==publisher for p in picks)
                return (count,category_counts[e['category']],-e['selection_reason']['score'],-instant(e['reported_at']).timestamp(),e['id'])
            chosen=min(near,key=key);picks.append(chosen);out.remove(chosen)
        return picks

    def write_edition(self, db, day, items, now, origin, run_id, coverage, *, replace_observed=False):
        if not db.in_transaction:db.execute('BEGIN IMMEDIATE')
        if len(items)>6 or len({x['id'] for x in items})!=len(items):raise MarketDailyError('invalid_selection')
        if any(not e.get('articles') or any(not text_only_article(a) for a in e['articles']) for e in items):raise MarketDailyError('non_text_daily_selection')
        if replace_observed and any(not source_verified_text_event(e,self.bound_articles(db,e) or []) for e in items):raise MarketDailyError('replacement_requires_verified_text')
        old=db.execute('SELECT * FROM market_daily_editions WHERE edition_date=? ORDER BY revision DESC LIMIT 1',(day,)).fetchone()
        if old and old['origin']=='observed' and origin=='reconstructed' and not replace_observed:return {'date':day,'revision':old['revision'],'changed':False,'skipped':'observed_exists'}
        fingerprint=hashlib.sha256(packed([(e['id'],e['version'],e['article_versions'],e['category'],e['theme'],e['display_version'],
            e.get('related_event_refs',[]),{k:e.get(k) for k in ('display_title','display_summary','display_fact','display_conditions','display_title_en','display_summary_en','display_conditions_en')}) for e in items]).encode()).hexdigest()
        for e in items:
            # All bindings are checked again within the same write transaction.
            if not self.trusted_event(db,self.event_row(db,e['id'],e['version']),e) or self.bound_articles(db,e) is None:raise MarketDailyError('missing_snapshot_version')
        if _uses_live_publication_clock(self) and origin=='observed':
            # Read the real clock after lock acquisition and all binding checks.
            # An explicit test clock remains fixed; reconstructed history is inert.
            now=self.clock()
            if day!=now.date().isoformat():raise MarketDailyError('daily_selection_date_changed')
        for e in items:
            if origin=='observed' and self.selection_eligibility(db,e,now) is None:raise MarketDailyError('daily_selection_eligibility_expired')
        if old and old['fingerprint']==fingerprint and old['origin']==origin and old['rule_version']==RULE_VERSION:return {'date':day,'revision':old['revision'],'changed':False}
        revision=old['revision']+1 if old else 1
        state='updating' if day==now.date().isoformat() else 'sealed'
        db.execute('INSERT INTO market_daily_editions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (day,revision,'Asia/Shanghai',origin,state,now.isoformat(),now.isoformat(),run_id,RULE_VERSION,len(items),fingerprint,packed(coverage)))
        for pos,event in enumerate(items,1):
            db.execute('INSERT INTO market_daily_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (day,revision,pos,event['id'],event['version'],event['primary_article_id'],packed(event['article_versions']),event['category'],event['theme'],event['display_version'],packed(event['selection_reason']),packed(event)))
        return {'date':day,'revision':revision,'changed':True,'origin':origin,'count':len(items)}

    def update(self, *, now=None, run_id, valid_check, coverage=None):
        live_clock=now is None;now=self.clock(now);coverage=coverage or {}
        required=coverage.get('required_count')
        if required is not None and (type(required) is not int or required!=6):raise MarketDailyError('invalid_daily_target')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE market_daily_editions SET period_state='sealed' WHERE edition_date<? AND period_state='updating'",(now.date().isoformat(),))
            db.execute('INSERT INTO market_daily_checks VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET checked_at=excluded.checked_at,valid=excluded.valid,coverage=excluded.coverage',
                (run_id,now.isoformat(),now.date().isoformat(),int(valid_check),packed(coverage)))
            if not valid_check:return {'changed':False,'skipped':'check_failed'}
            items=self.candidates(db,now)
            if required is not None and len(items)<required:
                # 不以不足六条的新日版替换上一完整日版；已审核事件保留，
                # 下次采集可继续补齐，不能改日期、复制事件或放宽来源门禁。
                return {'changed':False,'skipped':'daily_target_not_met','count':len(items),'required_count':required}
            # Partial checks can publish verified material, but cannot prove an empty day.
            if not items and coverage.get('limited'):return {'changed':False,'skipped':'coverage_insufficient'}
            token=_publication_clock_scope.set((self,_clock_execution_owner()) if live_clock else None)
            try:
                result=self.write_edition(db,now.date().isoformat(),items,now,'observed',run_id,coverage)
                if required is not None:result.update(count=len(items),required_count=required)
                return result
            finally:_publication_clock_scope.reset(token)

    def seal(self, now=None):
        with self.store.db() as db:
            db.execute("UPDATE market_daily_editions SET period_state='sealed' WHERE edition_date<? AND period_state='updating'",(self.clock(now).date().isoformat(),))

    def _checks(self, db, day):
        rows=db.execute('SELECT * FROM market_daily_checks WHERE edition_date=? ORDER BY checked_at DESC',(day,)).fetchall()
        success=next((r['checked_at'] for r in rows if r['valid']),None)
        return {'last_check_at':rows[0]['checked_at'] if rows else None,'last_success_at':success,
            'latest_check_coverage':json.loads(rows[0]['coverage']) if rows else None}

    def checks(self, day):
        with self.store.db() as db:
            self.read_snapshot(db)
            return self._checks(db,day)

    def reconstruct(self, *, now=None, run_id='initial-history-backfill'):
        now=self.clock(now);start,today=self.window(now);results=[]
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for offset in range(30):
                day=start+timedelta(days=offset)
                items=self.candidates(db,now,reconstructed_date=day)
                if not items:continue # No reliable materials means missing, not a synthetic zero-day.
                results.append(self.write_edition(db,day.isoformat(),items,now,'reconstructed',run_id,{'limited':True,'scope':'verified_existing_materials','backfilled_at':now.isoformat()}))
        return {'backfilled_at':now.isoformat(),'window_start':start.isoformat(),'window_end':today.isoformat(),'days':results}

    def edition(self, db, day, revision=None):
        if revision is None:return db.execute('SELECT * FROM market_daily_editions WHERE edition_date=? ORDER BY revision DESC LIMIT 1',(day,)).fetchone()
        return db.execute('SELECT * FROM market_daily_editions WHERE edition_date=? AND revision=?',(day,revision)).fetchone()

    def availability(self, db, snapshot):
        self.read_snapshot(db)
        latest=self.event_row(db,snapshot['id']);pointer=None
        if latest and latest['status']=='published' and latest['version']!=snapshot['version']:
            pointer={'id':snapshot['id'],'version':latest['version'],'released_at':latest['released_at'],'kind':'event'}
        states=[]
        if not self.trusted_event(db,self.event_row(db,snapshot['id'],snapshot['version']),snapshot):states.append('temporarily_unavailable')
        for id,version in snapshot['article_versions'].items():
            current=self.article_row(db,id)
            if current is None:states.append('temporarily_unavailable');continue
            if current['status'] in WITHDRAWN:states.append('withdrawn')
            elif current['version']!=version:states.append('revised')
            elif current['status']!='active' or not evidence_readable(self.row_body(current)):states.append('temporarily_unavailable')
        if latest is None:states.append('temporarily_unavailable')
        elif latest['status'] in WITHDRAWN:states.append('withdrawn')
        elif latest['version']!=snapshot['version'] or latest['status']=='source_revised':states.append('revised')
        elif latest['status']!='published':states.append('temporarily_unavailable')
        if not states and compound_announcement_issues(snapshot,self.bound_articles(db,snapshot) or []):
            states.append('temporarily_unavailable')
        status=next((s for s in ('withdrawn','revised','temporarily_unavailable') if s in states),'active')
        return status,pointer

    def expose(self, db, item, edition, detail=False):
        snapshot=json.loads(item['snapshot']);state,pointer=self.availability(db,snapshot)
        meta={'edition_date':edition['edition_date'],'revision':edition['revision'],'position':item['position'],
            'origin':edition['origin'],'event_version':item['event_version'],'primary_article_id':item['primary_article_id'],
            'article_versions':json.loads(item['article_versions']),'selected_at':edition['selected_at']}
        result={**snapshot,'archive':meta,'availability':state,'latest_pointer':pointer,'status':'published' if state=='active' else state,
            'selection_reason':json.loads(item['selection_reason'])}
        if state!='active':
            # An inactive immutable snapshot may contain top-level source_check,
            # nested editorial proof, or prior reading content. Retain only
            # explicitly typed metadata, never arbitrary snapshot fields.
            metadata={'id','version','content_version','headline','display_title','display_title_en',
                'published_at','reported_at','reported_precision','reported_time_kind','category','theme',
                'primary_article_id','content_mode','card_template','display_version','cover_status',
                'cover_reason','after_revision_material','revision_confirmed_at'}
            result={k:v for k,v in snapshot.items() if k in metadata and (v is None or isinstance(v,(str,int,bool)))}
            reason=json.loads(item['selection_reason'])
            reason_fields={'rule_version','score','impact','personal_finance_relevance','new_information',
                'recency','within_72_hours','weights_are_design_parameters','event_group'}
            safe_reason={k:v for k,v in reason.items() if k in reason_fields and isinstance(v,(str,int,float,bool))}
            if isinstance(reason.get('evidence_refs'),list):
                safe_reason['evidence_refs']=[{'article_id':ref['article_id'],'version':ref['version']}
                    for ref in reason['evidence_refs'] if isinstance(ref,dict)
                    and isinstance(ref.get('article_id'),str) and type(ref.get('version')) is int and ref['version']>0]
            ids=snapshot.get('article_ids',list(meta['article_versions']))
            ids=[i for i in ids if isinstance(i,str) and i in meta['article_versions']] if isinstance(ids,list) else list(meta['article_versions'])
            result.update(archive=meta,availability=state,latest_pointer=pointer,status=state,
                article_ids=ids,article_versions=dict(meta['article_versions']),
                selection_reason=safe_reason)
            for key in ('summary','analysis','uncertainty','display_summary','display_conditions','transmission',
                'display_summary_en','display_conditions_en'):result[key]=''
            for key in ('facts','conflicts'):result[key]=[]
            result.pop('display_fact',None)
            exact=self.event_row(db,snapshot['id'],snapshot['version'])
            if exact and exact['status']=='editorial_review_required':
                result.update(headline='这条解读正在重新核验',display_title='这条解读正在重新核验',
                    display_title_en=None,availability_reason='editorial_review_required')
            from public_market_history import metadata_only
            result['articles']=[{**metadata_only(a),'status':state} for a in snapshot['articles']]
        if not detail:
            for key in ('facts','analysis','uncertainty'):result.pop(key,None)
        result.pop('ranking_reason',None)
        return result

    def day(self, day, category='all', *, now=None, revision=None):
        self.validate_date(day,now);category=self.validate_category(category);revision=self.validate_revision(revision)
        with self.store.db() as db:
            self.read_snapshot(db)
            return self._read_day(db,day,category,now=now,revision=revision)

    def _read_day(self, db, day, category='all', *, now=None, revision=None):
        self.read_snapshot(db)
        edition=self.edition(db,day,revision)
        if revision and edition is None:raise MarketDailyError('edition_not_found',404)
        if edition is None:return {**self.metadata(now),**self._checks(db,day),'edition_date':day,'status':'missing_archive','archive_state':'missing','total_count':None,'matched_count':None,'original_count':None,'readable_count':None,'distinct_event_count':None,'events':[],'revision':None,'origin':None,'period_state':None,'category':category}
        rows=db.execute('SELECT * FROM market_daily_items WHERE edition_date=? AND revision=? ORDER BY position',(day,edition['revision'])).fetchall()
        all_events=[self.expose(db,r,edition) for r in rows]
        distinct_count=distinct_event_count([json.loads(r['snapshot']) for r in rows])
        events=[e for e in all_events if category=='all' or e['category']==category]
        today=self.clock(now).date().isoformat()
        return {**self.metadata(now),**self._checks(db,day),'edition_date':day,'revision':edition['revision'],'origin':edition['origin'],
            'period_state':'sealed' if day<today else edition['period_state'],'selected_at':edition['selected_at'],
            'created_at':edition['created_at'],'rule_version':edition['rule_version'],'content_version':edition['fingerprint'][:16],
            'status':'published' if events else 'no_matching_category' if rows else 'no_qualified_hotspots',
            'archive_state':'archived' if rows else 'no_qualified','total_count':edition['item_count'],
            'distinct_event_count':distinct_count,
            'matched_count':len(events),'original_count':edition['item_count'],'readable_count':sum(e['availability']=='active' for e in all_events),
            'matched_readable_count':sum(e['availability']=='active' for e in events),'category':category,'events':events,
            'selection_coverage':json.loads(edition['coverage'])}

    def index(self, from_date=None, to_date=None, category='all', *, now=None):
        start,today=self.window(now);first=self.validate_date(from_date or start.isoformat(),now);last=self.validate_date(to_date or today.isoformat(),now)
        if first>last:raise MarketDailyError('invalid_date_range')
        category=self.validate_category(category);days=[]
        with self.store.db() as db:
            self.read_snapshot(db)
            for offset in range((last-first).days+1):
                data=self._read_day(db,(first+timedelta(days=offset)).isoformat(),category,now=now)
                days.append({'date':data['edition_date'],**{k:data.get(k) for k in ('archive_state','origin','revision','total_count','matched_count','readable_count','period_state')}})
        default=next((d['date'] for d in reversed(days) if d['readable_count']),None)
        return {**self.metadata(now),'dates':days,'category':category,'default_date':default,'status':'ready' if any(d['archive_state']!='missing' for d in days) else 'no_archives'}

    def event(self, day, id, revision=None, *, now=None, expected_version=None):
        self.validate_date(day,now);revision=self.validate_revision(revision)
        if not isinstance(id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',id):raise MarketDailyError('invalid_event_id')
        with self.store.db() as db:
            self.read_snapshot(db)
            edition=self.edition(db,day,revision)
            row=db.execute('SELECT * FROM market_daily_items WHERE edition_date=? AND revision=? AND event_id=?',(day,edition['revision'] if edition else 0,id)).fetchone()
            if not row:raise MarketDailyError('historical_event_not_found',404)
            if expected_version is not None and self.validate_revision(expected_version)!=row['event_version']:raise MarketDailyError('historical_version_conflict',409)
            return self.expose(db,row,edition,detail=True)

    def hotspots(self, *, now=None):
        now=self.clock(now);meta=self.metadata(now);today=meta['server_today']
        with self.store.db() as db:
            self.read_snapshot(db)
            current=self._read_day(db,today,now=now)
            current_meta={k:current.get(k) for k in ('edition_date','revision','status','readable_count','total_count','distinct_event_count')}
            result=None
            # 单次读事务内选择最近完整且仍有效的六条；局部失效不能展示残缺的2/4条。
            rows=db.execute('SELECT edition_date,revision FROM market_daily_editions WHERE edition_date BETWEEN ? AND ? AND item_count=6 ORDER BY edition_date DESC,revision DESC',(meta['window_start'],today)).fetchall()
            for row in rows:
                packet=self._read_day(db,row['edition_date'],now=now,revision=row['revision'])
                if len(packet['events'])!=6 or packet.get('distinct_event_count')!=6 or any(e.get('availability')!='active' for e in packet['events']):continue
                valid=True
                for event in packet['events']:
                    try:
                        primary,published=event_primary_publication(event,event.get('articles',[]))
                        if primary.get('reported_time_kind')=='source_page_updated_at' or published.date().isoformat()!=row['edition_date'] or published>now:valid=False;break
                    except (ValueError,KeyError,TypeError,AttributeError):valid=False;break
                if valid:result=packet;break
            if result is None:
                result={**current,'events':[],'status':'no_qualified_hotspots'}
            events=result['events'];fallback=bool(events and (result['edition_date']!=today or result.get('revision')!=current.get('revision')))
            result.update(current_edition=current_meta,requested_edition_date=today,is_fallback=fallback,
                display_mode='retained' if fallback else 'current' if events else 'empty')
            for field in ('total_count','matched_count','readable_count','matched_readable_count'):result[field]=len(events)
        result['as_of']=max((e['reported_at'] for e in events),default=None)
        result['reading']=[]
        result['coverage']=self.store.status()
        return result

    def route(self, path, query, *, now=None):
        """Validated contract for the main HTTP dispatcher, unknown query fields rejected."""
        def params(allowed):
            if set(query)-set(allowed):raise MarketDailyError('unknown_query_parameter')
            out={}
            for k,v in query.items():
                if isinstance(v,list):
                    if len(v)!=1:raise MarketDailyError('duplicate_query_parameter')
                    v=v[0]
                if not isinstance(v,str) or not v:raise MarketDailyError('invalid_query_parameter')
                out[k]=v
            return out
        if path=='/api/market/hotspots':params(());return self.hotspots(now=now)
        if path=='/api/market/hotspots/history':
            p=params(('from','to','category'));return self.index(p.get('from'),p.get('to'),p.get('category','all'),now=now)
        match=re.fullmatch(r'/api/market/hotspots/history/([^/]+)(?:/events/([^/]+))?',path)
        if not match:raise MarketDailyError('unknown_market_route',404)
        if match[2]:
            p=params(('revision',));return self.event(match[1],match[2],p.get('revision'),now=now)
        p=params(('category',));return self.day(match[1],p.get('category','all'),now=now)
