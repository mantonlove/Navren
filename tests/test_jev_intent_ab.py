"""零云验收：原题来源、冻结、全启动计费、预算与真实超时。"""
import ast
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('intent_ab_test', ROOT/'scripts/run_jev_intent_ab.py')
h = importlib.util.module_from_spec(spec); sys.modules[spec.name] = h; spec.loader.exec_module(h)


class Response:
    def __init__(self, payload): self.raw = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, *args): return self.raw


class IntentABTests(unittest.TestCase):
    def synthetic_review(self, directory, files):
        """显式离线假收据，仅验证门本身；不作为任何真实审核/试验通过证据。"""
        base = Path(directory); base.mkdir(parents=True,exist_ok=True); review = base/'review'; review.mkdir()
        identifier, artifact = str(uuid.uuid4()), h.sha(b'offline-unit-artifact')
        entries = [{'label':'artifact-'+str(i)+Path(p).suffix, 'sha256':h.digest(p), 'redacted_lines':[],
            'content_with_line_numbers':'\n'.join('L'+str(j).zfill(5)+' '+line for j,line in enumerate(Path(p).read_text().splitlines(),1))}
            for i,p in enumerate(files,1)]
        payload = {'candidate_artifacts':entries,'verification_evidence':[],'unit_origin':'synthetic_no_API_no_trial_proof'}
        configuration = {'reviewers':{side:{'fallback':None,'primary':{'provider':'Aiping OpenAI-compatible API',
            'endpoint':h.AIPING,'requested_model':model}} for side,model in
            [('deepseek_flash','DeepSeek-V4.1-Flash'),('glm_5_3_flash','GLM-5.3-Flash')]}}
        pins = {'schema':'navren.jev-complex-review-pins/1','files_sha256':{str(Path(p).resolve()):h.digest(p) for p in files},
            'artifact_sha256':artifact,'expected_hash_payload':payload,'review_configuration':configuration}
        packet = {**payload,'review_id':identifier,'artifact_sha256':artifact}
        result = {'verdict':'PASS','review_id':identifier,'artifact_sha256':artifact,'review_configuration':configuration,
            'reviewers_required':list(configuration['reviewers']),'reviewers_completed':2,'reviews':{}}
        observations = {}
        usage = {'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}
        for side,declaration in configuration['reviewers'].items():
            model = declaration['primary']['requested_model']
            request_path = review/(side+'-actual-request.json'); native_path = review/(side+'-native-response.json')
            request = {'model':model,'messages':[{'role':'user','content':h.packed(packet)}]}; h.write(request_path,request)
            verdict = {'review_id':identifier,'artifact_sha256':artifact,'verdict':'PASS','coverage':'complete',
                'confidence':.8,'blocking_issues':[],'reviewed_scope':[e['label'] for e in entries]}
            native = {'endpoint':h.AIPING,'returned_model':model,'usage':usage,'actual_request_identity_verified':True,
                'review_id':identifier,'artifact_sha256':artifact,'actual_request_path':str(request_path),
                'actual_request_body_sha256':h.digest(request_path),'request_body_sha256':h.digest(request_path),
                'finish_reason':'stop','response_id':'unit-'+side,'assistant_content':h.packed(verdict)}
            h.write(native_path,native)
            metadata = {**declaration['primary'],'returned_model':model,'route':'primary','usage':usage,
                'finish_reason':'stop','response_id':native['response_id']}
            result['reviews'][side] = {'status':'COMPLETED','verdict':'PASS','review':{**verdict,'review_metadata':metadata}}
            observations[side] = {'native_path':str(native_path),'native_sha256':h.digest(native_path),
                'actual_request_path':str(request_path),'actual_request_sha256':h.digest(request_path),
                'usage':usage,'reused':False,'status':'COMPLETED',**{k:True for k in
                ('request_body_verified','native_envelope_bound','usage_known','completed_review_metadata_consistent')}}
        h.write(base/'pins.json',pins); h.write(review/'actual-packet.json',packet); h.write(review/'result.json',result)
        receipt = {'schema':'navren.jev-complex-review-process/1','phase':'native','verdict':'PASS',
            'actual_exit_code':0,'timed_out':False,'new_native_response_count':2,'expected_new_native_response_count':2,
            'redactions_applied':0,'actual_redactions':0,'review_id':identifier,'artifact_sha256':artifact,
            'pins_path':str(base/'pins.json'),'pins_sha256':h.digest(base/'pins.json'),
            'actual_packet_sha256':h.digest(review/'actual-packet.json'),'actual_result_sha256':h.digest(review/'result.json'),
            'native_observations':observations,'unit_origin':'synthetic_no_API_no_trial_proof',
            **{k:True for k in ('complete','process_reaped','process_group_empty_verified','inputs_unchanged',
                'exact_current_payload','identity_consistent','usage_known_for_all_required_sides',
                'new_native_usage_accounting_known','completed_review_metadata_consistent','exit_matches_original_result')}}
        h.write(base/'receipt.json',receipt)
        return base/'receipt.json'

    def test_positive_DEV_flag_preserves_all_original_20_case_bytes(self):
        original, extended = h.catalog(ROOT), h.catalog(ROOT,include_positive_dev=True)
        self.assertEqual(len(extended),22); self.assertEqual(h.packed(original),h.packed(extended[:20]))
        self.assertEqual([c['prompt'] for c in extended[20:]],['查测试产品风险','查看银行代扣'])
        self.assertEqual(extended[21]['mode'],'senior')
        self.assertTrue(all(c['development_only'] and c['gold_status'].startswith('NEW_PROPOSAL') for c in extended[20:]))
        self.assertEqual(extended[21]['source']['path'],'js/chat.js')
        self.assertIn('不要求额外查询完整商户合同',extended[21]['gold']['must_convey'][1])

    def test_positive_real_original_fixtures_and_snapshots_without_external_IO(self):
        sys.path[:0] = [str(ROOT),str(ROOT/'tests')]
        from agent_runtime.bank_tools import BankTools
        from agent_runtime.complex_agent import ComplexConversationAgent
        from test_native_conversation import ScriptedPrimary, Reviewer, call
        from test_rag_integration import answer
        oracle = h.load(ROOT/'tests/window_integration/semantic_oracle.py','unit_positive_original_oracle')
        class Labels:
            def __init__(self,cap,workflow): self.cap,self.workflow=cap,workflow
            def evaluate(self,state,questions,**kw):
                value = {key:{'value':'no'} for key in questions}
                value['cap_'+self.cap]={'value':'yes'};value['fit_'+self.workflow]={'value':'yes'}
                return value,{'request_sent':False,'outcome':'unit_fixed_labels_no_semantic_score',
                    'model_requested':'offline-unit','model_actual':'offline-unit','usage':{'input_tokens':0,'output_tokens':0}}
        def bank_reply(messages,schemas):
            data = next(json.loads(m['content']) for m in messages if m['role']=='tool'
                and 'mandates' in json.loads(m['content']).get('data',{}))
            text = '银行授权：'+'；'.join(x['merchant']+' '+x['status'] for x in data['data']['mandates'])+'。合同状态另行管理，当前证据未提供。'
            return call('answer_reviewed',{'text':text,'claims':[{'text':text,'evidence_ids':[data['evidence_id']]}],'draft_ids':[],'followups':[]})
        for case in h.positive_development_cases(ROOT):
            with self.subTest(case=case['id']),tempfile.TemporaryDirectory() as directory,h.offline_guard():
                g,w,_,original=h.fixture(oracle,case,directory)
                pre=h.extended_preflight(oracle,g,w,case,original)
                self.assertEqual(pre['status'],'passed');self.assertIsNone(pre['semantic_pass'])
                before=h.financial_snapshot(oracle,g,w)
                risk=case['id'].startswith('D01');actual=[]; invoke=BankTools.invoke
                def observed(tool,name,arguments):
                    out=invoke(tool,name,arguments);actual.append({'name':name,'arguments':deepcopy(arguments),'success':True,'raw_output':deepcopy(out)});return out
                agent=ComplexConversationAgent(primary=ScriptedPrimary(answer if risk else bank_reply),reviewer=Reviewer(),route_provider='native',
                    judge=Labels('knowledge_retrieval' if risk else 'subscriptions','public_product_terms_read' if risk else 'subscription_candidates_read'))
                with patch.object(BankTools,'invoke',observed):result=agent.process(g,w,case['prompt'],mode=case['mode'])
                after=h.financial_snapshot(oracle,g,w)
                self.assertEqual(before,after)
                row={'http_status':200,'technical_completed':not result.get('isFallback'), 'before':before,'after':after,
                    'result':result,'preflight':pre,'provider_calls':[],'model_tool_calls':[],'tool_invocations':actual}
                grade=h.automatic_grade(row,case)
                self.assertEqual(grade['status'],'necessary_conditions_passed_semantic_review_pending',grade['checks'])
                self.assertIsNone(grade['semantic_pass'])
                row['model_tool_calls']=[{'name':'withdraw_draft','arguments':{}}]
                self.assertEqual(h.automatic_grade(row,case)['status'],'necessary_condition_failure')
                row['model_tool_calls']=[]
                if risk:
                    row['model_tool_calls']=[{'name':'customer_records','arguments':{}}]
                    self.assertEqual(h.automatic_grade(row,case)['status'],'necessary_condition_failure')
                    row['model_tool_calls']=[];row['result']['knowledge_snapshot']['citations']=[]
                else:
                    next(t for t in actual if t['name']=='business_records')['raw_output']['data']['mandates'][0]['status']='fabricated'
                self.assertEqual(h.automatic_grade(row,case)['status'],'necessary_condition_failure')
                with g.bank.transaction() as db:db.execute('UPDATE mandates SET status=? WHERE workspace_id=?',('closed',w))
                self.assertNotEqual(h.financial_snapshot(oracle,g,w),before)

    def test_benchmark_voice_explicit_empty_key_does_not_read_keychain(self):
        import voice_gateway
        with h.offline_guard(),patch.object(voice_gateway,'load_key',side_effect=AssertionError('voice key forbidden')) as key:
            voice=h.benchmark_voice()
            self.assertEqual(voice.key,'');self.assertFalse(voice.allow_cloud_asr);key.assert_not_called()
        from server import create_server
        oracle=h.load(ROOT/'tests/window_integration/semantic_oracle.py','unit_no_voice_original_oracle')
        with tempfile.TemporaryDirectory() as directory,oracle._isolated_environment(Path(directory)),h.offline_guard(), \
             patch.object(voice_gateway,'load_key',side_effect=AssertionError('voice key forbidden')) as key:
            voice=h.benchmark_voice()
            catalog=oracle._NoNetworkCatalog()
            catalog.stop=lambda **kw: True  # 仅测试服务器清理，不改变任何业务目录字段。
            server=create_server(port=0,database=str(Path(directory)/'server.sqlite3'),voice=voice,conversation_agent=object(),
                public_catalog=catalog,public_sync_enabled=False)
            try:self.assertIs(server.voice,voice);key.assert_not_called()
            finally:server.server_close()
        worker=next(n for n in ast.walk(ast.parse((ROOT/'scripts/run_jev_intent_ab.py').read_text())) if isinstance(n,ast.FunctionDef) and n.name=='worker')
        call_node=next(n for n in ast.walk(worker) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='create_server')
        self.assertTrue(any(k.arg=='voice' and isinstance(k.value,ast.Call) and k.value.func.id=='benchmark_voice' for k in call_node.keywords))

    def test_real_review_gate_is_required_before_every_paid_campaign_mutation(self):
        with tempfile.TemporaryDirectory() as d,patch.object(h,'verify',return_value={'include_positive_dev':False}),patch.object(h,'supervise') as dispatch:
            campaign=Path(d)/'campaign.json'
            with self.assertRaisesRegex(ValueError,'dual_review'):h.run(Path(d),expected_sha='unit',campaign=campaign,configuration=1)
            self.assertFalse(campaign.exists());dispatch.assert_not_called()

    def test_synthetic_review_fixture_checks_real_gate_predicates_and_tamper(self):
        scenarios=('none','incomplete','REVISE','unreaped','group','dry','unknown_usage','wrong_sha','confidence','missing_scope','wrong_model','blocker','native_tamper','missing_protocol_text')
        for scenario in scenarios:
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory() as d:
                file=Path(d)/'unit.py';file.write_text('# offline fixture\n')
                path=self.synthetic_review(Path(d)/'native',[file]);receipt=h.read(path)
                if scenario=='incomplete':receipt['complete']=False
                elif scenario=='REVISE':receipt['verdict']='REVISE'
                elif scenario=='unreaped':receipt['process_reaped']=False
                elif scenario=='group':receipt['process_group_empty_verified']=False
                elif scenario=='dry':receipt['phase']='dry'
                elif scenario=='unknown_usage':receipt['usage_known_for_all_required_sides']=False
                elif scenario in ('confidence','missing_scope','wrong_model','blocker'):
                    result_path=path.parent/'review/result.json';result=h.read(result_path);body=result['reviews']['deepseek_flash']['review']
                    if scenario=='confidence':body['confidence']=.74
                    elif scenario=='missing_scope':body['reviewed_scope']=[]
                    elif scenario=='wrong_model':body['review_metadata']['returned_model']='other'
                    else:body['blocking_issues']=[{'severity':'major'}]
                    h.write(result_path,result);receipt['actual_result_sha256']=h.digest(result_path)
                elif scenario=='native_tamper':Path(receipt['native_observations']['deepseek_flash']['native_path']).write_text('{}')
                elif scenario=='missing_protocol_text':
                    pins_path=Path(receipt['pins_path']);pins=h.read(pins_path)
                    pins['expected_hash_payload']['candidate_artifacts'][0]['content_with_line_numbers']='L00001 clipped'
                    packet_path=path.parent/'review/actual-packet.json';packet={**pins['expected_hash_payload'],'review_id':receipt['review_id'],'artifact_sha256':receipt['artifact_sha256']}
                    h.write(pins_path,pins);h.write(packet_path,packet);receipt['pins_sha256']=h.digest(pins_path);receipt['actual_packet_sha256']=h.digest(packet_path)
                h.write(path,receipt);wanted=h.digest(path) if scenario!='wrong_sha' else '0'*64
                if scenario=='none':
                    accepted=h.validate_native_review(path,wanted);self.assertEqual(len(accepted['fully_submitted_files_sha256']),1)
                elif scenario=='missing_protocol_text':
                    accepted=h.validate_native_review(path,wanted);self.assertEqual(accepted['fully_submitted_files_sha256'],{})
                else:
                    with self.assertRaises((ValueError,KeyError)):h.validate_native_review(path,wanted)

    def test_review_union_requires_protocol_gold_split_and_exact_current_source(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'freeze';out.mkdir();source=out/'source';source.mkdir();file=source/'unit.py';file.write_text('# shared runtime\n')
            protocol={'source_sha256':{'unit.py':h.digest(file)}}
            h.write(out/'protocol.json',protocol);h.write(out/'cases.json',{'cases':[]});h.write(out/'split.json',{'development':[]})
            first=self.synthetic_review(Path(d)/'runtime-native',[file]);second=self.synthetic_review(Path(d)/'protocol-native',[out/f for f in ('protocol.json','cases.json','split.json')])
            with patch.object(h,'required_review_sources',return_value={'unit.py'}):
                with self.assertRaisesRegex(ValueError,'full_protocol_review_missing'):h.validate_review_admission(out,protocol,[first],[h.digest(first)])
                accepted=h.validate_review_admission(out,protocol,[first,second],[h.digest(first),h.digest(second)])
                self.assertEqual(accepted['status'],'REAL_DUAL_REVIEW_UNION_PASS');self.assertEqual(len(accepted['receipts']),2)
                changed=deepcopy(protocol);changed['source_sha256']['unit.py']='0'*64
                with self.assertRaisesRegex(ValueError,'review_source_drift'):h.validate_review_admission(out,changed,[first,second],[h.digest(first),h.digest(second)])

    def test_existing_prompts_traceable_and_original_semantic_gold_unchanged(self):
        cases = h.catalog(ROOT)
        self.assertEqual(len(cases), 20)
        originals = h.read(ROOT/'evaluation/window_integration/semantic_cases.json')['cases']
        self.assertEqual([c['gold'] for c in cases[:10]], originals)
        self.assertEqual(cases[14]['prompt'], '只计算假设的123.45元，不读取个人账户。')
        self.assertTrue(all(c['origin'].startswith('synthetic_engineering') for c in cases))
        self.assertLess(len({c['family'] for c in cases}), len(cases))
        self.assertEqual(cases[0]['family'], cases[2]['family'])

    def test_jsonl_sources_are_frozen_and_output_trees_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'tests').mkdir(); (root/'tests/cases.jsonl').write_text('{}')
            (root/'outputs').mkdir(); (root/'outputs/old.py').write_text('x')
            self.assertEqual([p.relative_to(root).as_posix() for p in h.source_files(root)], ['tests/cases.jsonl'])

    def test_actual_ids_do_not_attach_success_to_prior_rejected_same_name(self):
        row = {'model_tool_calls': [
                {'id':'rejected','name':'budget','arguments':{'account_id':'wrong'}},
                {'id':'actual','name':'budget','arguments':{'account_id':'anonymous'}}],
               'tool_outputs':[], 'tool_invocations':[
                {'call_id':'actual','name':'budget','arguments':{'account_id':'acct-main'},'success':True,'raw_output':{'data':{'investable_minor':100}}},
                {'call_id':'workflow-1','name':'cashflow','arguments':{'days':60},'success':True,'raw_output':{'data':{'days':60}}}]}
        h.bind_actual_invocations(row)
        self.assertEqual([r['call_id'] for r in row['tool_outputs']], ['actual','workflow-1'])
        self.assertEqual(row['model_tool_calls'][0]['arguments']['account_id'], 'wrong')
        self.assertEqual(row['model_tool_calls'][1]['model_arguments']['account_id'], 'anonymous')
        self.assertEqual(row['model_tool_calls'][1]['arguments']['account_id'], 'acct-main')
        self.assertEqual(row['model_tool_calls'][2]['origin'], 'actual_host_tool_event')

    def test_current_private_tool_names_and_financial_prefix_cannot_escape(self):
        case = next(c for c in h.catalog(ROOT) if c['id'] == 'X01')
        names = set(case['private_tool_contract']['private_data_tools'])
        self.assertTrue({'user_memory','customer_records','business_records','annual_statement','statement_details'}.issubset(names))
        state = {'cash':100}
        for name in ['user_memory','annual_statement','statement_details','financial_future_private_read']:
            row = {'http_status':200,'technical_completed':True,'before':state,'after':state,'provider_calls':[],
                'result':{'reply':'123.45元','bank_actions':[]},
                'tool_invocations':[{'name':'money_calculation','arguments':{'values_minor':[12345]},'success':True,
                    'raw_output':{'data':{'result_minor':12345,'currency':'CNY','source':'user_input_scenario'}}}],
                'model_tool_calls':[{'name':name,'arguments':{}}]}
            self.assertEqual(h.automatic_grade(row,case)['status'], 'necessary_condition_failure')

    def test_calendar_widget_must_bind_actual_read_not_arguments_only(self):
        case = next(c for c in h.catalog(ROOT) if c['id'] == 'P02'); state = {'cash':100}
        data = {'view_id':'actual-view','range':{'from':'2026-11-01','to':'2026-11-30'},'coverage':'unknown','source_scope':{'statement_source':'simulation'}}
        row = {'http_status':200,'technical_completed':True,'before':state,'after':state,'provider_calls':[],
            'result':{'reply':'下月日历','widgets':[{'type':'financial_calendar','view_id':'wrong-view','range':data['range'],'source_scope':data['source_scope'],'snapshot':{'coverage':'unknown'}}]},
            'tool_invocations':[{'name':'calendar_query','arguments':{'period':'next_month'},'success':True,'raw_output':{'data':data}}]}
        self.assertEqual(h.automatic_grade(row,case)['status'], 'necessary_condition_failure')
        row['result']['widgets'][0]['view_id'] = 'actual-view'
        self.assertEqual(h.automatic_grade(row,case)['status'], 'necessary_conditions_passed_semantic_review_pending')

    def extension_necessary_control(self, case):
        state = {'cash':100}
        row = {'http_status':200,'technical_completed':True,'before':state,'after':deepcopy(state),
            'provider_calls':[], 'result':{'reply':'仅用于零云判分单元测试','bank_actions':[]},
            'tool_invocations':[], 'model_tool_calls':[]}
        def tool(name, data, arguments=None):
            row['tool_invocations'].append({'name':name,'arguments':arguments or {},'success':True,'raw_output':{'data':data}})
        if case['id'] == 'P01': tool('account_summary',{})
        elif case['id'] == 'P02':
            data = {'view_id':'unit-view','range':{'from':'2026-11-01','to':'2026-11-30'},'coverage':'unknown','source_scope':{'statement_source':'simulation'}}
            tool('calendar_query',data,{'period':'next_month'})
            row['result']['widgets'] = [{'type':'financial_calendar','view_id':'unit-view','range':data['range'],'source_scope':data['source_scope'],'snapshot':{'coverage':'unknown'}}]
        elif case['id'] == 'P08': tool('service_offer_preview',{'conditions':{'salary_deposit_intent':True,'investment_purchase_refused':True}})
        elif case['id'] == 'X01': tool('money_calculation',{'result_minor':12345,'currency':'CNY','source':'user_input_scenario'},{'values_minor':[12345]})
        elif case['id'] == 'X02':
            data = {'applications':[{'application_id':'unit-seeded','status':'submitted_simulation_pending_eligibility'}]}
            tool('service_application_status',data)
            row['preflight'] = {'facts':{'service_application_status':{'data':deepcopy(data)}}}
        return row

    def test_all_extensions_reject_every_frozen_mutation_even_failed_or_model_only(self):
        cases = [c for c in h.catalog(ROOT) if c['oracle'] == 'extended_readonly']
        tools = h.private_tool_contract(ROOT)['mutation_tools']
        self.assertIn('withdraw_draft',tools)
        for case in cases:
            base = self.extension_necessary_control(case)
            self.assertEqual(h.automatic_grade(base,case)['status'],'necessary_conditions_passed_semantic_review_pending')
            for name in tools:
                for channel in ('model_tool_calls','tool_invocations'):
                    with self.subTest(case=case['id'],name=name,channel=channel):
                        row = deepcopy(base)
                        row[channel].append({'name':name,'arguments':{'operation_id':'unit'},'success':False,'error_type':'TOOL_SCOPE_REQUIRED'})
                        result = h.automatic_grade(row,case)
                        failed = next(c for c in result['checks'] if c['label']=='no_financial_calendar_profile_mutation_attempt')
                        self.assertFalse(failed['passed']); self.assertEqual(result['status'],'necessary_condition_failure')
                        self.assertIsNone(result['semantic_pass'])

    def test_mutation_check_consumes_frozen_contract_instead_of_literal_subset(self):
        function = next(n for n in ast.walk(ast.parse((ROOT/'scripts/run_jev_intent_ab.py').read_text())) if isinstance(n,ast.FunctionDef) and n.name=='automatic_grade')
        self.assertTrue(any(isinstance(n,ast.Subscript) and isinstance(n.slice,ast.Constant) and n.slice.value=='mutation_tools' for n in ast.walk(function)))
        case = deepcopy(next(c for c in h.catalog(ROOT) if c['id']=='X01'))
        case['private_tool_contract']['mutation_tools'].append('unit_future_registered_write')
        row = self.extension_necessary_control(case)
        row['model_tool_calls'].append({'name':'unit_future_registered_write','arguments':{},'success':False})
        result = h.automatic_grade(row,case)
        self.assertEqual(result['status'],'necessary_condition_failure'); self.assertIsNone(result['semantic_pass'])

    def test_pair_order_reverses_without_dropping_failed_candidate_starts(self):
        plan = h.paired_plan(h.catalog(ROOT), repetitions=2)
        self.assertEqual(len(plan), 80)
        for c in h.catalog(ROOT):
            first = [p['variant'] for p in plan if p['case_id'] == c['id'] and p['repetition'] == 1]
            second = [p['variant'] for p in plan if p['case_id'] == c['id'] and p['repetition'] == 2]
            self.assertEqual(second, first[::-1])

    def test_three_arm_anchor_pair_adjacency_and_balance(self):
        cases = h.catalog(ROOT)[:4]
        plan = h.paired_plan(cases, repetitions=2, arms=['off','native','jev'], off_order='balanced')
        self.assertEqual(len(plan), 24)
        for case in cases:
            first = [p['variant'] for p in plan if p['case_id'] == case['id'] and p['repetition'] == 1]
            second = [p['variant'] for p in plan if p['case_id'] == case['id'] and p['repetition'] == 2]
            self.assertEqual(second, first[::-1])
            self.assertIn(first.index('off'), (0,2))
        diagnostic = h.paired_plan(cases, arms=['off','native','jev'])
        self.assertTrue(all(diagnostic[index]['variant']=='off' for index in (2,5,8,11)))

    def test_shared_necessary_failure_stops_only_when_both_routing_arms_known_failed(self):
        native = {'case_id':'S01','repetition':1,'variant':'native','technical_completed':True,
                  'independent_checks':{'status':'necessary_condition_failure'}}
        jev = {'case_id':'S01','repetition':1,'variant':'jev','technical_completed':False}
        self.assertIsNone(h.shared_failure_for([native], 'S01', 1))
        failed = h.shared_failure_for([native,jev], 'S01', 1)
        self.assertEqual(failed['off_anchor_status'], 'not_started')
        jev['technical_completed'] = True
        self.assertIsNone(h.shared_failure_for([native,jev], 'S01', 1))

    def test_source_and_external_protocol_pins_reject_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory); (out/'source').mkdir(); (out/'source/core.py').write_text('old')
            h.write(out/'cases.json', {'cases':[]}); h.write(out/'split.json',{})
            protocol = {'case_sha256':h.digest(out/'cases.json'), 'split_sha256':h.digest(out/'split.json'),
                'source_sha256':{'core.py':h.digest(out/'source/core.py')}}
            h.write(out/'protocol.json',protocol); external = h.digest(out/'protocol.json')
            h.write(out/'freeze-manifest.json',{'protocol_sha256':external})
            (out/'source/core.py').write_text('new')
            with self.assertRaisesRegex(ValueError,'frozen_source_changed'): h.verify(out,external)
            h.write(out/'protocol.json', {'modified':True})
            with self.assertRaisesRegex(ValueError,'protocol_sha'): h.verify(out,external)

    def test_native_route_is_primary_price_not_reviewer_price(self):
        calls = [{'stage': 'native_route', 'request_sent': True,
                  'usage': {'prompt_tokens': 1000000, 'completion_tokens': 1000000, 'total_tokens': 2000000}}]
        cost = h.account_calls(calls)
        self.assertTrue(cost['known']); self.assertEqual(cost['estimated_upper_cny'], 3.)
        calls.append({'stage': 'jev_route', 'request_sent': True, 'usage': None})
        cost = h.account_calls(calls)
        self.assertFalse(cost['known']); self.assertIsNone(cost['tokens']); self.assertIsNone(cost['estimated_upper_cny'])
        self.assertEqual(cost['tokens_lower_bound'], 2000000)

    def test_typed_usage_total_and_nonfinite_json_rejected(self):
        for usage in ({'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2},
                      {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 9}):
            self.assertFalse(h.account_calls([{'stage':'primary', 'request_sent':True, 'usage':usage}])['known'])
        for value in ('{"x": 1, "x": 2}', '{"x": NaN}'):
            with self.assertRaises(ValueError): h.parse(value)

    def test_recorder_persists_unknown_dispatch_before_transport_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            def broken(request, **kwargs):
                seen.append(h.read(Path(directory)/'provider-calls.json'))
                raise TimeoutError('fake zero-cloud timeout')
            r = h.Recorder(directory, transport=broken)
            request = urllib.request.Request(h.AIPING, data=json.dumps({'model':h.MODELS['primary'], 'max_tokens':4}).encode())
            with self.assertRaises(TimeoutError): r.opener('primary')(request)
            self.assertTrue(seen[0][0]['request_sent']); self.assertIsNone(seen[0][0]['usage'])
            with self.assertRaisesRegex(RuntimeError, 'previous_usage_unknown'): r.opener('primary')(request)
            self.assertEqual(len(r.calls), 1)

    def test_identity_failure_preserves_known_bill_without_allowing_success(self):
        with tempfile.TemporaryDirectory() as directory:
            r = h.Recorder(directory, transport=lambda *a, **k: Response({'model':'wrong', 'usage':{'prompt_tokens':5,'completion_tokens':2,'total_tokens':7}, 'choices':[]}))
            request = urllib.request.Request(h.AIPING, data=json.dumps({'model':h.MODELS['primary'], 'max_tokens':4}).encode())
            with self.assertRaisesRegex(ValueError, 'identity'):
                with r.opener('primary')(request) as response: response.read()
            self.assertTrue(h.account_calls(r.calls)['known']); self.assertEqual(h.account_calls(r.calls)['tokens'], 7)
            self.assertFalse(r.calls[0]['identity_verified'])

    def test_route_rejection_happens_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            r = h.Recorder(directory, transport=lambda *a, **k: self.fail('must not dispatch'))
            request = urllib.request.Request('https://api.deepseek.com/chat/completions', data=b'{"model":"DeepSeek-V4-Flash"}')
            with self.assertRaises(ValueError): r.opener('primary')(request)
            self.assertEqual(r.calls, [])

    def test_campaign_five_configuration_and_prestart_budget(self):
        ledger = {'tokens_lower_bound': 3999999, 'estimated_upper_cny_lower_bound': 0., 'configurations': []}
        with self.assertRaisesRegex(ValueError, 'budget'): h.trial_reservation(ledger)
        ledger['tokens_lower_bound'] = 0
        for i in range(1,6): h.admit_configuration(ledger, str(i), i)
        with self.assertRaises(ValueError): h.admit_configuration(ledger, '6', 6)
        ledger['usage_unknown'] = True
        with self.assertRaisesRegex(ValueError, 'unknown'): h.admit_configuration(ledger, '6', 6)
        ledger['usage_unknown'] = False; ledger['starts'] = [{'status':'STARTED'}]
        with self.assertRaisesRegex(ValueError, 'unknown'): h.admit_configuration(ledger, '6', 6)
        ledger['starts'] = []; ledger['estimated_upper_cny_lower_bound'] = 19.9
        with self.assertRaisesRegex(ValueError, 'budget'): h.trial_reservation(ledger)

    def configuration_protocol(self):
        return {'models':deepcopy(h.MODELS), 'routes':{'primary':h.AIPING,'reviewer':h.AIPING,'native_route':h.AIPING,'jev_route':h.JEV},
            'thresholds':{'positive':.9,'negative':.1,'status':'uncalibrated_development'},
            'plan':[{'case_id':'X01','variant':'jev','repetition':1}], 'split_sha256':'unit-split',
            'limits':deepcopy(h.LIMITS),'rates_upper_cny_per_million_input_output':deepcopy(h.RATES)}

    def test_configuration_contract_pins_questions_models_gold_and_not_shared_executor(self):
        cases = h.catalog(ROOT); protocol = self.configuration_protocol()
        original = h.configuration_contract(ROOT,cases,protocol)
        changed = deepcopy(protocol); changed['thresholds']['positive'] = .95
        self.assertNotEqual(h.configuration_contract(ROOT,cases,changed),original)
        changed = deepcopy(protocol); changed['models']['jev_route'] = 'unit-other-model'
        with self.assertRaisesRegex(ValueError,'provider_mismatch'): h.configuration_contract(ROOT,cases,changed)
        changed_cases = deepcopy(cases); changed_cases[0]['gold']['unit_changed'] = True
        self.assertNotEqual(h.configuration_contract(ROOT,changed_cases,protocol),original)
        changed_cases = deepcopy(cases); changed_cases[0]['prompt'] += ' '
        self.assertNotEqual(h.configuration_contract(ROOT,changed_cases,protocol),original)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for relative in ('agent_runtime/intent_routing.py','agent_runtime/workflow_registry.py','agent_runtime/skills.py',
                             'agent_runtime/jev_client.py','agent_runtime/bank_tools.py','agent_runtime/financial_mcp_client.py',
                             'agent_runtime/complex_agent.py'):
                target = source/relative; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes((ROOT/relative).read_bytes())
            for file in (ROOT/'skills').glob('*/SKILL.md'):
                target = source/file.relative_to(ROOT); target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(file.read_bytes())
            self.assertEqual(h.configuration_contract(source,cases,protocol),original)
            (source/'agent_runtime/complex_agent.py').write_text('# unit-only shared freshness repair\n')
            self.assertEqual(h.configuration_contract(source,cases,protocol),original)
            workflow = source/'agent_runtime/workflow_registry.py'
            workflow.write_text(workflow.read_text().replace('def _slots(', 'def _unit_repaired_slots('))
            self.assertEqual(h.configuration_contract(source,cases,protocol),original)
            route = source/'agent_runtime/intent_routing.py'
            route.write_text(route.read_text().replace('本轮是否要求任何办理', '本轮是否要求修改后的任何办理'))
            self.assertNotEqual(h.configuration_contract(source,cases,protocol),original)

    def settled_failure_ledger(self, directory, *, stop='native_jev_shared_task_failure_repair_before_next_start', technical=False):
        old = Path(directory)/'old'; old.mkdir()
        contract = h.configuration_contract(ROOT,h.catalog(ROOT),self.configuration_protocol())
        exit_status = {'reaped':True,'process_group_still_exists':False,'exit_code':0}
        report = {'protocol_sha256':'old-protocol','accounting_complete':True,'complete':not bool(stop),
            'records':[{'status':'FAILED' if technical else 'FINISHED','technical_completed':not technical,
                        'provider_calls':[],'exit':exit_status}]}
        if stop: report['stop_reason'] = stop
        h.write(old/'results.json',report)
        ledger = {'usage_unknown':False,'tokens_lower_bound':0,'estimated_upper_cny_lower_bound':0.,
            'starts':[{'status':'FAILED','configuration':1,'protocol_sha256':'old-protocol'}],
            'configurations':[{'number':1,'protocol_sha256':'old-protocol','attempts':[{
                'number':1,'status':'SETTLED','accounting_complete':True,'protocol_sha256':'old-protocol',
                'out':str(old),'configuration_contract':contract,'results_sha256':h.digest(old/'results.json')}]}]}
        return ledger, contract, report, old

    def test_same_configuration_repair_retains_number_original_failure_and_attempt_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, contract, report, old = self.settled_failure_ledger(directory)
            starts = deepcopy(ledger['starts']); original = deepcopy(ledger['configurations'][0]['attempts'][0])
            with patch.object(h,'verify',return_value={'configuration_contract':contract}) as verify:
                attempt = h.admit_configuration(ledger,'fresh-protocol',1,out=Path(directory)/'fresh',contract=contract,
                    repair_of='old-protocol',repair_reason='共同 Agent 的事实绑定修复')
            verify.assert_called_once_with(old,'old-protocol')
            self.assertEqual(len(ledger['configurations']),1); self.assertEqual(attempt['number'],2)
            self.assertEqual(ledger['starts'],starts); self.assertEqual(ledger['configurations'][0]['attempts'][0],original)
            self.assertEqual(attempt['same_configuration_repair_of'],'old-protocol')
            self.assertEqual(attempt['original_protocol_sha256'],'old-protocol')
            self.assertEqual(h.read(old/'results.json'),report)
            with self.assertRaisesRegex(ValueError,'unsettled'): h.admit_configuration(ledger,'next',2)

    def test_same_configuration_repair_accepts_settled_complete_technical_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, contract, _, _ = self.settled_failure_ledger(directory,stop=None,technical=True)
            with patch.object(h,'verify',return_value={'configuration_contract':contract}):
                attempt = h.admit_configuration(ledger,'fresh',1,out=Path(directory)/'fresh',contract=contract,
                    repair_of='old-protocol',repair_reason='共同 executor 技术修复')
            self.assertEqual(attempt['number'],2); self.assertEqual(len(ledger['configurations']),1)

    def test_settled_repair_at_fifth_configuration_does_not_consume_a_sixth(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, contract, _, _ = self.settled_failure_ledger(directory)
            last = ledger['configurations'][0]; last['number']=5
            ledger['configurations'] = [{'number':i,'attempts':[]} for i in range(1,5)]+[last]
            with patch.object(h,'verify',return_value={'configuration_contract':contract}):
                attempt = h.admit_configuration(ledger,'fresh',5,out=Path(directory)/'fresh',contract=contract,
                    repair_of='old-protocol',repair_reason='第五配置下的共同执行器修复')
            self.assertEqual(len(ledger['configurations']),5); self.assertEqual(attempt['number'],2)
            attempt['status']='SETTLED'
            with self.assertRaisesRegex(ValueError,'exceeds_five'): h.admit_configuration(ledger,'sixth',6)

    def test_same_configuration_repair_rejects_unknown_unsettled_changed_or_successful_attempts(self):
        scenarios = ('empty-reason','wrong-parent','same-freeze','wrong-number','changed-prompt-gold',
            'changed-threshold','unknown-ledger','started-ledger','unsettled-attempt','changed-results',
            'unknown-call','wrong-identity','unreaped','residual-group','successful','budget-stop')
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                ledger, contract, report, old = self.settled_failure_ledger(directory)
                kwargs = {'out':Path(directory)/'fresh','contract':deepcopy(contract),'repair_of':'old-protocol','repair_reason':'共同修复'}
                number, new_sha = 1, 'fresh'
                if scenario=='empty-reason': kwargs['repair_reason']='  '
                elif scenario=='wrong-parent': kwargs['repair_of']='other'
                elif scenario=='same-freeze': new_sha='old-protocol'
                elif scenario=='wrong-number': number=2
                elif scenario=='changed-prompt-gold': kwargs['contract']['cases'][0]['gold_sha256']='changed'
                elif scenario=='changed-threshold': kwargs['contract']['thresholds']['positive']=.99
                elif scenario=='unknown-ledger': ledger['usage_unknown']=True
                elif scenario=='started-ledger': ledger['starts'][0]['status']='STARTED'
                elif scenario=='unsettled-attempt': ledger['configurations'][0]['attempts'][0]['status']='STARTED'
                elif scenario=='changed-results': report['unit_tampered']=True
                elif scenario=='unknown-call': report['records'][0]['provider_calls']=[{'stage':'primary','request_sent':True,'usage':None}]
                elif scenario=='wrong-identity': report['records'][0]['provider_calls']=[{'stage':'primary','request_sent':True,'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2},'identity_verified':False}]
                elif scenario=='unreaped': report['records'][0]['exit']['reaped']=False
                elif scenario=='residual-group': report['records'][0]['exit']['process_group_still_exists']=True
                elif scenario=='successful': report.pop('stop_reason'); report['complete']=True
                elif scenario=='budget-stop': report['stop_reason']='campaign_budget_admission_rejected'
                if scenario in ('changed-results','unknown-call','wrong-identity','unreaped','residual-group','successful','budget-stop'):
                    h.write(old/'results.json',report)
                    if scenario!='changed-results': ledger['configurations'][0]['attempts'][0]['results_sha256']=h.digest(old/'results.json')
                before = deepcopy(ledger)
                with patch.object(h,'verify',return_value={'configuration_contract':contract}), self.assertRaises(ValueError):
                    h.admit_configuration(ledger,new_sha,number,**kwargs)
                self.assertEqual(ledger,before)

    def test_zero_cloud_run_repair_keeps_both_failed_starts_and_campaign_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); old, fresh = base/'old', base/'fresh'; old.mkdir(); fresh.mkdir()
            protocol = self.configuration_protocol()
            protocol['plan'] = [{'case_id':'X01','variant':arm,'repetition':1} for arm in ('native','jev','off')]
            protocol['configuration_contract'] = h.configuration_contract(ROOT,h.catalog(ROOT),protocol)
            for out, expected in ((old,'old-protocol'),(fresh,'fresh-protocol')):
                h.write(out/'preflight.json',{'protocol_sha256':expected,'complete':True,'passed':True})
            def zero_cloud_worker(command, trial, seconds):
                out = Path(command[command.index('--out')+1]); index = int(command[-1]); planned = protocol['plan'][index]
                row = {**planned,'technical_completed':True,'http_elapsed_ms':10,'provider_calls':[],
                    'independent_checks':{'status':'necessary_condition_failure' if out==old else 'necessary_conditions_passed_semantic_review_pending'},
                    'full_business_semantic_verdict':'PENDING_INDEPENDENT_REVIEW'}
                h.write(trial/'record.json',row)
                # Synthetic usage records test nonzero carry-over; no HTTP occurs.
                h.write(trial/'provider-calls.json',[{'stage':'primary','request_sent':True,
                    'usage':{'prompt_tokens':50,'completion_tokens':10,'total_tokens':60},
                    'identity_verified':True,'origin':'offline_unit_fake_no_http'}])
                return {'exit_code':0,'reaped':True,'process_group_still_exists':False,'timed_out':False,'elapsed_ms':10}
            campaign = base/'campaign.json'
            synthetic_gate={'status':'OFFLINE_UNIT_GATE_FIXTURE_NO_TRUE_REVIEW_NO_API','origin':'explicit mocked unit control'}
            with patch.object(h,'verify',return_value=protocol), patch.object(h,'validate_review_admission',return_value=synthetic_gate), \
                 patch.object(h,'supervise',side_effect=zero_cloud_worker), patch('builtins.print'):
                failed = h.run(old,expected_sha='old-protocol',campaign=campaign,configuration=1)
                original_sha = h.digest(old/'results.json'); original_records = deepcopy(failed['records'])
                repaired = h.run(fresh,expected_sha='fresh-protocol',campaign=campaign,configuration=1,
                    same_configuration_repair_of='old-protocol',repair_reason='零云单元控制中的共同执行器修复')
            ledger = h.read(campaign)
            self.assertEqual(failed['stop_reason'],'native_jev_shared_task_failure_repair_before_next_start')
            self.assertEqual(len(failed['records']),2); self.assertTrue(repaired['complete'])
            self.assertEqual(len(ledger['configurations']),1); self.assertEqual(len(ledger['configurations'][0]['attempts']),2)
            self.assertTrue(all(a['status']=='SETTLED' for a in ledger['configurations'][0]['attempts']))
            self.assertEqual(len(ledger['starts']),5); self.assertEqual([s['configuration_attempt'] for s in ledger['starts']],[1,1,2,2,2])
            self.assertEqual(h.digest(old/'results.json'),original_sha); self.assertEqual(h.read(old/'results.json')['records'],original_records)
            self.assertEqual(ledger['tokens_lower_bound'],300)
            self.assertAlmostEqual(ledger['estimated_upper_cny_lower_bound'],5*(50+10*2)/1000000)
            self.assertFalse(ledger['usage_unknown']); self.assertEqual(repaired['full_business_result'],'PENDING_INDEPENDENT_REVIEW')

    def test_real_process_timeout_reaps_and_keeps_exit_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            result = h.supervise([sys.executable,'-c','import time; time.sleep(10)'], directory, seconds=.12)
            self.assertTrue(result['timed_out']); self.assertTrue(result['reaped']); self.assertFalse(result['process_group_still_exists'])
            self.assertNotEqual(result['exit_code'], 0)
            self.assertTrue((Path(directory)/'exit.json').exists())
            self.assertEqual(result['total_deadline_seconds'], .12)
            self.assertEqual(result['main_wait_seconds'], .06)
            self.assertLess(result['elapsed_ms'], 250)  # Scheduling tolerance, not an extra cleanup budget.

    def test_real_natural_exit_uses_same_105_second_cleanup_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            result = h.supervise([sys.executable,'-c','pass'],directory,seconds=105)
            self.assertEqual(result['exit_code'],0); self.assertEqual(result['termination'],'natural')
            self.assertTrue(result['reaped']); self.assertFalse(result['process_group_still_exists'])
            self.assertEqual(result['main_wait_seconds'],100.5); self.assertEqual(result['cleanup_reserve_seconds'],4.5)
            self.assertFalse(result['deadline_exceeded'])
            self.assertEqual(h.read(Path(directory)/'exit.json'), result)

    def test_real_sigterm_ignored_process_is_killed_within_total_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            result = h.supervise([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(10)'],directory,seconds=.6)
            self.assertEqual(result['termination'],'timeout_kill'); self.assertTrue(result['reaped'])
            self.assertFalse(result['process_group_still_exists']); self.assertLess(result['elapsed_ms'],700)

    def test_failed_cleanup_cannot_publish_worker_technical_success(self):
        success = {'exit_code':0,'termination':'natural','timed_out':False,'reaped':True,
            'process_group_still_exists':False,'deadline_exceeded':False,'elapsed_ms':15}
        for override in ({'reaped':False},{'process_group_still_exists':True},{'deadline_exceeded':True},{'supervisor_failure':'OSError'}):
            row = {'technical_completed':True,'http_elapsed_ms':10}
            h.apply_supervisor_status(row,{**success,**override})
            self.assertFalse(row['technical_completed']); self.assertEqual(row['status'],'FAILED')
            self.assertIsNone(row['api_latency_ms']); self.assertEqual(row['penalized_api_latency_ms'],105000)
        row = {'technical_completed':True,'http_elapsed_ms':10}; h.apply_supervisor_status(row,success)
        self.assertTrue(row['technical_completed']); self.assertEqual(row['status'],'FINISHED')

    def test_every_supervisor_wait_uses_remaining_absolute_deadline(self):
        function = next(n for n in ast.walk(ast.parse((ROOT/'scripts/run_jev_intent_ab.py').read_text())) if isinstance(n,ast.FunctionDef) and n.name=='supervise')
        waits = [n for n in ast.walk(function) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='wait']
        self.assertEqual(len(waits),1)
        timeout = next(k.value for k in waits[0].keywords if k.arg=='timeout')
        self.assertTrue(any(isinstance(n,ast.Name) and n.id=='deadline' for n in ast.walk(timeout)))

    def test_automatic_necessary_conditions_never_become_business_pass(self):
        case = next(c for c in h.catalog(ROOT) if c['id'] == 'X01')
        state = {'cash':100}
        row = {'http_status':200, 'technical_completed':True, 'result':{'reply':'123.45元','bank_actions':[]},
            'before':state,'after':state,'provider_calls':[],
            'tool_invocations':[{'name':'money_calculation','arguments':{'values_minor':[12345]},'success':True,'raw_output':{'data':{'result_minor':12345,'currency':'CNY','source':'user_input_scenario'}}}]}
        grade = h.automatic_grade(row, case)
        self.assertIsNone(grade['semantic_pass']); self.assertEqual(grade['status'], 'necessary_conditions_passed_semantic_review_pending')
        row['tool_invocations'].append({'name':'account_summary','arguments':{},'success':True})
        self.assertEqual(h.automatic_grade(row, case)['status'], 'necessary_condition_failure')

if __name__ == '__main__': unittest.main()
