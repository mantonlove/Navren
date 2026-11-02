"""Candidate only: explicitly filtered public market JSON projection.

No server is modified by importing this module.  A separate, reviewed patch
would be needed both in the business server and its authentication gateway.
This is not a proof/replica of the server's internal evidence database.
"""
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
import re
import ipaddress
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit
from market_content import MarketStore
from market_sources import REGISTRY, source_verified_brief_event, source_verified_text_event

PREFIX = '/api/public-market/v1'
ID = r'[A-Za-z0-9_.:-]{1,160}'
DATE = r'\d{4}-\d{2}-\d{2}'
POS = r'[1-9][0-9]{0,8}'

class PublicProjectionError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)

def route_target(raw_path):
    """Resolve a fixed, read-only route without accepting a URL authority."""
    if not isinstance(raw_path, str) or len(raw_path)>500:
        raise PublicProjectionError('route_invalid')
    split=urlsplit(raw_path)
    if split.scheme or split.netloc or split.fragment or '%' in split.path or '\\' in split.path:
        raise PublicProjectionError('route_invalid')
    path=split.path
    if path == PREFIX+'/hotspots':
        allowed=();kind='hotspots';tail='/api/market/hotspots'
    elif path == PREFIX+'/hotspots/history':
        allowed=('from','to','category');kind='index';tail='/api/market/hotspots/history'
    else:
        match=re.fullmatch(PREFIX+'/hotspots/history/('+DATE+r')(?:/events/('+ID+'))?',path)
        if not match: raise PublicProjectionError('route_not_found',404)
        kind='event' if match[2] else 'day'
        allowed=('revision',) if kind=='event' else ('revision','category')
        tail='/api/market/hotspots/history/'+match[1]+('/events/'+match[2] if match[2] else '')
    try:query=parse_qs(split.query,keep_blank_values=True,strict_parsing=True)
    except ValueError as exc:raise PublicProjectionError('query_invalid') from exc
    if set(query)-set(allowed) or any(len(v)!=1 or not v[0] for v in query.values()):
        raise PublicProjectionError('query_invalid')
    if 'revision' in query and not re.fullmatch(POS,query['revision'][0]):
        raise PublicProjectionError('revision_invalid')
    return kind,tail,query

def _scalar(value):
    # Null is useful for unknown precision/version, but complex or secret
    # internal objects are never copied even when under an allowed key.
    if value is None or type(value) in (bool,int,float):return value
    if type(value) is str and len(value)<=12000:return value
    raise PublicProjectionError('field_invalid',503)

def _fields(obj,names):
    if type(obj) is not dict:raise PublicProjectionError('object_invalid',503)
    return {k:_scalar(obj[k]) for k in names if k in obj}

def _url(value,source_id):
    if type(value) is not str or len(value)>2048 or any(ord(c)<33 or ord(c)==127 for c in value):
        raise PublicProjectionError('url_invalid',503)
    try:
        p=urlsplit(value)
        host=(p.hostname or '').lower().rstrip('.')
        if p.scheme not in ('https','http') or not host or p.username is not None or p.password is not None or p.fragment:
            raise ValueError('authority')
        if p.port not in (None,80,443) or host=='localhost' or host.endswith(('.localhost','.local','.internal')):
            raise ValueError('host')
        try:
            address=ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or re.fullmatch(r'[0-9.]+',host) or not re.fullmatch(r'[a-z0-9.-]+',host):
                raise ValueError('host')
        else:
            if not address.is_global:raise ValueError('host')
        source=REGISTRY.get(source_id)
        if source is None or source.enabled is not True or host!=source.domain or not re.fullmatch(source.pattern,p.path):
            raise ValueError('source identity')
        # No query parameters are source-verified in this candidate. Reject
        # all of them rather than stripping or decoding an original URL and
        # accidentally identifying a different article or leaking credentials.
        if p.query:raise ValueError('unverified query')
    except (ValueError,UnicodeError) as exc:
        raise PublicProjectionError('url_invalid',503) from exc
    return value

ARTICLE_FIELDS=('id','version','title','title_cn','publisher','origin_publisher','source_id','identity',
                'published_at','published_precision','reported_at','reported_precision','reported_time_kind',
                'read_scope','link_status','access_status','status','content_mode','publication_platform',
                'reporting_body','byline_text','page_author','original_published_at','first_publication_status',
                'publication_time_kind','source_verified','video_content_read')
EVENT_FIELDS=('id','version','content_version','headline','summary','analysis','uncertainty','category','theme',
              'topic_id','published_at','reported_at','reported_precision','reported_time_kind','primary_article_id',
              'availability','status','display_title','display_summary','display_conditions','display_version',
              'display_title_en','display_summary_en','display_conditions_en','card_template',
              'after_revision_material','revision_confirmed_at','content_mode')
