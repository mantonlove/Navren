"""当前 RAG harness 的输入/冻结/日志边界；只用临时合成资料，不启动服务。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, redirect_stdout
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('current_rag_harness_under_test',
                                             PROJECT / 'scripts/run_current_rag_acceptance.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ReviewerDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def envelope(value):
        return json.dumps({'choices': [{'message': {'content': json.dumps(value),
            'reasoning_content': 'hidden-value-must-not-be-recorded'}}]}, indent=2).encode()

    def test_response_read_preserves_exact_bytes_arguments_and_context(self):
        raw = b' \n' + self.envelope({'verdict': 'approve', 'reason': 'private-reason', 'issues': []})
        response = unittest.mock.Mock()
        response.read.return_value = raw
        response.__enter__ = unittest.mock.Mock(return_value=response)
        response.__exit__ = unittest.mock.Mock(return_value=False)
        diagnostics = []
        with runner.ReviewerDiagnosticResponse(response, diagnostics) as observed:
            self.assertIs(observed.read(256_000), raw)
        response.read.assert_called_once_with(256_000)
        response.__enter__.assert_called_once_with()
        response.__exit__.assert_called_once_with(None, None, None)
        self.assertEqual(diagnostics[0]['response_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(diagnostics[0]['missing_required'], [])
        self.assertEqual(diagnostics[0]['unknown_fields'], [])
        self.assertNotIn('private-reason', json.dumps(diagnostics))
        self.assertNotIn('hidden-value-must-not-be-recorded', json.dumps(diagnostics))

    def test_missing_unknown_fields_and_types_never_record_values(self):
        raw = self.envelope({'verdict': 'private-verdict-value', 'issues': 'private-issues-value',
            'provider_extra': {'secret': 'private-extra-value'}, 'reasoning_content': 'private-root-reasoning'})
        diagnostic = runner.reviewer_response_diagnostic(raw)
        self.assertEqual(diagnostic['root_keys'], ['issues', 'provider_extra', 'reasoning_content', 'verdict'])
        self.assertEqual(diagnostic['missing_required'], ['reason'])
        self.assertEqual(diagnostic['unknown_fields'], ['provider_extra', 'reasoning_content'])
        self.assertEqual(diagnostic['field_types'], {'verdict': 'string', 'issues': 'string',
            'provider_extra': 'object', 'reasoning_content': 'string'})
        for secret in ['private-verdict-value', 'private-issues-value', 'private-extra-value',
                       'private-root-reasoning', 'hidden-value-must-not-be-recorded']:
            self.assertNotIn(secret, json.dumps(diagnostic))

    def test_schema_failure_keeps_safe_packet_and_diagnostics_without_changing_request(self):
        raw = self.envelope({'verdict': 'approve', 'issues': [], 'unexpected': 'private-response-value'})
        request = runner.urllib.request.Request('https://unit.invalid/', data=b' exact original request \n',
            headers={'Authorization': 'Bearer private-header-value'}, method='POST')
        calls = []
        def transport(*args, **kwargs):
            calls.append((args, kwargs))
            return io.BytesIO(raw)
        reviewer = SimpleNamespace(client=SimpleNamespace(urlopen=transport), records=[])
        packet = {'draft': {'text': 'complete public draft', 'claims': [{'text': 'original source'}]},
                  'evidence': [{'body': 'complete public evidence'}], 'api_key': 'private-packet-key'}
        original_packet = deepcopy(packet)
        error = ValueError('private-error-message')
        error.code = 'INVALID_TOOL_ARGUMENTS'
        def review(instance, supplied, *args, **kwargs):
            self.assertIs(supplied, packet)
            with instance.client.urlopen(request, timeout=7) as response:
                self.assertEqual(response.read(256_000), raw)
            raise error
        with self.assertRaises(ValueError) as caught:
            runner.trace_reviewer_call(reviewer, review, runner.safe_record, packet, 'unit-request', 7)
        self.assertIs(caught.exception, error)
        self.assertIs(reviewer.client.urlopen, transport)
        self.assertEqual(packet, original_packet)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0][0], request)
        self.assertEqual(calls[0][0][0].data, b' exact original request \n')
        self.assertEqual(calls[0][1], {'timeout': 7})
        self.assertEqual(reviewer.records[0]['packet'], runner.safe_record(original_packet))
        self.assertEqual(reviewer.records[0]['error_code'], 'INVALID_TOOL_ARGUMENTS')
        self.assertEqual(reviewer.records[0]['response_diagnostics'][0]['missing_required'], ['reason'])
        self.assertNotIn('decision', reviewer.records[0])
        for secret in ['private-header-value', 'private-packet-key', 'private-response-value', 'private-error-message']:
            self.assertNotIn(secret, json.dumps(reviewer.records))

    def test_network_read_error_is_not_swallowed_and_success_result_is_unchanged(self):
        error = runner.urllib.error.URLError('private-network-message')
        def failed_transport(*args, **kwargs):
            response = unittest.mock.Mock()
            response.read.side_effect = error
            return response
        reviewer = SimpleNamespace(client=SimpleNamespace(urlopen=failed_transport), records=[])
        def failed_review(instance, packet):
            return instance.client.urlopen('unit').read(256_000)
        with self.assertRaises(runner.urllib.error.URLError) as caught:
            runner.trace_reviewer_call(reviewer, failed_review, runner.safe_record, {'draft': 'complete'})
        self.assertIs(caught.exception, error)
        self.assertIs(reviewer.client.urlopen, failed_transport)
        self.assertEqual(reviewer.records[0]['response_diagnostics'], [])
        self.assertNotIn('private-network-message', json.dumps(reviewer.records))
        result = {'verdict': 'approve', 'reason': 'public synthetic reason', 'issues': [],
                  '_agent_review_wire': b'original wire'}
        self.assertIs(runner.trace_reviewer_call(reviewer, lambda *a: result,
            runner.safe_record, {'draft': 'complete'}), result)
        self.assertEqual(result['_agent_review_wire'], b'original wire')
        self.assertEqual(reviewer.records[1]['decision']['review_wire_identity']['sha256'],
                         hashlib.sha256(b'original wire').hexdigest())


class CurrentRagHarnessSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='navren-rag-harness-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = (Path(self.temporary.name) / 'project').resolve()
        self.public = self.root / 'outputs/window-progress/rag'
        self.output = self.root / 'outputs/rag-overall-acceptance-20261012'
        self.public.mkdir(parents=True)
        self.output.mkdir(parents=True)
        (self.root / 'scripts').mkdir()
        # Reuse the actual old standard-library-only freezer under a synthetic ROOT.
        shutil.copy2(PROJECT / 'scripts/run_rag_acceptance.py', self.root / 'scripts/run_rag_acceptance.py')
        (self.root / 'app.py').write_text('VALUE = 1\n')
        (self.root / 'local_knowledge').mkdir()
        (self.root / 'local_knowledge/schema.sql').write_text('CREATE TABLE public_rows(id TEXT);\n')
        self.database = self.public / 'public.sqlite3'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE public_rows(id TEXT PRIMARY KEY,text TEXT)')
            db.execute('INSERT INTO public_rows VALUES(?,?)', ('PUBLIC:1', '合成公共条款，不是真实产品事实'))
        self.cases = {'cases': [
            {'id': 'P01', 'question': '公开资料中的费用条件是什么？', 'expected_answer': '合成预期一',
             'expected_review_keys': ['synthetic-fees'], 'request': {'query': '费用 条件', 'facets': ['fees']}},
            {'id': 'P02', 'question': '公开资料是否保证本金？', 'expected_answer': '合成预期二',
             'expected_review_keys': ['synthetic-risk'], 'request': {'query': '风险提示', 'facets': ['risk']}}
        ]}
        self.questions = self.public / 'manual-question-set.json'
        self.questions.write_text(json.dumps(self.cases, ensure_ascii=False))
        (self.public / 'product-review-manifest.json').write_text('{"fixture":"synthetic_only"}')
        (self.public / 'integration-data-manifest.json').write_text(json.dumps({
            'knowledge_database': {'path': str(self.database), 'sha256': runner.sha(self.database),
                                   'release_id': 'pub-synthetic-only'}
        }))
        (self.public / 'sources/products').mkdir(parents=True)
        # Only copied/hashed; never passed to a PDF parser or presented as a real document.
        (self.public / 'sources/products/synthetic.pdf').write_bytes(b'%PDF-fixture-not-a-real-financial-document')
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(runner, 'ROOT', self.root))
        stack.enter_context(patch.object(runner, 'PUBLIC', self.public))
        stack.enter_context(patch.object(runner, 'OUTPUT_ROOT', self.output))
        self.urlopen = stack.enter_context(patch.object(runner.urllib.request, 'urlopen',
            side_effect=AssertionError('unit_test_must_not_call_network')))
        self.spawn = stack.enter_context(patch.object(runner.subprocess, 'Popen',
            side_effect=AssertionError('unit_test_must_not_spawn_server_or_worker')))

    def args(self, **changes):
        values = {'out_dir': self.output / 'run-01', 'question_set': None, 'cases': '',
                  'knowledge_database': None, 'database_sha256': None, 'release_id': None,
                  'cloud': False, 'execute': False, 'max_total_tokens': 200000,
                  'case_hard_seconds': 150, 'stop_on_failure': True,
                  'vector_enabled': 0, 'structure_enabled': 0,
                  'rerank_enabled': 0, 'answer_form_enabled': 0,
                  'prepared_public_manifest': None, 'prepared_public_manifest_sha256': None}
        values.update(changes)
        return argparse.Namespace(**values)

    def assert_no_execution(self):
        self.urlopen.assert_not_called()
        self.spawn.assert_not_called()

    def prepared_fixture(self):
        """合成公共schema/小资产；实际预检边界，不构建ONNX或真实产品数据。"""
        self.database.unlink()
        release = 'pub-synthetic-only'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''CREATE TABLE chunks(id TEXT PRIMARY KEY,excerpt TEXT);
                CREATE TABLE release_chunks(release_id TEXT,chunk_id TEXT);
                CREATE TABLE releases(id TEXT PRIMARY KEY,manifest_sha256 TEXT,sealed INTEGER);
                CREATE TABLE current_release(scope TEXT PRIMARY KEY,release_id TEXT);''')
            db.executemany('INSERT INTO chunks VALUES(?,?)', [(str(i), '合成公开原文'+str(i)) for i in range(22)])
            db.executemany('INSERT INTO release_chunks VALUES(?,?)', [(release, str(i)) for i in range(22)])
            db.execute('INSERT INTO releases VALUES(?,?,1)', (release, 'a'*64))
            db.execute('INSERT INTO current_release VALUES(?,?)', ('public', release))
            for name in runner.IDENTITY_TABLES:
                if name in ('chunks', 'release_chunks', 'releases', 'current_release'):continue
                db.execute('CREATE TABLE '+name+'(id TEXT PRIMARY KEY,body BLOB)')
                count = 6 if name in ('document_versions', 'raw_artifacts', 'documents') else 1
                db.executemany('INSERT INTO '+name+' VALUES(?,?)', [(str(i), b'synthetic-public') for i in range(count)])
            identity = runner.public_table_identity(db, runner.IDENTITY_TABLES)
        self.source_sha = runner.sha(self.database)
        reference = {'knowledge_database': {'path': str(self.database), 'sha256': self.source_sha, 'release_id': release}}
        (self.public/'integration-data-manifest.json').write_text(json.dumps(reference))
        for path in (self.public/'sources/products').glob('*.pdf'):path.unlink()
        documents = []
        for i in range(6):
            path = self.public/'sources/products'/('public-'+str(i)+'.pdf')
            path.write_bytes(b'%PDF-synthetic-public-fixture-'+str(i).encode())
            documents.append({'file': str(path), 'raw_sha256': runner.sha(path)})
        (self.public/'product-review-manifest.json').write_text(json.dumps({'funds':[{'documents':documents}]}))
        target = self.output/'prepared-public.sqlite3';shutil.copy2(self.database, target)
        profiles = [{'name': name, 'profile_id': name+':'+hashlib.sha256(name.encode()).hexdigest(),
                     'dimension': 384, 'providers':['CPUExecutionProvider']} for name in runner.PROFILE_NAMES]
        manifests = []
        with closing(sqlite3.connect(target)) as db, db:
            db.executescript('''CREATE TABLE vector_releases(release_id TEXT,profile_id TEXT,
                release_manifest TEXT,vector_manifest TEXT,dimension INTEGER,chunk_count INTEGER,
                vector_count INTEGER,created_at TEXT);
                CREATE TABLE chunk_vectors(release_id TEXT,profile_id TEXT,citation_id TEXT,
                window_no INTEGER,text_sha256 TEXT,vector BLOB,vector_sha256 TEXT);''')
            for profile, count in zip(profiles, (23, 53)):
                item = {'release_id':release,'profile_id':profile['profile_id'],'release_manifest':'a'*64,
                        'vector_manifest':'b'*64,'dimension':384,'chunk_count':22,'vector_count':count,'created_at':'synthetic'}
                manifests.append(item)
                db.execute('INSERT INTO vector_releases VALUES(?,?,?,?,?,?,?,?)', tuple(item.values()))
                db.executemany('INSERT INTO chunk_vectors VALUES(?,?,?,?,?,?,?)',
                    [(release, profile['profile_id'], str(i%22), i//22, 'c'*64, b'synthetic-vector', 'd'*64) for i in range(count)])
        self.prepared_database = target
        environment = {'NAVREN_VECTOR_ENCODER':'dual','NAVREN_QUERY_TRANSLATION_ENABLED':'0',
                       'NAVREN_KNOWLEDGE_DB':'/forbidden/original.sqlite3', 'NAVREN_FINANCIAL_DB':'/forbidden/private.sqlite3',
                       'PATH':'/untrusted', 'API_KEY':'untrusted-not-a-real-secret'}
        assets = {}
        for folder, (variable, members) in runner.MODEL_FILES.items():
            directory = self.output/'models'/folder;environment[variable] = str(directory)
            for filename in members:
                path = directory/filename;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(('synthetic-public-asset:'+folder+'/'+filename).encode())
                digest = runner.sha(path)
                assets[folder+'/'+filename] = {'actual_sha256':digest,'v11_sha256':digest,'matches_v11':True}
        extensions = {}
        for name in ('numpy','onnxruntime','tokenizers'):
            path = self.output/(name+'-fixture.so');path.write_bytes(b'synthetic-native-extension')
            extensions[name] = {'path':str(path),'sha256':runner.sha(path)}
        self.native = {'python':str(Path(sys.executable).absolute()),'python_version':'synthetic',
                       'architecture':'synthetic','platform':'synthetic','packages':{'numpy':'test','onnxruntime':'test','tokenizers':'test'},
                       'extensions':extensions,'cpu_provider_available':True,'model_sessions_created':0}
        status = {'public_release_id':release,'published_chunk_count':22,'vector_index':{'ready':True,
            'profiles':[{'profile_id':p['profile_id'],'ready':True,'dimension':384,'vector_count':m['vector_count']}
                        for p,m in zip(profiles, manifests)]}}
        sources = {'local_knowledge/schema.sql':runner.sha(self.root/'local_knowledge/schema.sql')}
        self.receipt = {'schema':'navren_persistent_public_vector_preparation/v1','passed':True,
            'checks':{name:True for name in runner.PREPARED_CHECKS},'actual_database_path':str(target),
            'database_sha256':runner.sha(target),'database_sha256_before_queries':runner.sha(target),
            'release_id':release,'source_database_path':str(self.database),'source_database_sha256':self.source_sha,
            'original_table_identity_before':identity,'original_table_identity_after':identity,
            'fixed_environment':environment,'model_assets_before':assets,'model_assets_after':assets,
            'runtime':{'python':self.native['python'],'packages':self.native['packages'],
                       'loaded_embedding_profiles':profiles,'CPUExecutionProvider_only':True},
            'vector_preparation':{'ready':True,'profiles':profiles,'manifest_rows':manifests},
            'derived_manifests':{'vector_releases':manifests},
            'build_result':{'ready':True,'release_id':release,'profiles':[
                {'profile_id':p['profile_id'],'ready':True,'chunk_count':22} for p in profiles]},
            'provider_status':status,'structure_preparation':{'ready':True,'applicable':False,
                'parent_count':0,'child_count':0,'manifest_rows':[]},
            'source_sha256_before':sources,'source_sha256_after':sources}
        self.receipt_path = self.output/'prepared-results.json'
        self.write_receipt()
        self.native_mock = patch.object(runner,'native_runtime',return_value=self.native)
        self.native_mock.start();self.addCleanup(self.native_mock.stop)
        return self.prepared_args()

    def write_receipt(self):
        self.receipt_path.write_text(json.dumps(self.receipt))

    def prepared_args(self, **changes):
        return self.args(knowledge_database=self.prepared_database,
            database_sha256=self.receipt['database_sha256'],release_id=self.receipt['release_id'],
            prepared_public_manifest=self.receipt_path,prepared_public_manifest_sha256=runner.sha(self.receipt_path),
            vector_enabled=1,structure_enabled=1,rerank_enabled=1,answer_form_enabled=1,**changes)

    def request_budget_fixture(self):
        """小字节替身只供冻结校验；模块一旦误载入就失败，不复制真实分词器。"""
        self.budget_files = {}
        for relative in runner.REQUEST_BUDGET_FILES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            contents = (b'raise AssertionError("unit_test_must_not_import_request_budget")\n'
                        if path.suffix == '.py' else b'{"synthetic_budget_tokenizer":true}\n')
            path.write_bytes(contents)
            self.budget_files[relative] = runner.sha(path)
        files_patch = patch.object(runner, 'REQUEST_BUDGET_FILES', self.budget_files)
        files_patch.start(); self.addCleanup(files_patch.stop)
        extension = self.output / 'request-budget-tokenizers-fixture.so'
        extension.write_bytes(b'synthetic-native-extension-never-loaded')
        self.budget_native = {'python': str(Path(sys.executable).resolve()),
            'python_version': 'synthetic', 'tokenizers_version': '0.23.3',
            'extension': {'path': str(extension), 'sha256': runner.sha(extension)},
            'tokenizer_loaded': False, 'model_sessions_created': 0}
        runtime_patch = patch.object(runner, 'request_budget_runtime', return_value=self.budget_native)
        self.budget_runtime = runtime_patch.start(); self.addCleanup(runtime_patch.stop)

    def test_current_registered_complete_resources_freeze_without_loading_budget_module(self):
        registered = runner.registered_request_budget_files(PROJECT)
        for relative in registered:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT / relative, target)
        with patch.object(runner, 'request_budget_runtime',
                          side_effect=AssertionError('identity_check_must_not_load_runtime')) as runtime:
            self.assertEqual(runner.registered_request_budget_files(self.root), registered)
            target = self.output / 'current-registered-frozen'; target.mkdir()
            manifest = {}
            runner.freeze_request_budget_resources(target, manifest, self.root)
            self.assertEqual(manifest, registered)
            self.assertEqual(runner.registered_request_budget_files(target), manifest)
            for relative in manifest:
                self.assertEqual((target / relative).read_bytes(), (self.root / relative).read_bytes())
            runtime.assert_not_called()
        self.assertEqual(runner.LIMITS['max_tokens'], 100000)
        self.assertEqual(runner.LIMITS['timeout_seconds'], 105)
        self.assertEqual(runner.LIMITS['max_rounds'], 8)
        self.assertEqual(runner.LIMITS['max_tools'], 12)
        self.assertEqual(runner.LIMITS['reviewer_output_tokens'], 1400)

    def test_current_registered_missing_license_and_changed_dependency_still_rejected(self):
        registered = runner.registered_request_budget_files(PROJECT)
        for relative in registered:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT / relative, target)
        for relative in ('agent_runtime/vendor/DEEPSEEK-MIT-LICENSE.md',
                         'dependency-resources/deepseek-v4/LICENSE',
                         'agent_runtime/vendor/deepseek_v4_base.py'):
            path = self.root / relative
            contents = path.read_bytes()
            with self.subTest(relative=relative):
                if relative.endswith('.py'):
                    path.write_bytes(contents + b'\nchanged_dependency\n')
                else:
                    path.unlink()
                with self.assertRaisesRegex(ValueError, 'request_budget_resource_missing_redirected_or_changed'):
                    runner.registered_request_budget_files(self.root)
                path.write_bytes(contents)
        self.assertEqual(runner.registered_request_budget_files(self.root), registered)

    def test_app_release_assets_register_only_exact_public_files_and_reject_redirects(self):
        (self.root / 'server.py').write_text("APP_PAGES={'index.html'}\n")
        (self.root / 'index.html').write_text('public page')
        (self.root / 'js').mkdir(); (self.root / 'css').mkdir()
        (self.root / 'js/app.js').write_text('public script')
        (self.root / 'css/app.css').write_text('public style')
        (self.root / 'private.sqlite3').write_text('must not be registered')
        expected = {name: runner.sha(self.root / name) for name in ('index.html', 'js/app.js', 'css/app.css')}
        self.assertEqual(runner.public_app_release_assets(self.root), expected)
        path = self.root / 'js/app.js'; contents = path.read_bytes(); path.unlink()
        path.symlink_to(self.root / 'private.sqlite3')
        with self.assertRaisesRegex(ValueError, 'public_app_asset_missing_or_redirected'):
            runner.public_app_release_assets(self.root)
        path.unlink(); path.write_bytes(contents)
        (self.root / 'server.py').write_text("APP_PAGES={'../private.html'}\n")
        with self.assertRaisesRegex(ValueError, 'public_app_pages_contract_invalid'):
            runner.public_app_release_assets(self.root)

    def consumer_source_fixture(self, *, requirements=False, original_worker=False):
        """登记合成消费者源码；其helper/业务模块一旦误导入就失败。"""
        directory = self.root / 'outputs/consumer-source-fixture'
        self.consumer_root = directory / 'source'
        self.consumer_root.mkdir(parents=True)
        self.consumer_manifest = directory / 'source-manifest.json'
        contents = {'app.py': b'CONSUMER_FIXTURE = True\n',
            'agent_runtime/engine.py': b'raise AssertionError("unit_test_must_not_import_consumer_business")\n',
            'local_knowledge/schema.sql': b'CREATE TABLE consumer_public_rows(id TEXT);\n',
            'scripts/run_rag_acceptance.py': b'raise AssertionError("unit_test_must_not_import_consumer_helper")\n'}
        if requirements:
            contents['local_knowledge/requirements-vector.txt'] = b'numpy==test\nonnxruntime==test\ntokenizers==test\n'
        if original_worker:
            contents['scripts/run_current_rag_acceptance.py'] = b'raise AssertionError("unit_test_must_not_run_old_worker")\n'
        self.consumer_files = {}
        for relative, value in contents.items():
            path = self.consumer_root / relative
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
            self.consumer_files[relative] = runner.sha(path)
        self.write_consumer_manifest()
        return self.consumer_args()

    def write_consumer_manifest(self, data=None):
        value = {'source_sha256': self.consumer_files} if data is None else data
        self.consumer_manifest.write_text(json.dumps(value))

    def consumer_args(self, **changes):
        values = {'consumer_source_root': self.consumer_root,
                  'consumer_source_manifest': self.consumer_manifest,
                  'consumer_source_manifest_sha256': runner.sha(self.consumer_manifest)}
        values.update(changes)
        return self.args(**values)

    def consumer_prepared_args(self, **changes):
        args = self.prepared_args(**changes)
        consumer = self.consumer_args()
        for name in ('consumer_source_root', 'consumer_source_manifest', 'consumer_source_manifest_sha256'):
            setattr(args, name, getattr(consumer, name))
        return args

    def assert_consumer_preflight_rejected(self, args, error_code=None):
        printed = io.StringIO()
        with redirect_stdout(printed):
            self.assertEqual(runner.run(args), 2)
        record = json.loads(printed.getvalue())
        self.assertEqual(record['status'], 'preflight_failed')
        self.assertEqual(record['model_calls'], 0)
        if error_code is not None:
            self.assertEqual(record['error_code'], error_code)
        self.assert_no_execution()

    def assert_consumer_dispatch_rejected(self, config):
        args = self.consumer_args(cloud=True, execute=True)
        with patch.object(runner, 'prepare', return_value=config), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(args), 2)
        record = json.loads((Path(config['out_dir']) / 'results.json').read_text())
        self.assertEqual(record['status'], 'PRE_EXECUTION_INTEGRITY_FAILED')
        self.assertEqual(record['exit_code'], 2)
        self.assertEqual(record['records'], [])
        self.assert_no_execution()

    def test_approved_host_response_accepts_absent_flag_and_rejects_fallback_or_missing_proof(self):
        response = {'compliance_pass': True, 'agent_review': {'status': 'approved'},
                    'answer_contract': {'kind': 'reviewed_text', 'evidence_ids': ['PUBLIC:1']}}
        self.assertTrue(runner.approved_reply(200, response))
        self.assertTrue(runner.approved_reply(200, {**response, 'isFallback': False}))
        for mutation in ({'isFallback': True}, {'isFallback': None}, {'isFallback': 0},
                         {'compliance_pass': False}, {'compliance_pass': None},
                         {'agent_review': {'status': 'blocked'}}, {'agent_review': None},
                         {'answer_contract': {}}, {'answer_contract': None},
                         {'error': {'code': 'AGENT_BUDGET_EXCEEDED'}}):
            with self.subTest(mutation=mutation):
                self.assertFalse(runner.approved_reply(200, {**response, **mutation}))
        self.assertFalse(runner.approved_reply(500, response))
        self.assertFalse(runner.approved_reply(200, {}))
        self.assert_no_execution()

    def test_sqlite_uri_is_decoded_and_escape_or_foreign_authority_rejected(self):
        state = self.output / 'isolated'
        state.mkdir()
        self.assertTrue(runner.isolated_sqlite_target(state / 'public.sqlite3', state))
        self.assertTrue(runner.isolated_sqlite_target(':memory:', state))
        self.assertTrue(runner.isolated_sqlite_target((state / 'public name.sqlite3').as_uri() + '?mode=ro', state))
        self.assertFalse(runner.isolated_sqlite_target('file:' + str(state) + '/%2e%2e/outside.sqlite3', state))
        self.assertFalse(runner.isolated_sqlite_target('file://foreign' + str(state / 'public.sqlite3'), state))
        self.assertFalse(runner.isolated_sqlite_target(self.public / 'public.sqlite3', state))
        self.assertFalse(runner.isolated_sqlite_target(None, state))
        self.assert_no_execution()

    def test_attach_guard_survives_business_authorizer_replacement_and_reset(self):
        state = self.output / 'isolated'
        state.mkdir()
        factory = runner.isolated_connection_factory(state)
        with closing(sqlite3.connect(state / 'main.sqlite3', factory=factory)) as db:
            db.execute('CREATE TABLE rows(value TEXT)')
            callbacks = (lambda *args: sqlite3.SQLITE_OK, None,
                         lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK)
            for callback in callbacks:
                with self.subTest(callback=callback):
                    db.set_authorizer(callback)
                    for target in (str(self.public / 'public.sqlite3'),
                                   'file:' + str(state) + '/%2e%2e/outside.sqlite3'):
                        with self.assertRaises(sqlite3.DatabaseError):
                            db.execute('ATTACH DATABASE ? AS outside', (target,))
                    # SQLite 预编译带参数 ATTACH 时不向 authorizer 提供文件名，故拒绝。
                    with self.assertRaises(sqlite3.DatabaseError):
                        db.execute('ATTACH DATABASE ? AS inside', (str(state / 'allowed.sqlite3'),))
                    target_literal = str(state / 'allowed.sqlite3').replace("'", "''")
                    db.execute("ATTACH DATABASE '" + target_literal + "' AS inside")
                    db.execute('DETACH DATABASE inside')
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("INSERT INTO rows VALUES ('forbidden')")
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rows').fetchone()[0], 0)
            db.set_authorizer(None)
            db.execute("INSERT INTO rows VALUES ('allowed')")
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rows').fetchone()[0], 1)
        self.assert_no_execution()

    def test_direct_opener_ignores_system_proxy_and_denies_redirect_without_network(self):
        with patch.object(runner.urllib.request, 'getproxies', return_value={'https': 'http://127.0.0.1:7897'}) as proxies:
            opener = runner.direct_http_opener()
            proxies.assert_not_called()
        self.assertFalse(any(isinstance(handler, runner.urllib.request.ProxyHandler) and handler.proxies
                             for handler in opener.handlers))
        redirect = next(handler for handler in opener.handlers
                        if isinstance(handler, runner.urllib.request.HTTPRedirectHandler))
        with self.assertRaisesRegex(PermissionError, 'unregistered_redirect'):
            redirect.redirect_request(None, None, 302, '', {}, 'https://foreign.invalid/')
        self.assert_no_execution()

    def test_path_case_ids_rejected_before_any_output_dispatch(self):
        for ident in ('../escape', 'folder/P01', '/tmp/outside', '.', '..'):
            with self.subTest(ident=ident):
                rows = {'cases': [{'id': ident, 'question': '公共问题'}]}
                with self.assertRaisesRegex(ValueError, 'invalid_or_duplicate_case_id'):
                    runner.selected_cases(rows, '')
        self.assert_no_execution()

    def test_unknown_and_duplicate_selection_do_not_silently_reduce_suite(self):
        for selection in ('P99', 'P01,P99', 'P01,P01'):
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(ValueError, 'unknown_or_duplicate_selected_case'):
                    runner.selected_cases(self.cases, selection)
        self.assert_no_execution()

    def test_duplicate_source_ids_and_empty_suite_are_rejected(self):
        duplicate = {'cases': [self.cases['cases'][0], self.cases['cases'][0]]}
        with self.assertRaisesRegex(ValueError, 'invalid_or_duplicate_case_id'):
            runner.selected_cases(duplicate, '')
        for data in ({}, {'cases': []}):
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError, 'empty_question_set'):
                    runner.selected_cases(data, '')

    def test_invalid_questions_rejected_without_rewriting_valid_natural_question(self):
        for text in ('', '  \n ', '字' * 6001, None, 42):
            with self.subTest(text_type=type(text).__name__):
                with self.assertRaisesRegex(ValueError, 'invalid_case_question'):
                    runner.selected_cases({'cases': [{'id': 'P01', 'question': text}]}, '')
        natural = '公开资料中的费用条件是什么？'
        selected = runner.selected_cases(self.cases, 'P01')
        self.assertEqual(selected[0]['question'], natural)
        self.assertEqual(runner.canonical_sha(selected[0]), runner.canonical_sha(self.cases['cases'][0]))
        selected[0]['request']['query'] = '局部更改'
        self.assertEqual(self.cases['cases'][0]['request']['query'], '费用 条件')

    def test_metadata_redaction_preserves_public_citation_geometry_and_model_usage(self):
        original = {'workspace_id': 'private-workspace', 'csrf': 'private-csrf',
                    'speech_receipt': 'private-receipt', 'API_KEY': 'do-not-store',
                    'message': 'private-workspace private-receipt',
                    'nested': [{'account_id': 'private-account', 'reasoning_content': 'hidden',
                                'citation_id': 'public-citation', 'document_version_id': 'public-version',
                                'product_id': 'PUBLIC:1', 'source_url': 'https://official.example/document',
                                'locator': {'page': 2, 'start_char': 10, 'end_char': 20},
                                'usage': {'prompt_tokens': 18, 'total_tokens': 23},
                                'model': 'DeepSeek-V4-Flash'}]}
        safe = runner.safe_record(original, ('private-workspace', 'private-receipt'))
        serialized = json.dumps(safe)
        for secret in ('private-workspace', 'private-receipt', 'private-account', 'private-csrf', 'do-not-store', 'hidden'):
            self.assertNotIn(secret, serialized)
        evidence = safe['nested'][0]
        self.assertEqual(evidence['citation_id'], 'public-citation')
        self.assertEqual(evidence['document_version_id'], 'public-version')
        self.assertEqual(evidence['product_id'], 'PUBLIC:1')
        self.assertEqual(evidence['locator'], {'page': 2, 'start_char': 10, 'end_char': 20})
        self.assertEqual(evidence['usage']['total_tokens'], 23)
        self.assertEqual(original['speech_receipt'], 'private-receipt')

    def test_insufficient_budget_with_cloud_execute_stops_before_api_or_worker(self):
        with redirect_stdout(io.StringIO()):
            result = runner.run(self.args(cloud=True, execute=True, max_total_tokens=199999))
        self.assertEqual(result, 2)
        record = json.loads((self.output / 'run-01/results.json').read_text())
        self.assertEqual(record['error_code'], 'aggregate_token_preflight_rejected')
        self.assertEqual(record['model_calls'], 0)
        self.assert_no_execution()

    def test_default_public_database_hash_mismatch_is_rejected_before_execution(self):
        reference = self.public / 'integration-data-manifest.json'
        data = json.loads(reference.read_text())
        data['knowledge_database']['sha256'] = '0' * 64
        reference.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'public_database_changed'):
            runner.prepare(self.args(cloud=True, execute=True))
        self.assert_no_execution()

    def test_custom_public_database_requires_explicit_hash_and_release(self):
        for number, changes in enumerate(({}, {'database_sha256': runner.sha(self.database)},
                                          {'release_id': 'pub-custom'})):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, 'custom_public_database_requires_hash_and_release'):
                    runner.prepare(self.args(out_dir=self.output / ('run-' + str(number)),
                                             knowledge_database=self.database, **changes))
        self.assert_no_execution()

    def test_custom_database_bad_hash_cannot_override_frozen_release(self):
        with self.assertRaisesRegex(ValueError, 'public_database_changed'):
            runner.prepare(self.args(knowledge_database=self.database,
                database_sha256='1' * 64, release_id='pub-custom'))
        self.assert_no_execution()

    def test_existing_output_keeps_old_results_bytes_and_does_not_freeze_again(self):
        out = self.output / 'run-01'
        out.mkdir()
        path = out / 'results.json'
        original = b'{"old_failure":"must_remain"}\n'
        path.write_bytes(original)
        with patch.object(runner, 'freeze', side_effect=AssertionError('must_not_freeze_existing')) as freezer:
            with redirect_stdout(io.StringIO()):
                code = runner.run(self.args(cloud=True, execute=True))
        self.assertEqual(code, 2)
        self.assertEqual(path.read_bytes(), original)
        freezer.assert_not_called()
        self.assert_no_execution()

    def test_output_escape_is_rejected_without_creating_outside_directory(self):
        outside = self.root / 'outside-registered-output'
        with self.assertRaisesRegex(ValueError, 'output_outside_registered_scope'):
            runner.prepare(self.args(out_dir=outside))
        self.assertFalse(outside.exists())
        self.assert_no_execution()

    def test_hash_validation_detects_candidate_input_and_original_drift_separately(self):
        config = runner.prepare(self.args())
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        for path, key in ((Path(config['candidate']) / 'app.py', 'candidate_unchanged'),
                          (self.output / 'run-01/inputs/knowledge.sqlite3', 'frozen_inputs_unchanged'),
                          (self.database, 'original_inputs_unchanged')):
            with self.subTest(boundary=key):
                original = path.read_bytes()
                path.write_bytes(original + b'\nchanged')
                checks = runner.hashes_unchanged(config)
                self.assertFalse(checks[key])
                self.assertTrue(all(v for k, v in checks.items() if k != key))
                path.write_bytes(original)
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_real_financial_packages_and_sql_schema_are_in_frozen_manifest(self):
        expected = {}
        for directory in ('financial_data_service', 'financial_mcp'):
            for source in sorted((PROJECT / directory).rglob('*')):
                if source.is_file() and source.suffix in ('.py', '.sql', '.json') and '__pycache__' not in source.parts:
                    relative = source.relative_to(PROJECT)
                    target = self.root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    expected[str(relative)] = runner.sha(source)
        self.assertGreater(len(expected), 0)
        destination = self.output / 'frozen'
        destination.mkdir()
        manifest = runner.freeze(destination)
        for relative, digest in expected.items():
            with self.subTest(relative=relative):
                self.assertEqual(manifest.get(relative), digest)
                self.assertEqual(runner.sha(destination / relative), digest)
        self.assertEqual(manifest['local_knowledge/schema.sql'], runner.sha(self.root / 'local_knowledge/schema.sql'))
        self.assertTrue({'financial_data_service', 'financial_mcp'} <= set(runner.BUSINESS))
        self.assert_no_execution()

    def test_request_budget_freezes_all_fixed_resources_and_detects_frozen_tamper(self):
        self.request_budget_fixture()
        self.assertEqual(len(self.budget_files), 4)
        config = runner.prepare(self.args())
        manifest = json.loads((self.output / 'run-01/source-manifest.json').read_text())
        self.assertEqual(config['request_budget_resources'],
                         {'files': self.budget_files, 'native_runtime': self.budget_native})
        self.assertEqual(set(runner.hashes_unchanged(config)),
                         {'candidate_unchanged', 'frozen_inputs_unchanged', 'original_inputs_unchanged',
                          'shared_model_assets_unchanged', 'native_extensions_unchanged'})
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        for relative, digest in self.budget_files.items():
            with self.subTest(relative=relative):
                path = Path(config['candidate']) / relative
                self.assertEqual(manifest[relative], digest)
                self.assertEqual(config['source_sha256'][relative], digest)
                self.assertEqual(runner.sha(path), digest)
                original = path.read_bytes()
                path.write_bytes(original + b'changed-after-freeze')
                checks = runner.hashes_unchanged(config)
                self.assertFalse(checks['candidate_unchanged'])
                self.assertFalse(checks['native_extensions_unchanged'])
                path.write_bytes(original)
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_request_budget_missing_dependency_blocks_cloud_before_runtime_or_worker(self):
        self.request_budget_fixture()
        dependencies = [p for p in self.budget_files if p != 'agent_runtime/request_budget.py']
        for index, relative in enumerate(dependencies):
            with self.subTest(relative=relative):
                path = self.root / relative
                contents = path.read_bytes(); path.unlink()
                args = self.args(out_dir=self.output / ('missing-budget-' + str(index)),
                                 cloud=True, execute=True)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(args), 2)
                record = json.loads((args.out_dir / 'results.json').read_text())
                self.assertEqual(record['status'], 'preflight_failed')
                self.assertEqual(record['error_code'], 'request_budget_resource_missing_redirected_or_changed')
                self.assertEqual(record['model_calls'], 0)
                self.assertFalse((args.out_dir / 'configuration.json').exists())
                path.write_bytes(contents)
        self.budget_runtime.assert_not_called()
        self.assert_no_execution()

    def test_request_budget_changed_source_blocks_cloud_before_runtime_or_worker(self):
        self.request_budget_fixture()
        for index, relative in enumerate(self.budget_files):
            with self.subTest(relative=relative):
                path = self.root / relative
                contents = path.read_bytes(); path.write_bytes(contents + b'changed-before-freeze')
                args = self.args(out_dir=self.output / ('changed-budget-' + str(index)),
                                 cloud=True, execute=True)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(args), 2)
                record = json.loads((args.out_dir / 'results.json').read_text())
                self.assertEqual(record['error_code'], 'request_budget_resource_missing_redirected_or_changed')
                self.assertEqual(record['model_calls'], 0)
                self.assertFalse((args.out_dir / 'configuration.json').exists())
                path.write_bytes(contents)
        self.budget_runtime.assert_not_called()
        self.assert_no_execution()

    def test_request_budget_resource_redirects_rejected_even_with_identical_bytes(self):
        self.request_budget_fixture()
        redirects = self.root.parent / 'budget-redirects'; redirects.mkdir()
        for index, relative in enumerate(self.budget_files):
            with self.subTest(relative=relative, boundary='source-file'):
                path = self.root / relative
                contents = path.read_bytes()
                external = redirects / ('resource-' + str(index)); external.write_bytes(contents)
                path.unlink(); path.symlink_to(external)
                args = self.args(out_dir=self.output / ('redirected-budget-' + str(index)),
                                 cloud=True, execute=True)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(args), 2)
                record = json.loads((args.out_dir / 'results.json').read_text())
                self.assertEqual(record['error_code'], 'request_budget_resource_missing_redirected_or_changed')
                self.assertEqual(record['model_calls'], 0)
                path.unlink(); path.write_bytes(contents)
        # 父目录重定向时文件本身不是symlink，也不能接受相同SHA。
        directory = self.root / 'dependency-resources'
        moved = redirects / 'dependency-resources'; directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        args = self.args(out_dir=self.output / 'redirected-budget-parent', cloud=True, execute=True)
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(runner.run(args), 2)
            record = json.loads((args.out_dir / 'results.json').read_text())
            self.assertEqual(record['error_code'], 'request_budget_resource_missing_redirected_or_changed')
            self.assertEqual(record['model_calls'], 0)
        finally:
            directory.unlink(); moved.rename(directory)
        self.budget_runtime.assert_not_called()
        config = runner.prepare(self.args())
        for index, relative in enumerate(self.budget_files):
            with self.subTest(relative=relative, boundary='frozen-file'):
                path = Path(config['candidate']) / relative
                contents = path.read_bytes()
                path.unlink(); path.symlink_to(redirects / ('resource-' + str(index)))
                checks = runner.hashes_unchanged(config)
                self.assertFalse(checks['candidate_unchanged'])
                self.assertFalse(checks['native_extensions_unchanged'])
                self.assertTrue(all(value for key, value in checks.items()
                                    if key not in ('candidate_unchanged', 'native_extensions_unchanged')))
                path.unlink(); path.write_bytes(contents)
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_request_budget_copy_mismatch_blocks_before_runtime_or_worker(self):
        self.request_budget_fixture()
        tokenizer = self.root / 'dependency-resources/deepseek-v4/tokenizer.json'
        original_copy = runner.shutil.copy2
        def changed_copy(source, destination, *args, **kwargs):
            copied = original_copy(source, destination, *args, **kwargs)
            if Path(source) == tokenizer:
                Path(destination).write_bytes(b'changed-during-budget-copy')
            return copied
        args = self.args(cloud=True, execute=True)
        with patch.object(runner.shutil, 'copy2', side_effect=changed_copy), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(args), 2)
        record = json.loads((args.out_dir / 'results.json').read_text())
        self.assertEqual(record['error_code'], 'request_budget_resource_missing_redirected_or_changed')
        self.assertEqual(record['model_calls'], 0)
        self.assertFalse((args.out_dir / 'configuration.json').exists())
        self.budget_runtime.assert_not_called()
        self.assert_no_execution()

    def test_without_request_budget_module_keeps_legacy_freeze_without_runtime_loading(self):
        with patch.object(runner, 'request_budget_runtime',
                          side_effect=AssertionError('legacy_root_must_not_load_budget_runtime')) as runtime:
            config = runner.prepare(self.args())
            self.assertIsNone(config['request_budget_resources'])
            self.assertFalse(set(runner.REQUEST_BUDGET_FILES) & set(config['source_sha256']))
            self.assertTrue(all(runner.hashes_unchanged(config).values()))
            runtime.assert_not_called()
        self.assert_no_execution()

    def test_request_budget_runtime_drift_or_deleted_contract_fails_existing_integrity_gate(self):
        self.request_budget_fixture()
        config = runner.prepare(self.args())
        changed_version = deepcopy(self.budget_native); changed_version['tokenizers_version'] = 'changed'
        changed_hash = deepcopy(self.budget_native); changed_hash['extension']['sha256'] = '0' * 64
        changed_path = deepcopy(self.budget_native); changed_path['extension']['path'] += '.redirected'
        for native in (changed_version, changed_hash, changed_path):
            with self.subTest(runtime=native):
                self.budget_runtime.return_value = native
                checks = runner.hashes_unchanged(config)
                self.assertFalse(checks['native_extensions_unchanged'])
                self.assertTrue(all(value for key, value in checks.items() if key != 'native_extensions_unchanged'))
        self.budget_runtime.return_value = self.budget_native
        self.budget_runtime.side_effect = ValueError('request_budget_runtime_missing')
        self.assertFalse(runner.hashes_unchanged(config)['native_extensions_unchanged'])
        self.budget_runtime.side_effect = None
        for mutation in ('deleted', 'null', 'missing-file', 'unbound-file-hash', 'missing-runtime'):
            changed = deepcopy(config)
            contract = changed['request_budget_resources']
            if mutation == 'deleted': changed.pop('request_budget_resources')
            elif mutation == 'null': changed['request_budget_resources'] = None
            elif mutation == 'missing-file': contract['files'].pop('agent_runtime/request_budget.py')
            elif mutation == 'unbound-file-hash': contract['files']['agent_runtime/request_budget.py'] = '0' * 64
            elif mutation == 'missing-runtime': contract.pop('native_runtime')
            with self.subTest(contract=mutation):
                checks = runner.hashes_unchanged(changed)
                self.assertFalse(checks['native_extensions_unchanged'])
                self.assertTrue(all(value for key, value in checks.items() if key != 'native_extensions_unchanged'))
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_request_budget_runtime_drift_makes_preflight_return_failure_without_dispatch(self):
        self.request_budget_fixture()
        changed_native = deepcopy(self.budget_native)
        changed_native['extension']['sha256'] = '0' * 64
        self.budget_runtime.side_effect = [self.budget_native, changed_native]
        args = self.args()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(args), 2)
        record = json.loads((args.out_dir / 'results.json').read_text())
        self.assertEqual(record['status'], 'PREFLIGHT_INTEGRITY_FAILED')
        self.assertEqual(record['exit_code'], 2)
        self.assertFalse(record['source_integrity']['native_extensions_unchanged'])
        self.assertTrue(all(value for key, value in record['source_integrity'].items()
                            if key != 'native_extensions_unchanged'))
        self.assertEqual(self.budget_runtime.call_count, 2)
        self.assert_no_execution()

    def test_request_budget_module_manifest_mismatch_cannot_bypass_contract_or_preflight(self):
        self.request_budget_fixture()
        config = runner.prepare(self.args())
        candidate = Path(config['candidate'])
        module = candidate / 'agent_runtime/request_budget.py'
        trimmed_manifest = deepcopy(config['source_sha256'])
        trimmed_manifest.pop('agent_runtime/request_budget.py')
        self.budget_runtime.reset_mock()
        with self.assertRaisesRegex(ValueError, 'request_budget_resource_manifest_mismatch'):
            runner.request_budget_contract(candidate, trimmed_manifest)
        for mutation in ('deleted', 'null'):
            with self.subTest(contract=mutation, boundary='unregistered-existing-module'):
                changed = deepcopy(config)
                changed['source_sha256'] = deepcopy(trimmed_manifest)
                if mutation == 'deleted': changed.pop('request_budget_resources')
                else: changed['request_budget_resources'] = None
                checks = runner.hashes_unchanged(changed)
                self.assertFalse(checks['candidate_unchanged'])
                self.assertFalse(checks['native_extensions_unchanged'])
                self.assertTrue(all(value for key, value in checks.items()
                                    if key not in ('candidate_unchanged', 'native_extensions_unchanged')))
                with patch.object(runner, 'prepare', return_value=changed), redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(self.args()), 2)
                record = json.loads((self.output / 'run-01/results.json').read_text())
                self.assertEqual(record['status'], 'PREFLIGHT_INTEGRITY_FAILED')
                self.assertEqual(record['exit_code'], 2)
                self.assertEqual(record['records'], [])
                self.assertFalse(record['source_integrity']['native_extensions_unchanged'])
        with self.subTest(boundary='registered-missing-module'):
            contents = module.read_bytes(); module.unlink()
            try:
                with self.assertRaisesRegex(ValueError, 'request_budget_resource_manifest_mismatch'):
                    runner.request_budget_contract(candidate, config['source_sha256'])
                checks = runner.hashes_unchanged(config)
                self.assertFalse(checks['candidate_unchanged'])
                self.assertFalse(checks['native_extensions_unchanged'])
            finally:
                module.write_bytes(contents)
        self.budget_runtime.assert_not_called()
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_preflight_freezes_gold_and_all_flags_off_without_server_api(self):
        with redirect_stdout(io.StringIO()):
            code = runner.run(self.args())
        self.assertEqual(code, 0)
        out = self.output / 'run-01'
        config = json.loads((out / 'configuration.json').read_text())
        result = json.loads((out / 'results.json').read_text())
        self.assertEqual(result['status'], 'PREFLIGHT_ONLY_NO_SERVER_NO_API')
        self.assertFalse(config['cloud'])
        self.assertFalse(config['execute'])
        self.assertEqual(config['aggregate_admission_tokens'], 200000)
        self.assertEqual(config['cases'], self.cases['cases'])
        self.assertEqual(set(config['retrieval_flags'].values()), {'0'})
        self.assertEqual(config['retrieval_environment']['NAVREN_QUERY_TRANSLATION_ENABLED'], '0')
        self.assertEqual(config['runner_sha256'], runner.sha(PROJECT / 'scripts/run_current_rag_acceptance.py'))
        self.assertTrue(all(result['source_integrity'].values()))
        self.assert_no_execution()

    def test_enabled_retrieval_requires_explicit_prepared_receipt_and_hash(self):
        for index, changes in enumerate(({'vector_enabled':1}, {'structure_enabled':1},
                {'rerank_enabled':1}, {'answer_form_enabled':1})):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError,'prepared_public_manifest_required'):
                    runner.prepare(self.args(out_dir=self.output/('missing-'+str(index)),**changes))
        self.assert_no_execution()

    def test_prepared_preflight_freezes_public_originals_assets_and_whitelisted_environment(self):
        args = self.prepared_fixture()
        with patch.dict(os.environ, {'NAVREN_QUERY_TRANSLATION_ENABLED':'1','NAVREN_VECTOR_ENCODER':'bge'}),\
                redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(args),0)
        config = json.loads((args.out_dir/'configuration.json').read_text())
        result = json.loads((args.out_dir/'results.json').read_text())
        prepared = config['prepared_public']
        self.assertEqual(result['status'],'PREFLIGHT_ONLY_NO_SERVER_NO_API')
        self.assertEqual(config['cases'],self.cases['cases'])
        self.assertEqual(config['retrieval_environment']['NAVREN_QUERY_TRANSLATION_ENABLED'],'0')
        self.assertEqual(config['retrieval_environment']['NAVREN_VECTOR_ENCODER'],'dual')
        for name in ('NAVREN_KNOWLEDGE_DB','NAVREN_FINANCIAL_DB','PATH','API_KEY'):
            self.assertNotIn(name,prepared['environment'])
        self.assertEqual(len(prepared['model_assets']),24)
        self.assertEqual(prepared['native_runtime']['model_sessions_created'],0)
        self.assertEqual(prepared['native_runtime']['extensions'],self.native['extensions'])
        self.assertEqual(len([k for k in config['input_sha256'] if k.startswith('products/')]),6)
        self.assertTrue({'product-review-manifest.json','integration-data-manifest.json',
                         'prepared-public-manifest.json'} <= set(config['input_sha256']))
        self.assertEqual(config['input_bindings']['prepared-source-database']['sha256'],self.source_sha)
        self.assertTrue(all(result['source_integrity'].values()))
        self.assert_no_execution()

    def test_prepared_receipt_drift_and_incomplete_checks_do_not_authorize_execution(self):
        self.prepared_fixture();old_args=self.prepared_args()
        self.receipt['passed']=False;self.write_receipt()
        with self.assertRaisesRegex(ValueError,'prepared_public_manifest_changed'):
            runner.prepare(old_args)
        for index, mutation in enumerate(({'passed':False},{'checks':{}},
                {'original_table_identity_before':{},'original_table_identity_after':{}},
                {'checks':{**self.receipt['checks'],'two_vector_profiles_ready':1}})):
            base=deepcopy(self.receipt);base['passed']=True;base.update(mutation)
            self.receipt_path.write_text(json.dumps(base))
            with self.subTest(mutation=list(mutation)),self.assertRaises(ValueError):
                runner.prepare(self.prepared_args(out_dir=self.output/('receipt-'+str(index))))
        self.assert_no_execution()

    def test_prepared_profiles_duplicates_and_forged_asset_set_rejected(self):
        self.prepared_fixture();base=deepcopy(self.receipt)
        mutations=[]
        duplicate=deepcopy(base);duplicate['vector_preparation']['profiles'].append(duplicate['vector_preparation']['profiles'][0]);mutations.append(duplicate)
        no_ready=deepcopy(base);no_ready['provider_status']['vector_index']['profiles'][0]['ready']=1;mutations.append(no_ready)
        foreign=deepcopy(base);assets=foreign['model_assets_before'];key=next(iter(assets));assets['unknown/extra.bin']=assets.pop(key)
        foreign['model_assets_after']=deepcopy(assets);mutations.append(foreign)
        bad_env=deepcopy(base);bad_env['fixed_environment']['NAVREN_QUERY_TRANSLATION_ENABLED']='1';mutations.append(bad_env)
        for index, receipt in enumerate(mutations):
            self.receipt_path.write_text(json.dumps(receipt))
            with self.subTest(index=index),self.assertRaises(ValueError):
                runner.prepare(self.prepared_args(out_dir=self.output/('profile-'+str(index))))
        self.assert_no_execution()

    def test_prepared_db_hash_and_original_identity_drift_are_distinct_boundaries(self):
        self.prepared_fixture();old_args=self.prepared_args()
        with closing(sqlite3.connect(self.prepared_database)) as db,db:
            db.execute("UPDATE chunks SET excerpt='合成资料被替换' WHERE id='0'")
        with self.assertRaisesRegex(ValueError,'public_database_changed'):
            runner.prepare(old_args)
        digest=runner.sha(self.prepared_database)
        self.receipt.update(database_sha256=digest,database_sha256_before_queries=digest);self.write_receipt()
        with self.assertRaisesRegex(ValueError,'prepared_original_identity_changed'):
            runner.prepare(self.prepared_args(out_dir=self.output/'identity-drift'))
        self.assert_no_execution()

    def test_prepared_shared_assets_native_extensions_and_receipt_checked_after_freeze(self):
        self.prepared_fixture();config=runner.prepare(self.prepared_args())
        boundaries=[(Path(next(iter(config['prepared_public']['model_assets'].values()))['path']),
                     'shared_model_assets_unchanged'),
                    (Path(config['prepared_public']['native_runtime']['extensions']['numpy']['path']),
                     'native_extensions_unchanged'),(self.receipt_path,'original_inputs_unchanged')]
        for path,key in boundaries:
            old=path.read_bytes();path.write_bytes(old+b'changed')
            with self.subTest(boundary=key):self.assertFalse(runner.hashes_unchanged(config)[key])
            path.write_bytes(old)
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_prepared_asset_drift_blocks_before_native_checks_or_dispatch(self):
        self.prepared_fixture()
        variable,members=next(iter(runner.MODEL_FILES.values()))
        path=Path(self.receipt['fixed_environment'][variable])/members[0]
        path.write_bytes(b'changed-public-model')
        with self.assertRaisesRegex(ValueError,'prepared_model_asset_changed'):
            runner.prepare(self.prepared_args())
        self.assert_no_execution()

    def test_custom_all_off_database_also_retains_original_pdf_and_manifests(self):
        config=runner.prepare(self.args(knowledge_database=self.database,
            database_sha256=runner.sha(self.database),release_id='pub-custom'))
        self.assertTrue({'products/synthetic.pdf','product-review-manifest.json',
                         'integration-data-manifest.json'} <= set(config['input_sha256']))
        self.assertIsNone(config['prepared_public'])
        self.assert_no_execution()

    def test_case_scoped_suppression_covers_two_constructors_publish_and_restores_schedule(self):
        scheduled=[]
        def original(*args):scheduled.append(args)
        vectors=SimpleNamespace(schedule=original,query=lambda value:value+'-query')
        record={}
        with ExitStack() as stack:
            runner.suppress_vector_background(vectors,stack,record)
            for kind in ('runtime-provider','gateway-provider','for-release','publish'):
                vectors.schedule(kind,'release')
            self.assertEqual(vectors.query('actual'),'actual-query')
            self.assertEqual(record['vector_background_suppression']['calls'],4)
            self.assertEqual(scheduled,[])
        self.assertIs(vectors.schedule,original)
        vectors.schedule('outside-case');self.assertEqual(scheduled,[('outside-case',)])
        self.assert_no_execution()

    def test_runtime_ready_requires_exact_two_profiles_and_counts_not_top_level_ready(self):
        self.prepared_fixture();config=runner.prepare(self.prepared_args())
        contract=config['prepared_public'];base=self.receipt['provider_status']
        runner.assert_prepared_provider(base,contract)
        mutations=[]
        for key,value in (('ready',False),('ready',1),('vector_count',22)):
            changed=deepcopy(base);changed['vector_index']['profiles'][0][key]=value;mutations.append(changed)
        changed=deepcopy(base);changed['vector_index']['profiles']=changed['vector_index']['profiles'][:1];mutations.append(changed)
        for changed in mutations:
            with self.assertRaisesRegex(ValueError,'prepared_runtime_profiles_not_ready'):
                runner.assert_prepared_provider(changed,contract)
        self.assert_no_execution()

    def test_retrieval_diagnostics_are_bounded_fixed_fields_without_raw_text_or_logits(self):
        profile=runner.PROFILE_NAMES[0]+':'+'a'*64
        raw={'mode':'hybrid','vector_used':True,'vector_candidate_count':22,
             'query':'must-never-log','translated_query':'must-never-log','raw_logit':42,
             'vector_branches':[{'profile_id':profile,'mode':'vector','candidate_count':22,'query':'must-never-log'}]*30,
             'reranking':{'used':True,'scored_count':True,'ranked_groups':['must-never-log'],
                          'answer_form_preference':{'used':1,'reason':'must-never-log'}}}
        diagnostic=runner.retrieval_diagnostic(raw)
        self.assertTrue(diagnostic['vector_used']);self.assertEqual(len(diagnostic['vector_branches']),8)
        self.assertIsNone(diagnostic['rerank']['scored_count']);self.assertFalse(diagnostic['answer_form']['used'])
        self.assertNotIn('must-never-log',json.dumps(diagnostic));self.assertNotIn('raw_logit',diagnostic)
        self.assertEqual(runner.retrieval_diagnostic({'structure_index':{'reason':'structure_index_missing'}})
                         ['structure']['reason'],'structure_index_missing')
        self.assert_no_execution()

    def test_vector_used_needs_bound_actual_profile_mode_and_nonempty_candidates(self):
        profile=runner.PROFILE_NAMES[0]+':'+'a'*64
        secondary=runner.PROFILE_NAMES[1]+':'+'b'*64
        contract={'profiles':[{'profile_id':profile},{'profile_id':secondary}]}
        good={'mode':'hybrid','vector_used':True,'vector_candidate_count':22,
              'vector_branches':[{'profile_id':profile,'mode':'vector'}]}
        self.assertTrue(runner.vector_runtime_observed([good],contract))
        for changed in ({**good,'vector_candidate_count':0},{**good,'vector_candidate_count':True},
                        {**good,'vector_used':1},{**good,'mode':'fts5'},
                        {**good,'vector_branches':[{'profile_id':'unbound','mode':'vector'}]},
                        {**good,'vector_branches':[{'profile_id':profile,'mode':'fts5'}]}):
            self.assertFalse(runner.vector_runtime_observed([changed],contract))
        self.assertFalse(runner.vector_runtime_observed([good],None))
        self.assert_no_execution()

    def test_dual_indexes_ready_is_separate_from_optional_alignment_query_usage(self):
        primary=runner.PROFILE_NAMES[0]+':'+'a'*64
        secondary=runner.PROFILE_NAMES[1]+':'+'b'*64
        contract={'profiles':[{'profile_id':primary},{'profile_id':secondary}]}
        ordinary={'mode':'hybrid','vector_used':True,'vector_candidate_count':22,
                  'vector_reason':None,
                  'vector_branches':[{'profile_id':primary,'mode':'vector','reason':None}]}
        self.assertTrue(runner.vector_runtime_observed([ordinary],contract))
        bilingual={**ordinary,'vector_branches':ordinary['vector_branches']+
                   [{'profile_id':secondary,'mode':'vector','reason':None}]}
        self.assertTrue(runner.vector_runtime_observed([bilingual],contract))
        for broken in ({**bilingual,'vector_reason':'vector_partial'},
                {**ordinary,'vector_branches':ordinary['vector_branches']+
                 [{'profile_id':secondary,'mode':'fts5','reason':'vector_inference_failed'}]},
                {**ordinary,'vector_branches':ordinary['vector_branches']+
                 [{'profile_id':None,'mode':'vector','reason':None}]}):
            self.assertFalse(runner.vector_runtime_observed([broken],contract))
        self.assertFalse(runner.vector_runtime_observed([ordinary],
                         {'profiles':[{'profile_id':primary}]}))
        # 请求前另行核验两个空间；secondary未ready不能以ordinary成功抵消。
        status={'public_release_id':'pub-synthetic','published_chunk_count':22,
                'vector_index':{'ready':True,'profiles':[
                    {'profile_id':primary,'ready':True,'dimension':384,'vector_count':23},
                    {'profile_id':secondary,'ready':False,'dimension':384,'vector_count':53}]}}
        bound={**contract,'release_id':'pub-synthetic','vector_manifests':[
            {'profile_id':primary,'vector_count':23},{'profile_id':secondary,'vector_count':53}]}
        with self.assertRaisesRegex(ValueError,'prepared_runtime_profiles_not_ready'):
            runner.assert_prepared_provider(status,bound)
        self.assert_no_execution()

    def test_native_preflight_hashes_core_extensions_without_creating_sessions(self):
        requirement=self.root/'local_knowledge/requirements-vector.txt'
        requirement.write_text('numpy==test\nonnxruntime==test\ntokenizers==test\n')
        extensions={}
        for name,filename in (('numpy','_multiarray_umath.test.so'),
                ('onnxruntime','onnxruntime_pybind11_state.test.so'),('tokenizers','tokenizers.test.so')):
            path=self.output/filename;path.write_bytes(b'synthetic-native-binary');extensions[name]=path
        inference=unittest.mock.Mock(side_effect=AssertionError('preflight_must_not_create_session'))
        ort=SimpleNamespace(InferenceSession=inference,get_available_providers=lambda:['CPUExecutionProvider'])
        def distribution(name):
            path=extensions[name]
            return SimpleNamespace(files=[Path(path.name)],locate_file=lambda item:path)
        with patch.dict(sys.modules,{'numpy':SimpleNamespace(),'onnxruntime':ort,'tokenizers':SimpleNamespace()}),\
                patch.object(runner.importlib.metadata,'version',return_value='test'),\
                patch.object(runner.importlib.metadata,'distribution',side_effect=distribution):
            runtime=runner.native_runtime()
        self.assertEqual(runtime['python'],str(Path(sys.executable).absolute()))
        self.assertEqual(set(runtime['extensions']),{'numpy','onnxruntime','tokenizers'})
        self.assertTrue(all(v['sha256']==runner.sha(v['path']) for v in runtime['extensions'].values()))
        self.assertEqual(runtime['model_sessions_created'],0);inference.assert_not_called()
        self.assert_no_execution()


    def test_consumer_source_binding_is_optional_but_requires_the_complete_triplet(self):
        self.assertIsNone(runner.consumer_source_contract(self.args()))
        args = self.consumer_source_fixture()
        names = ('consumer_source_root', 'consumer_source_manifest', 'consumer_source_manifest_sha256')
        for mask in range(1, 7):
            with self.subTest(mask=mask):
                partial = {name: getattr(args, name) for index, name in enumerate(names) if mask & (1 << index)}
                with self.assertRaisesRegex(ValueError, 'consumer_source_binding_required'):
                    runner.consumer_source_contract(self.args(**partial))
        contract = runner.consumer_source_contract(args)
        self.assertEqual(contract['source_root'], str(self.consumer_root))
        self.assertEqual(contract['manifest_path'], str(self.consumer_manifest))
        self.assertEqual(contract['manifest_sha256'], runner.sha(self.consumer_manifest))
        self.assertEqual(contract['source_sha256'], self.consumer_files)
        self.assertEqual(contract['project_root'], str(self.root))
        self.assertEqual(contract['trusted_helper']['path'], str(self.root / 'scripts/run_rag_acceptance.py'))
        self.assert_no_execution()

    def test_consumer_manifest_rejects_unsafe_paths_duplicate_keys_and_invalid_digests(self):
        self.consumer_source_fixture()
        for relative in (str(self.root / 'app.py'), '../app.py', 'local_knowledge/../../app.py',
                         'agent_runtime\\engine.py', 'agent_runtime//engine.py', './app.py', '', 'app.py/../app.py'):
            with self.subTest(relative=relative):
                sources = {**self.consumer_files, relative: self.consumer_files['app.py']}
                self.write_consumer_manifest({'source_sha256': sources})
                with self.assertRaisesRegex(ValueError, 'consumer_source_manifest_invalid'):
                    runner.consumer_source_contract(self.consumer_args())
        for digest in (None, 42, 'g' * 64, 'A' * 64, '0' * 63):
            with self.subTest(digest=digest):
                self.write_consumer_manifest({'source_sha256': {**self.consumer_files, 'app.py': digest}})
                with self.assertRaisesRegex(ValueError, 'consumer_source_manifest_invalid'):
                    runner.consumer_source_contract(self.consumer_args())
        repeated_path = '{"source_sha256":{"app.py":' + json.dumps(self.consumer_files['app.py']) + ',' + json.dumps(self.consumer_files)[1:] + '}'
        repeated_section = '{"source_sha256":{},"source_sha256":' + json.dumps(self.consumer_files) + '}'
        for value in (repeated_path, repeated_section):
            with self.subTest(duplicate=value):
                self.consumer_manifest.write_text(value)
                with self.assertRaisesRegex(ValueError, 'consumer_source_manifest_duplicate_key'):
                    runner.consumer_source_contract(self.consumer_args())
        for value in ({}, {'source_sha256': {}}, {'source_sha256': []}, []):
            with self.subTest(structure=value):
                self.write_consumer_manifest(value)
                with self.assertRaisesRegex(ValueError, 'consumer_source_manifest_invalid'):
                    runner.consumer_source_contract(self.consumer_args())
        self.assert_no_execution()

    def test_consumer_source_root_and_manifest_must_be_canonical_and_within_outputs(self):
        self.consumer_source_fixture()
        outside = self.root.parent / 'outside-source'; shutil.copytree(self.consumer_root, outside)
        outside_manifest = self.root.parent / 'outside-manifest.json'
        outside_manifest.write_bytes(self.consumer_manifest.read_bytes())
        variants = ({'consumer_source_root': outside},
                    {'consumer_source_manifest': outside_manifest, 'consumer_source_manifest_sha256': runner.sha(outside_manifest)},
                    {'consumer_source_root': self.consumer_root.relative_to(self.root)},
                    {'consumer_source_manifest': self.consumer_manifest.relative_to(self.root)},
                    {'consumer_source_root': self.root / 'outputs/../outputs/consumer-source-fixture/source'})
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runner.consumer_source_contract(self.consumer_args(**changes))
        root_link = self.output / 'source-link'; root_link.symlink_to(self.consumer_root, target_is_directory=True)
        manifest_link = self.output / 'manifest-link.json'; manifest_link.symlink_to(self.consumer_manifest)
        for changes in ({'consumer_source_root': root_link}, {'consumer_source_manifest': manifest_link}):
            with self.subTest(redirect=changes), self.assertRaisesRegex(ValueError, 'consumer_source_path_redirected_or_not_absolute'):
                runner.consumer_source_contract(self.consumer_args(**changes))
        self.assert_no_execution()

    def test_consumer_registered_source_files_and_parents_reject_same_byte_symlinks(self):
        self.consumer_source_fixture()
        external = self.root.parent / 'same-byte-consumer-app.py'
        path = self.consumer_root / 'app.py'; contents = path.read_bytes()
        external.write_bytes(contents); path.unlink(); path.symlink_to(external)
        try:
            self.assert_consumer_preflight_rejected(self.consumer_args(cloud=True, execute=True))
        finally:
            path.unlink(); path.write_bytes(contents)
        directory = self.consumer_root / 'agent_runtime'
        moved = self.root.parent / 'same-byte-consumer-directory'; directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        try:
            self.assert_consumer_preflight_rejected(self.consumer_args(cloud=True, execute=True))
        finally:
            directory.unlink(); moved.rename(directory)
        self.assert_no_execution()

    def test_consumer_manifest_hash_and_original_source_drift_block_paid_execution(self):
        self.consumer_source_fixture()
        original = self.consumer_manifest.read_bytes()
        args = self.consumer_args(cloud=True, execute=True)
        self.consumer_manifest.write_bytes(original + b'\n')
        self.assert_consumer_preflight_rejected(args, 'consumer_source_binding_invalid')
        self.consumer_manifest.write_bytes(original)
        path = self.consumer_root / 'app.py'; contents = path.read_bytes()
        for mutation in ('changed', 'missing'):
            with self.subTest(source=mutation):
                if mutation == 'changed': path.write_bytes(contents + b'changed')
                else: path.unlink()
                self.assert_consumer_preflight_rejected(self.consumer_args(cloud=True, execute=True))
                path.write_bytes(contents)
        self.assert_no_execution()

    def test_consumer_unregistered_runtime_files_are_rejected_before_freezing_or_dispatch(self):
        self.consumer_source_fixture()
        for index, relative in enumerate(('pickle.py', 'agent_runtime/unregistered.py',
                                          'local_knowledge/requirements-vector.txt', 'financial_data_service/unregistered.sql')):
            with self.subTest(relative=relative):
                path = self.consumer_root / relative
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'synthetic-unregistered-source')
                self.assert_consumer_preflight_rejected(
                    self.consumer_args(out_dir=self.output / ('unregistered-source-' + str(index)), cloud=True, execute=True),
                    'consumer_source_tree_not_registered_or_changed')
                path.unlink()
        self.assert_no_execution()

    def test_consumer_freeze_uses_trusted_helper_and_overlays_latest_missing_or_old_worker(self):
        self.consumer_source_fixture()
        worker = 'scripts/run_current_rag_acceptance.py'
        current = Path(runner.__file__).resolve()
        for present in (False, True):
            with self.subTest(original_worker_present=present):
                if present:
                    old = self.consumer_root / worker; old.write_bytes(b'raise AssertionError("old_worker_must_not_execute")\n')
                    self.consumer_files[worker] = runner.sha(old); self.write_consumer_manifest()
                args = self.consumer_args(out_dir=self.output / ('worker-overlay-' + str(present)))
                config = runner.prepare(args); candidate = Path(config['candidate'])
                contract = config['consumer_source_contract']
                self.assertEqual(contract['source_sha256'], self.consumer_files)
                self.assertEqual(contract['worker_overlay'], {'relative_path': worker, 'path': str(current),
                    'sha256': runner.sha(current), 'original_sha256': self.consumer_files.get(worker)})
                self.assertEqual(config['source_sha256'][worker], runner.sha(current))
                self.assertEqual((candidate / worker).read_bytes(), current.read_bytes())
                supports = contract['support_overlays']
                self.assertEqual(set(supports), {'scripts/run_rag_acceptance.py', 'scripts/run_skill_acceptance.py'})
                self.assertEqual(set(config['source_sha256']), set(self.consumer_files) | {worker} | set(supports))
                for relative, identity in supports.items():
                    self.assertEqual(identity['original_sha256'], self.consumer_files.get(relative))
                    self.assertEqual(config['source_sha256'][relative], identity['sha256'])
                    self.assertEqual((candidate / relative).read_bytes(), Path(identity['path']).read_bytes())
                self.assertEqual(supports['scripts/run_rag_acceptance.py']['sha256'],
                                 contract['trusted_helper']['sha256'])
                self.assertNotEqual((candidate / 'scripts/run_rag_acceptance.py').read_bytes(),
                                    (self.consumer_root / 'scripts/run_rag_acceptance.py').read_bytes())
                self.assertEqual((candidate / 'app.py').read_bytes(), (self.consumer_root / 'app.py').read_bytes())
                self.assertNotEqual((candidate / 'app.py').read_bytes(), (self.root / 'app.py').read_bytes())
                name = 'consumer-source-manifest.json'
                self.assertEqual(config['input_sha256'][name], runner.sha(self.consumer_manifest))
                self.assertEqual(config['input_bindings'][name],
                                 {'source_path': str(self.consumer_manifest), 'sha256': runner.sha(self.consumer_manifest)})
                self.assertEqual((args.out_dir / 'inputs' / name).read_bytes(), self.consumer_manifest.read_bytes())
                self.assertTrue(runner.worker_identity_unchanged(config))
                self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_consumer_prepared_receipt_checks_actual_registered_rag_source_not_formal_source(self):
        self.prepared_fixture(); self.consumer_source_fixture(requirements=True)
        local = {name: digest for name, digest in self.consumer_files.items() if name.startswith('local_knowledge/')}
        self.receipt['source_sha256_before'] = deepcopy(local)
        self.receipt['source_sha256_after'] = deepcopy(local); self.write_receipt()
        formal = self.root / 'local_knowledge/schema.sql'; formal.write_bytes(b'formal-source-is-deliberately-different')
        with patch.object(runner, 'native_runtime', return_value=self.native) as native:
            config = runner.prepare(self.consumer_prepared_args())
            native.assert_called_once_with(Path(config['candidate']))
        self.assertEqual(config['prepared_public']['source_sha256'], local)
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        path = self.consumer_root / 'local_knowledge/schema.sql'; path.write_bytes(path.read_bytes() + b'changed')
        self.consumer_files['local_knowledge/schema.sql'] = runner.sha(path); self.write_consumer_manifest()
        with self.assertRaisesRegex(ValueError, 'prepared_retrieval_source_changed'):
            runner.prepare(self.consumer_prepared_args(out_dir=self.output / 'consumer-rag-source-drift'))
        self.assert_no_execution()

    def test_consumer_prepared_receipt_requires_registered_origin_vector_requirements(self):
        self.prepared_fixture(); self.consumer_source_fixture()
        with patch.object(runner, 'native_runtime', side_effect=AssertionError('missing_requirements_must_stop_before_native')) as native:
            with self.assertRaisesRegex(ValueError, 'consumer_vector_requirements_missing_or_redirected'):
                runner.prepare(self.consumer_prepared_args())
            native.assert_not_called()
        requirements = self.consumer_root / 'local_knowledge/requirements-vector.txt'
        requirements.write_text('numpy==test\nonnxruntime==test\ntokenizers==test\n')
        self.assert_consumer_preflight_rejected(
            self.consumer_prepared_args(out_dir=self.output / 'unregistered-consumer-requirements', cloud=True, execute=True),
            'consumer_source_tree_not_registered_or_changed')
        self.assert_no_execution()

    def test_consumer_original_source_manifest_helper_and_current_worker_drift_fail_integrity(self):
        self.consumer_source_fixture(); config = runner.prepare(self.consumer_args())
        paths = (self.consumer_root / 'app.py', self.consumer_manifest,
                 Path(config['consumer_source_contract']['trusted_helper']['path']))
        for path in paths:
            with self.subTest(origin=path.name):
                contents = path.read_bytes(); path.write_bytes(contents + b'changed-original')
                checks = runner.hashes_unchanged(config)
                self.assertFalse(checks['original_inputs_unchanged'])
                self.assertTrue(all(value for key, value in checks.items() if key != 'original_inputs_unchanged'))
                path.write_bytes(contents)
        actual_sha = runner.sha; current = Path(runner.__file__).resolve()
        def changed_worker_digest(path):
            return '0' * 64 if Path(path).resolve() == current else actual_sha(path)
        with patch.object(runner, 'sha', side_effect=changed_worker_digest):
            checks = runner.hashes_unchanged(config)
            self.assertFalse(checks['original_inputs_unchanged'])
            self.assertTrue(all(value for key, value in checks.items() if key != 'original_inputs_unchanged'))
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_consumer_deleted_contract_fails_when_physical_or_registered_input_manifest_remains(self):
        self.consumer_source_fixture(); config = runner.prepare(self.consumer_args())
        name = 'consumer-source-manifest.json'; copied = Path(config['out_dir']) / 'inputs' / name
        contents = copied.read_bytes()
        for mutation in ('deleted', 'null', 'only-physical-signal', 'only-registered-signal'):
            with self.subTest(contract=mutation):
                changed = deepcopy(config)
                if mutation == 'null': changed['consumer_source_contract'] = None
                else: changed.pop('consumer_source_contract')
                if mutation == 'only-physical-signal':
                    changed['input_sha256'].pop(name); changed['input_bindings'].pop(name)
                if mutation == 'only-registered-signal': copied.unlink()
                try:
                    self.assertFalse(runner.hashes_unchanged(changed)['original_inputs_unchanged'])
                    with patch.object(runner, 'prepare', return_value=changed), redirect_stdout(io.StringIO()):
                        self.assertEqual(runner.run(self.consumer_args()), 2)
                    report = json.loads((Path(config['out_dir']) / 'results.json').read_text())
                    self.assertEqual(report['status'], 'PREFLIGHT_INTEGRITY_FAILED')
                    self.assertFalse(report['source_integrity']['original_inputs_unchanged'])
                finally:
                    copied.write_bytes(contents)
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_consumer_candidate_worker_tamper_is_rejected_before_spawning(self):
        self.consumer_source_fixture(); config = runner.prepare(self.consumer_args(cloud=True, execute=True))
        worker = Path(config['candidate']) / 'scripts/run_current_rag_acceptance.py'
        worker.write_bytes(worker.read_bytes() + b'changed-candidate-worker')
        self.assertFalse(runner.worker_identity_unchanged(config))
        self.assertFalse(runner.hashes_unchanged(config)['candidate_unchanged'])
        self.assert_consumer_dispatch_rejected(config)

    def test_consumer_registered_but_unapproved_budget_resources_do_not_replace_fixed_shas(self):
        self.consumer_source_fixture()
        for relative in runner.REQUEST_BUDGET_FILES:
            path = self.consumer_root / relative
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'unapproved-synthetic-budget-resource')
            self.consumer_files[relative] = runner.sha(path)
        self.write_consumer_manifest()
        with patch.object(runner, 'request_budget_runtime',
                          side_effect=AssertionError('unapproved_budget_must_not_load_runtime')) as runtime:
            self.assert_consumer_preflight_rejected(self.consumer_args(cloud=True, execute=True),
                                                  'request_budget_resource_missing_redirected_or_changed')
            runtime.assert_not_called()
        self.assert_no_execution()

    def test_consumer_origin_and_candidate_tree_reject_new_shadow_modules_and_same_byte_symlinks(self):
        self.consumer_source_fixture(); config = runner.prepare(self.consumer_args(cloud=True, execute=True))
        shadow = self.consumer_root / 'pickle.py'; shadow.write_bytes(b'raise AssertionError("shadow_module_must_not_load")\n')
        self.assertFalse(runner.hashes_unchanged(config)['original_inputs_unchanged'])
        self.assert_consumer_preflight_rejected(
            self.consumer_args(out_dir=self.output / 'origin-shadow-module', cloud=True, execute=True),
            'consumer_source_tree_not_registered_or_changed')
        shadow.unlink()
        shadow = Path(config['candidate']) / 'pickle.py'; shadow.write_bytes(b'raise AssertionError("shadow_module_must_not_load")\n')
        self.assertFalse(runner.hashes_unchanged(config)['candidate_unchanged'])
        self.assert_consumer_dispatch_rejected(config); shadow.unlink()
        external = self.root.parent / 'same-byte-business.py'
        for boundary, base in (('origin', self.consumer_root), ('candidate', Path(config['candidate']))):
            with self.subTest(boundary=boundary):
                path = base / 'agent_runtime/engine.py'; contents = path.read_bytes()
                external.write_bytes(contents); path.unlink(); path.symlink_to(external)
                try:
                    checks = runner.hashes_unchanged(config)
                    key = 'original_inputs_unchanged' if boundary == 'origin' else 'candidate_unchanged'
                    self.assertFalse(checks[key])
                    if boundary == 'origin':
                        self.assert_consumer_preflight_rejected(
                            self.consumer_args(out_dir=self.output / 'origin-symlink-business', cloud=True, execute=True))
                    else:
                        self.assert_consumer_dispatch_rejected(config)
                finally:
                    path.unlink(); path.write_bytes(contents)
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_consumer_combined_manifest_entry_and_candidate_file_deletion_cannot_hide_drift(self):
        self.consumer_source_fixture(); config = runner.prepare(self.consumer_args(cloud=True, execute=True))
        changed = deepcopy(config)
        relative = 'agent_runtime/engine.py'
        changed['source_sha256'].pop(relative)
        path = Path(config['candidate']) / relative; path.unlink()
        self.assertFalse(runner.execution_manifest_unchanged(changed))
        self.assertFalse(runner.hashes_unchanged(changed)['candidate_unchanged'])
        self.assert_consumer_dispatch_rejected(changed)

    def test_consumer_frozen_input_manifest_same_byte_symlink_still_blocks_dispatch(self):
        self.consumer_source_fixture(); config = runner.prepare(self.consumer_args(cloud=True, execute=True))
        name = 'consumer-source-manifest.json'; copied = Path(config['out_dir']) / 'inputs' / name
        external = self.root.parent / 'identical-consumer-input-manifest.json'
        contents = copied.read_bytes(); external.write_bytes(contents)
        copied.unlink(); copied.symlink_to(external)
        try:
            self.assertEqual(runner.sha(copied), config['input_sha256'][name])
            self.assertFalse(runner.hashes_unchanged(config)['original_inputs_unchanged'])
            self.assert_consumer_dispatch_rejected(config)
        finally:
            copied.unlink(); copied.write_bytes(contents)
        self.assertTrue(all(runner.hashes_unchanged(config).values()))
        self.assert_no_execution()

    def test_consumer_foreign_control_package_entries_are_rejected_even_if_registered(self):
        self.consumer_source_fixture()
        for index, relative in enumerate(('scripts.py', 'scripts/__init__.py')):
            with self.subTest(relative=relative):
                path = self.consumer_root / relative
                path.write_bytes(b'raise AssertionError("foreign_control_package_must_not_load")\n')
                self.consumer_files[relative] = runner.sha(path); self.write_consumer_manifest()
                self.assert_consumer_preflight_rejected(
                    self.consumer_args(out_dir=self.output / ('foreign-control-package-' + str(index)), cloud=True, execute=True),
                    'consumer_control_package_conflict')
                path.unlink(); self.consumer_files.pop(relative); self.write_consumer_manifest()
        self.assert_no_execution()

    def test_consumer_prepared_receipt_cannot_register_rag_source_that_was_not_frozen(self):
        self.prepared_fixture(); self.consumer_source_fixture(requirements=True)
        relative = 'local_knowledge/origin-only.bak'
        path = self.consumer_root / relative; path.write_bytes(b'not-consumed-by-the-registered-freezer')
        self.consumer_files[relative] = runner.sha(path); self.write_consumer_manifest()
        local = {name: digest for name, digest in self.consumer_files.items() if name.startswith('local_knowledge/')}
        self.receipt['source_sha256_before'] = deepcopy(local)
        self.receipt['source_sha256_after'] = deepcopy(local); self.write_receipt()
        with self.assertRaisesRegex(ValueError, 'prepared_retrieval_source_(changed|not_frozen)'):
            runner.prepare(self.consumer_prepared_args())
        self.assertFalse((self.output / 'run-01/candidate' / relative).exists())
        self.assert_no_execution()


if __name__ == '__main__':
    unittest.main()
