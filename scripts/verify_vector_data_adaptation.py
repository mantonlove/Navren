#!/usr/bin/env python3
"""真实官方资料的本地向量适配验收；固定题目，无云调用，无运行库写入。"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import resource
import socket
import sqlite3
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_knowledge import LocalKnowledgeProvider
from local_knowledge.embeddings import get_encoder, MODEL_ARTIFACTS, MODEL_REVISION
from local_knowledge.graph import paths
from local_knowledge.store import packed, sha, tokens
from local_knowledge.vector_documents import INPUT_POLICY, representation
from local_knowledge.vectors import VectorIndex, fuse, MIN_SIMILARITY, MAX_SCORE_DISTANCE
from agent_runtime.knowledge_retrieval import KnowledgeRetrieval

OUT = ROOT / 'outputs/vector-data-adaptation'
CATALOG = ROOT / 'outputs/official-discovery-audit-20261009/catalog-original.jsonp'
PDF_SOURCE = ROOT / 'outputs/window-progress/rag/reviewed-public.sqlite3'
PDF_QUESTIONS = ROOT / 'outputs/window-progress/rag/manual-question-set.json'
MARKET_SOURCE = ROOT / 'outputs/local-vector/public-knowledge-copy.sqlite3'
CATALOG_AS_OF = '2026-09-30'
CURRENT_AS_OF = '2026-10-10'
# 人工根据原字段意义制定，执行前冻结；不根据召回结果改变题目或阈值。
PRODUCT_NAMES = {
    '000001': '华夏成长混合',
    '000006': '西部利得量化成长混合A',
    '000008': '嘉实中证500ETF联接A',
    '17321D': '招银理财招睿月添利（平衡）1号固定收益类理财计划',
    'GRKYB0301': '科研保2.0',
    'B99353': '平安信托微享安稳3M-1号',
}
FIELD_QUESTIONS = (
    ('risk', '公开披露的风险等级是什么？'),
    ('publish', '发行机构是哪一家？'),
    ('charge', '收费标准披露了什么？'),
    ('investor', '合理投资范围对应哪些投资者？'),
    ('customerGroupType', '客群类别范围写的是个人还是机构？'),
)
WEBANK_CASES = tuple({'id': code + '-' + field, 'code': code, 'field': field,
                     'query': name + '，' + question}
                    for code, name in PRODUCT_NAMES.items() for field, question in FIELD_QUESTIONS)
MARKET_CASES = (
    {'id': 'ecb-english', 'query': 'ECB survey new euro banknotes', 'language': 'en'},
    {'id': 'ecb-chinese-probe', 'query': '欧洲央行关于新版欧元纸币图案的调查有哪些结果？', 'language': 'zh'},
)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def reset_destination(path):
    """只能重建本脚本输出目录内的受控验收副本。"""
    if path.parent.resolve() != OUT.resolve() or not path.name.startswith('data-acceptance'):
        raise ValueError('acceptance_destination_out_of_scope')
    for name in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
        if name.exists(): name.unlink()


def backup(source, destination):
    reset_destination(destination)
    reader = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    writer = sqlite3.connect(destination)
    try: reader.backup(writer)
    finally: writer.close(); reader.close()


def release(provider):
    with provider.db() as database:
        return database.execute("SELECT release_id FROM current_release WHERE scope='public'").fetchone()[0]


def source_inventory(provider, release_id):
    with provider.db() as database:
        rows = provider._rows(database, release_id)
        originals = database.execute('''SELECT DISTINCT v.id,v.raw_sha256,v.parsed_sha256,
                    v.parser_id,v.shape,d.source_id,d.source_url
                    FROM document_versions v JOIN documents d ON d.id=v.document_id
                    JOIN chunks c ON c.version_id=v.id JOIN release_chunks rc ON rc.chunk_id=c.id
                    WHERE rc.release_id=? ORDER BY v.id''', (release_id,)).fetchall()
        payload_hashes_valid = all(sha(bytes(r['payload'])) == r['sha256'] for r in database.execute(
            'SELECT * FROM raw_artifacts WHERE sha256 IN (SELECT raw_sha256 FROM document_versions)'))
    return {'document_count': len(originals), 'chunk_count': len(rows), 'release_id': release_id,
            'kinds': dict(Counter(representation(r)['kind'] for r in rows)),
            'source_originals': [dict(r) for r in originals], 'raw_payload_hashes_valid': payload_hashes_valid}


def candidates(provider, release_id, request):
    """与真实读侧同样的许可/时点/身份/原件/图准入；不做维度补召回。"""
    with provider.db() as database:
        database.execute('BEGIN')
        if not provider._release_manifest_valid(database, release_id):
            raise ValueError('release_manifest_invalid')
        requested = [k for k in ('product_id', 'share_class_id', 'channel_id') if request.get(k)]
        facets = request.get('facets', [])
        eligible = {}; checked = {}
        for row in provider._rows(database, release_id):
            if any(row[k] != request[k] for k in requested): continue
            available, _ = provider._available(database, row, request['as_of'], checked)
            relevant = set(available) & set(facets) if facets else set(available)
            if relevant and paths(database, row, request['as_of'], request=request, facets=list(relevant)):
                eligible[row['id']] = dict(row)
        terms = tokens(request['query'])[:80]
        lexical = {}
        if terms:
            expression = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
            scored = {r['rowid']: r['score'] for r in database.execute(
                'SELECT rowid,bm25(chunks_fts,3.0,1.0) score FROM chunks_fts WHERE chunks_fts MATCH ?', (expression,))}
            lexical = {cid: scored[row['row_id']] for cid, row in eligible.items() if row['row_id'] in scored}
        vector = VectorIndex(provider, provider.vector_encoder).search(database, release_id, request['query'], set(eligible))
    return eligible, lexical, vector


def cosine(encoder, matrix, ids, query, eligible):
    import numpy as np
    offsets = [index for index, cid in enumerate(ids) if cid in eligible]
    if not offsets: return {}
    scores = matrix[offsets] @ np.asarray(encoder.encode_query(query), dtype=np.float32)
    threshold = max(MIN_SIMILARITY, float(scores.max()) - MAX_SCORE_DISTANCE)
    result = {}
    for index, value in zip(offsets, scores):
        if float(value) >= threshold: result[ids[index]] = max(result.get(ids[index], -1), float(value))
    return result


def ranked(scores, *, descending=False, limit=3):
    return sorted(scores, key=lambda cid: ((-scores[cid] if descending else scores[cid]), cid))[:limit]


def canonical_proof(provider, release_id, as_of, ids, original_rows, request=None):
    if not ids: return {'checked': 0, 'all_valid': True, 'citations': []}
    resolved = provider.resolve_public_evidence_batch(ids, release_id, as_of)
    proofs = []
    request = request or {}
    for cid in ids:
        hit = resolved.get(cid); row = original_rows[cid]
        expected_locator = {key: row[key] for key in ('page', 'paragraph', 'start_char', 'end_char')}
        valid = bool(hit and hit['excerpt'] == row['excerpt'] and hit['locator'] == expected_locator
                     and hit['document_version_id'] == row['version_id'] and hit['source_url'] == row['source_url']
                     and hit['release_id'] == release_id and hit['product_id'] == row['product_id']
                     and hit['share_class_id'] == row['share_class_id'] and hit['channel_id'] == row['channel_id']
                     and all(hit[key] == request[key] for key in ('product_id', 'share_class_id', 'channel_id') if request.get(key))
                     and provider.graph_trace(cid, release_id, as_of, request=request)['paths'])
        proofs.append({'citation_id': cid, 'paragraph': row['paragraph'], 'locator': expected_locator,
                       'valid': valid, 'excerpt': hit['excerpt'] if hit else None})
    return {'checked': len(ids), 'all_valid': all(p['valid'] for p in proofs), 'citations': proofs}


def webank(destination, encoder):
    reset_destination(destination)
    provider = LocalKnowledgeProvider(destination, encoder=encoder)
    body = CATALOG.read_bytes()
    imported = provider.import_webank_disclosure(body, codes=tuple(PRODUCT_NAMES), publish=True, limit=6)
    release_id = imported['release_id']
    built = provider.build_vector_index(release_id)
    baseline = LocalKnowledgeProvider(destination, release_id=release_id, encoder=False)
    with provider.db() as database:
        original_rows = {r['id']: dict(r) for r in provider._rows(database, release_id)}
    # 旧表示只存在内存对照中，不写入派生索引，也不改任何原文。
    old_parts = []; old_ids = []
    ordered = sorted(original_rows.values(), key=lambda r: r['id'])
    for offset in range(0, len(ordered), 4):
        batch = ordered[offset:offset + 4]
        for row, values in zip(batch, encoder.encode_documents([r['title'][:120] + '\n' + r['excerpt'] for r in batch])):
            old_parts.extend(values); old_ids.extend([row['id']] * len(values))
    import numpy as np
    old_matrix = np.stack(old_parts)
    expected = {(r['product_id'][7:], r['paragraph'].rsplit('/', 1)[-1]): cid for cid, r in original_rows.items()}
    cases = []; timings = []
    for case in WEBANK_CASES:
        for mode in ('explicit_identity_no_facets', 'no_identity_no_facets'):
            request = {'scope': 'public', 'query': case['query'], 'as_of': CATALOG_AS_OF, 'facets': [], 'limit': 3}
            if mode == 'explicit_identity_no_facets':
                request.update(product_id='WEBANK:' + case['code'], channel_id='WEBANK')
            started = time.monotonic()
            eligible, lexical, current = candidates(provider, release_id, request)
            old = cosine(encoder, old_matrix, old_ids, request['query'], set(eligible))
            hybrid = provider.search_public(request, release_id)
            fts = baseline.search_public(request, release_id)
            rankings = {'fts': fts['citation_ids'], 'old_vector': ranked(old, descending=True),
                        'adapted_vector': ranked(current['scores'], descending=True),
                        'old_mixed': ranked(fuse(lexical, old) if old else lexical),
                        'adapted_mixed': hybrid['citation_ids']}
            timings.append((time.monotonic() - started) * 1000)
            target = expected[(case['code'], case['field'])]
            all_ids = list(dict.fromkeys(cid for ids in rankings.values() for cid in ids))
            proof = canonical_proof(provider, release_id, CATALOG_AS_OF, all_ids, original_rows, request)
            cases.append({**case, 'query_mode': mode, 'request': request, 'expected_citation_id': target,
                          'expected_original_excerpt': original_rows[target]['excerpt'],
                          'rankings': {method: [{'citation_id': cid, 'code': original_rows[cid]['product_id'][7:],
                                                'field': original_rows[cid]['paragraph'].rsplit('/', 1)[-1]}
                                               for cid in ids] for method, ids in rankings.items()},
                          'hit_at_1': {method: ids[:1] == [target] for method, ids in rankings.items()},
                          'hit_at_3': {method: target in ids for method, ids in rankings.items()},
                          'eligible_count': len(eligible), 'adapted_vector_reason': current['reason'],
                          'retrieval': hybrid['retrieval'], 'canonical_proof': proof})
    summary = {}
    for mode in ('explicit_identity_no_facets', 'no_identity_no_facets'):
        own = [c for c in cases if c['query_mode'] == mode]
        summary[mode] = {method: {'question_count': len(own), 'hit_at_1': sum(c['hit_at_1'][method] for c in own),
                                 'hit_at_3': sum(c['hit_at_3'][method] for c in own)}
                         for method in ('fts', 'old_vector', 'adapted_vector', 'old_mixed', 'adapted_mixed')}
    expired_request = {'scope': 'public', 'query': PRODUCT_NAMES['000001'] + '风险和费用',
                       'product_id': 'WEBANK:000001', 'channel_id': 'WEBANK',
                       'as_of': CURRENT_AS_OF, 'facets': [], 'limit': 3}
    expired = provider.search_public(expired_request, release_id)
    wrong_identity = provider.search_public({**expired_request, 'as_of': CATALOG_AS_OF,
                                             'product_id': 'WEBANK:NONEXISTENT'}, release_id)
    sales = provider.search_public({**expired_request, 'as_of': CATALOG_AS_OF,
                                     'facets': ['sale_status'], 'query': PRODUCT_NAMES['000001'] + '销售状态'}, release_id)
    consumer_sales = KnowledgeRetrieval(provider.for_release(release_id)).search(
        query=PRODUCT_NAMES['000001'] + '销售状态', product_id='WEBANK:000001', channel_id='WEBANK',
        as_of=CATALOG_AS_OF, facets=['sale_status'], limit=3)
    fees = [r for r in original_rows.values() if r['paragraph'].endswith('/charge')]
    checks = {'six_official_records_imported': imported['imported'] == 6 and len(original_rows) == 72,
              'all_canonical_quotes_locators_and_identities_valid': all(c['canonical_proof']['all_valid'] for c in cases),
              'all_current_vector_reads_succeed': all(c['adapted_vector_reason'] is None for c in cases),
              'no_facet_fallback_in_comparison': all(c['request']['facets'] == [] for c in cases),
              'fee_reference_only_preserved': len(fees) == 6 and all(representation(r)['detail_status'] == 'reference_only' for r in fees),
              'current_expired_originals_rejected': not expired['citation_ids'] and any(g['code'] == 'expired_evidence' for g in expired['coverage_gaps']),
              'unknown_product_identity_rejected': not wrong_identity['citation_ids'] and any(g['code'] == 'identity_unverified' for g in wrong_identity['coverage_gaps']),
              'sales_disclosure_does_not_authorize_buying': any(g['code'] == 'sale_status_unknown' for g in sales['coverage_gaps'])
                  and bool(consumer_sales['hits']) and all(h['sale_status'] == 'unknown' for h in consumer_sales['hits'])}
    return {'source_sha256': sha(body), 'disclosure_date': CATALOG_AS_OF, 'import': imported,
            'inventory': source_inventory(provider, release_id), 'build': built, 'old_representation_window_count': len(old_ids),
            'question_set_sha256': sha(packed(WEBANK_CASES)), 'summary': summary, 'cases': cases,
            'query_batch_median_ms': round(statistics.median(timings), 3),
            'query_batch_max_ms': round(max(timings), 3), 'expired_as_of_current': expired,
            'unknown_product': wrong_identity, 'sale_status': sales, 'checks': checks,
            'limits': ['字段语义召回不等于费率、底层基金份额、交易准入或当前可售状态核验。',
                       '2026-09-30 原件按历史时点验收；2026-10-10 已过七日有效期。',
                       '所有题目与阈值执行前固定，语义提升不作为通过硬门。']}


def pdf(destination, encoder):
    backup(PDF_SOURCE, destination)
    provider = LocalKnowledgeProvider(destination, encoder=encoder)
    release_id = release(provider); built = provider.build_vector_index(release_id)
    baseline = LocalKnowledgeProvider(destination, release_id=release_id, encoder=False)
    question_bytes = PDF_QUESTIONS.read_bytes(); questions = json.loads(question_bytes)['cases']
    with provider.db() as database:
        original_rows = {r['id']: dict(r) for r in provider._rows(database, release_id)}
    cases = []; timings = []
    for case in questions:
        request = {'scope': 'public', **case['request']}
        started = time.monotonic()
        fts = baseline.search_public(request, release_id); hybrid = provider.search_public(request, release_id)
        eligible, _, direct = candidates(provider, release_id, request)
        direct_ids = ranked(direct['scores'], descending=True)
        keys = lambda ids: [original_rows[cid]['paragraph'].split('/', 1)[1] for cid in ids]
        expected = set(case['expected_review_keys'])
        mixed_keys = keys(hybrid['citation_ids']); lexical_keys = keys(fts['citation_ids'])
        ids = list(dict.fromkeys(hybrid['citation_ids'] + fts['citation_ids'] + direct_ids))
        proof = canonical_proof(provider, release_id, request['as_of'], ids, original_rows, request)
        combined = '\n'.join(original_rows[cid]['excerpt'] for cid in hybrid['citation_ids'])
        identity_fragment = case['id'] not in ('P10', 'P12') or ('006211' if case['id'] == 'P10' else '021095') in combined
        timings.append((time.monotonic() - started) * 1000)
        cases.append({'id': case['id'], 'query': request['query'], 'expected_review_keys': sorted(expected),
                      'fts_review_keys': lexical_keys, 'hybrid_review_keys': mixed_keys,
                      'direct_vector_top3_keys': keys(direct_ids),
                      'fts_all_required_keys': expected.issubset(lexical_keys),
                      'hybrid_all_required_keys': expected.issubset(mixed_keys),
                      'direct_top3_all_required_keys': expected.issubset(keys(direct_ids)),
                      'required_original_fragments_present': all(fragment in combined for fragment in case['required_original_fragments']),
                      'share_identity_original_fragment_present': identity_fragment,
                      'retrieval': hybrid['retrieval'], 'direct_vector_reason': direct['reason'],
                      'canonical_proof': proof})
    checks = {'six_documents_22_spans': len({r['version_id'] for r in original_rows.values()}) == 6 and len(original_rows) == 22,
              'frozen_question_file_unchanged': sha(PDF_QUESTIONS.read_bytes()) == sha(question_bytes),
              'all_12_existing_queries_still_cover_required_originals': len(cases) == 12 and all(c['hybrid_all_required_keys'] for c in cases),
              'all_12_original_quote_locator_and_share_checks': all(c['canonical_proof']['all_valid'] and c['required_original_fragments_present']
                                                                   and c['share_identity_original_fragment_present'] for c in cases),
              'all_vector_branches_usable': all(c['direct_vector_reason'] is None and c['retrieval']['vector_reason'] is None for c in cases)}
    return {'question_set_sha256': sha(question_bytes), 'annotation_method': '已有 Codex 原件审阅固定题，非银行专家人工标注',
            'inventory': source_inventory(provider, release_id), 'build': built, 'cases': cases,
            'summary': {'questions': len(cases), 'fts_all_required_keys': sum(c['fts_all_required_keys'] for c in cases),
                        'hybrid_all_required_keys': sum(c['hybrid_all_required_keys'] for c in cases),
                        'direct_vector_top3_all_required_keys': sum(c['direct_top3_all_required_keys'] for c in cases)},
            'query_batch_median_ms': round(statistics.median(timings), 3), 'checks': checks,
            'limits': ['原 FTS 已覆盖 12/12；本项证明适配后没有原件/页码/份额回退，不证明新增语义提升。',
                       '当前未生成或验证费率计算和资产配置结论；资料有效期未知仍由消费侧报告。']}


def market(destination, encoder):
    backup(MARKET_SOURCE, destination)
    provider = LocalKnowledgeProvider(destination, encoder=encoder)
    release_id = release(provider); built = provider.build_vector_index(release_id)
    baseline = LocalKnowledgeProvider(destination, release_id=release_id, encoder=False)
    with provider.db() as database:
        original_rows = {r['id']: dict(r) for r in provider._rows(database, release_id)}
    cases = []
    for case in MARKET_CASES:
        request = {'scope': 'public', 'query': case['query'], 'as_of': CURRENT_AS_OF, 'facets': ['general'], 'limit': 3}
        started = time.monotonic()
        fts = baseline.search_public(request, release_id); hybrid = provider.search_public(request, release_id)
        eligible, _, direct = candidates(provider, release_id, request)
        direct_ids = ranked(direct['scores'], descending=True)
        ids = list(dict.fromkeys(hybrid['citation_ids'] + fts['citation_ids'] + direct_ids))
        proof = canonical_proof(provider, release_id, request['as_of'], ids, original_rows, request)
        by_method = {'fts': fts['citation_ids'], 'vector': direct_ids, 'hybrid': hybrid['citation_ids']}
        cases.append({**case, 'query_batch_ms': round((time.monotonic() - started) * 1000, 3),
                      'rankings': {method: [{'citation_id': cid, 'title': original_rows[cid]['title'],
                                           'source_url': original_rows[cid]['source_url']} for cid in ranked_ids]
                                   for method, ranked_ids in by_method.items()},
                      'retrieval': hybrid['retrieval'], 'direct_vector_reason': direct['reason'],
                      'coverage_gaps': hybrid['coverage_gaps'], 'canonical_proof': proof})
    english = cases[0]
    # 原件题仅要求实际检索到目标主题，不能据此认定已回答具体票选数字。
    english_theme = any('banknote' in h['title'].lower() and 'survey' in h['title'].lower()
                        for h in english['rankings']['hybrid'])
    checks = {'real_282_spans_present': len(original_rows) == 282,
              'all_original_citation_locator_checks': all(c['canonical_proof']['all_valid'] for c in cases),
              'all_local_vector_branches_usable': all(c['direct_vector_reason'] is None and c['retrieval']['vector_reason'] is None for c in cases),
              'existing_english_ecb_theme_nonregression': english_theme}
    return {'inventory': source_inventory(provider, release_id), 'build': built, 'cases': cases, 'checks': checks,
            'limits': ['中文 ECB 题只是跨语探针，如实呈现结果；两题不能证明中文对全部英文资料的召回能力。',
                       '当前资料效期、主题背景与具体事实结论仍不同；没有自动翻译或云模型补全。']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, help='outputs/vector-data-adaptation/data-acceptance*.json')
    args = parser.parse_args(); destination = Path(args.out).resolve()
    if destination.parent != OUT.resolve() or not destination.name.startswith('data-acceptance') or destination.suffix != '.json':
        parser.error('输出只能为 outputs/vector-data-adaptation/data-acceptance*.json')
    OUT.mkdir(parents=True, exist_ok=True)
    sources = (CATALOG, PDF_SOURCE, PDF_QUESTIONS, MARKET_SOURCE)
    source_hashes = {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in sources}
    code_inputs = tuple(ROOT / p for p in ('local_knowledge/store.py', 'local_knowledge/vectors.py',
        'local_knowledge/vector_documents.py', 'local_knowledge/embeddings.py', 'local_knowledge/importers.py',
        'scripts/verify_vector_data_adaptation.py'))
    code_hashes = {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in code_inputs}
    network_attempts = []
    def forbidden(*args, **kwargs):
        network_attempts.append('socket_connect'); raise AssertionError('network_forbidden_during_local_acceptance')
    started = time.monotonic()
    # 问题定义与独立原件源 hash 在加载模型前落盘，防止调题掩盖真实失败。
    atomic_json(destination.with_name(destination.stem + '-frozen-input.json'),
                {'webank_cases': WEBANK_CASES, 'market_cases': MARKET_CASES, 'pdf_questions_sha256': source_hashes[str(PDF_QUESTIONS.relative_to(ROOT))],
                 'source_hashes': source_hashes, 'code_hashes': code_hashes,
                 'minimum_similarity': MIN_SIMILARITY, 'maximum_score_distance': MAX_SCORE_DISTANCE})
    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden):
        encoder = get_encoder(); cold = time.monotonic() - started
        sections = {}
        for name, evaluator in (('webank', webank), ('pdf', pdf), ('market', market)):
            section_start = time.monotonic()
            sections[name] = evaluator(destination.with_name(destination.stem + '-' + name + '.sqlite3'), encoder)
            sections[name]['acceptance_seconds'] = round(time.monotonic() - section_start, 3)
            print(json.dumps({'stage': name, 'checks': sections[name]['checks'], 'build': sections[name]['build'],
                              'summary': sections[name].get('summary')}, ensure_ascii=False), flush=True)
    unchanged = all(sha(p.read_bytes()) == source_hashes[str(p.relative_to(ROOT))] for p in sources)
    code_unchanged = all(sha(p.read_bytes()) == code_hashes[str(p.relative_to(ROOT))] for p in code_inputs)
    checks = {'zero_query_network_attempts': not network_attempts, 'source_files_unchanged': unchanged,
              'code_files_unchanged_during_acceptance': code_unchanged,
              'all_official_raw_hashes_valid': all(s['inventory']['raw_payload_hashes_valid'] for s in sections.values()),
              **{name + '/' + key: value for name, section in sections.items() for key, value in section['checks'].items()}}
    result = {'schema_version': 1, 'status': 'module_data_adaptation_pass' if all(checks.values()) else 'failed',
              'executed_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'data_mode': 'fixed_official_originals_in_isolated_sqlite_copies', 'model_mode': 'real_local_onnx_cpu',
              'model_revision': MODEL_REVISION, 'profile_id': encoder.profile_id, 'document_policy': INPUT_POLICY,
              'model_bytes': sum(a['size'] for a in MODEL_ARTIFACTS.values()),
              'thresholds': {'minimum_similarity': MIN_SIMILARITY, 'maximum_score_distance': MAX_SCORE_DISTANCE},
              'cold_load_seconds': round(cold, 3), 'elapsed_seconds': round(time.monotonic() - started, 3),
              'process_max_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'network_attempt_count': len(network_attempts), 'cloud_model_calls': 0,
              'source_hashes': source_hashes, 'code_hashes': code_hashes,
              'webank': sections['webank'], 'pdf': sections['pdf'], 'market': sections['market'],
              'checks': checks, 'limits': ['隔离库真实 encoder 验收，未更改当前服务或生产共享库。',
                  'Python socket 守卫仅覆盖本 Python 进程，未做全系统网络审计。',
                  '没有测试真实云端对话、敏感个人数据库、大规模并发或银行生产部署。',
                  '语义命中率用于报告，只有原件/定位/身份/过期/无网络等明确硬门决定本次退出码。']}
    atomic_json(destination, result)
    print(json.dumps({'status': result['status'], 'checks_passed': sum(checks.values()), 'check_count': len(checks),
                      'output': str(destination), 'elapsed_seconds': result['elapsed_seconds']}, ensure_ascii=False), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