ARCHIVE_FIELDS=('edition_date','revision','position','origin','event_version','primary_article_id','selected_at')
PACKET_FIELDS=('schema_version','server_today','server_now','window_start','window_end','edition_date','revision',
               'origin','period_state','selected_at','created_at','content_version','status','archive_state',
               'total_count','distinct_event_count','matched_count','original_count','readable_count',
               'matched_readable_count','category','default_date','default_revision','requested_edition_date',
               'is_fallback','display_mode','as_of')
DATE_FIELDS=('date','archive_state','origin','revision','total_count','matched_count','readable_count',
             'distinct_event_count','period_state','complete_revision')

def project_article(article,active):
    result=_fields(article,ARTICLE_FIELDS)
    if active:
        try:
            result['url']=_url(article.get('url'),article.get('source_id'))
            result['public_link_state']='available'
        except PublicProjectionError:
            result['public_link_state']='unavailable'
        source_check=article.get('source_check')
        if isinstance(source_check,dict):
            result['source_check']=_fields(source_check,('method',))
    if not active:
        result.pop('title',None);result.pop('title_cn',None)
    return result

def project_event(event,detail=False,*,special_proven=False):
    result=_fields(event,EVENT_FIELDS)
    archive=event.get('archive')
    if archive is not None: result['archive']=_fields(archive,ARCHIVE_FIELDS)
    active=event.get('availability')=='active' and event.get('status')=='published'
    articles=event.get('articles',[])
    if not isinstance(articles,list) or len(articles)>25:raise PublicProjectionError('articles_invalid',503)
    mode=event.get('content_mode')
    if active and mode in ('source_brief','source_verified_text') and not special_proven:
        # The independently callable projection cannot prove source binding
        # from public booleans.  Fail the individual event closed until a
        # single-transaction raw-source proof is implemented and tested.
        active=False;result['availability']='reading_unavailable';result['status']='reading_unavailable'
        result['public_projection_state']='source_binding_not_proven'
    elif active and mode in ('source_brief','source_verified_text'):
        result.pop('analysis',None)
        result.pop('uncertainty',None)
        result['public_projection_state']='source_verified_only'
    result['articles']=[project_article(a,active) for a in articles]
    # Redact prior text even if an upstream regression accidentally returns it.
    if not active:
        for k in ('summary','analysis','uncertainty','display_summary','display_conditions',
                  'display_summary_en','display_conditions_en','headline','display_title','display_title_en'):
            result.pop(k,None)
        result['headline']='这条资料已不可用'
    elif detail:
        facts=event.get('facts',[])
        if not isinstance(facts,list) or len(facts)>100:raise PublicProjectionError('facts_invalid',503)
        result['facts']=[_fields(f,('text','article_id')) for f in facts]
    else:
        result.pop('analysis',None);result.pop('uncertainty',None)
    return result

def project_packet(data,kind,*,special_proofs=frozenset()):
    def key(event):
        archive=event.get('archive') or {}
        return event.get('id'),event.get('version'),archive.get('edition_date'),archive.get('revision')
    if kind=='event':return {**project_event(data,detail=True,special_proven=key(data) in special_proofs),'projection_contract':'navren.public-market.v1'}
    result=_fields(data,PACKET_FIELDS)
    if isinstance(data.get('current_edition'),dict):
        result['current_edition']=_fields(data['current_edition'],('edition_date','revision','status',
            'readable_count','total_count','distinct_event_count'))
        result['current_edition']['counts_scope']='upstream_only'
    if kind in ('hotspots','day'):
        events=data.get('events',[])
        if not isinstance(events,list) or len(events)>6:raise PublicProjectionError('events_invalid',503)
        result['events']=[project_event(e,special_proven=key(e) in special_proofs) for e in events]
        def projected_readable(event):
            articles=event.get('articles')
            return (event.get('availability')=='active' and event.get('status')=='published' and
                    bool(articles) and all(a.get('public_link_state')=='available' and
                    a.get('status')=='active' and a.get('link_status')=='verified' and
                    ((a.get('read_scope')=='full_text' and a.get('access_status')=='free_full_text') or
                     (event.get('content_mode')=='source_brief' and a.get('read_scope')=='source_brief' and
                      a.get('access_status')=='free_video_with_brief' and a.get('source_verified') is True and
                      a.get('video_content_read') is False and a.get('source_check',{}).get('method')=='official_page_title_brief_date_binding'))
                    for a in articles))
        matched=sum(projected_readable(e) for e in result['events'])
        if 'readable_count' in result:result['upstream_readable_count']=result['readable_count']
        if 'matched_readable_count' in result:result['upstream_matched_readable_count']=result['matched_readable_count']
        # A filtered day contains no information about omitted items.  Do not
        # claim a projection-wide count from the selected category.
        all_categories=kind=='hotspots' or data.get('category','all')=='all'
        if 'readable_count' in result:result['readable_count']=matched if all_categories else None
        if 'matched_readable_count' in result:result['matched_readable_count']=matched
        result['counts_scope']='projection_all' if all_categories else 'projection_matched_only'
        if matched<len(result['events']):result['public_projection_state']='partially_unavailable'
    if kind=='index':
        dates=data.get('dates',[])
        if not isinstance(dates,list) or len(dates)>31:raise PublicProjectionError('dates_invalid',503)
        result['dates']=[_fields(d,DATE_FIELDS) for d in dates]
        for d in result['dates']:d['counts_scope']='upstream_only'
        # Index counts describe the upstream day before this projection; only
        # an individual projected day can establish this contract's coverage.
        result['public_projection_state']='index_counts_upstream_only'
    if 'categories' in data:
        categories=data['categories']
        if not isinstance(categories,list) or len(categories)>20:raise PublicProjectionError('categories_invalid',503)
        result['categories']=[_fields(c,('id','name','name_en')) for c in categories]
    result['projection_contract']='navren.public-market.v1'
    return result

