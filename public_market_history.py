"""Unmerged explicit historical-audit provider; never changes latest/daily semantics.

Uses stored exact rows and the proposed monotonic source-permission schema.
Receipts are created locally; future consumers must not accept caller receipts as authority.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import hashlib
import json
import re
import sqlite3

from market_sources import REGISTRY, readable_article, source_brief_readable, text_only_article, source_verified_text_article

CONTRACT = 'navren.market-history-audit.v1'
PROVIDER_ID = 'navren.public-market-history.v1'
GENERATION = 'weekly-generation-v3'
MAX_VERSION = 999999999
META = frozenset('id period version title title_cn headline publisher source_id identity url published_at published_precision reported_at released_at read_scope link_status access_status'.split())

class HistoryError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)

def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)

def sha(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()

def body_sha(row):
    return hashlib.sha256(row['body'].encode()).hexdigest()

def positive(value):
    return type(value) is int and 1 <= value <= MAX_VERSION

def publication_sha(body):
    stable = {k:v for k,v in body.items() if k not in ('generated_at','released_at','version','status','content_version','schema_version','publication_sha256')}
    if isinstance(stable.get('review'),dict):
        stable['review'] = {k:v for k,v in stable['review'].items() if k != 'reviewed_at'}
    if isinstance(stable.get('reading'),list):
        stable['reading'] = [{k:v for k,v in a.items() if k not in ('checked_at','fetched_at')} if isinstance(a,dict) else a for a in stable['reading']]
    return sha(stable)

def metadata_only(value):
    """Positive whitelist: nested source checks, quotes and all old prose are absent."""
    return {k:deepcopy(v) for k,v in value.items() if k in META and isinstance(v,(str,int)) and not isinstance(v,bool)}

def source_permission_state(db, source_id, *, allow_source_brief=False):
    """Current dynamic permission identity, including denied epochs for snapshot checks."""
    if type(allow_source_brief) is not bool:
        raise HistoryError('invalid_source_permission_scope',400)
    source = REGISTRY.get(source_id)
    try:
        row = db.execute('SELECT enabled,revision FROM main.market_source_permissions WHERE source_id=?',(source_id,)).fetchone()
        configured = db.execute('SELECT definition FROM main.market_sources WHERE id=?',(source_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):
            raise
        return {'source_id':source_id,'enabled':False,'revision':None,'reason':'history_permission_binding_missing'}
    definition = json.loads(configured['definition']) if configured is not None else None
    brief_scope = (allow_source_brief and source_id == 'cctv_tv' and source is not None
                   and source.scope == 'source_brief_only')
    eligible = (source is not None and source.enabled is True
        and (brief_scope if allow_source_brief else source.scope in ('official_facts','public_media'))
        and row is not None and type(row['enabled']) is int and row['enabled'] == 1 and positive(row['revision'])
        and isinstance(definition,dict) and definition.get('enabled') is True
        and (definition.get('scope') == 'source_brief_only' if allow_source_brief
             else definition.get('scope') in ('official_facts','public_media')))
    state = {'source_id':source_id,'enabled':bool(eligible),
        'revision':row['revision'] if row is not None and positive(row['revision']) else None,
        'registry_sha256':sha(source.__dict__) if source is not None else None,
        'definition_sha256':sha(definition) if definition is not None else None}
    if not eligible:
        state['reason'] = 'source_permission_revoked' if row is not None and row['enabled'] == 0 else 'history_source_permission_unavailable'
    return state

def mask_public_article(article, *, status=None, reason=None):
    """Used by the separate default-provider integration proposal for every content mode."""
    result = metadata_only(article)
    result.update(status=status or article.get('status','reading_unavailable'),
        evidence_excerpt='',excerpt_notice='当前资料不可用于事实分析。',
        source_summary_quotes=[],source_brief='',source_verified=False)
    if reason:
        result['availability_reason'] = reason
    return result

def article_content_available(article):
    """Static entry has no license DB authority; validate content mode and stored state."""
    if article.get('status','active') != 'active':
        return False
    if article.get('content_mode') == 'source_verified_text':
        return source_verified_text_article(article)
    if article.get('content_mode') == 'source_brief':
        return source_brief_readable(article)
    return readable_article(article)

def current_article_view(store, db, row):
    article = {**json.loads(row['body']),'status':row['status'],'checked_at':row['checked_at']}
    permission = source_permission_state(db,article.get('source_id'),
        allow_source_brief=article.get('content_mode') == 'source_brief' and source_brief_readable(article))
    if article['status'] != 'active':
        result = mask_public_article(article)
    elif not permission['enabled']:
        result = mask_public_article(article,status=permission['reason'],reason=permission['reason'])
    else:
        result = store.public_article(article)
        if result.get('status') != 'active':
            result = mask_public_article(result)
    result['source_permission'] = permission
    return result

def weekly_reference_identities(report):
    """失效周报只保留原引用身份；不是批准、当前事实或重新检索指令。"""
    citations=report.get('citations');generation=report.get('generation')
    if (type(citations) is not list or not citations
            or type(generation) is not dict or generation.get('contract')!=GENERATION):
        return []
    bindings=generation.get('event_bindings')
    if type(bindings) is not list or len(bindings)!=len(citations):return []
    expected={}
    for binding in bindings:
        if type(binding) is not dict:return []
        identifier,version=binding.get('event_id'),binding.get('event_version')
        if (type(identifier) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',identifier)
                or not positive(version) or identifier in expected):return []
        expected[identifier]=version
    result=[];seen=set()
    for citation in citations:
        if type(citation) is not dict:return []
        identifier,version=citation.get('event_id'),citation.get('event_version')
        if (type(identifier) is not str or type(version) is not int
                or identifier not in expected or expected[identifier]!=version
                or identifier in seen):return []
        seen.add(identifier);result.append({'event_id':identifier,'event_version':version})
    return result

def mask_public_weekly(report, articles, *, status=None):
    result = metadata_only(report)
    result.update(status=status or report.get('status','reading_unavailable'),
        source_articles=[mask_public_article(a) for a in articles],
        source_permissions=[deepcopy(a['source_permission']) for a in articles if 'source_permission' in a],
        summary='',analysis='',transmission='',changes=[],facts=[],
        citations=weekly_reference_identities(report),reading=[])
    return result

class PublicMarketHistory:
    def __init__(self, store):
        self.store = store
        self.database_path = Path(store.path).resolve(strict=True)
        self.source_token = hashlib.sha256(str(self.database_path).encode()).hexdigest()

    @staticmethod
    def _identity(kind, identifier, version, mode):
        if mode != 'historical_audit':
            raise HistoryError('history_mode_required',400)
        if not positive(version):
            raise HistoryError('invalid_history_version',400)
        if kind == 'weekly':
            try:
                if not isinstance(identifier,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',identifier):
                    raise ValueError()
                date.fromisoformat(identifier)
            except ValueError:
                raise HistoryError('invalid_history_period',400) from None
        elif not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',identifier):
            raise HistoryError('invalid_history_identity',400)
        return {'kind':kind,'id':identifier,'version':version,'reading_mode':mode}

    @contextmanager
    def _transaction(self, db=None):
        if db is not None:
            if not db.in_transaction:
                raise HistoryError('history_transaction_required',503)
            self._assert_database_identity(db)
            yield db
            return
        with self.store.db() as connection:
            self._assert_database_identity(connection)
            connection.execute('BEGIN')
            yield connection

    def _assert_database_identity(self, db):
        main = [row[2] for row in db.execute('PRAGMA database_list').fetchall() if row[1] == 'main']
        if len(main) != 1 or not isinstance(main[0],str) or not main[0] or Path(main[0]).resolve(strict=True) != self.database_path:
            raise HistoryError('history_database_identity_mismatch',503)

    @staticmethod
    def _row(db, table, identifier, version=None):
        key = 'period' if table == 'market_weeklies' else 'id'
        if table not in ('market_articles','market_events','market_weeklies'):
            raise HistoryError('invalid_history_table',400)
        if version is None:
            row = db.execute('SELECT * FROM main.'+table+' WHERE '+key+'=? ORDER BY version DESC LIMIT 1',(identifier,)).fetchone()
        else:
            row = db.execute('SELECT * FROM main.'+table+' WHERE '+key+'=? AND version=?',(identifier,version)).fetchone()
        if row is None:
            raise HistoryError('history_version_not_found',404)
        body = json.loads(row['body'])
        if not isinstance(body,dict) or body.get(key) != identifier or type(body.get('version')) is not int or body['version'] != row['version']:
            raise HistoryError('history_row_identity_corrupt')
        return row, body

    @staticmethod
    def _permission(db, source_id):
        state = source_permission_state(db,source_id)
        if not state['enabled']:
            raise HistoryError(state['reason'])
        return state

    @staticmethod
    def _complete_text(article, row):
        if article.get('evidence_truncated') is not False or not text_only_article(article):
            raise HistoryError('historical_original_text_unbound')
        if row['fingerprint'] != article.get('fingerprint'):
            raise HistoryError('historical_original_text_unbound')
        text = article.get('evidence_text')
        if not isinstance(text,str) or len(text) < 120:
            raise HistoryError('historical_original_text_unbound')
        if article.get('content_mode') == 'source_verified_text':
            if not source_verified_text_article(article):
                raise HistoryError('historical_original_text_unbound')
        else:
            keys = ('title','published_at','published_precision','evidence_text','access_status','identity','origin_publisher')
            if any(not isinstance(article.get(k),str) or not article[k] for k in keys):
                raise HistoryError('historical_original_text_unbound')
            fingerprint = hashlib.sha256(('source-parser-v3\n'+'\n'.join(article[k] for k in keys)).encode()).hexdigest()
            if fingerprint != row['fingerprint']:
                raise HistoryError('historical_original_text_unbound')

    def _article(self, db, identifier, version):
        row, body = self._row(db,'market_articles',identifier,version)
        head, current = self._row(db,'market_articles',identifier)
        if row['status'] != 'active' or head['status'] != 'active':
            raise HistoryError('history_article_unavailable')
        if body.get('source_id') != current.get('source_id') or body.get('url') != current.get('url'):
            raise HistoryError('history_source_identity_changed')
        permission = self._permission(db,body.get('source_id'))
        self._complete_text(body,row)
        self._complete_text(current,head)
        # Full text remains local; public/history clients get the existing short-evidence limit.
        article = metadata_only(body)
        article.update(status='historical_readable',reading_mode='historical_audit',
            historical_only=True,current_evidence_eligible=False,
            original_permission_epoch='unknown',evidence_excerpt=body['evidence_text'][:120],
            excerpt_notice='选定版本的历史短证据；不代表现行事实，完整原文保留在本地。')
        binding = {'article_id':identifier,'version':version,'article_body_sha256':body_sha(row),
            'source_id':body['source_id'],'permission_revision':permission['revision']}
        head_state = {'kind':'article','id':identifier,'version':head['version'],
            'status':head['status'],'body_sha256':body_sha(head)}
        return article, binding, permission, head_state

    def _weekly(self, db, identifier, version):
        row, body = self._row(db,'market_weeklies',identifier,version)
        head, current = self._row(db,'market_weeklies',identifier)
        if row['status'] not in ('published','source_revised') or head['status'] not in ('published','source_revised'):
            raise HistoryError('history_weekly_unavailable')
        generation = body.get('generation')
        required = {'contract','generation_id','started_at','period_start','period_end_exclusive','expected_weekly_version','event_bindings','article_bindings','source_permissions','materials_sha256','input_hash','snapshot_sha256'}
        if not isinstance(generation,dict) or set(generation) != required or generation['contract'] != GENERATION:
            raise HistoryError('historical_dependency_unbound')
        if not re.fullmatch(r'[0-9a-f]{32}',str(generation['generation_id'])) or type(generation['expected_weekly_version']) is not int or generation['expected_weekly_version'] != version-1:
            raise HistoryError('historical_dependency_unbound')
        try:
            captured = db.execute('SELECT snapshot FROM main.market_weekly_generations WHERE generation_id=?',(generation['generation_id'],)).fetchone()
            reviewed = db.execute('SELECT * FROM main.market_weekly_review_bindings WHERE generation_id=?',(generation['generation_id'],)).fetchone()
        except sqlite3.OperationalError as error:
            if 'no such table' in str(error):
                raise HistoryError('historical_dependency_unbound') from error
            raise
        if captured is None or json.loads(captured['snapshot']) != generation or reviewed is None:
            raise HistoryError('historical_dependency_unbound')
        input_fields = ('contract','period_start','period_end_exclusive','event_bindings','article_bindings','source_permissions','materials_sha256')
        if generation['input_hash'] != sha({k:generation[k] for k in input_fields}) or generation['snapshot_sha256'] != sha({k:v for k,v in generation.items() if k not in ('snapshot_sha256','input_hash')}):
            raise HistoryError('history_generation_hash_mismatch')
        start = datetime.fromisoformat(body.get('period_start',''))
        end = datetime.fromisoformat(body.get('period_end_exclusive',''))
        if start.tzinfo is None or end.tzinfo is None or start >= end or start.date().isoformat() != identifier or generation['period_start'] != body['period_start'] or generation['period_end_exclusive'] != body['period_end_exclusive']:
            raise HistoryError('history_generation_period_mismatch')
        event_bindings, article_bindings, source_permissions = (generation[k] for k in ('event_bindings','article_bindings','source_permissions'))
        if not isinstance(event_bindings,list) or not 1 <= len(event_bindings) <= 6 or not isinstance(article_bindings,list) or not 1 <= len(article_bindings) <= 48 or not isinstance(source_permissions,list) or not 1 <= len(source_permissions) <= 48:
            raise HistoryError('historical_dependency_unbound')
        permission_ids = set()
        for permission in source_permissions:
            if (not isinstance(permission,dict) or set(permission) != {'source_id','enabled','revision','registry_sha256','definition_sha256'}
                    or not isinstance(permission['source_id'],str) or not permission['source_id']
                    or permission['source_id'] in permission_ids or permission['enabled'] is not True
                    or not positive(permission['revision'])
                    or any(not isinstance(permission[k],str) or not re.fullmatch(r'[0-9a-f]{64}',permission[k]) for k in ('registry_sha256','definition_sha256'))):
                raise HistoryError('history_permission_binding_invalid')
            permission_ids.add(permission['source_id'])
        events, original_events, article_versions, seen_events, heads = [], [], {}, set(), []
        for binding in event_bindings:
            if not isinstance(binding,dict) or set(binding) != {'event_id','event_version','article_ids','article_versions','event_body_sha256'} or not positive(binding['event_version']) or binding['event_id'] in seen_events:
                raise HistoryError('history_event_binding_invalid')
            event_id = binding['event_id']; self._identity('event',event_id,binding['event_version'],'historical_audit'); seen_events.add(event_id)
            event_row, event = self._row(db,'market_events',event_id,binding['event_version'])
            event_head, unused = self._row(db,'market_events',event_id)
            if event_row['status'] not in ('published','source_revised') or event_head['status'] not in ('published','source_revised') or body_sha(event_row) != binding['event_body_sha256']:
                raise HistoryError('history_event_binding_changed')
            ids, versions = binding['article_ids'], binding['article_versions']
            if not isinstance(ids,list) or not ids or len(ids)>48 or any(not isinstance(i,str) for i in ids) or len(set(ids)) != len(ids) or not isinstance(versions,dict) or set(versions) != set(ids) or any(not positive(v) for v in versions.values()) or event.get('article_ids') != ids or event.get('article_versions') != versions:
                raise HistoryError('history_event_binding_invalid')
            stamp = datetime.fromisoformat(event['published_at'])
            if stamp.tzinfo is None or not start <= stamp < end:
                raise HistoryError('history_event_outside_period')
            for article_id in ids:
                if article_id in article_versions and article_versions[article_id] != versions[article_id]:
                    raise HistoryError('history_article_version_conflict')
                article_versions[article_id] = versions[article_id]
            original_events.append(event)
            events.append({k:deepcopy(v) for k,v in event.items() if k in ('id','version','headline','summary','analysis','uncertainty','facts','published_at','article_ids','article_versions')})
            heads.append({'kind':'event','id':event_id,'version':event_head['version'],'status':event_head['status'],'body_sha256':body_sha(event_head)})
        materials = [{k:v for k,v in e.items() if k in ('id','version','headline','summary','analysis','uncertainty','facts','published_at','identity','article_ids','article_versions')} for e in original_events]
        if generation['materials_sha256'] != sha(materials):
            raise HistoryError('history_generation_materials_mismatch')
        article_map, actual_bindings, permission_map = {}, [], {}
        for binding in article_bindings:
            if not isinstance(binding,dict) or set(binding) != {'article_id','version','article_body_sha256','source_id','permission_revision'} or not positive(binding['version']) or not positive(binding['permission_revision']) or binding['article_id'] in article_map or article_versions.get(binding['article_id']) != binding['version']:
                raise HistoryError('history_article_binding_invalid')
            article, actual, permission, article_head = self._article(db,binding['article_id'],binding['version'])
            if actual != binding:
                raise HistoryError('history_article_binding_changed')
            article['original_permission_epoch'] = binding['permission_revision']
            article_map[binding['article_id']] = article
            actual_bindings.append(actual); heads.append(article_head); permission_map[permission['source_id']] = permission
        if set(article_map) != set(article_versions) or source_permissions != list(permission_map.values()):
            raise HistoryError('history_permission_epoch_changed')
        expected_citations = [{'event_id':e['id'],'event_version':e['version'],'heading':e['headline'],'article_ids':e['article_ids'],'article_versions':e['article_versions']} for e in original_events]
        if body.get('citations') != expected_citations or body.get('article_versions') != article_versions or body.get('article_ids') != list(article_versions) or body.get('input_hash') != generation['input_hash'] or row['fingerprint'] != generation['input_hash']:
            raise HistoryError('history_weekly_citations_mismatch')
        changes = body.get('changes')
        if not isinstance(changes,list) or len(changes) != min(3,len(events)) or any(not isinstance(c,dict) or set(c) != {'event_id','heading','text','conditions'} or c['event_id'] not in seen_events or any(not isinstance(c[k],str) or not c[k].strip() for k in ('heading','text','conditions')) for c in changes):
            raise HistoryError('history_weekly_changes_unbound')
        review = body.get('review',{})
        draft = {k:body.get(k) for k in ('title','summary','changes','transmission')}
        for key in ('title','summary','transmission'):
            if not isinstance(draft[key],str) or not draft[key].strip():
                raise HistoryError('history_weekly_body_invalid')
        pub_sha = publication_sha(body)
        if review.get('status') != 'approved' or review.get('generation_snapshot_sha256') != generation['snapshot_sha256'] or review.get('materials_sha256') != generation['materials_sha256'] or review.get('draft_sha256') != sha(draft) or body.get('publication_sha256') != pub_sha or reviewed['publication_sha256'] != pub_sha or reviewed['draft_sha256'] != sha(draft) or reviewed['review_sha256'] != sha({k:v for k,v in review.items() if k != 'reviewed_at'}):
            raise HistoryError('history_weekly_review_unbound')
        for event in events:
            event.update(status='historical_readable',historical_only=True,current_evidence_eligible=False,
                articles=[deepcopy(article_map[i]) for i in event['article_ids']])
        report = {k:deepcopy(v) for k,v in body.items() if k in META or k in ('summary','changes','transmission','period_start','period_end_exclusive','as_of','article_ids','article_versions','citations','observation_notice','schedule_notice','limitations')}
        report.update(status='historical_readable',historical_only=True,current_evidence_eligible=False,
            source_articles=list(article_map.values()))
        heads.append({'kind':'weekly','id':identifier,'version':head['version'],'status':head['status'],'body_sha256':body_sha(head)})
        return {'report':report,'supporting_events':events}, {'weekly_body_sha256':body_sha(row),
            'generation_snapshot_sha256':generation['snapshot_sha256'],'event_bindings':deepcopy(event_bindings),
            'article_bindings':actual_bindings,'source_permissions':list(permission_map.values()),'heads':heads}

    def _read(self, kind, identifier, version, mode, db):
        request = self._identity(kind,identifier,version,mode)
        try:
            if kind == 'article':
                article, binding, permission, head = self._article(db,identifier,version)
                data = {'article':article}
                dependencies = {'article_bindings':[binding],'source_permissions':[permission],'heads':[head]}
            elif kind == 'weekly':
                data, dependencies = self._weekly(db,identifier,version)
            else:
                raise HistoryError('invalid_history_kind',400)
        except HistoryError as error:
            missing_weekly_dependency = (kind == 'weekly' and error.status == 404
                and error.code == 'history_version_not_found')
            if error.status != 409 and not missing_weekly_dependency:
                raise
            table = 'market_articles' if kind == 'article' else 'market_weeklies'
            # This exact selected row must exist. If the requested weekly itself
            # is absent, _row still raises the original 404; no latest fallback.
            unused, value = self._row(db,table,identifier,version)
            reason = 'historical_dependency_unbound' if missing_weekly_dependency else error.code
            return {'contract':CONTRACT,'requested':request,'reading_mode':mode,'historical_only':True,
                'current_evidence_eligible':False,'status':'history_unavailable','reason':reason,
                'metadata':metadata_only(value),'receipt':None}
        except (KeyError,ValueError,TypeError,AttributeError) as error:
            raise HistoryError('history_stored_binding_corrupt') from error
        receipt = {'contract':CONTRACT,'schema_version':1,'provider_id':PROVIDER_ID,
            'requested':request,'source_token':self.source_token,
            'dependencies':dependencies,'result_sha256':sha(data)}
        receipt['receipt_sha256'] = sha(receipt)
        return {'contract':CONTRACT,'requested':request,'reading_mode':mode,'historical_only':True,
            'current_evidence_eligible':False,'status':'historical_readable','data':data,'receipt':receipt,
            'execution_authority':False,
            'notice':'明确选定版本的历史复核，不代表当前市场事实或产品条件。'}

    def read_article(self, identifier, version, *, reading_mode, db=None):
        with self._transaction(db) as connection:
            return self._read('article',identifier,version,reading_mode,connection)

    def read_weekly(self, period, version, *, reading_mode, db=None):
        with self._transaction(db) as connection:
            return self._read('weekly',period,version,reading_mode,connection)

    def assert_fresh(self, receipt, *, db=None):
        if (not isinstance(receipt,dict) or set(receipt) != {'contract','schema_version','provider_id','requested','source_token','dependencies','result_sha256','receipt_sha256'}
                or receipt.get('contract') != CONTRACT or type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1
                or receipt.get('provider_id') != PROVIDER_ID or receipt.get('source_token') != self.source_token
                or receipt.get('receipt_sha256') != sha({k:v for k,v in receipt.items() if k != 'receipt_sha256'})):
            raise HistoryError('stale_history_evidence')
        request = receipt['requested']
        if not isinstance(request,dict) or set(request) != {'kind','id','version','reading_mode'}:
            raise HistoryError('stale_history_evidence')
        with self._transaction(db) as connection:
            try:
                result = self._read(request['kind'],request['id'],request['version'],request['reading_mode'],connection)
            except (HistoryError,sqlite3.Error,KeyError,ValueError,TypeError) as error:
                raise HistoryError('stale_history_evidence') from error
        if result.get('status') != 'historical_readable' or result.get('receipt') != receipt:
            raise HistoryError('stale_history_evidence')
        return True
