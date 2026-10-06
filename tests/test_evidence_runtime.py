"""真实临时SQLite/受控目录夹具；不代表真实金融来源或模型语义通过。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_knowledge import LocalKnowledgeProvider, KnowledgeError
from local_knowledge.runtime import EvidenceRuntime, RoundProductCatalog
from local_knowledge.store import today
from local_knowledge.webank_runtime import decode_webank_catalog, ensure_webank_source
from webank_catalog import FIELD_MAP, URL, decode_catalog
from base_runtime.evidence import PublicEvidenceObserver, EvidenceGuardedClient
from simulated_bank import BankError


def catalog_body(source_date=None, extra=None):
    records=[]
    for code,name in [('000001','微众测试甲产品'),('000002','微众测试乙产品')]:
        row={k:'' for k in FIELD_MAP}
        row.update(code=code,name=name,risk='R2',charge='目录披露费用',chargeWay='完整费率另行核实',
                   saleStatus='在售',investor='个人',publish='目录披露机构')
        records.append(row)
    if extra:records[0].update(extra)
    return ('_jsonpproductpresentation('+json.dumps({'extractDate':source_date or today(),
            'productPresentation':records},ensure_ascii=False)+');').encode()


class EvidenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.p=LocalKnowledgeProvider(Path(self.temp.name)/'public.sqlite3')
        self.calls=[];self.body=catalog_body()
        def reader(provider,source_id,url,**options):
            self.calls.append((source_id,url,options));return self.body
        self.reader=reader
        self.r=EvidenceRuntime(self.p,reader=reader,background=False)
        self.addCleanup(self.r.close)

    def prepare(self,text='微众测试甲产品的风险和费用'):
        return self.r.prepare_round(text)

    def test_cold_round_is_retrievable_before_shared_publication(self):
        prepared=self.prepare()
        self.assertEqual(prepared.report['status'],'prepared')
        self.assertTrue(prepared.report['release_id'].startswith('tmp-'))
        self.assertIsNone(self.p.status()['public_release_id'])
        raw=prepared.provider.search_public({'scope':'public','query':'风险','product_id':'WEBANK:000001',
            'as_of':today(),'facets':['risk'],'limit':4},prepared.report['release_id'])
        self.assertTrue(raw['citation_ids'])
        hit=prepared.provider.resolve_public_evidence(raw['citation_ids'][0],prepared.report['release_id'],today())
        self.assertIn('R2',hit['excerpt'])
        self.assertEqual(len(self.calls),1)

    def test_private_text_never_enters_public_database_or_fetch_request(self):
        text='我叫私密测试甲，手机13812345678，存款8754321元，微众测试甲产品风险是什么'
        prepared=self.prepare(text)
        self.assertEqual(prepared.report['status'],'prepared')
        transmitted=json.dumps(self.calls,ensure_ascii=False)
        raw=self.p.path.read_bytes()
        for private in ('私密测试甲','13812345678','8754321',text):
            self.assertNotIn(private,transmitted);self.assertNotIn(private.encode(),raw)
        self.assertEqual(self.calls[0][1],URL)
        with self.p.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM share_classes').fetchone()[0],0)
            jobs=[dict(r) for r in db.execute('SELECT * FROM evidence_promotions')]
        self.assertNotIn('workspace',json.dumps(jobs))

    def test_temporary_identity_is_scoped_to_selected_round(self):
        first=self.prepare()
        second=self.prepare('微众测试乙产品的风险')
        self.assertIn('WEBANK:000001',first.provider.public_catalog()['identifiers'])
        self.assertNotIn('WEBANK:000001',second.provider.public_catalog()['identifiers'])
        self.assertIn('WEBANK:000002',second.provider.public_catalog()['identifiers'])
        self.assertEqual(self.p.public_catalog()['identifiers'],[])

    def test_publication_is_background_and_does_not_replace_round_pin(self):
        prepared=self.prepare();temporary=prepared.report['release_id']
        self.r.drain_promotions()
        self.assertTrue(self.p.status()['public_release_id'].startswith('pub-'))
        self.assertEqual(prepared.provider.status()['public_release_id'],temporary)
        self.assertTrue(prepared.provider.status()['ready'])
        with self.p.db() as db:
            self.assertEqual(db.execute('SELECT status FROM evidence_promotions').fetchone()[0],'completed')

    def test_durable_jobs_recovered_without_question_or_owner(self):
        prepared=self.prepare();self.r.close()
        restart=EvidenceRuntime(self.p,reader=self.reader,background=False);self.addCleanup(restart.close)
        restart.drain_promotions()
        self.assertTrue(self.p.status()['ready'])
        self.assertEqual(len(self.calls),1)
        self.assertNotEqual(self.p.status()['public_release_id'],prepared.report['release_id'])

    def test_candidate_cannot_enter_automatic_promotion(self):
        ensure_webank_source(self.p)
        self.p.register_product('OTHER',issuer='OTHER',scheme='fixture',code='OTHER',name='另一个产品',verified=True)
        self.p.register_source('other',publisher='测试源',endpoints=['https://official.example/'],
            permission_basis='fixture',freshness={'general':None})
        v=self.p.stage_document(source_id='other',source_url='https://official.example/text',title='标题',
                               text='仅为候选',product_id='OTHER')
        with self.assertRaisesRegex(KnowledgeError,'promotion_admission_required'):
            self.r.enqueue_promotions([v])
        self.p.approve_version(v,verification_basis='test-only')
        with self.assertRaisesRegex(KnowledgeError,'promotion_admission_required'):
            self.r.enqueue_promotions([v])

    def test_cached_source_permission_revocation_is_not_restored(self):
        first=self.prepare()
        self.p.set_source_state('webank-direct-disclosure',outbound_allowed=False)
        second=self.prepare()
        self.assertEqual(second.report['status'],'gap');self.assertEqual(len(self.calls),1)
        self.assertFalse(self.p.source('webank-direct-disclosure')['outbound_allowed'])
        self.assertEqual(RoundProductCatalog(first.provider,first.provider.records).read()['products'],[])
        self.r.drain_promotions()
        self.assertIsNone(self.p.status()['public_release_id'])

    def test_expired_catalog_does_not_become_current_evidence(self):
        self.body=catalog_body((date.fromisoformat(today())-timedelta(days=8)).isoformat())
        prepared=self.prepare()
        self.assertEqual(prepared.report['status'],'gap')
        self.assertFalse(prepared.report['temporary'])
        with self.p.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM document_versions').fetchone()[0],0)

    def test_sales_status_expiry_is_separate_from_other_fields(self):
        self.body=catalog_body((date.fromisoformat(today())-timedelta(days=1)).isoformat())
        prepared=self.prepare();rows=RoundProductCatalog(prepared.provider,prepared.provider.records).read()['products']
        self.assertEqual(rows[0]['risk'],'R2')
        self.assertIsNone(rows[0]['sale_status']);self.assertIsNone(rows[0]['current_sale_status'])
        self.assertFalse(rows[0]['executable'])
        self.assertTrue(any(g['facet']=='sale_status' for g in prepared.report['coverage_gaps']))

    def test_schema_drift_pauses_without_admitting_product(self):
        self.body=catalog_body(extra={'unregistered_field':'新结构'})
        prepared=self.prepare()
        self.assertEqual(prepared.report['status'],'gap')
        self.assertTrue(self.p.source('webank-direct-disclosure')['paused'])
        with self.p.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM products').fetchone()[0],0)

    def test_unknown_product_name_is_a_gap_not_a_guessed_identity(self):
        prepared=self.prepare('微众未知名称产品的费率')
        self.assertEqual(prepared.report['status'],'gap')
        with self.p.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM products').fetchone()[0],0)

    def test_same_key_cold_download_coalesces(self):
        barrier=threading.Barrier(4)
        def reader(*args,**kwargs):
            self.calls.append(('fixed',URL,{}));time.sleep(.03);return self.body
        self.r.reader=reader
        def run(_):barrier.wait();return self.prepare().report
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(run,range(4)))
        self.assertTrue(all(r['status']=='prepared' for r in results),results)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(len({r['release_id'] for r in results}),1)
        with self.p.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM raw_artifacts').fetchone()[0],1)

    def test_non_financial_question_does_not_fetch(self):
        prepared=self.prepare('我的工资和手机号码是多少')
        self.assertEqual(prepared.report['status'],'not_requested');self.assertEqual(self.calls,[])

    def test_date_contract_uses_shanghai_source_day(self):
        with patch('webank_catalog.publication_today',return_value=date(2026,10,6)):
            decode_catalog(catalog_body('2026-10-06'))
            with self.assertRaisesRegex(ValueError,'invalid_catalog_date'):
                decode_catalog(catalog_body('2026-10-07'))

    def test_warm_product_name_reuses_verified_release_without_network(self):
        self.body=catalog_body(extra={'name':'本地甲号'})
        self.prepare('WEBANK:000001 风险和费用');self.r.drain_promotions()
        self.r.reader=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('must_not_fetch'))
        prepared=self.prepare('本地甲号风险如何')
        self.assertEqual(prepared.report['status'],'reused')
        result=RoundProductCatalog(prepared.provider,prepared.provider.records).read('本地甲号')
        self.assertEqual(result['products'][0]['risk'],'R2');self.assertEqual(len(self.calls),1)

    def test_comparison_supplements_second_object_missing_from_shared_release(self):
        self.prepare();self.r.drain_promotions()
        prepared=self.prepare('微众测试甲产品和微众测试乙产品风险对比')
        self.assertEqual(prepared.report['status'],'prepared')
        result=RoundProductCatalog(prepared.provider,prepared.provider.records).read()
        self.assertEqual({p['code'] for p in result['products']},{'000001','000002'})
        self.assertEqual(len(self.calls),1)

    def test_unknown_comparison_object_is_an_explicit_gap(self):
        self.prepare();self.r.drain_promotions()
        prepared=self.prepare('微众测试甲产品和WEBANK:UNKNOWN风险对比')
        self.assertIn('comparison_object_unconfirmed',[g['code'] for g in prepared.report['coverage_gaps']])

    def test_renamed_product_identity_uses_new_canonical_name(self):
        yesterday=(date.fromisoformat(today())-timedelta(days=1)).isoformat()
        self.body=catalog_body(yesterday,extra={'name':'旧的青禾'})
        self.prepare('WEBANK:000001 风险');self.r.drain_promotions()
        self.body=catalog_body(extra={'name':'新的青禾'});self.r.catalog_cache=None
        self.prepare('WEBANK:000001 风险和能买状态');self.r.drain_promotions()
        prepared=self.prepare('新的青禾风险');observer=PublicEvidenceObserver(prepared.provider)
        self.assertEqual(prepared.report['status'],'reused')
        self.assertIn('新的青禾',observer.public_catalog()['terms']);self.assertTrue(observer.pins)

    def test_directory_limit_is_not_reported_as_the_whole_bank_catalog(self):
        rows=[]
        for i in range(13):
            row={k:'' for k in FIELD_MAP};row.update(code=f'{i:06}',name=f'公开目录产品{i}',risk='R2')
            rows.append(row)
        self.body=('_jsonpproductpresentation('+json.dumps({'extractDate':today(),
                    'productPresentation':rows},ensure_ascii=False)+');').encode()
        prepared=self.prepare('微众产品目录')
        data=RoundProductCatalog(prepared.provider,prepared.provider.records).read()
        self.assertEqual(len(data['products']),12);self.assertEqual(data['catalog_total_count'],13)
        self.assertTrue(data['catalog_truncated']);self.assertEqual(data['total_scope'],'selected_round_objects')
        self.assertIn('selection_limit_reached',[g['code'] for g in prepared.report['coverage_gaps']])

    def test_public_product_query_accepts_canonical_id_without_false_staleness(self):
        prepared=self.prepare();catalog=RoundProductCatalog(prepared.provider,prepared.provider.records)
        data=catalog.read('WEBANK:000001')
        self.assertEqual(data['products'][0]['risk'],'R2');self.assertIs(data['stale'],False)
        missing=catalog.read('WEBANK:UNKNOWN')
        self.assertEqual(missing['products'],[]);self.assertIsNone(missing['stale'])

    def test_negative_cache_recovers_on_first_healthy_request_after_expiry(self):
        ticks=[100.0];self.r.clock=lambda:ticks[0]
        def bad(*a,**k):raise KnowledgeError('source_unavailable')
        self.r.reader=bad;self.assertEqual(self.prepare().report['status'],'gap')
        self.r.reader=self.reader;ticks[0]+=31
        self.assertEqual(self.prepare().report['status'],'prepared')
        self.assertEqual(len(self.calls),1)

    def test_failed_promotion_can_retry_after_explicit_permission_restoration(self):
        prepared=self.prepare()
        self.p.set_source_state('webank-direct-disclosure',outbound_allowed=False)
        self.r.drain_promotions()
        self.p.set_source_state('webank-direct-disclosure',outbound_allowed=True)
        self.assertEqual(self.prepare().report['status'],'prepared')
        self.r.drain_promotions();self.assertTrue(self.p.status()['ready'])

    def test_identity_read_is_pinned_and_rechecked_before_cloud(self):
        prepared=self.prepare();observer=PublicEvidenceObserver(prepared.provider)
        self.assertIn('WEBANK:000001',observer.public_catalog()['identifiers'])
        self.assertTrue(observer.pins)
        class Client:
            def complete(inner,*a,**k):self.fail('revoked identity reached cloud')
        self.p.set_source_state('webank-direct-disclosure',outbound_allowed=False)
        with self.assertRaises(BankError):EvidenceGuardedClient(Client(),observer).complete([])
        self.assertEqual(prepared.provider.public_catalog()['identifiers'],[])

    def test_preparation_limits_reach_both_models_without_private_text(self):
        prepared=self.prepare();observer=PublicEvidenceObserver(prepared.provider)
        class Client:
            def complete(inner,messages,*a,**k):return messages
            def review(inner,packet,*a,**k):return packet
        client=EvidenceGuardedClient(Client(),observer,{'status':'prepared',
                'coverage_gaps':[{'code':'comparison_object_unconfirmed','private_marker':'不能出库私密'}]})
        messages=client.complete([{'role':'user','content':'公开问题'}])
        self.assertIn('比较对象不齐须追问',messages[-1]['content'])
        packet=client.review({'question':'公开问题'})
        self.assertEqual(packet['public_evidence_limits'],messages[-1]['content'])
        self.assertNotIn('不能出库私密',json.dumps([messages,packet],ensure_ascii=False))

    def test_other_valid_source_cannot_override_channel_fields(self):
        prepared=self.prepare();pid='WEBANK:000001'
        self.p.register_source('other-official',publisher='另一个测试机构',endpoints=['https://official.example/'],
            freshness={'risk':None},permission_basis='fixture')
        v=self.p.stage_document(source_id='other-official',source_url='https://official.example/risk',
            title='另一渠道',text='"risk":"R5"',product_id=pid,facets=['risk'],
            spans=[{'start_char':0,'end_char':11,'paragraph':'other/risk','facets':['risk']}])
        self.p.approve_version(v,verification_basis='fixture')
        with self.p.db() as db:versions=[r[0] for r in db.execute("SELECT id FROM document_versions WHERE status='verified'")]
        release=self.p.publish(version_ids=versions,temporary=True,activate=False)
        record=dict(prepared.provider.records[0]);record.update(source_date='1900-01-01',source_url='https://wrong.invalid/')
        data=RoundProductCatalog(self.p.for_release(release),[record]).read()['products'][0]
        self.assertEqual(data['risk'],'R2');self.assertEqual(data['source_date'],today())
        self.assertNotIn('wrong',data['source_url'])

    def test_pending_queue_limit_is_atomic_and_completed_jobs_deduplicate(self):
        self.prepare();self.prepare('微众测试乙产品风险')
        with self.p.db() as db:
            versions=[r[0] for r in db.execute("SELECT id FROM document_versions WHERE status='verified'")]
            db.execute('DELETE FROM evidence_promotions')
            for i in range(63):db.execute("INSERT INTO evidence_promotions VALUES(?,?,'pending',?,NULL,NULL)",
                                        ('filler-'+str(i),'[]','fixture'))
        barrier=threading.Barrier(2)
        def enqueue(vid):barrier.wait();return self.r.enqueue_promotions([vid])
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(enqueue,versions))
        self.assertEqual(sorted(results),[False,True])
        with self.p.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evidence_promotions WHERE status='pending'").fetchone()[0],64)
            row=db.execute("SELECT version_ids FROM evidence_promotions WHERE id LIKE 'promote-%'").fetchone()
            db.execute("UPDATE evidence_promotions SET status='completed' WHERE id LIKE 'promote-%'")
            db.execute("INSERT INTO evidence_promotions VALUES('extra','[]','pending','fixture',NULL,NULL)")
        self.assertTrue(self.r.enqueue_promotions(json.loads(row[0])))

    def test_empty_round_does_not_adopt_later_shared_publication(self):
        empty=self.prepare('你好')
        self.prepare();self.r.drain_promotions()
        self.assertTrue(self.p.status()['ready'])
        self.assertIsNone(empty.provider.status()['public_release_id'])
        self.assertFalse(empty.provider.status()['ready'])

    def test_timed_out_catalog_cannot_be_admitted_when_it_finishes_late(self):
        finished=threading.Event()
        def late(*a,**k):
            time.sleep(.08);finished.set();return self.body
        self.r.reader=late;self.r.timeout=.01
        result=self.prepare();self.assertEqual(result.report['status'],'gap')
        self.assertTrue(finished.wait(1))
        with self.p.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM document_versions').fetchone()[0],0)
        self.assertIsNone(self.r.catalog_cache)


class EvidenceGuardedWireTests(unittest.TestCase):
    """真实适配器配合本地HTTP夹具，只证明wire/cap，不调用云模型。"""
    NOTICE=('本轮只核验选中公开对象的目录字段，不能据此推断即时可买、完整费率、收益或底层份额。'
            '仍有对象、维度或时效未核实；只能回答已核实部分并说明缺口，比较对象不齐须追问。')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.keys=[];self.sent=[];self.fresh=[]
        class Observer:
            def assert_fresh(inner):self.fresh.append(True)
        self.observer=Observer()
        self.preparation={'status':'prepared','coverage_gaps':[
            {'code':'comparison_object_unconfirmed','private_marker':'不能外发的用户原句'}]}

    def key_loader(self):
        self.keys.append(True)
        return 'synthetic-header-token'

    def opener(self, envelope):
        import io
        class Response(io.BytesIO):status=200
        def open_(request,**options):
            self.sent.append(request.data)
            return Response(json.dumps(envelope,ensure_ascii=False).encode('utf-8'))
        return open_

    def guarded_transports(self,opener=None):
        from agent_runtime.native_client import DeepSeekToolClient
        from agent_runtime.conversation import GLMConversationReviewer
        from agent_runtime.model_adapters import OpenAICompatibleJsonClient
        native=DeepSeekToolClient(urlopen=opener or self.opener({}),key_loader=self.key_loader,
            log_path=str(Path(self.temp.name)/'preflight-native.jsonl'))
        review=GLMConversationReviewer(OpenAICompatibleJsonClient(provider='ai_ping',model_id='synthetic-review',
            url='https://example.test',env_name='UNUSED_SYNTHETIC_KEY',keychain_service='UNUSED_SYNTHETIC_KEY',
            urlopen=opener or self.opener({}),key_loader=self.key_loader,
            log_path=str(Path(self.temp.name)/'preflight-review.jsonl'),max_retries=0))
        return [(native,'complete',([{'role':'user','content':'公开资料。'}],[])),
                (review,'review',({'question':'公开资料。','evidence':[],'draft':{'text':'公开字段。'}},'local-fixture',2))]

    def test_predelegate_freshness_rejection_has_zero_requests_and_original_code(self):
        from simulated_bank import BankError
        for transport,method,args in self.guarded_transports():
            with self.subTest(method=method):
                error=BankError('STALE_KNOWLEDGE_EVIDENCE','公开资料已撤回。',409)
                class Revoked:
                    def assert_fresh(inner):raise error
                client=EvidenceGuardedClient(transport,Revoked(),self.preparation)
                with self.assertRaises(BankError) as raised:
                    getattr(client,method)(*args,**({'request_id':'local-fixture','remaining_seconds':2} if method=='complete' else {}))
                self.assertIs(raised.exception,error)
                self.assertEqual((error.code,error.request_sent,error.attempts),('STALE_KNOWLEDGE_EVIDENCE',False,0))
        self.assertEqual(self.keys,[]);self.assertEqual(self.sent,[])

    def test_delegate_provider_errors_keep_actual_sent_and_attempts(self):
        from urllib.error import HTTPError
        from agent_runtime.model_adapters import ModelAdapterError
        def denied(request,**options):
            self.sent.append(request.data)
            raise HTTPError(request.full_url,403,'fixture-denied',None,None)
        for transport,method,args in self.guarded_transports(denied):
            with self.subTest(method=method):
                client=EvidenceGuardedClient(transport,self.observer,self.preparation)
                with self.assertRaises(ModelAdapterError) as raised:
                    getattr(client,method)(*args,**({'request_id':'local-fixture','remaining_seconds':2} if method=='complete' else {}))
                self.assertTrue(raised.exception.request_sent)
                self.assertEqual(raised.exception.attempts,1)
        self.assertEqual(len(self.keys),2);self.assertEqual(len(self.sent),2)

    def test_native_notice_exact_wire_cap_and_input_purity(self):
        from copy import deepcopy
        from agent_runtime.native_client import DeepSeekToolClient
        from agent_runtime.model_adapters import ModelAdapterError
        envelope={'choices':[{'finish_reason':'stop','message':{
            'role':'assistant','content':'本地协议夹具答复。'}}],
            'usage':{'prompt_tokens':1,'completion_tokens':0,'total_tokens':1}}
        native=DeepSeekToolClient(urlopen=self.opener(envelope),key_loader=self.key_loader,
            log_path=str(Path(self.temp.name)/'native.jsonl'))
        client=EvidenceGuardedClient(native,self.observer,self.preparation)
        messages=[{'role':'user','content':'中文与"引号"及\\转义。'}]
        tools=[{'type':'function','function':{'name':'fixture_read',
            'parameters':{'type':'object','properties':{},'additionalProperties':False}}}]
        original=deepcopy((messages,tools));expected_messages=[*messages,{'role':'system','content':self.NOTICE}]
        expected=native.serialized_request(expected_messages,tools)
        bound=client.request_input_bound(messages,tools)
        self.assertEqual(client.notice,self.NOTICE)
        self.assertEqual(bound,len(expected));self.assertGreater(bound,native.request_input_bound(messages,tools))
        self.assertEqual(client.request_payload(messages,tools),native.request_payload(expected_messages,tools))
        self.assertEqual(client.serialized_request(messages,tools),expected)
        with self.assertRaises(ModelAdapterError) as raised:
            client.complete(messages,tools,request_id='local-over-cap',remaining_seconds=2,max_input_bytes=bound-1)
        self.assertEqual((raised.exception.code,raised.exception.attempts),('request_budget_exceeded',0))
        self.assertEqual(self.keys,[]);self.assertEqual(self.sent,[])
        for _ in range(2):
            client.complete(messages,tools,request_id='local-exact-cap',remaining_seconds=2,max_input_bytes=bound)
        self.assertEqual(self.sent,[expected,expected]);self.assertEqual(len(self.keys),2)
        self.assertEqual(len(self.fresh),3);self.assertEqual((messages,tools),original)
        for wire in self.sent:
            payload=json.loads(wire)
            self.assertEqual(sum(m.get('content')==self.NOTICE for m in payload['messages']),1)
            self.assertNotIn('不能外发的用户原句',wire.decode())

    def test_glm_notice_exact_wire_cap_and_input_purity(self):
        from copy import deepcopy
        from agent_runtime.conversation import GLMConversationReviewer
        from agent_runtime.model_adapters import OpenAICompatibleJsonClient,ModelAdapterError
        envelope={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'verdict':'approve','reason':'仅核验本地协议夹具。','issues':[]},ensure_ascii=False)}}],
            'usage':{'prompt_tokens':1,'completion_tokens':0,'total_tokens':1}}
        transport=OpenAICompatibleJsonClient(provider='ai_ping',model_id='synthetic-review',
            url='https://example.test',env_name='UNUSED_SYNTHETIC_KEY',keychain_service='UNUSED_SYNTHETIC_KEY',
            urlopen=self.opener(envelope),key_loader=self.key_loader,
            log_path=str(Path(self.temp.name)/'review.jsonl'),max_retries=0)
        reviewer=GLMConversationReviewer(transport)
        client=EvidenceGuardedClient(reviewer,self.observer,self.preparation)
        packet={'question':'中文与"引号"及\\转义。','evidence':[],'draft':{'text':'公开字段。'}}
        original=deepcopy(packet);expected_packet={**packet,'public_evidence_limits':self.NOTICE}
        expected=reviewer.serialized_request(expected_packet)
        bound=client.request_input_bound(packet)
        self.assertEqual(client.notice,self.NOTICE)
        self.assertEqual(bound,len(expected));self.assertGreater(bound,reviewer.request_input_bound(packet))
        self.assertEqual(client.request_payload(packet),reviewer.request_payload(expected_packet))
        self.assertEqual(client.serialized_request(packet),expected)
        with self.assertRaises(ModelAdapterError) as raised:
            client.review(packet,'local-over-cap',2,max_input_bytes=bound-1)
        self.assertEqual((raised.exception.code,raised.exception.attempts),('request_budget_exceeded',0))
        self.assertEqual(self.keys,[]);self.assertEqual(self.sent,[])
        for _ in range(2):client.review(packet,'local-exact-cap',2,max_input_bytes=bound)
        self.assertEqual(self.sent,[expected,expected]);self.assertEqual(len(self.keys),2)
        self.assertEqual(len(self.fresh),3);self.assertEqual(packet,original)
        for wire in self.sent:
            payload=json.loads(wire);sent_packet=json.loads(payload['messages'][1]['content'])
            self.assertEqual(sent_packet,expected_packet)
            self.assertEqual(sent_packet['public_evidence_limits'],self.NOTICE)
            self.assertEqual(wire.decode().count('本轮只核验选中公开对象的目录字段'),1)
            self.assertNotIn('不能外发的用户原句',wire.decode())


if __name__=='__main__':unittest.main()