class ReadOnlyMarketStore(MarketStore):
    """The public reader never creates tables or opens a write connection."""
    def __init__(self,path):
        self.path=Path(path).resolve(strict=True)
        self._active_db=None

    @contextmanager
    def db(self):
        if self._active_db is not None:
            yield self._active_db
            return
        db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            self._active_db=db
            yield db
        finally:
            self._active_db=None
            db.close()

    def status(self):
        # The status object contains upstream source/collector metadata that
        # this public contract intentionally neither emits nor needs.
        return {}

def dispatch(store,raw_path,*,now=None):
    """Read only the configured market DB; no account or write connection."""
    kind,path,query=route_target(raw_path)
    reader=ReadOnlyMarketStore(store.path)
    # DailyMarket.read_snapshot opens BEGIN itself; this outer reader keeps
    # that same read-only transaction alive until projection is complete.
    with reader.db() as db:
        data=reader.route(path,query,now=now)
        proofs=_special_proofs(reader,db,data,kind)
        return project_packet(data,kind,special_proofs=proofs)

def _special_proofs(reader,db,data,kind):
    from market_daily import DailyMarket
    if not db.in_transaction:raise PublicProjectionError('snapshot_missing',503)
    market=DailyMarket(reader)
    events=[data] if kind=='event' else data.get('events',[]) if kind in ('hotspots','day') else []
    proven=set()
    for event in events:
        mode=event.get('content_mode')
        if mode not in ('source_brief','source_verified_text') or event.get('availability')!='active':continue
        try:
            archive=event['archive'];day=archive['edition_date'];rev=archive['revision']
            edition=market.edition(db,day,rev)
            item=db.execute('SELECT * FROM market_daily_items WHERE edition_date=? AND revision=? AND event_id=?',
                            (day,rev,event['id'])).fetchone()
            if edition is None or item is None:continue
            canonical=market.expose(db,item,edition,detail=(kind=='event'))
            if canonical!=event or canonical.get('availability')!='active':continue
            row=market.event_row(db,event['id'],event['version'])
            if not market.trusted_event(db,row,json.loads(item['snapshot'])):continue
            raw=market.row_body(row);articles=market.bound_articles(db,raw)
            if not articles or raw.get('headline')!=event.get('headline') or raw.get('content_mode')!=mode:continue
            verify=source_verified_brief_event if mode=='source_brief' else source_verified_text_event
            if not verify(raw,articles):continue
            derived=(market.source_brief_snapshot(db,row) if mode=='source_brief'
                     else market.source_text_snapshot(db,row))
            if not derived:continue
            bound_fields=('headline','summary','analysis','uncertainty','facts','display_title','display_summary',
                          'display_conditions','reported_at','reported_precision','reported_time_kind',
                          'primary_article_id','content_mode','card_template','display_version')
            if any(event.get(field)!=derived.get(field) for field in bound_fields if field in event):continue
            actual=[reader.public_article(a) for a in articles]
            if actual!=event.get('articles'):continue
            proven.add((event['id'],event['version'],day,rev))
        except (KeyError,ValueError,TypeError,AttributeError,sqlite3.Error):
            continue
    return frozenset(proven)
