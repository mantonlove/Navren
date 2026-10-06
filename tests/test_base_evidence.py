"""公开条款/市场版本在文字、语音、草案确认边界的确定性反例。"""
from contextlib import closing, contextmanager
from copy import deepcopy
from unittest.mock import patch
import unittest
import uuid
import json
import tempfile
from pathlib import Path

from base_runtime.evidence import PublicEvidenceObserver
from scripts.verify_base_runtime import temporary_runtime, Browser
from simulated_bank import BankError
from simulated_bank.agent_guard import bind_review, context_hash
from agent_runtime.bank_tools import BankTools


class PublicFixture:
    def __init__(self, original):
        self.original=original
        self.value={'citation_id':'citation-fixture','release_id':'release-fixture',
                    'document_version_id':'v1','excerpt':'经验证的公开条款',
                    'outbound_allowed':True,'facets':['product_terms']}

    def db(self):return self.original.db()
    def resolve_public_evidence(self,*args):return deepcopy(self.value)


class MarketFixture:
    def __init__(self, original):
        self.original=original;self.path=original.path
        self.value={'id':'event-fixture','version':1,'title':'合成市场证据测试','status':'published'}

    def db(self):return self.original.db()
    def event(self,*args):
        if self.value is None:raise KeyError('withdrawn')
        return deepcopy(self.value)


class EvidenceGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=temporary_runtime(initialize=False)
        cls.server,cls.base,_=cls.runtime.__enter__();cls.g=cls.server.finance

    @classmethod
    def tearDownClass(cls):cls.runtime.__exit__(None,None,None)

    def setUp(self):
        self.browser=Browser(self.base);self.state,_=self.browser.start();self.w=self.state['workspace_id']
        self.public=PublicFixture(self.g.knowledge_provider);self.market=MarketFixture(self.g.market)
        self.patchers=[patch.object(self.g,'knowledge_provider',self.public),patch.object(self.g,'market',self.market)]
        for p in self.patchers:p.start();self.addCleanup(p.stop)

    def knowledge(self):
        observer=PublicEvidenceObserver(self.public)
        observer.resolve_public_evidence('citation-fixture','release-fixture','2026-10-06')
        return observer.snapshot({'release_id':'release-fixture','citations':[{'citation_id':'citation-fixture','as_of':'2026-10-06'}]})

    def approved(self, knowledge=None, market=None, service=None):
        view=self.g.overview(self.w);account=max(view['accounts'],key=lambda r:r['balance_minor'])
        beneficiary=next(r for r in view['beneficiaries'] if r['verified'] and r['status']=='active')
        draft=self.g.prepare(self.w,{'action':'transfer','account_id':account['id'],
            'beneficiary_id':beneficiary['id'],'amount_minor':10000,
            'idempotency_key':'evidence-'+uuid.uuid4().hex},agent_origin=True)
        op=draft['operation'];oid=op['id']
        with closing(self.g.bank.connect()) as db:context=context_hash(db,self.w)
        bind_review(self.g.bank,self.w,oid,expected_context=context,evidence_hash='fixture-evidence-hash',
                    reviewer_model='offline-review-fixture',review_id=uuid.uuid4().hex)
        result={'compliance_pass':True,'bank_actions':[{'operation_id':oid}],
                'knowledge_snapshot':knowledge,'market_snapshot':market,'service_snapshot':service}
        with self.g.response_guard(self.w,self.state['privacy_revision'],self.g.personal_data.revision(self.w),
             knowledge,context,market_snapshot=market,service_snapshot=service) as db:
            self.g.operation_evidence.bind_answer(self.w,result,db)
        return op

    def confirm(self, op):
        _,code,_=self.browser.request('/api/finance/demo-code')
        return self.browser.request('/api/finance/operations/'+op['id']+'/confirm','POST',
            {'confirmed':True,'confirmation_digest':op['confirmation_digest'],'verification_code':code['code']})

    def test_public_terms_change_blocks_confirmation_and_preserves_cash(self):
        op=self.approved(knowledge=self.knowledge());before=self.g.overview(self.w)['total_cash_minor']
        self.public.value['excerpt']='修订后的不同条款'
        status,result,_=self.confirm(op)
        self.assertEqual((status,result['error']['code']),(409,'STALE_KNOWLEDGE_EVIDENCE'))
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'],before)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM transactions WHERE operation_id=?',(op['id'],)).fetchone()[0],0)

    def test_market_revision_blocks_confirmation_and_preserves_cash(self):
        tools=BankTools(self.g,self.w,uuid.uuid4().hex)
        data=tools._read_market('market_event',{'event_id':'event-fixture'})
        tools._pin_market_read('market_event',{'event_id':'event-fixture'},data)
        op=self.approved(market=tools.market_snapshot());before=self.g.overview(self.w)['total_cash_minor']
        self.market.value['version']=2
        status,result,_=self.confirm(op)
        self.assertEqual((status,result['error']['code']),(409,'STALE_MARKET_EVIDENCE'))
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'],before)

    def test_executed_receipt_remains_after_source_withdrawal(self):
        op=self.approved(knowledge=self.knowledge());before=self.g.overview(self.w)['total_cash_minor']
        self.assertEqual(self.confirm(op)[0],200)
        self.public.value=None
        status,result,_=self.confirm(op)
        self.assertEqual(status,200)
        self.assertEqual(result['operation']['state'],'succeeded')
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'],before-10000)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM transactions WHERE operation_id=?',(op['id'],)).fetchone()[0],1)

    def test_text_and_speech_use_same_canonical_evidence_digest(self):
        knowledge=self.knowledge();metrics=self.g.financial_metrics.snapshot(self.w)
        with closing(self.g.bank.connect()) as db:context=context_hash(db,self.w)
        result={**self.state,'personal_revision':self.g.personal_data.revision(self.w),'reply':'测试核验答复',
            'compliance_pass':True,'answer_contract':{'kind':'reviewed_text'},'knowledge_snapshot':knowledge,
            '_answer_context':context,'financial_metric_snapshot':metrics}
        token=next(c.value for c in self.browser.jar)
        receipt=self.g.approve_speech(self.w,result,token)
        self.assertEqual(self.g.resolve_speech(self.w,receipt,token),'测试核验答复')
        self.public.value['outbound_allowed']=False
        with self.assertRaises(BankError) as text:
            with self.g.response_guard(self.w,self.state['privacy_revision'],result['personal_revision'],knowledge,context):pass
        with self.assertRaises(BankError) as speech:self.g.resolve_speech(self.w,receipt,token)
        self.assertEqual(text.exception.code,speech.exception.code)
        self.assertEqual(text.exception.code,'STALE_KNOWLEDGE_EVIDENCE')

    def test_observer_refuses_unread_or_changed_evidence(self):
        observer=PublicEvidenceObserver(self.public)
        bare={'release_id':'release-fixture','citations':[{'citation_id':'citation-fixture','as_of':'2026-10-06'}]}
        with self.assertRaises(BankError):observer.snapshot(bare)
        observer.resolve_public_evidence('citation-fixture','release-fixture','2026-10-06')
        self.public.value['document_version_id']='v2'
        with self.assertRaises(BankError):observer.resolve_public_evidence('citation-fixture','release-fixture','2026-10-06')

    def service_fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        rules=self.g.service_negotiations.rules()
        path=Path(temp.name)/'benefit-rules.json'
        path.write_text(json.dumps(rules),encoding='utf-8')
        patcher=patch.object(self.g.service_negotiations,'rules_path',path)
        patcher.start();self.addCleanup(patcher.stop)
        tools=BankTools(self.g,self.w,uuid.uuid4().hex)
        tools._pin_service_rules(rules)
        return path,rules,tools.service_snapshot()

    def test_service_revision_blocks_confirmation_without_changing_cash(self):
        path,rules,snapshot=self.service_fixture()
        op=self.approved(service=snapshot);before=self.g.overview(self.w)['total_cash_minor']
        rules['offers'][0]['conditions']=['修订的电子账单条件']
        path.write_text(json.dumps(rules),encoding='utf-8')
        status,result,_=self.confirm(op)
        self.assertEqual((status,result['error']['code']),(409,'STALE_SERVICE_EVIDENCE'))
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'],before)

    def test_text_and_speech_recheck_the_same_service_revision(self):
        path,rules,snapshot=self.service_fixture()
        result={**self.state,'personal_revision':self.g.personal_data.revision(self.w),
            'reply':'演示服务条件已核验','compliance_pass':True,
            'answer_contract':{'kind':'reviewed_text'},'service_snapshot':snapshot}
        token=next(c.value for c in self.browser.jar)
        receipt=self.g.approve_speech(self.w,result,token)
        self.assertEqual(self.g.resolve_speech(self.w,receipt,token),result['reply'])
        rules['offers'][0]['conditions']=['本人尚未确认的不同条件']
        path.write_text(json.dumps(rules),encoding='utf-8')
        with self.assertRaises(BankError) as text:
            with self.g.response_guard(self.w,self.state['privacy_revision'],result['personal_revision'],
                                      service_snapshot=snapshot):pass
        with self.assertRaises(BankError) as speech:self.g.resolve_speech(self.w,receipt,token)
        self.assertEqual(text.exception.code,speech.exception.code)
        self.assertEqual(text.exception.code,'STALE_SERVICE_EVIDENCE')


if __name__=='__main__':unittest.main()
