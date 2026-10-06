#!/usr/bin/env python3
"""本地 RAG 验收：默认使用临时合成夹具；现有公共库选项严格只读。

模型适配器为本地协议桩，知识消费、FTS5、图路径、版本和权限使用真实实现。
JSON 报告只含状态、计数及固定错误码，不包含个人正文、确认凭据或加密密钥。
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_knowledge import LocalKnowledgeProvider

SCHEMA_VERSION = 1
PUBLIC_TABLES = {'sources', 'products', 'document_versions', 'chunks', 'releases',
                 'release_chunks', 'current_release', 'graph_entities',
                 'graph_relations', 'graph_provenance'}
PRIVATE_TABLES = {'workspaces', 'user_memories', 'agent_dialogues', 'personal_decisions'}


class VerificationFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise VerificationFailure(code)


def safe_code(error):
    code = getattr(error, 'code', None)
    if not isinstance(code, str):
        code = str(error) if isinstance(error, VerificationFailure) else type(error).__name__
    return code if re.fullmatch(r'[A-Za-z0-9_]{1,80}', code) else type(error).__name__


def denied(action, codes=None):
    from simulated_bank import BankError
    from local_knowledge import KnowledgeError
    from agent_runtime.data_boundary import BoundaryError
    try:
        action()
    except (BankError, KnowledgeError, BoundaryError, ValueError) as error:
        code = safe_code(error)
        require(codes is None or code in codes, 'unexpected_denial_code')
        return code
    raise VerificationFailure('expected_denial_missing')


def native_call(name, arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': 'fixture-call-' + name, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)},
    }]}


def last_tool(messages):
    return json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))


class ProtocolPrimary:
    """只驱动现有工具协议；不联网、不推导金融事实。"""
    model_id = 'local-smoke-protocol-fixture'

    def __init__(self, *steps):
        self.steps = list(steps)
        self.messages = []

    def complete(self, messages, tools, **options):
        self.messages.append(json.loads(json.dumps(messages)))
        require(bool(self.steps), 'unexpected_model_round')
        step = self.steps.pop(0)
        return (step(messages, tools) if callable(step) else step), {'total_tokens': 1}


class ProtocolReviewer:
    """仅验证审核接线，approve 不代表真实模型语义评估。"""
    model_id = 'local-smoke-review-fixture'

    def __init__(self, hook=None):
        self.packets = []
        self.hook = hook

    def review(self, packet, *options):
        self.packets.append(packet)
        if self.hook:
            self.hook(packet)
        return {'verdict': 'approve', 'reason': '本地协议验收夹具', 'issues': []}


def public_reply(messages, tools):
    result = last_tool(messages)
    return native_call('answer_reviewed', {
        'text': '合成条款披露可能损失本金，当前在售状态仍需核实。',
        'claims': [{'text': '可能损失本金来自本轮条款证据。',
                    'evidence_ids': [result['evidence_id']]}],
        'draft_ids': [], 'followups': [],
    })


def fact_reply(messages, tools):
    output = last_tool(messages)
    ids = [f['id'] for f in output['facts'] if '已结算余额' in f['text']]
    require(bool(ids), 'account_fact_missing')
    return native_call('answer_facts', {'fact_ids': ids})


@contextmanager
def fixture_environment(root):
    from cryptography.fernet import Fernet
    from unittest.mock import patch
    names = ('NAVREN_PERSONAL_DATA_KEY', 'NAVREN_KNOWLEDGE_DB')
    previous = {name: os.environ.get(name) for name in names}
    key = Fernet.generate_key().decode()
    os.environ['NAVREN_PERSONAL_DATA_KEY'] = key
    os.environ['NAVREN_KNOWLEDGE_DB'] = str(root / 'unused-public.sqlite3')
    def forbid_network(*arguments, **options):
        raise VerificationFailure('unexpected_network_request')
    try:
        with patch('urllib.request.urlopen', forbid_network), patch('socket.create_connection', forbid_network), \
             patch('socket.socket.connect', forbid_network), patch('socket.socket.connect_ex', forbid_network):
            yield key
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def publish_fixture(gateway, as_of, suffix='A', effective_to=None):
    provider = gateway.knowledge_provider
    pid, share, channel = 'SMOKE-P-' + suffix, 'SMOKE-S-' + suffix, 'SMOKE-C-' + suffix
    provider.register_source('isolated-smoke', publisher='合成验收来源',
        endpoints=['https://official.example/'],
        freshness={'risk': None, 'fees': None, 'liquidity': None, 'terms': None},
        permission_basis='isolated_synthetic_fixture_only')
    provider.register_product(pid, issuer='SMOKE-ISSUER', scheme='fixture', code=pid,
                              name='合成验收产品' + suffix, verified=True)
    provider.register_share(share, product_id=pid, scheme='fixture', code=share, currency='CNY')
    provider.register_channel(channel, name='合成验收渠道' + suffix)
    # 保留独立段落，费用、赎回、风险各自拥有原文跨度。
    paragraphs = [('fees', '合成费用条款：费率为0.10%。'),
                  ('liquidity', '合成赎回条款：申请后两个工作日到账。'),
                  ('risk', '合成风险条款：合成机构甲发行并管理，净值可能波动，可能损失本金。')]
    text = '\n'.join(value for _, value in paragraphs)
    spans, start = [], 0
    for number, (facet, paragraph) in enumerate(paragraphs, 1):
        spans.append({'start_char': start, 'end_char': start + len(paragraph),
                      'page': 1, 'paragraph': '合成条款/' + str(number), 'facets': [facet]})
        start += len(paragraph) + 1
    version = provider.stage_document(source_id='isolated-smoke',
        source_url='https://official.example/smoke-' + suffix,
        title='合成验收产品条款', text=text, published_on=as_of, retrieved_on=as_of,
        effective_from=as_of,
        effective_to=effective_to or (date.fromisoformat(as_of) + timedelta(days=7)).isoformat(),
        product_id=pid, share_class_id=share, channel_id=channel,
        facets=['fees', 'liquidity', 'risk'], spans=spans)
    provider.approve_version(version, verification_basis='isolated_fixture_verified')
    release = provider.publish()
    return {'version': version, 'release': release, 'product_id': pid,
            'share_class_id': share, 'channel_id': channel}


def search(provider, fixture, as_of, facets=None):
    from agent_runtime.knowledge_retrieval import KnowledgeRetrieval
    return KnowledgeRetrieval(provider).search('费用和赎回风险',
        product_id=fixture['product_id'], share_class_id=fixture['share_class_id'],
        channel_id=fixture['channel_id'], as_of=as_of, facets=facets or ['fees', 'liquidity', 'risk'])


def bind_speech_versions(gateway, owner, result):
    from user_memory import privacy_revision
    with closing(gateway.bank.connect()) as db:
        result['privacy_revision'] = privacy_revision(db, owner)
    result['personal_revision'] = gateway.personal_data.revision(owner)
    return gateway.approve_speech(owner, result)


def fixture_checks(root, as_of, key):
    from finance_gateway import FinanceGateway
    from agent_runtime.conversation import ConversationAgent
    from agent_runtime.bank_tools import BankTools
    from agent_runtime.data_boundary import CloudDataBoundary
    from agent_runtime.knowledge_retrieval import KnowledgeRetrieval

    def gateway(name):
        os.environ['NAVREN_KNOWLEDGE_DB'] = str(root / (name + '.public.sqlite3'))
        g = FinanceGateway(root / (name + '.bank.sqlite3'))
        owner, _ = g.create_session()
        return g, owner

    def consumed():
        g, owner = gateway('consumption')
        fixture = publish_fixture(g, as_of)
        data = search(g.knowledge_provider, fixture, as_of)
        require(len(data['hits']) == 3, 'facet_coverage_missing')
        with g.knowledge_provider.db() as db:
            for hit in data['hits']:
                original = db.execute('SELECT parsed_text FROM document_versions WHERE id=?',
                                      (hit['document_version_id'],)).fetchone()[0]
                loc = hit['locator']
                require(original[loc['start_char']:loc['end_char']] == hit['excerpt'], 'quote_span_mismatch')
            count = db.execute('SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?', ('"赎回"',)).fetchone()[0]
        trace = g.knowledge_provider.graph_trace(data['hits'][0]['citation_id'], fixture['release'], as_of)
        require(bool(trace['paths']), 'graph_paths_missing')
        primary = ProtocolPrimary(native_call('knowledge_search', {'query': '风险',
            'product_id': fixture['product_id'], 'facets': ['risk']}), public_reply)
        reviewer = ProtocolReviewer()
        result = ConversationAgent(primary, reviewer).process(g, owner, '请研究合成验收产品A的风险')
        require(not result.get('isFallback') and result.get('knowledge_snapshot'), 'consumer_runtime_failed')
        require(bool(reviewer.packets[0]['draft']['claims'][0]['evidence_ids']), 'review_citation_missing')
        require(g.personal_data.database != g.knowledge_provider.path != g.bank.database, 'database_isolation_failed')
        return {'retrieved_spans': len(data['hits']), 'fts_matches': count,
                'graph_paths': len(trace['paths']), 'runtime_tool_calls': result['agent_runtime']['tool_calls'],
                'scripted_review_calls': len(reviewer.packets), 'independent_database_files': True}

    def missing_path():
        g, _ = gateway('missing-path')
        fixture = publish_fixture(g, as_of)
        hit = search(g.knowledge_provider, fixture, as_of, ['risk'])['hits'][0]
        with g.knowledge_provider.db() as db:
            relation = db.execute("SELECT id FROM graph_relations WHERE citation_id=? AND predicate='quoted_in'",
                                  (hit['citation_id'],)).fetchone()[0]
        g.knowledge_provider.withdraw_relation(relation)
        with g.knowledge_provider.db() as db:
            lexical_count = db.execute('SELECT count(*) FROM chunks_fts').fetchone()[0]
        rejected = search(g.knowledge_provider, fixture, as_of, ['risk'])
        require(lexical_count > 0 and not rejected['hits'], 'missing_graph_path_admitted')
        require(g.knowledge_provider.resolve_public_evidence(hit['citation_id'], fixture['release'], as_of) is None,
                'missing_graph_path_resolved')
        return {'lexical_rows_remaining': lexical_count, 'admitted_after_missing_path': 0,
                'citation_recheck_blocked': True}

    def withdrawn():
        g, _ = gateway('withdrawn')
        fixture = publish_fixture(g, as_of)
        consumer = KnowledgeRetrieval(g.knowledge_provider)
        data = consumer.search('风险', product_id=fixture['product_id'], as_of=as_of, facets=['risk'])
        require(bool(data['hits']), 'withdrawal_setup_missing')
        g.knowledge_provider.withdraw(fixture['version'])
        denied(consumer.assert_fresh)
        require(not search(g.knowledge_provider, fixture, as_of)['hits'], 'withdrawn_version_admitted')
        return {'pinned_consumer_invalidated': True, 'withdrawn_hit_count': 0}

    def time_and_identity():
        g, _ = gateway('time-identity')
        fixture = publish_fixture(g, as_of)
        for offset in (-1, 7):
            day = (date.fromisoformat(as_of) + timedelta(days=offset)).isoformat()
            require(not search(g.knowledge_provider, fixture, day)['hits'], 'invalid_time_admitted')
        g.knowledge_provider.register_share('SMOKE-S-C', product_id=fixture['product_id'],
                                             scheme='fixture', code='SMOKE-S-C', currency='CNY')
        mismatch = {**fixture, 'share_class_id': 'SMOKE-S-C'}
        require(not search(g.knowledge_provider, mismatch, as_of)['hits'], 'wrong_share_admitted')
        require(bool(search(g.knowledge_provider, fixture, as_of)['hits']), 'valid_identity_rejected')
        return {'future_publication_blocked': True, 'expired_interval_blocked': True,
                'different_share_blocked': True, 'valid_identity_admitted': True}

    def same_institution():
        g, _ = gateway('institution')
        a, b = publish_fixture(g, as_of, 'A'), publish_fixture(g, as_of, 'B')
        relations = []
        for item in (a, b):
            hit = search(g.knowledge_provider, item, as_of, ['risk'])['hits'][0]
            loc = hit['locator']
            relations.append(g.knowledge_provider.add_reviewed_relation(subject_kind='product',
                subject_ref=item['product_id'], predicate='issued_by', object_kind='institution',
                object_ref='SMOKE-INSTITUTION', object_label='合成机构甲', citation_id=hit['citation_id'],
                reviewed_by='isolated_fixture_human_binding',
                start_char=loc['start_char'], end_char=loc['end_char']))
        release = g.knowledge_provider.status()['public_release_id']
        peers = g.knowledge_provider.related_products(a['product_id'], release, as_of)
        require(len(peers) == 1 and len(peers[0]['path']) == 2, 'two_hop_relation_missing')
        require(len({edge['citation_id'] for edge in peers[0]['path']}) == 2, 'peer_evidence_reused')
        g.knowledge_provider.withdraw_relation(relations[1])
        require(not g.knowledge_provider.related_products(a['product_id'], release, as_of), 'withdrawn_peer_relation_admitted')
        return {'peer_products': 1, 'independent_citations': 2, 'graph_hops': 2,
                'withdrawn_peer_relation_blocked': True}

    def personal_confirmation():
        g, owner = gateway('personal')
        other, _ = g.create_session()
        g.memories.set_enabled(owner, {'enabled': True, 'expected_revision': 0})
        proposal = g.personal_data.propose_candidates(owner, '我的长期目标是明年准备进修学费', 'smoke-source')[0]
        proof = {name: proposal[name] for name in ('proposal_id', 'source_ref', 'confirmation_token', 'expected_revision')}
        require(not g.memories.state(owner)['items'], 'candidate_auto_confirmed')
        denied(lambda: g.memories.create(other, proof))
        denied(lambda: g.memories.create(owner, {**proof, 'confirmation_token': '0' * 32}))
        denied(lambda: g.memories.create(owner, {**proof, 'value': '客户端改写'}))
        saved = g.memories.create(owner, proof)
        require(saved['personal_revision'] == proposal['expected_revision'] + 1, 'confirmation_revision_missing')
        denied(lambda: g.memories.create(owner, proof))
        require(not g.memories.state(other)['items'], 'cross_owner_read')
        require(g.personal_data.database.stat().st_mode & 0o777 == 0o600, 'private_file_permission')
        raw = g.personal_data.database.read_bytes()
        require(proposal['value'].encode() not in raw and key.encode() not in raw, 'private_plaintext_at_rest')
        with closing(g.bank.connect()) as db:
            require(db.execute('SELECT count(*) FROM user_memories').fetchone()[0] == 0, 'bank_personal_body_remaining')
        node = saved['items'][0]['node_id']
        g.memories.delete(owner, node, {'expected_revision': saved['personal_revision']})
        require(not g.personal_data.rows(owner, 'conversation_sources'), 'withdrawn_source_remaining')
        require(not g.personal_data.rows(owner, 'memory_candidates'), 'withdrawn_candidate_remaining')
        return {'candidate_auto_confirmed': False, 'cross_owner_confirmation_blocked': True,
                'forged_confirmation_blocked': True, 'replay_blocked': True,
                'ciphertext_at_rest': True, 'file_mode_0600': True, 'withdrawn_source_removed': True}

    def concurrent_versions():
        g, owner = gateway('concurrency')
        barrier, outcomes = threading.Barrier(2), []
        expected = g.personal_data.revision(owner)
        def write():
            barrier.wait(timeout=5)
            try:
                g.memories.create(owner, {'category': 'goal', 'value': '一年后准备进修', 'expected_revision': expected})
                outcomes.append('committed')
            except Exception as error:
                outcomes.append(safe_code(error))
        threads = [threading.Thread(target=write, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        require(not any(thread.is_alive() for thread in threads), 'concurrent_write_timeout')
        require(sorted(outcomes) == ['PERSONAL_VERSION_CONFLICT', 'committed'], 'concurrent_overwrite')
        return {'committed_writers': 1, 'version_conflicts': 1}

    def lifecycle():
        g, owner = gateway('lifecycle')
        g.memories.set_enabled(owner, {'enabled': True})
        before = g.bank.customer_archive(owner)
        tools = BankTools(g, owner, 'smoke-loop')
        candidate = g.personal_graph.remember(owner, '我的风险等级是R5', 'smoke-profile',
                                             {'category': 'wealth', 'quote': '我的风险等级是R5'})
        tools.fresh()
        g.personal_graph.update(owner, candidate['observation_id'], {'decision': 'confirm',
            'confirmation_token': candidate['confirmation_token'], 'expected_revision': candidate['expected_revision']})
        denied(tools.fresh, {'STALE_EVIDENCE'})
        after = g.bank.customer_archive(owner)
        require(before['profile'] == after['profile'] and before['accounts'] == after['accounts'], 'personal_statement_changed_bank')
        revision = g.personal_data.revision(owner)
        g.personal_data.save_history(owner, [{'role': 'assistant', 'content': '已核实的本地测试历史'}], expected_revision=revision)
        g.personal_data.save_widgets(owner, 'smoke-view', [{'kind': 'fixture'}], expected_revision=revision)
        g.memories.set_enabled(owner, {'enabled': False, 'expected_revision': revision})
        require(not g.personal_data.read_history(owner) and not g.personal_data.read_widgets(owner), 'disabled_context_remaining')
        require(not g.personal_data.save_history(owner, [], expected_revision=revision), 'stale_history_saved')
        require(not any(n.get('observation_id') for n in g.personal_graph.read(owner, for_agent=True)['nodes']), 'disabled_profile_exposed')
        return {'candidate_keeps_current_context': True, 'confirmed_change_invalidates_context': True,
                'formal_risk_and_balances_unchanged': True, 'disabled_history_and_widgets_cleared': True}

    def cloud_projection():
        g, owner = gateway('projection')
        account = g.overview(owner)['accounts'][0]['id']
        g.personal_data.save_history(owner, [{'role': 'assistant', 'content': '上次查询账户 ' + account}])
        primary, reviewer = ProtocolPrimary(native_call('account_summary', {}), fact_reply), ProtocolReviewer()
        result = ConversationAgent(primary, reviewer).process(g, owner,
            '我叫张三，邮箱fixture.private@example.com，手机号13812345678，查询账户 ' + account)
        require(not result.get('isFallback'), 'projection_runtime_failed')
        payload = json.dumps(primary.messages, ensure_ascii=False)
        for marker in ('张三', 'fixture.private@example.com', '13812345678', owner, account):
            require(marker not in payload, 'private_marker_sent')
        boundary = CloudDataBoundary()
        denied(lambda: boundary.public_request({'query': '我的账户余额100元'}), {'BOUNDARY_PUBLIC_QUERY'})
        require('张三' not in result['_approved_tts_text'], 'private_tts_marker')
        require(key.encode() not in g.personal_data.database.read_bytes(), 'key_in_database')
        return {'model_protocol_calls': len(primary.messages), 'known_identifiers_removed': True,
                'credentials_and_contact_removed': True, 'private_public_query_blocked': True,
                'tts_projection_checked': True}

    def revocation_before_next_call():
        g, owner = gateway('revocation')
        g.memories.set_enabled(owner, {'enabled': True})
        g.memories.create(owner, {'category': 'goal', 'value': '我的长期目标是明年进修'})
        primary = ProtocolPrimary(native_call('user_memory', {}), native_call('answer_reviewed',
            {'text': '不应发送的后续协议桩', 'claims': [], 'draft_ids': [], 'followups': []}))
        def revoke(event):
            if event.get('kind') == 'tool_end' and event.get('status') == 'completed':
                g.memories.set_enabled(owner, {'enabled': False})
        result = ConversationAgent(primary, ProtocolReviewer()).process(g, owner, '我的进修目标', on_event=revoke)
        require(len(primary.messages) == 1 and result.get('isFallback'), 'revoked_memory_sent_later')
        require(result['agent_runtime']['failure'] == 'STALE_EVIDENCE', 'revocation_failure_not_reported')
        return {'model_calls': 1, 'subsequent_model_call_blocked': True}

    def speech_authority():
        g, owner = gateway('speech')
        primary = ProtocolPrimary(native_call('account_summary', {}), fact_reply)
        result = ConversationAgent(primary, ProtocolReviewer()).process(g, owner, '查询账户余额')
        receipt = bind_speech_versions(g, owner, result)
        require(isinstance(receipt, str) and bool(g.resolve_speech(owner, receipt)), 'approved_speech_missing')
        other, _ = g.create_session()
        denied(lambda: g.resolve_speech(other, receipt), {'SPEECH_RECEIPT_INVALID'})
        denied(lambda: g.resolve_speech(owner, 'forged-receipt'), {'SPEECH_RECEIPT_INVALID'})
        require(g.approve_speech(owner, {'reply': '未经核验的夹具', 'compliance_pass': False}) is None, 'unreviewed_speech_admitted')
        g.bank.advance_days(owner, 1)
        denied(lambda: g.resolve_speech(owner, receipt), {'STALE_EVIDENCE'})
        return {'approved_speech_admitted': True, 'cross_owner_and_forged_receipts_blocked': True,
                'unreviewed_speech_blocked': True, 'bank_change_blocks_old_speech': True}

    def public_speech_and_review_invalidation():
        g, owner = gateway('public-speech')
        fixture = publish_fixture(g, as_of)
        make_primary = lambda: ProtocolPrimary(native_call('knowledge_search',
            {'query': '风险', 'product_id': fixture['product_id'], 'facets': ['risk']}), public_reply)
        result = ConversationAgent(make_primary(), ProtocolReviewer()).process(g, owner, '合成产品风险')
        receipt = bind_speech_versions(g, owner, result)
        require(bool(receipt), 'public_speech_setup_missing')
        g.knowledge_provider.withdraw(fixture['version'])
        denied(lambda: g.resolve_speech(owner, receipt))
        g2, owner2 = gateway('review-withdrawal')
        fixture2 = publish_fixture(g2, as_of)
        primary = ProtocolPrimary(native_call('knowledge_search',
            {'query': '风险', 'product_id': fixture2['product_id'], 'facets': ['risk']}), public_reply)
        reviewer = ProtocolReviewer(lambda packet: g2.knowledge_provider.withdraw(fixture2['version']))
        stopped = ConversationAgent(primary, reviewer).process(g2, owner2, '合成产品风险')
        require(stopped.get('isFallback') and stopped['agent_runtime']['failure'] == 'STALE_KNOWLEDGE_EVIDENCE',
                'withdrawn_review_released')
        require(not stopped.get('bank_actions'), 'withdrawn_review_operation_released')
        return {'withdrawn_citation_blocks_old_speech': True, 'withdrawal_during_review_blocks_result': True,
                'operations_released_after_withdrawal': 0}

    return [('public_consumer_and_exact_spans', consumed), ('missing_graph_path_blocks_fts_hit', missing_path),
            ('withdrawn_version_invalidates_consumer', withdrawn), ('dates_and_share_binding', time_and_identity),
            ('reviewed_two_hop_institution_path', same_institution), ('personal_confirmation_and_ciphertext', personal_confirmation),
            ('concurrent_personal_versions', concurrent_versions), ('personal_lifecycle_and_bank_authority', lifecycle),
            ('cloud_and_tts_projection', cloud_projection), ('revocation_blocks_next_model_call', revocation_before_next_call),
            ('speech_receipt_owner_and_bank_versions', speech_authority),
            ('public_withdrawal_blocks_speech_and_review', public_speech_and_review_invalidation)]


class ReadOnlyKnowledgeProvider(LocalKnowledgeProvider):
    """跳过提供器初始化和 chmod；现有文件不创建表、不发布、不补充。"""
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.fixed_release = None
        require(self.path.is_file(), 'knowledge_database_missing')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        try:
            yield db
        finally:
            db.close()


def public_database_checks(path, as_of):
    provider = ReadOnlyKnowledgeProvider(path)
    def schema():
        with provider.db() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require(not PRIVATE_TABLES & tables, 'personal_database_forbidden')
        require(PUBLIC_TABLES <= tables, 'public_schema_missing')
        return {'read_only': True, 'public_schema_present': True, 'personal_tables_present': False}

    def inventory():
        schema()
        state = provider.status()
        with provider.db() as db:
            counts = {table + '_count': db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                      for table in ('sources', 'products', 'document_versions', 'chunks', 'graph_entities', 'graph_relations')}
        return {**counts, 'ready': state['ready'], 'graph_ready': state['graph_ready'],
                'published_chunk_count': state['published_chunk_count'],
                'published_evidenced_relation_count': state['published_evidenced_relation_count']}

    def availability():
        schema()
        state = provider.status()
        reasons, current, paths_checked, paths_valid, unknown_bounds = {}, 0, 0, 0, 0
        if state['ready']:
            with provider.db() as db:
                rows = provider._rows(db, state['public_release_id'])
                checked = {}
                from local_knowledge.graph import paths
                for row in rows:
                    unknown_bounds += row['effective_from'] is None or row['effective_to'] is None
                    facets, reason = provider._available(db, row, as_of, checked)
                    if not facets:
                        reason = reason or 'evidence_unavailable'
                        reasons[reason] = reasons.get(reason, 0) + 1
                        continue
                    current += 1
                    if paths_checked < 64:
                        paths_checked += 1
                        paths_valid += paths(db, row, as_of, facets=facets) is not None
        return {'available_spans_before_graph_gate': current, 'unavailable_reasons': reasons,
                'spans_with_unknown_effective_bounds': unknown_bounds,
                'graph_paths_sampled': paths_checked, 'graph_valid_paths_sampled': paths_valid,
                'graph_invalid_paths_sampled': paths_checked - paths_valid,
                'graph_sample_limit': 64, 'complete_graph_coverage_claimed': False}
    return [('readonly_public_schema', schema), ('readonly_public_inventory', inventory),
            ('readonly_effectivity_and_graph_sample', availability)]


def validate_report(report):
    require(set(report) == {'schema_version', 'run_id', 'created_at', 'as_of', 'mode', 'data_origin',
                           'model_verification', 'checks', 'summary', 'validation', 'status'}, 'report_schema_keys')
    require(type(report['schema_version']) is int and report['schema_version'] == SCHEMA_VERSION, 'report_schema_version')
    require(report['mode'] in {'isolated_fixture', 'existing_public_readonly'}, 'report_mode')
    require(report['data_origin'] == ('synthetic_fixture' if report['mode'] == 'isolated_fixture' else 'existing_public_database'), 'report_origin')
    require(report['model_verification']['cloud_calls'] == 0, 'report_cloud_claim')
    require(isinstance(report['checks'], list) and bool(report['checks']), 'report_checks_missing')
    seen = set()
    for check in report['checks']:
        require(set(check) <= {'id', 'status', 'duration_ms', 'metrics', 'error_code'}, 'report_check_keys')
        require(check['id'] not in seen and re.fullmatch(r'[a-z0-9_]+', check['id']), 'report_check_id')
        seen.add(check['id'])
        require(check['status'] in {'passed', 'failed'}, 'report_check_status')
        require(type(check['duration_ms']) is int and check['duration_ms'] >= 0, 'report_check_duration')
        require(('metrics' in check) == (check['status'] == 'passed') and
                ('error_code' in check) == (check['status'] == 'failed'), 'report_check_payload')
        if 'metrics' in check:
            def metric(value):
                if isinstance(value, dict):
                    require(all(isinstance(k, str) and re.fullmatch(r'[a-z0-9_]+', k) for k in value), 'metric_keys')
                    for item in value.values():
                        metric(item)
                else:
                    require(type(value) in (bool, int, float), 'metric_value_type')
            require(isinstance(check['metrics'], dict), 'report_metrics_type')
            metric(check['metrics'])
    passed = sum(c['status'] == 'passed' for c in report['checks'])
    failed = len(report['checks']) - passed
    require(report['summary'] == {'total': len(report['checks']), 'passed': passed, 'failed': failed}, 'report_summary')
    require(report['status'] == ('passed' if failed == 0 else 'failed'), 'report_status')
    # 报告不得顺带序列化测试载荷或凭据。
    forbidden = {'password', 'secret', 'encryption_key', 'confirmation_token', 'messages', 'packets',
                 'quote', 'excerpt', 'raw_text', 'personal_text', 'speech_receipt'}
    def walk(value):
        if isinstance(value, dict):
            require(not forbidden & set(value), 'sensitive_report_field')
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        else:
            require(value is None or type(value) in (str, bool, int, float), 'report_value_type')
    walk(report)
    date.fromisoformat(report['as_of'])
    uuid.UUID(report['run_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--knowledge-database', help='严格只读检查现有公共 SQLite；不构建个人库')
    parser.add_argument('--output', help='保存 JSON 报告并读回核验；省略时仅输出 JSON')
    args = parser.parse_args()
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    mode = 'existing_public_readonly' if args.knowledge_database else 'isolated_fixture'
    report = {'schema_version': SCHEMA_VERSION, 'run_id': str(uuid.uuid4()), 'created_at': now.isoformat(),
        'as_of': now.date().isoformat(), 'mode': mode,
        'data_origin': 'existing_public_database' if args.knowledge_database else 'synthetic_fixture',
        'model_verification': {'cloud_calls': 0, 'protocol_fixtures_only': not bool(args.knowledge_database),
                               'real_model_quality_evaluated': False, 'cloud_tts_provider_called': False},
        'checks': [], 'summary': {}, 'validation': {'schema_checked': False, 'json_round_trip': False,
                                                   'file_readback_verified': False}, 'status': 'failed'}
    def run(checks):
        for name, action in checks:
            start = time.monotonic()
            item = {'id': name, 'status': 'failed'}
            try:
                item['metrics'] = action()
                item['status'] = 'passed'
            except Exception as error:
                item['error_code'] = safe_code(error)
            item['duration_ms'] = round((time.monotonic() - start) * 1000)
            report['checks'].append(item)
    try:
        if args.knowledge_database:
            run(public_database_checks(args.knowledge_database, report['as_of']))
        else:
            with tempfile.TemporaryDirectory(prefix='navren-rag-smoke-') as temporary:
                root = Path(temporary)
                with fixture_environment(root) as key:
                    run(fixture_checks(root, report['as_of'], key))
                    serialized = json.dumps(report, ensure_ascii=False)
                    require(key not in serialized, 'secret_in_report')
                    for marker in ('张三', 'fixture.private@example.com', '13812345678', '我的长期目标是'):
                        require(marker not in serialized, 'personal_body_in_report')
    except Exception as error:
        report['checks'].append({'id': 'run_initialization', 'status': 'failed', 'duration_ms': 0,
                                 'error_code': safe_code(error)})
    passed = sum(c['status'] == 'passed' for c in report['checks'])
    report['summary'] = {'total': len(report['checks']), 'passed': passed, 'failed': len(report['checks']) - passed}
    report['status'] = 'passed' if report['summary']['failed'] == 0 else 'failed'
    validate_report(report)
    report['validation']['schema_checked'] = True
    require(json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False)) == report, 'json_round_trip_failed')
    report['validation']['json_round_trip'] = True
    if args.output:
        destination = Path(args.output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        require(json.loads(destination.read_text(encoding='utf-8')) == report, 'report_file_readback_failed')
        report['validation']['file_readback_verified'] = True
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        loaded = json.loads(destination.read_text(encoding='utf-8'))
        validate_report(loaded)
        require(loaded == report, 'final_report_file_readback_failed')
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (VerificationFailure, ValueError, OSError) as error:
        # 不输出异常正文、路径上下文或 traceback，避免夹带个人载荷和密钥。
        print(json.dumps({'schema_version': SCHEMA_VERSION, 'status': 'failed',
                          'error_code': safe_code(error)}, ensure_ascii=False))
        raise SystemExit(2)
