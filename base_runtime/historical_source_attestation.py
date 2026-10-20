"""持久历史来源证明候选：保留原公开撤回门，不把当轮票据当历史寿命。"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib,json,re

from financial_metrics import fingerprint
from simulated_bank import BankError
from local_knowledge.catalog_information import validate_source, digest as catalog_digest
from local_knowledge.catalog_information_host import response_snapshot

CONTRACT='navren.historical-source-attestation/1'
CATALOG_CONTRACT='navren.archived-catalog-source/1'
CATALOG_KEY_CONTRACT='navren.archived-catalog-source-id/1'
WITHDRAWN={'withdrawn','retracted','removed','source_withdrawn','404','410'}

def fail(code='HISTORICAL_SOURCE_CHANGED',status=409):
    raise BankError(code,'历史资料的原件或来源许可已变化，正文已停止恢复。',status)

def catalog_record(source):
    """只接实际受信 source；同包重建不使用随机 issuer generation 或短 pin。"""
    from local_knowledge.catalog_information import CatalogInformationSource
    if type(source) is not CatalogInformationSource:fail('HISTORICAL_CATALOG_SOURCE_REQUIRED')
    with source._lock:
        package=validate_source(source._package,source._kind)
        record={'contract':CATALOG_CONTRACT,'source_kind':source._kind,
                'original_source':package['source'],'original_as_of':package['as_of'],
                'full_package_sha256':catalog_digest(package),'original_package':package}
    key={k:record[k]for k in ('source_kind','original_source','original_as_of','full_package_sha256')}
    record['source_id']=fingerprint({'contract':CATALOG_KEY_CONTRACT,**key})
    return record

class HistoricalSourceAttestor:
    """仅服务端 publication/read 装配；没有 HTTP 或模型签发入口。

    private registry writer 必须由 storage owner 提供。Catalog revocation 按
    bank→source 锁序持久化；常规 select 不等于公开撤回。
    """
    def __init__(self,gateway):
        self.g,self.store=gateway,gateway.personal_data
        bind=getattr(gateway.catalog_information_host,'bind_history_authority',None)
        if callable(bind):bind(self)

    def bind_catalog_source(self,source):
        """Host install/reinstall 也装持久撤回，不能等下一次publication。"""
        record=catalog_record(source)
        writer=getattr(self.store,'register_catalog_source_guarded',None)
        binder=getattr(source,'bind_history_authority',None)
        if not callable(writer)or not callable(binder):fail('HISTORICAL_SOURCE_STORAGE_REQUIRED',503)
        with self.g.bank.transaction()as db:
            row=writer(record['source_id'],record,bank_db=db)
            binder(self)
            if row.get('status')=='withdrawn':source.revoke(selection_only=True)

    @contextmanager
    def catalog_revoke_guard(self,source):
        record=catalog_record(source)
        writer=getattr(self.store,'withdraw_catalog_source_guarded',None)
        if not callable(writer):fail('HISTORICAL_SOURCE_STORAGE_REQUIRED',503)
        with self.g.bank.transaction()as db:
            value=writer(record['source_id'],bank_db=db)
            if value is None:fail('HISTORICAL_CATALOG_RECORD_REQUIRED')
            yield

    def _catalog_prepare(self,w,original,db):
        snapshot=response_snapshot(original)
        if snapshot is None:return None
        observer=self.g.catalog_information_host.lookup(snapshot,workspace=w,
            privacy_revision=original['privacy_revision'],personal_revision=original['personal_revision'],
            request_id=original['bank_response_id'])
        source=observer.source;record=catalog_record(source)
        if (record['full_package_sha256']!=snapshot['full_package_sha256']
                or record['original_source']!=snapshot['original_source']
                or record['original_as_of']!=snapshot['original_as_of']
                or record['source_kind']!=snapshot['source_kind']):fail()
        writer=getattr(self.store,'register_catalog_source_guarded',None)
        bind=getattr(source,'bind_history_authority',None)
        if not callable(writer)or not callable(bind):fail('HISTORICAL_SOURCE_STORAGE_REQUIRED',503)
        # 当前 publication 已持 bank 和 source 锁。先装回调再登记；并发 revoke
        # 只能在 bank 锁释放后完成，不存在已封存但没有撤回挂接的空隙。
        bind(self)
        row=writer(record['source_id'],record,bank_db=db)
        if (type(row)is not dict or row.get('status')!='active' or row.get('record')!=record
                or row.get('withdrawn_generation')!=0):fail('HISTORICAL_CATALOG_WITHDRAWN')
        return {'source_id':record['source_id'],'record_sha256':fingerprint(record),
                'withdrawn_generation':0,'original_snapshot_sha256':fingerprint(snapshot)}

    def _catalog_check(self,claim,original,db):
        if claim is None:
            if response_snapshot(original)is not None:fail()
            return
        reader=getattr(self.store,'read_catalog_source_guarded',None)
        if not callable(reader):fail('HISTORICAL_SOURCE_STORAGE_REQUIRED',503)
        if type(claim)is not dict or set(claim)!={'source_id','record_sha256','withdrawn_generation','original_snapshot_sha256'}:fail()
        row=reader(claim['source_id'],bank_db=db)
        if (type(row)is not dict or row.get('source_id')!=claim['source_id']
                or row.get('status')!='active' or row.get('withdrawn_generation')!=claim['withdrawn_generation']
                or fingerprint(row.get('record'))!=claim['record_sha256']
                or fingerprint(response_snapshot(original))!=claim['original_snapshot_sha256']):fail('HISTORICAL_CATALOG_WITHDRAWN')
        record=row['record'];package=validate_source(record['original_package'],record['source_kind'])
        if (catalog_digest(package)!=record['full_package_sha256'] or package['source']!=record['original_source']
                or package['as_of']!=record['original_as_of']):fail()

    @staticmethod
    def _row(db,table,identifier,version):
        if table not in ('market_articles','market_events','market_weeklies'):fail()
        key='period'if table=='market_weeklies'else'id'
        row=db.execute('SELECT * FROM '+table+' WHERE '+key+'=? AND version=?',(identifier,version)).fetchone()
        if row is None:fail('HISTORICAL_MARKET_ORIGINAL_MISSING')
        return row

    @staticmethod
    def _permission(db,source_id):
        from public_market_history import source_permission_state
        value=source_permission_state(db,source_id)
        if not value['enabled']:fail('HISTORICAL_MARKET_PERMISSION_WITHDRAWN')
        return value

    def _article_claim(self,market,db,identifier,version,*,historical=False):
        row=self._row(db,'market_articles',identifier,version);body=json.loads(row['body'])
        allowed=('active','source_revised')if historical else('active',)
        if row['status']not in allowed:fail('HISTORICAL_MARKET_ORIGINAL_WITHDRAWN')
        # 原 full-text/brief 可读性继续由实际提供端判断，不赋予新的正文范围。
        view=market.public_article({**body,'status':'active','checked_at':row['checked_at']})
        if view.get('status')!='active':fail('HISTORICAL_MARKET_ORIGINAL_UNREADABLE')
        permission=self._permission(db,body['source_id'])
        return {'id':identifier,'version':version,'body_sha256':hashlib.sha256(row['body'].encode()).hexdigest(),
                'fingerprint':row['fingerprint'],'source_id':body['source_id'],'permission':permission}

    def _source_brief_claim(self,market,db,identifier,version):
        """简介是独立原许可；不能借此走全文history_article或视频读取。"""
        from market_sources import source_brief_readable
        from public_market_history import source_permission_state
        row=self._row(db,'market_articles',identifier,version);body=json.loads(row['body'])
        head=db.execute('SELECT status FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(identifier,)).fetchone()
        if (row['status']not in ('active','source_revised')or head is None or head['status']not in ('active','source_revised')
                or body.get('id')!=identifier or body.get('version')!=version or not source_brief_readable(body)):
            fail('HISTORICAL_SOURCE_BRIEF_UNAVAILABLE')
        permission=source_permission_state(db,body['source_id'],allow_source_brief=True)
        if not permission['enabled']:fail('HISTORICAL_MARKET_PERMISSION_WITHDRAWN')
        return {'contract':'navren.archived-source-brief/1','id':identifier,'version':version,
                'body_sha256':hashlib.sha256(row['body'].encode()).hexdigest(),'fingerprint':row['fingerprint'],
                'source_id':body['source_id'],'permission':permission,'read_scope':'source_brief',
                'original_brief_sha256':hashlib.sha256(body['evidence_text'].encode()).hexdigest(),
                'video_content_read':False}

    @staticmethod
    def _assert_history_receipt(market,receipt,db):
        from public_market_history import HistoryError,sha
        # 原receipt本身必须完整；不能借下面的head容忍恢复损坏证明。
        if (type(receipt)is not dict or receipt.get('receipt_sha256')!=sha({k:v for k,v in receipt.items()if k!='receipt_sha256'})):fail()
        try:
            market.assert_history_receipt(receipt,db=db)
            return
        except HistoryError:
            # 同一旧原文在正常升版后仍可由原历史提供端读出；它的heads会
            # 更新，不能把这一个辅助字段的变化误当撤回。原摘要、版本、
            # permission代次、正文hash、source_token和result_hash仍须逐字相同。
            request=receipt.get('requested',{})
            if request.get('kind')=='article':current=market.history_article(request['id'],request['version'],reading_mode='historical_audit',db=db)
            elif request.get('kind')=='weekly':current=market.history_weekly(request['id'],request['version'],reading_mode='historical_audit',db=db)
            else:fail()
            if current.get('status')!='historical_readable':fail('HISTORICAL_MARKET_ORIGINAL_WITHDRAWN')
            def stable(value):
                value=deepcopy(value);value.pop('receipt_sha256',None)
                value['dependencies'].pop('heads',None)
                return value
            if stable(receipt)!=stable(current['receipt']):fail('HISTORICAL_MARKET_SOURCE_CHANGED')

    def _event_claim(self,market,db,event,archive=None):
        identifier,version=event['id'],event['version'];row=self._row(db,'market_events',identifier,version)
        body=json.loads(row['body'])
        if row['status']!='published' or body.get('id')!=identifier or body.get('version')!=version:fail()
        versions=body.get('article_versions')
        if type(versions)is not dict or not versions:fail('HISTORICAL_MARKET_DEPENDENCIES_MISSING')
        claim={'id':identifier,'version':version,'body_sha256':hashlib.sha256(row['body'].encode()).hexdigest(),
               'fingerprint':row['fingerprint'],'articles':[self._article_claim(market,db,i,v)for i,v in sorted(versions.items())]}
        if archive is not None:
            day,revision=archive['edition_date'],archive['revision']
            edition=db.execute('SELECT * FROM market_daily_editions WHERE edition_date=? AND revision=?',(day,revision)).fetchone()
            item=db.execute('SELECT * FROM market_daily_items WHERE edition_date=? AND revision=? AND event_id=?',(day,revision,identifier)).fetchone()
            if edition is None or item is None or item['event_version']!=version:fail()
            # updating→sealed 只是日期封存，不改变原精选内容或资料许可。
            stable={k:edition[k]for k in edition.keys()if k not in ('period_state','coverage')}
            claim['archive']={'edition_date':day,'revision':revision,'edition_sha256':fingerprint(stable),'item_sha256':fingerprint(dict(item))}
        return claim

    def _market_prepare(self,original):
        snapshot=original.get('market_snapshot')
        if snapshot is None:return None
        market=getattr(self.g,'market',None)
        if market is None:fail('HISTORICAL_MARKET_REQUIRED',503)
        db=self.g.operation_evidence.current_market_connection(market)
        # 已明确 historical_audit 的原 receipt 可直接保存，不继承内存 cache id。
        from agent_runtime import history_binding
        if snapshot.get('schema_version')==2 or snapshot.get('provider_id')==history_binding.PROVIDER:
            if type(snapshot)is not history_binding.HostHistorySnapshot:fail('HISTORICAL_MARKET_ORIGINAL_RECEIPT_REQUIRED')
            history_binding.assert_snapshot(market,snapshot,db=db)
            # 仅在原publication中从已验证的实际宿主对象提取；不是从普通
            # JSON snapshot_id 猜缓存，也不会在历史读取时重新发行短票据。
            receipt=deepcopy(snapshot._binding['receipt'])
            market.assert_history_receipt(receipt,db=db)
            return {'original_snapshot_sha256':fingerprint(snapshot),'source_token':hashlib.sha256(str(market.path).encode()).hexdigest(),'reads':[{'tool':'historical_audit','history_receipt':receipt}]}
        from agent_runtime.bank_tools import BankTools
        BankTools.assert_market_snapshot(market,snapshot)
        reads=[]
        for read in snapshot['reads']:
            name,args=read['tool'],read['arguments'];data=BankTools._read_market_data(market,name,args)
            if BankTools._market_digest(data)!=read['sha256']:fail()
            claim={'tool':name,'arguments':deepcopy(args),'original_digest':read['sha256']}
            if name=='market_hotspots':
                claim['events']=[self._event_claim(market,db,event,archive={'edition_date':data['edition_date'],'revision':data['revision']})for event in data['events']]
                if not claim['events']:fail('HISTORICAL_MARKET_DEPENDENCIES_MISSING')
            elif name=='market_event':claim['events']=[self._event_claim(market,db,data,data.get('archive'))]
            elif name=='market_article':
                if data.get('content_mode')=='source_brief':
                    claim['source_brief_receipt']=self._source_brief_claim(market,db,data['id'],data['version'])
                else:
                    value=market.history_article(data['id'],data['version'],reading_mode='historical_audit',db=db)
                    if value.get('status')!='historical_readable':fail('HISTORICAL_MARKET_ORIGINAL_UNREADABLE')
                    claim['history_receipt']=value['receipt']
            elif name=='market_weekly':
                report=data.get('report')or{};value=market.history_weekly(report['period'],report['version'],reading_mode='historical_audit',db=db)
                if value.get('status')!='historical_readable':fail('HISTORICAL_MARKET_ORIGINAL_UNREADABLE')
                claim['history_receipt']=value['receipt']
            else:fail('HISTORICAL_MARKET_UNSUPPORTED_READ')
            reads.append(claim)
        return {'original_snapshot_sha256':fingerprint(snapshot),'source_token':hashlib.sha256(str(market.path).encode()).hexdigest(),'reads':reads}

    def _market_check(self,claim,original,market,db):
        if claim is None:
            if original.get('market_snapshot')is not None:fail()
            return
        if claim['original_snapshot_sha256']!=fingerprint(original.get('market_snapshot')):fail()
        if claim['source_token']!=hashlib.sha256(str(market.path).encode()).hexdigest():fail('HISTORICAL_MARKET_SOURCE_CHANGED')
        for read in claim['reads']:
            if 'source_brief_receipt'in read:
                original_brief=read['source_brief_receipt']
                actual=self._source_brief_claim(market,db,original_brief['id'],original_brief['version'])
                if actual!=original_brief:fail('HISTORICAL_SOURCE_BRIEF_CHANGED')
                continue
            if 'history_receipt'in read:
                self._assert_history_receipt(market,read['history_receipt'],db);continue
            for event in read.get('events',[]):
                row=self._row(db,'market_events',event['id'],event['version'])
                head=db.execute('SELECT status FROM market_events WHERE id=? ORDER BY version DESC LIMIT 1',(event['id'],)).fetchone()
                if (row['status']not in ('published','source_revised')or head is None or head['status']not in ('published','source_revised')
                        or hashlib.sha256(row['body'].encode()).hexdigest()!=event['body_sha256']or row['fingerprint']!=event['fingerprint']):fail('HISTORICAL_MARKET_ORIGINAL_WITHDRAWN')
                for article in event['articles']:
                    actual=self._article_claim(market,db,article['id'],article['version'],historical=True)
                    head=db.execute('SELECT status FROM market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(article['id'],)).fetchone()
                    if actual!=article or head is None or head['status']not in ('active','source_revised'):fail('HISTORICAL_MARKET_ORIGINAL_WITHDRAWN')
                archive=event.get('archive')
                if archive is not None:
                    edition=db.execute('SELECT * FROM market_daily_editions WHERE edition_date=? AND revision=?',(archive['edition_date'],archive['revision'])).fetchone()
                    item=db.execute('SELECT * FROM market_daily_items WHERE edition_date=? AND revision=? AND event_id=?',(archive['edition_date'],archive['revision'],event['id'])).fetchone()
                    if edition is None or item is None:fail('HISTORICAL_MARKET_ORIGINAL_MISSING')
                    stable={k:edition[k]for k in edition.keys()if k not in ('period_state','coverage')}
                    if fingerprint(stable)!=archive['edition_sha256']or fingerprint(dict(item))!=archive['item_sha256']:fail()

    def prepare(self,w,original,*,bank_db):
        if type(original)is not dict or original.get('workspace_id')!=w or type(original.get('bank_response_id'))is not str:fail()
        with self.g.presentations._publication(bank_db)as db:
            self.g.presentations._session_binding(w,db,required=True)
            public={'market':original.get('market_snapshot'),'knowledge':original.get('knowledge_snapshot')}
            has_sources=any(public.values())or response_snapshot(original)is not None
            if has_sources:
                from finance_gateway import _publication_checkpoint
                if not callable(_publication_checkpoint.get()):fail('HISTORICAL_SOURCE_PUBLICATION_GUARD_REQUIRED',503)
                self.g.operation_evidence.assert_public_evidence(public)
            body={'contract':CONTRACT,'workspace_id':w,'response_id':original['bank_response_id'],
                  'original_response_hash':fingerprint(original),'knowledge_snapshot':deepcopy(original.get('knowledge_snapshot')),
                  'catalog':self._catalog_prepare(w,original,db),'market':self._market_prepare(original),
                  'historical_only':True,'execution_authority':False}
            seal=self.store.protect(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False))
            return {'contract':CONTRACT,'sealed_original':seal}

    def _open(self,envelope,w,original):
        if (type(envelope)is not dict or set(envelope)!={'contract','sealed_original'}or envelope['contract']!=CONTRACT
                or type(envelope['sealed_original'])is not str or not envelope['sealed_original'].startswith('pd1:')):fail()
        try:body=json.loads(self.store._decode(envelope['sealed_original']))
        except (BankError,TypeError,ValueError):fail()
        required={'contract','workspace_id','response_id','original_response_hash','knowledge_snapshot','catalog','market','historical_only','execution_authority'}
        if (type(body)is not dict or set(body)!=required or body['contract']!=CONTRACT or body['workspace_id']!=w
                or body['response_id']!=original.get('bank_response_id')or body['original_response_hash']!=fingerprint(original)
                or body['knowledge_snapshot']!=original.get('knowledge_snapshot')
                or body['historical_only']is not True or body['execution_authority']is not False):fail()
        return body

    @contextmanager
    def guard(self,w,envelope,original,*,bank_db=None):
        with self.guard_many(w,[(envelope,original)],bank_db=bank_db)as checkpoint:yield checkpoint

    @contextmanager
    def guard_many(self,w,entries,*,bank_db=None):
        if type(entries)is not list or len(entries)>50 or any(type(row)not in (tuple,list)or len(row)!=2 for row in entries):fail()
        records=[(self._open(envelope,w,original),original)for envelope,original in entries]
        with self.g.presentations._publication(bank_db)as db,ExitStack()as locks:
            self.g.presentations._session_binding(w,db,required=True)
            market=getattr(self.g,'market',None);market_db=None
            if any(body['market']is not None for body,_ in records):
                if market is None:fail('HISTORICAL_MARKET_REQUIRED',503)
                market_db=locks.enter_context(market.db());market_db.execute('BEGIN IMMEDIATE')
            # RAG 完整保留原release/as_of/canonical与显式withdraw校验，不变成current。
            # 多条历史可以属于不同release；只取一个原knowledge发布屏障，
            # 逐条调用原assert而不重复BEGIN IMMEDIATE造成同线程自锁。
            if any(body['knowledge_snapshot']is not None for body,_ in records):
                knowledge_db=locks.enter_context(self.g.knowledge_provider.db());knowledge_db.execute('BEGIN IMMEDIATE')
            def checkpoint():
                for body,original in records:
                    self._catalog_check(body['catalog'],original,db)
                    self._market_check(body['market'],original,market,market_db)
                    self.g.operation_evidence.assert_public_evidence({'market':None,'knowledge':body['knowledge_snapshot']})
                return True
            checkpoint();yield checkpoint;checkpoint()
