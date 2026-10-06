"""临时目录内的实际 SQLite/FTS5 风险回归；不访问外网或生产库。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_runtime.knowledge_retrieval import KnowledgeRetrieval
from local_knowledge import LocalKnowledgeProvider,KnowledgeError,SupplementService,FetchResult
from local_knowledge.importers import import_market_store,import_webank_disclosure
from local_knowledge.store import packed,sha
from local_knowledge.sources import fetch_registered
from local_knowledge.pdf_ingest import extract_pdf,stage_reviewed_pdf,PARSER_ID as PDF_PARSER,SHAPE as PDF_SHAPE


AS_OF='2026-10-06'
URL='https://official.example/terms'


class LocalKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.provider=LocalKnowledgeProvider(Path(self.temp.name)/'public.sqlite3')
        self.provider.register_source('official',publisher='测试官方源',endpoints=['https://official.example/'],
            freshness={'fees':None,'liquidity':None,'terms':None,'risk':None,'general':30,'sale_status':0,'returns':7},
            shape='plain-text-v1',permission_basis='test_fixture_only')
        for pid in ('PRODUCT-A','PRODUCT-B'):
            self.provider.register_product(pid,issuer='TEST',scheme='test-code',code=pid,name='测试产品'+pid,verified=True)
        self.provider.register_share('SHARE-A',product_id='PRODUCT-A',scheme='test-share',code='001A',currency='CNY')
        self.provider.register_share('SHARE-C',product_id='PRODUCT-A',scheme='test-share',code='001C',currency='CNY')
        self.provider.register_channel('CHANNEL-A',name='测试渠道甲')
        self.provider.register_channel('CHANNEL-B',name='测试渠道乙')

    def stage(self,**changes):
        args=dict(source_id='official',source_url=URL,title='测试产品条款',text='赎回申请后两个工作日到账。费率为0.10%，渠道条件以原文为准。',
                  published_on='2026-09-01',retrieved_on=AS_OF,effective_from='2026-09-01',effective_to='2026-11-01',
                  product_id='PRODUCT-A',share_class_id='SHARE-A',channel_id='CHANNEL-A',facets=['fees','liquidity','terms'])
        args.update(changes)
        return self.provider.stage_document(**args)

    def active(self,**changes):
        version=self.stage(**changes)
        self.provider.approve_version(version,verification_basis='test_verified')
        release=self.provider.publish()
        return version,release

    def search(self,provider=None,**changes):
        args=dict(query='费用和赎回',product_id='PRODUCT-A',share_class_id='SHARE-A',channel_id='CHANNEL-A',as_of=AS_OF,facets=['fees','liquidity'])
        args.update(changes)
        return KnowledgeRetrieval(provider or self.provider).search(**args)

    def market_fixture(self, *, complete=True):
        path=Path(self.temp.name)/'market-subset.sqlite3'
        db=sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE market_articles(id TEXT,version INTEGER,fingerprint TEXT,body TEXT,status TEXT,checked_at TEXT)')
            article={'source_id':'nbs','url':'https://www.stats.gov.cn/sj/zxfb/202610/t20261005_1965449.html','title':'官方统计发布测试',
                'published_at':'2026-10-05T10:00:00+00:00','read_scope':'full_text' if complete else 'metadata',
                'link_status':'verified','evidence_text':'统计数据显示经济指标变化。','evidence_truncated':False}
            db.execute('INSERT INTO market_articles VALUES(?,?,?,?,?,?)',('good',1,'test',packed(article),'active','2026-10-06T10:00:00+00:00'))
            db.commit()
        finally:db.close()
        return SimpleNamespace(path=path)

    @staticmethod
    def webank_fixture():
        rows=[{'code':code,'name':'测试产品'+code,'saleStatus':'在售','type':'基金','placement':'代销','saleType':'公开',
            'risk':'中风险','publish':'TEST','investor':'个人','charge':'0.10%','chargeWay':'费率见披露','customerGroupType':'个人'}
            for code in ('000001','000002')]
        return ('_jsonpproductpresentation('+packed({'extractDate':'2026-09-01','productPresentation':rows})+');').encode()

    def released_versions(self,release):
        with self.provider.db() as db:
            return {r[0] for r in db.execute('SELECT DISTINCT c.version_id FROM release_chunks rc JOIN chunks c ON c.id=rc.chunk_id WHERE rc.release_id=?',(release,))}

    @staticmethod
    def pdf_fixture():
        objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [4 0 R 6 0 R] /Count 2 >>',
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
        for index,line in enumerate((b'Fee 0.10%',b'Redemption T+2')):
            content=b'BT /F1 12 Tf 72 720 Td ('+line+b') Tj ET'
            contents=5+index*2
            objects.append(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents '+str(contents).encode()+b' 0 R >>')
            objects.append(b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream')
        raw=b'%PDF-1.4\n';offsets=[0]
        for number,obj in enumerate(objects,1):
            offsets.append(len(raw));raw+=str(number).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
        xref=len(raw);raw+=b'xref\n0 '+str(len(objects)+1).encode()+b'\n0000000000 65535 f \n'
        raw+=b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
        raw+=b'trailer\n<< /Size '+str(len(objects)+1).encode()+b' /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n'
        return raw

    def pdf_source(self):
        self.provider.register_source('official-pdf',publisher='测试PDF源',endpoints=[URL],parser_id=PDF_PARSER,
            freshness={'fees':None,'liquidity':None},shape=PDF_SHAPE,permission_basis='synthetic_pdf_test_fixture_only')

    def test_empty_and_candidate_cannot_be_public(self):
        self.assertFalse(self.provider.status()['ready'])
        version=self.stage()
        self.assertFalse(self.provider.status()['ready'])
        with self.assertRaisesRegex(KnowledgeError,'unverified_release_member'):
            self.provider.publish(version_ids=[version])
        self.assertEqual(self.search()['hits'],[])

    def test_real_fts_canonical_locator_and_consumer_contract(self):
        version,release=self.active()
        result=self.search()
        self.assertEqual(result['status'],'ready')
        self.assertEqual(len(result['hits']),1)
        hit=result['hits'][0]
        self.assertEqual(hit['document_version_id'],version)
        with self.provider.db() as db:
            text=db.execute('SELECT parsed_text FROM document_versions WHERE id=?',(version,)).fetchone()[0]
            self.assertGreater(db.execute('SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?',('"赎回"',)).fetchone()[0],0)
        loc=hit['locator']
        self.assertEqual(text[loc['start_char']:loc['end_char']],hit['excerpt'])
        self.assertEqual(hit['release_id'],release)

    def test_private_simulated_metadata_and_incomplete_rejected(self):
        for changes in ({'visibility':'workspace'},{'data_origin':'simulated'},{'read_scope':'metadata_only'},{'complete':False}):
            with self.subTest(changes=changes),self.assertRaises(KnowledgeError):self.stage(**changes)

    def test_source_allowlist_denies_arbitrary_urls(self):
        for url in ('http://official.example/terms','https://evil.example/terms','https://official.example.evil/terms',
                    'https://127.0.0.1/terms','https://official.example/terms?question=private'):
            with self.subTest(url=url),self.assertRaises(KnowledgeError):self.stage(source_url=url)
        with self.assertRaises(KnowledgeError):self.stage(source_id='unregistered')

    def test_same_product_share_channel_required(self):
        self.active()
        for changes in ({'product_id':'PRODUCT-B'},{'share_class_id':'SHARE-C'},{'channel_id':'CHANNEL-B'}):
            with self.subTest(changes=changes):self.assertEqual(self.search(**changes)['hits'],[])
        with self.assertRaises(sqlite3.IntegrityError):self.stage(product_id='PRODUCT-B')

    def test_unverified_product_cannot_be_admitted(self):
        self.provider.register_product('PRODUCT-B',issuer='TEST',scheme='test-code',code='PRODUCT-B',name='未知身份',verified=False)
        version=self.stage(product_id='PRODUCT-B',share_class_id=None)
        with self.assertRaisesRegex(KnowledgeError,'identity_unverified'):
            self.provider.approve_version(version,verification_basis='test')

    def test_exact_quote_and_span_boundary(self):
        with self.assertRaisesRegex(KnowledgeError,'quote_mismatch'):
            self.stage(spans=[{'start_char':0,'end_char':2,'excerpt':'伪造'}])
        with self.assertRaisesRegex(KnowledgeError,'invalid_spans'):
            self.stage(spans=[{'start_char':0,'end_char':999}])
        text='前文\n'+'费率为0.10%。'+'\n后文'
        self.active(text=text,spans=[{'start_char':3,'end_char':12,'paragraph':'费用/第一段','page':2,'facets':['fees']}])
        hit=self.search(facets=['fees'])['hits'][0]
        self.assertEqual(hit['locator']['start_char'],3)
        self.assertEqual(hit['locator']['page'],2)
        self.assertEqual(hit['excerpt'],text[3:12])

    def test_incremental_idempotent_publish(self):
        version,release=self.active()
        self.assertEqual(self.stage(),version)
        self.assertEqual(self.provider.publish(),release)
        other=self.stage(source_url='https://official.example/other',text='风险披露：净值可能波动。',facets=['risk'])
        self.provider.approve_version(other,verification_basis='test')
        new=self.provider.publish()
        self.assertNotEqual(new,release)
        with self.provider.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM document_versions').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM release_chunks WHERE release_id=?',(release,)).fetchone()[0],1)
        self.assertIsNotNone(self.provider.resolve_public_evidence(self.search()['hits'][0]['citation_id'],new,AS_OF))

    def test_release_manifest_is_frozen(self):
        _,release=self.active()
        with self.assertRaises(sqlite3.IntegrityError),self.provider.db() as db:
            db.execute('DELETE FROM release_chunks WHERE release_id=?',(release,))
        with self.assertRaises(sqlite3.IntegrityError),self.provider.db() as db:
            db.execute('UPDATE releases SET manifest_sha256=? WHERE id=?',('0'*64,release))

    def test_new_confirmed_version_revokes_old_before_publish_but_history_remains(self):
        old,release=self.active()
        cid=self.search()['hits'][0]['citation_id']
        new=self.stage(text='赎回申请后五个工作日到账。费率为0.20%。',published_on='2026-10-01',effective_from='2026-10-01')
        self.assertIsNotNone(self.provider.resolve_public_evidence(cid,release,AS_OF))
        self.provider.approve_version(new,verification_basis='confirmed_revision')
        self.assertIsNone(self.provider.resolve_public_evidence(cid,release,AS_OF))
        self.assertIsNotNone(self.provider.resolve_public_evidence(cid,release,'2026-09-20'))
        self.assertEqual(self.search()['hits'],[])
        self.provider.publish()
        self.assertEqual(self.search()['hits'][0]['document_version_id'],new)
        self.assertEqual(self.search(as_of='2026-09-20')['hits'][0]['document_version_id'],old)

    def test_late_approval_cannot_resurrect_old_version(self):
        old=self.stage()
        new=self.stage(text='新版费率0.20%。',published_on='2026-10-01',effective_from='2026-10-01')
        self.provider.approve_version(new,verification_basis='test')
        self.provider.approve_version(old,verification_basis='test')
        self.provider.publish()
        self.assertEqual({h['document_version_id'] for h in self.search()['hits']},{new})

    def test_withdrawal_and_permission_change_invalidate_pinned_consumer(self):
        version,_=self.active()
        consumer=KnowledgeRetrieval(self.provider)
        consumer.search('费用',as_of=AS_OF)
        self.provider.withdraw(version)
        with self.assertRaisesRegex(ValueError,'stale_knowledge_evidence'):consumer.assert_fresh()
        self.assertEqual(self.search()['hits'],[])

    def test_permission_revoke_applies_old_release(self):
        self.active();consumer=KnowledgeRetrieval(self.provider)
        consumer.search('费用',as_of=AS_OF)
        self.provider.set_source_state('official',outbound_allowed=False)
        with self.assertRaises(ValueError):consumer.assert_fresh()

    def test_allowlist_revision_rechecked(self):
        self.active()
        self.provider.register_source('official',publisher='测试官方源',endpoints=['https://official.example/other/'],
            freshness={'fees':None,'liquidity':None,'terms':None},permission_basis='revised_test')
        self.assertEqual(self.search()['hits'],[])

    def test_field_conflict_does_not_disable_independent_risk(self):
        _,release=self.active()
        risk=self.stage(source_url='https://official.example/risk',text='风险披露：可能损失本金。',facets=['risk'])
        self.provider.approve_version(risk,verification_basis='test');self.provider.publish()
        self.provider.block_conflict(product_id='PRODUCT-A',share_class_id='SHARE-A',channel_id='CHANNEL-A',facets=['fees'],from_on='2026-10-01',reason='different_rates')
        result=self.search(facets=['fees','risk'])
        self.assertIn('conflicting_sources',{g['code'] for g in result['coverage_gaps']})
        self.assertIn('fees',{g.get('facet') for g in result['coverage_gaps'] if g['code']=='facet_uncovered'})
        self.assertTrue(any('risk' in h['facets'] for h in result['hits']))
        self.assertTrue(all('fees' not in h['facets'] for h in result['hits']))

    def test_freshness_per_dimension_and_disclosure_not_retrieval_date(self):
        text='历史收益为0.10%。合同条款：赎回以确认日期为准。'
        split=text.index('合同')
        self.active(text=text,facets=['returns','terms'],published_on='2026-09-01',data_on='2026-09-01',
                    spans=[{'start_char':0,'end_char':split,'facets':['returns']},
                           {'start_char':split,'end_char':len(text),'facets':['terms']}])
        result=self.search(facets=['returns','terms'])
        self.assertTrue(result['hits'])
        self.assertEqual(result['hits'][0]['facets'],['terms'])
        self.assertNotIn('0.10%',result['hits'][0]['excerpt'])
        self.assertIn('returns',{g.get('facet') for g in result['coverage_gaps']})

    def test_unknown_effective_end_is_not_fabricated(self):
        self.active(effective_to=None)
        result=self.search()
        self.assertEqual(result['hits'][0]['effective_to'],None)
        self.assertIn('effective_period_unknown',{g['code'] for g in result['coverage_gaps']})

    def test_future_publication_and_expiration(self):
        self.active()
        self.assertEqual(self.search(as_of='2026-08-31')['hits'],[])
        self.assertEqual(self.search(as_of='2026-11-01')['hits'],[])

    def test_structure_drift_pauses_publish_preserves_good_version(self):
        old,release=self.active()
        new=self.stage(text='未核验新版结构。',shape='different-layout')
        with self.assertRaisesRegex(KnowledgeError,'source_structure_drift'):
            self.provider.approve_version(new,verification_basis='test')
        self.assertEqual(self.provider.status()['public_release_id'],release)
        self.assertEqual(self.search()['hits'][0]['document_version_id'],old)
        with self.assertRaisesRegex(KnowledgeError,'empty_release'):self.provider.publish()

    def test_temporary_snapshot_same_admission_no_global_promotion(self):
        old,release=self.active()
        new=self.stage(source_url='https://official.example/temporary',text='临时风险证据：可能亏损。',facets=['risk'])
        with self.assertRaises(KnowledgeError):self.provider.publish(version_ids=[new],activate=False,temporary=True)
        self.provider.approve_version(new,verification_basis='same_admission')
        temporary=self.provider.publish(version_ids=[new],activate=False,temporary=True)
        self.assertEqual(self.provider.status()['public_release_id'],release)
        self.assertEqual(self.search(provider=self.provider.for_release(temporary),facets=['risk'])['hits'][0]['document_version_id'],new)
        with self.assertRaises(KnowledgeError):self.provider.publish(version_ids=[new],activate=True,temporary=True)

    def test_corrupt_raw_or_quote_cannot_resolve(self):
        version,release=self.active();cid=self.search()['hits'][0]['citation_id']
        with self.provider.db() as db:
            db.execute('DROP TRIGGER immutable_raw')
            db.execute('UPDATE raw_artifacts SET payload=?',(b'corrupt',))
        self.assertIsNone(self.provider.resolve_public_evidence(cid,release,AS_OF))

    def test_fts_query_syntax_is_not_executed_as_operator(self):
        self.active()
        result=self.search(query='赎回 OR " * NEAR( ) - NOT费用')
        self.assertTrue(result['hits'])

    def test_resolver_wrong_release_cannot_cross_snapshot(self):
        _,release=self.active();cid=self.search()['hits'][0]['citation_id']
        other=self.stage(source_url='https://official.example/isolated',text='独立风险披露。',facets=['risk'])
        self.provider.approve_version(other,verification_basis='test')
        isolated=self.provider.publish(version_ids=[other],activate=False,temporary=True)
        self.assertIsNone(self.provider.resolve_public_evidence(cid,isolated,AS_OF))

    def test_supplement_uses_registered_public_fields_not_user_question(self):
        service=SupplementService(self.provider);self.addCleanup(service.close)
        observed=[]
        def fetch(request):
            observed.append(request)
            return FetchResult((dict(source_url=URL,title='测试条款',text='费用规则0.10%。',published_on='2026-10-01',
                retrieved_on=AS_OF,facets=['fees'],shape='plain-text-v1'),))
        service.register('official','PRODUCT-A',endpoint=URL,fetcher=fetch,product_id='PRODUCT-A',share_class_id='SHARE-A',channel_id='CHANNEL-A',facets=('fees',))
        result=service.supplement({'scope':'public','as_of':AS_OF,'product_id':'PRODUCT-A','facets':['fees'],
                                   'query':'私人账户余额是秘密，不能外发','history':['私有历史']})
        self.assertEqual(len(result['candidate_version_ids']),1)
        self.assertNotIn('秘密',repr(observed))
        self.assertFalse(self.provider.status()['ready'])
        self.assertFalse(result['published'])
        self.assertEqual(service.supplement({'as_of':AS_OF,'product_id':'PRODUCT-A','facets':['fees']})['candidate_version_ids'],result['candidate_version_ids'])
        self.assertEqual(len(observed),1)

    def test_supplement_same_key_coalesces(self):
        service=SupplementService(self.provider);self.addCleanup(service.close)
        called=[]
        def fetch(request):
            called.append(request);time.sleep(.03)
            return FetchResult((dict(source_url=URL,title='条款',text='赎回两日到账。',published_on=AS_OF,facets=['liquidity'],shape='plain-text-v1'),))
        service.register('official','PRODUCT-A',endpoint=URL,fetcher=fetch,product_id='PRODUCT-A',facets=('liquidity',))
        request={'as_of':AS_OF,'product_id':'PRODUCT-A','facets':['liquidity']}
        with ThreadPoolExecutor(max_workers=3) as executor:
            results=list(executor.map(lambda _:service.supplement(request),range(3)))
        self.assertEqual(len(called),1)
        self.assertTrue(all(r['candidate_version_ids']==results[0]['candidate_version_ids'] for r in results))

    def test_supplement_timeout_empty_and_drift_do_not_overwrite_good(self):
        version,release=self.active()
        for code in ('empty_source_response','source_structure_drift','deadline_exceeded'):
            with self.subTest(code=code):
                self.provider.set_source_state('official',paused=False)
                service=SupplementService(self.provider);self.addCleanup(service.close)
                def fetch(request,code=code):
                    if code=='deadline_exceeded':time.sleep(.025);return FetchResult(())
                    return FetchResult(error_code=code)
                service.register('official','PRODUCT-A',endpoint=URL,fetcher=fetch,product_id='PRODUCT-A',facets=('fees',))
                result=service.supplement({'as_of':AS_OF,'product_id':'PRODUCT-A','facets':['fees']},timeout_seconds=.01 if code=='deadline_exceeded' else 1)
                self.assertEqual(result['candidate_version_ids'],[])
                self.assertEqual(self.provider.status()['public_release_id'],release)
                self.assertEqual(self.search()['hits'][0]['document_version_id'],version)

    def test_supplement_identity_and_limits_fail_closed(self):
        service=SupplementService(self.provider,max_documents=1);self.addCleanup(service.close)
        def fetch(request):return FetchResult((dict(source_url=URL,title='条款',text='条款',product_id='PRODUCT-B'),))
        service.register('official','PRODUCT-A',endpoint=URL,fetcher=fetch,product_id='PRODUCT-A',facets=('fees',))
        result=service.supplement({'as_of':AS_OF,'product_id':'PRODUCT-A','facets':['fees']})
        self.assertEqual(result['coverage_gaps'][0]['reason'],'subject_mismatch')
        self.assertEqual(result['candidate_version_ids'],[])
        absent=service.supplement({'as_of':AS_OF,'product_id':'PRODUCT-B','facets':['risk']})
        self.assertEqual(absent['coverage_gaps'][0]['facet'],'risk')

    def test_market_offline_import_and_rejection(self):
        path=Path(self.temp.name)/'market.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE market_articles(id TEXT,version INTEGER,fingerprint TEXT,body TEXT,status TEXT,checked_at TEXT)')
            good={'source_id':'nbs','url':'https://www.stats.gov.cn/sj/zxfb/202610/t20261005_1965449.html','title':'官方统计发布测试',
                  'published_at':'2026-10-05T10:00:00+00:00','read_scope':'full_text','link_status':'verified','evidence_text':'统计数据显示经济指标变化。','evidence_truncated':False}
            for identity,article in [('good',good),('metadata',{**good,'read_scope':'metadata_only'}),('truncated',{**good,'evidence_truncated':True}),('media',{**good,'source_id':'xinhua'})]:
                db.execute('INSERT INTO market_articles VALUES(?,?,?,?,?,?)',(identity,1,'test',packed(article),'active','2026-10-06T10:00:00+00:00'))
        with patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
            report=import_market_store(self.provider,SimpleNamespace(path=path))
        self.assertEqual(report['imported'],1)
        self.assertEqual(report['rejected'],3)
        self.assertTrue(report['ready'])
        repeated=import_market_store(self.provider,SimpleNamespace(path=path))
        self.assertEqual(report['release_id'],repeated['release_id'])

    def test_webank_direct_rows_have_separate_identity_and_versions(self):
        def body(source_date,charge):
            rows=[]
            for code in ('000001','000002'):
                rows.append({'code':code,'name':'测试产品'+code,'saleStatus':'在售','type':'基金','placement':'代销','saleType':'公开',
                    'risk':'中风险','publish':'TEST','investor':'个人','charge':charge,'chargeWay':'费率见披露','customerGroupType':'个人'})
            return ('_jsonpproductpresentation('+json.dumps({'extractDate':source_date,'productPresentation':rows},ensure_ascii=False)+');').encode()
        report=import_webank_disclosure(self.provider,body('2026-10-01','0.10%'),publish=True)
        old_release=report['release_id']
        old=self.search(product_id='WEBANK:000001',share_class_id=None,channel_id='WEBANK',facets=['fees'])['hits'][0]
        self.assertEqual(old['product_id'],'WEBANK:000001')
        self.assertIn('000001',old['locator']['paragraph'])
        revised=import_webank_disclosure(self.provider,body('2026-10-05','0.20%'),publish=False)
        self.assertIsNone(self.provider.resolve_public_evidence(old['citation_id'],old_release,AS_OF))
        self.assertEqual(self.provider.status()['public_release_id'],old_release)
        self.provider.publish()
        hits=self.search(product_id='WEBANK:000001',share_class_id=None,channel_id='WEBANK',facets=['fees'])['hits']
        current=hits[0]
        self.assertTrue(any('0.20%' in hit['excerpt'] for hit in hits))
        self.assertEqual(current['effective_to'],None)
        self.assertFalse(revised['share_mapping_verified'])

    def test_market_activation_preserves_subset_without_promoting_unrelated_verified_or_temporary(self):
        base=self.stage();self.provider.approve_version(base,verification_basis='test')
        initial=self.provider.publish(version_ids=[base])
        orphan=import_webank_disclosure(self.provider,self.webank_fixture(),codes=['000002'],publish=False)
        temporary=self.provider.publish(version_ids=orphan['version_ids'],activate=False,temporary=True)
        candidate=self.stage(product_id='PRODUCT-B',share_class_id=None,text='尚未准入的产品风险资料。',facets=['risk'])
        self.assertEqual(self.provider.status()['public_release_id'],initial)
        report=import_market_store(self.provider,self.market_fixture(),publish=True)
        self.assertEqual(self.released_versions(report['release_id']),{base,*report['version_ids']})
        self.assertTrue(set(orphan['version_ids']).isdisjoint(self.released_versions(report['release_id'])))
        self.assertNotIn(candidate,self.released_versions(report['release_id']))
        self.assertNotEqual(temporary,report['release_id'])
        repeated=import_market_store(self.provider,SimpleNamespace(path=Path(self.temp.name)/'market-subset.sqlite3'),publish=True)
        self.assertEqual(repeated['release_id'],report['release_id'])
        # 显式手动全库发布仍可包含已准入版本，与自动导入激活分开。
        explicit=self.provider.publish()
        self.assertTrue(set(orphan['version_ids']).issubset(self.released_versions(explicit)))

    def test_empty_market_import_does_not_change_current_subset(self):
        base=self.stage();self.provider.approve_version(base,verification_basis='test')
        initial=self.provider.publish(version_ids=[base])
        import_webank_disclosure(self.provider,self.webank_fixture(),codes=['000002'],publish=False)
        report=import_market_store(self.provider,self.market_fixture(complete=False),publish=True)
        self.assertEqual(report['imported'],0)
        self.assertEqual(report['release_id'],initial)
        self.assertEqual(self.provider.status()['public_release_id'],initial)

    def test_explicit_webank_activation_only_adds_requested_import_versions(self):
        base=self.stage();self.provider.approve_version(base,verification_basis='test')
        self.provider.publish(version_ids=[base])
        orphan=import_webank_disclosure(self.provider,self.webank_fixture(),codes=['000002'],publish=False)
        self.provider.publish(version_ids=orphan['version_ids'],activate=False,temporary=True)
        report=import_webank_disclosure(self.provider,self.webank_fixture(),codes=['000001'],publish=True)
        self.assertEqual(self.released_versions(report['release_id']),{base,*report['version_ids']})
        self.assertTrue(set(orphan['version_ids']).isdisjoint(self.released_versions(report['release_id'])))
        repeated=import_webank_disclosure(self.provider,self.webank_fixture(),codes=['000001'],publish=True)
        self.assertEqual(repeated['release_id'],report['release_id'])
        self.assertEqual(self.search(product_id='WEBANK:000001',share_class_id=None,channel_id='WEBANK',facets=['risk'])['hits'],[])

    def test_concurrent_import_activation_retains_both_explicit_batches(self):
        base=self.stage();self.provider.approve_version(base,verification_basis='test')
        self.provider.publish(version_ids=[base])
        versions=[]
        for pid in ('PRODUCT-A','PRODUCT-B'):
            version=self.stage(source_url=URL+'/'+pid,product_id=pid,share_class_id=None,text='风险披露：净值可能波动。',facets=['risk'])
            self.provider.approve_version(version,verification_basis='test');versions.append(version)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda v:self.provider.publish(version_ids=[v],preserve_current=True),versions))
        self.assertEqual(self.released_versions(self.provider.status()['public_release_id']),{base,*versions})

    def test_graph_gate_and_two_hop_rule_paths_are_actual_retrieval(self):
        _,release=self.active()
        hit=self.search()['hits'][0]
        trace=self.provider.graph_trace(hit['citation_id'],release,AS_OF)
        self.assertTrue(any(len(path['relations'])>=2 and path['purpose']=='fees' for path in trace['paths']))
        self.assertTrue(any(len(path['relations'])>=3 and path['purpose']=='original_source' for path in trace['paths']))
        self.assertTrue(self.provider.status()['graph_ready'])
        self.assertEqual(self.provider.status()['provider_id'],'navren.sqlite.graphrag.v1')
        with self.provider.db() as db:
            relation=db.execute("SELECT id FROM graph_relations WHERE citation_id=? AND predicate='quoted_in'",(hit['citation_id'],)).fetchone()[0]
        self.provider.withdraw_relation(relation)
        # FTS 仍能命中，缺图通路时检索与原文回查均不得放行。
        with self.provider.db() as db:self.assertGreater(db.execute('SELECT count(*) FROM chunks_fts').fetchone()[0],0)
        self.assertEqual(self.search()['hits'],[])
        self.assertIsNone(self.provider.resolve_public_evidence(hit['citation_id'],release,AS_OF))

    def test_graph_context_cannot_bind_another_product(self):
        _,release=self.active();cid=self.search()['hits'][0]['citation_id']
        trace=self.provider.graph_trace(cid,release,AS_OF,request={'product_id':'PRODUCT-B'})
        self.assertEqual(trace['paths'],[])

    def test_issuer_not_inferred_from_product_name_or_metadata(self):
        _,release=self.active()
        self.assertEqual(self.provider.related_products('PRODUCT-A',release,AS_OF),[])
        with self.provider.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM graph_relations WHERE predicate='issued_by'").fetchone()[0],0)
        cid=self.search()['hits'][0]['citation_id']
        with self.assertRaisesRegex(KnowledgeError,'relation_not_in_quote'):
            self.provider.add_reviewed_relation(subject_kind='product',subject_ref='PRODUCT-A',predicate='issued_by',
                object_kind='institution',object_ref='UNKNOWN',object_label='从未出现的机构',citation_id=cid,
                reviewed_by='local_operator',start_char=0,end_char=10)

    def test_reviewed_same_institution_relation_uses_two_distinct_evidences(self):
        versions=[]
        for pid,url in [('PRODUCT-A',URL),('PRODUCT-B','https://official.example/product-b')]:
            version=self.stage(product_id=pid,share_class_id=None,channel_id=None,source_url=url,
                               text='发行机构为甲机构。风险披露：可能损失本金。',facets=['risk'])
            self.provider.approve_version(version,verification_basis='verified_identity_and_quote');versions.append(version)
        release=self.provider.publish()
        edges=[]
        for pid in ('PRODUCT-A','PRODUCT-B'):
            cid=self.search(product_id=pid,share_class_id=None,channel_id=None,facets=['risk'])['hits'][0]['citation_id']
            edges.append(self.provider.add_reviewed_relation(subject_kind='product',subject_ref=pid,predicate='issued_by',
                object_kind='institution',object_ref='ISSUER-A',object_label='甲机构',citation_id=cid,
                reviewed_by='human_review_fixture',start_char=0,end_char=10))
        peers=self.provider.related_products('PRODUCT-A',release,AS_OF)
        self.assertEqual(peers[0]['product_id'],'PRODUCT-B')
        self.assertEqual(len(peers[0]['path']),2)
        self.assertNotEqual(peers[0]['path'][0]['citation_id'],peers[0]['path'][1]['citation_id'])
        self.provider.withdraw_relation(edges[1])
        self.assertEqual(self.provider.related_products('PRODUCT-A',release,AS_OF),[])

    def test_graph_schema_rejects_private_or_unknown_entity_type(self):
        with self.assertRaises(sqlite3.IntegrityError),self.provider.db() as db:
            db.execute('INSERT INTO graph_entities VALUES(?,?,?,?,?,?)',('private','user_account','acct-private','私有账户','{}','public'))
        with self.assertRaises(sqlite3.IntegrityError),self.provider.db() as db:
            db.execute('INSERT INTO graph_entities VALUES(?,?,?,?,?,?)',('private','product','product-private','私有资料','{}','workspace'))

    def test_personal_database_cannot_be_used_as_shared_knowledge(self):
        private=Path(self.temp.name)/'personal.sqlite3'
        with sqlite3.connect(private) as db:db.execute('CREATE TABLE workspaces(id TEXT PRIMARY KEY)')
        with self.assertRaisesRegex(KnowledgeError,'personal_database_forbidden'):LocalKnowledgeProvider(private)

    def test_graph_evidence_withdrawal_invalidates_consumer_before_release(self):
        _,release=self.active();consumer=KnowledgeRetrieval(self.provider)
        result=consumer.search('费用',as_of=AS_OF)
        self.assertTrue(result['hits'])
        with self.provider.db() as db:
            relation=db.execute("SELECT id FROM graph_relations WHERE citation_id=? AND predicate='quoted_in'",(result['hits'][0]['citation_id'],)).fetchone()[0]
        self.provider.withdraw_relation(relation)
        with self.assertRaisesRegex(ValueError,'stale_knowledge_evidence'):consumer.assert_fresh()

    def test_webank_structure_drift_pauses_existing_source(self):
        self.provider.register_source('webank-direct-disclosure',publisher='微众银行',endpoints=['https://www.webank.com/test'],
                                      freshness={'general':7},permission_basis='test')
        with self.assertRaisesRegex(KnowledgeError,'source_structure_drift'):
            import_webank_disclosure(self.provider,b'{"changed":true}')
        self.assertTrue(self.provider.source('webank-direct-disclosure')['paused'])

    def test_public_catalog_excludes_candidate_unpublished_and_revoked_objects(self):
        self.assertEqual(self.provider.public_catalog(),{'terms':[],'identifiers':[]})
        self.stage(product_id='PRODUCT-B',share_class_id=None)
        self.active()
        catalog=self.provider.public_catalog()
        self.assertIn('PRODUCT-A',catalog['identifiers'])
        self.assertIn('SHARE-A',catalog['identifiers'])
        self.assertIn('CHANNEL-A',catalog['identifiers'])
        self.assertNotIn('PRODUCT-B',catalog['identifiers'])
        self.assertIn('费用',catalog['terms'])
        self.assertLessEqual(sum(len(v) for v in self.provider.public_catalog(limit=3).values()),3)
        self.provider.set_source_state('official',outbound_allowed=False)
        self.assertEqual(self.provider.public_catalog(),{'terms':[],'identifiers':[]})

    def test_catalog_retains_published_identity_after_terms_expire(self):
        self.active(facets=['returns'],published_on='2026-09-01',data_on='2026-09-01')
        self.assertEqual(self.search(facets=['returns'])['hits'],[])
        self.assertIn('PRODUCT-A',self.provider.public_catalog()['identifiers'])
        self.provider.register_product('PRODUCT-A',issuer='TEST',scheme='test-code',code='PRODUCT-A',name='尚未发布的新名称',verified=True)
        self.assertNotIn('尚未发布的新名称',self.provider.public_catalog()['terms'])
        other=self.stage(product_id='PRODUCT-B',share_class_id=None,facets=['fees'])
        self.provider.approve_version(other,verification_basis='test')
        self.provider.publish(version_ids=[other])
        self.assertIn('PRODUCT-A',self.provider.public_catalog()['identifiers'])
        self.provider.set_source_state('official',outbound_allowed=False)
        self.assertEqual(self.provider.public_catalog(),{'terms':[],'identifiers':[]})

    def test_raw_and_parse_must_be_same_original(self):
        with self.assertRaisesRegex(KnowledgeError,'raw_parse_mismatch'):
            self.stage(raw_bytes=b'completely different original')

    def test_conditions_must_have_source_evidence(self):
        version=self.stage(conditions=['只适用于虚构资格'])
        with self.assertRaisesRegex(KnowledgeError,'condition_evidence_missing'):
            self.provider.approve_version(version,verification_basis='test')

    def test_supplement_independent_public_object_allowlist(self):
        service=SupplementService(self.provider);self.addCleanup(service.close)
        with self.assertRaisesRegex(KnowledgeError,'identity_unverified'):
            service.register('official','private-account-id',endpoint=URL,fetcher=lambda _:FetchResult(),product_id='private-account-id',facets=('fees',))
        with self.assertRaisesRegex(KnowledgeError,'public_concept_unregistered'):
            service.register('official','private-holding-key',endpoint=URL,fetcher=lambda _:FetchResult(),facets=('fees',))

    def test_future_data_on_is_not_available_for_earlier_task(self):
        self.active(facets=['returns'],data_on='2026-10-10')
        result=self.search(facets=['returns'])
        self.assertEqual(result['hits'],[])
        self.assertIn('not_yet_effective',{g['code'] for g in result['coverage_gaps']})

    def test_encoded_dot_segments_cannot_escape_registered_path(self):
        self.provider.register_source('path-guard',publisher='测试路径',endpoints=['https://official.example/public/'],
                                      freshness={'general':30},permission_basis='test')
        for path in ('public/../private/admin','public/%2e%2e/private/admin','public/%252e%252e/private/admin',
                     'public/./terms','public//terms','public/%2f../private/admin','public/%5c../private/admin'):
            with self.subTest(path=path),self.assertRaisesRegex(KnowledgeError,'source_url_denied'):
                self.provider.validate_source_url('path-guard','https://official.example/'+path)

    def test_mixed_conflicted_or_stale_raw_piece_is_wholly_rejected(self):
        self.active(facets=['fees','risk'])
        self.provider.block_conflict(product_id='PRODUCT-A',facets=['fees'],reason='rates_conflict',from_on='2026-10-01')
        self.assertEqual(self.search(facets=['risk'])['hits'],[])

    def test_source_reader_has_total_caller_deadline_and_discards_late_body(self):
        finished=threading.Event()
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):finished.set()
            def geturl(self):return URL
            def read1(self,size):time.sleep(.20);return b'late body'
        class Opener:
            def open(self,*args,**kwargs):time.sleep(.02);return Response()
        with patch('local_knowledge.sources.urllib.request.build_opener',return_value=Opener()):
            start=time.monotonic()
            with self.assertRaisesRegex(KnowledgeError,'deadline_exceeded'):
                fetch_registered(self.provider,'official',URL,timeout=.05)
            self.assertLess(time.monotonic()-start,.12)
            self.assertTrue(finished.wait(.5))

    def test_import_cannot_restore_source_permissions(self):
        from local_knowledge.importers import _register_once
        self.provider.set_source_state('official',enabled=False,outbound_allowed=False)
        _register_once(self.provider,'official',publisher='测试官方源',endpoints=['https://official.example/'],
            freshness={'general':30},shape='plain-text-v1',permission_basis='test')
        source=self.provider.source('official')
        self.assertFalse(source['enabled']);self.assertFalse(source['outbound_allowed'])

    def test_webank_unknown_structure_pauses_before_approval(self):
        from webank_catalog import URL as official_url
        self.provider.register_source('webank-direct-disclosure',publisher='微众银行',endpoints=[official_url],
            parser_id='webank-record-json/1',shape='webank-productpresentation-v1',freshness={'general':7},permission_basis='test')
        row={k:'' for k in ('code','name','saleStatus','type','placement','saleType','risk','publish','investor','charge','chargeWay')}
        row.update(code='000001',name='测试产品',unknownPrivateField='unreviewed')
        body=('_jsonpproductpresentation('+packed({'extractDate':'2026-10-01','productPresentation':[row]})+');').encode()
        with self.assertRaisesRegex(KnowledgeError,'source_structure_drift'):
            import_webank_disclosure(self.provider,body,publish=True)
        self.assertTrue(self.provider.source('webank-direct-disclosure')['paused'])
        self.assertFalse(self.provider.status()['ready'])

    def test_pdf_parser_preserves_raw_pages_spans_and_unknown_effective_dates(self):
        raw=self.pdf_fixture();extracted=extract_pdf(raw)
        self.assertEqual([p['text'] for p in extracted['pages']],['Fee 0.10%','Redemption T+2'])
        self.pdf_source()
        spans=[{'page':p['page'],'start_char':p['start_char'],'end_char':p['end_char'],
                'facets':['fees' if p['page']==1 else 'liquidity']} for p in extracted['pages']]
        result=stage_reviewed_pdf(self.provider,raw,spans,source_id='official-pdf',source_url=URL,title='测试PDF条款',
            reviewed_by='fixture_reviewer',review_reference='synthetic_pdf_test',published_on='2026-09-01',
            product_id='PRODUCT-A',share_class_id='SHARE-A',channel_id='CHANNEL-A')
        self.assertFalse(result['approved']);self.assertFalse(self.provider.status()['ready'])
        self.provider.approve_version(result['version_id'],verification_basis=result['verification_basis'])
        self.provider.publish(version_ids=[result['version_id']])
        hits=self.search()['hits'];self.assertEqual(len(hits),2)
        self.assertEqual({h['locator']['page'] for h in hits},{1,2})
        self.assertTrue(all(h['effective_from'] is None and h['effective_to'] is None for h in hits))
        with self.provider.db() as db:
            payload=bytes(db.execute('SELECT payload FROM raw_artifacts WHERE sha256=?',(result['raw_sha256'],)).fetchone()[0])
            self.assertEqual(payload,raw)
        # 展示时回查原始/解析hash与不可变跨度，不重复解析完整PDF。
        with patch('local_knowledge.pdf_ingest.extract_pdf',side_effect=AssertionError('no_reparse_on_read')):
            self.assertIsNotNone(self.provider.resolve_public_evidence(hits[0]['citation_id'],hits[0]['release_id'],AS_OF))

    def test_pdf_manual_text_or_wrong_page_cannot_enter(self):
        raw=self.pdf_fixture();extracted=extract_pdf(raw);self.pdf_source()
        values=dict(source_id='official-pdf',source_url=URL,title='测试PDF',raw_bytes=raw,parser_id=PDF_PARSER,shape=PDF_SHAPE,
            product_id='PRODUCT-A',facets=['fees'],spans=[{'page':1,'start_char':0,'end_char':9}])
        with self.assertRaisesRegex(KnowledgeError,'raw_parse_mismatch'):
            self.provider.stage_document(text=extracted['text'].replace('0.10%','0.00%'),**values)
        for spans in ([{'start_char':0,'end_char':9}],
                      [{'page':2,'start_char':0,'end_char':9}],
                      [{'page':1,'start_char':0,'end_char':len(extracted['text'])}]):
            with self.subTest(spans=spans),self.assertRaises(KnowledgeError):
                self.provider.stage_document(text=extracted['text'],**{**values,'spans':spans})
        # 原始PDF不能冒充exact-text/1，即便文字由人工审核过。
        with self.assertRaisesRegex(KnowledgeError,'raw_parse_mismatch'):
            self.stage(text=extracted['text'],raw_bytes=raw)

    def test_pdf_version_cache_and_review_authority_are_fixed(self):
        raw=self.pdf_fixture();extracted=extract_pdf(raw)
        extracted['pages'][0]['text']='fake';extracted['parameters']['layout']=True
        self.assertEqual(extract_pdf(raw)['pages'][0]['text'],'Fee 0.10%')
        self.assertFalse(extract_pdf(raw)['parameters']['layout'])
        with patch('local_knowledge.pdf_ingest.metadata.version',return_value='0.0'):
            with self.assertRaisesRegex(KnowledgeError,'pdf_parser_version_mismatch'):extract_pdf(raw)
        with self.assertRaisesRegex(KnowledgeError,'pdf_human_review_required'):
            stage_reviewed_pdf(self.provider,raw,[],source_id='official-pdf',source_url=URL,title='测试',reviewed_by='',review_reference='')

    def test_pdf_invalid_or_no_text_is_not_a_source(self):
        with self.assertRaisesRegex(KnowledgeError,'invalid_pdf_original'):extract_pdf(b'not a PDF')
        with self.assertRaisesRegex(KnowledgeError,'pdf_parse_failed'):extract_pdf(b'%PDF-1.4\nbroken')


if __name__=='__main__':unittest.main()
