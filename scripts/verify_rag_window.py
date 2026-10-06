"""用已独立复核的官方原件构建隔离公共发布并验收；不联网、不读私人库。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import resource
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_runtime.knowledge_retrieval import KnowledgeRetrieval
from local_knowledge import KnowledgeError, LocalKnowledgeProvider
from local_knowledge.pdf_ingest import extract_pdf, stage_reviewed_pdf, PARSER_ID, SHAPE
from local_knowledge.store import sha, tokens

OUT = ROOT / 'outputs/window-progress/rag'
AS_OF = '2026-10-06'
SOURCE = 'orient-reviewed-pdf'


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def selection_span(selection, extracted):
    """只执行审核记录中的页内锚点，不改写、拼表或自动识别金融语义。"""
    pages = extracted['pages']
    if selection['mode'] == 'risk_disclaimer':
        matches = [p for p in pages if '本基金不提供任何保证' in p['text']]
        if len(matches) != 1:
            raise ValueError('risk_anchor_not_unique:' + selection['key'])
        page = matches[0]
        start = page['text'].index('本基金不提供任何保证')
        end = min(len(page['text']), start + selection['maximum'])
    else:
        page = pages[selection['page'] - 1]
        text = page['text']
        if selection['mode'] == 'prefix':
            start, end = 0, min(selection['maximum'], len(text))
        elif selection['mode'] == 'workday_definition':
            anchor = text.index('工作日：')
            start = text.rfind('\n', 0, anchor) + 1
            stop = re.search(r'37、\s*开放时间', text[anchor:])
            if not stop:
                raise ValueError('workday_end_missing:' + selection['key'])
            end = anchor + stop.start()
            if end - start > selection['maximum']:
                raise ValueError('workday_review_limit:' + selection['key'])
        elif selection['mode'] == 'section':
            anchor = selection['start_anchor']
            if text.count(anchor) != 1:
                raise ValueError('section_anchor_not_unique:' + selection['key'])
            start = text.index(anchor)
            stops = [text.find(a, start + len(anchor)) for a in selection['end_anchors']]
            end = min([x for x in stops if x >= 0] or [len(text)])
            if end - start > selection.get('maximum', 1200):
                raise ValueError('section_review_limit:' + selection['key'])
        else:
            raise ValueError('unknown_review_selection')
    absolute_start, absolute_end = page['start_char'] + start, page['start_char'] + end
    return {'page': page['page'], 'paragraph': 'review/' + selection['key'],
            'start_char': absolute_start, 'end_char': absolute_end,
            'excerpt': extracted['text'][absolute_start:absolute_end],
            'share_class_id': selection['share_class_id'], 'facets': selection['facets'],
            'conditions': []}


def build(database, manifest):
    provider = LocalKnowledgeProvider(database)
    docs = [d for f in manifest['funds'] for d in f['documents']]
    provider.register_source(SOURCE, publisher='东方基金管理股份有限公司',
        endpoints=[d['source_url'] for d in docs], parser_id=PARSER_ID, shape=SHAPE,
        freshness={f: None for f in ('fees', 'liquidity', 'terms', 'risk', 'general', 'eligibility')},
        permission_basis='官方公开完整原件供本地原件核验及有限短引文；不证明批量转载授权或银行生产授权')
    for fund in manifest['funds']:
        provider.register_product(fund['product_id'], issuer=fund['manager'], scheme='CN-public-fund',
            code=fund['code'], name=fund['legal_name'], verified=True)
        for share in fund['shares']:
            provider.register_share(share['share_class_id'], product_id=fund['product_id'],
                scheme='CN-public-fund-share', code=share['code'], currency=share['currency'])
    versions, locators = [], []
    for fund in manifest['funds']:
        for document in fund['documents']:
            path = Path(document['file']).resolve()
            if not path.is_relative_to((OUT / 'sources/products').resolve()):
                raise ValueError('original_path_out_of_scope')
            raw = path.read_bytes()
            if sha(raw) != document['raw_sha256']:
                raise ValueError('original_hash_changed')
            started = time.perf_counter()
            extracted = extract_pdf(raw)
            spans = [selection_span(s, extracted) for s in fund['selections'] if s['kind'] == document['kind']]
            for span in spans:
                span['product_id'] = fund['product_id']
            candidate = stage_reviewed_pdf(provider, raw, spans, source_id=SOURCE,
                source_url=document['source_url'], title=document['title'], product_id=fund['product_id'],
                document_key=document['kind'], published_on=document['published_on'], retrieved_on=AS_OF,
                effective_from=None, effective_to=None, reviewed_by='Codex original reading + independent source reviewer',
                review_reference='product-review-manifest sha256:' + sha((OUT / 'product-review-manifest.json').read_bytes()))
            provider.approve_version(candidate['version_id'], verification_basis=candidate['verification_basis'])
            versions.append({**document, **candidate, 'approved': True,
                             'extract_and_stage_seconds': time.perf_counter() - started})
            for span in spans:
                locators.append({**span, 'key': span['paragraph'].split('/', 1)[1],
                    'document_version_id': candidate['version_id'], 'source_url': document['source_url'],
                    'published_on': document['published_on'], 'raw_sha256': candidate['raw_sha256'],
                    'parsed_sha256': candidate['text_sha256'],
                    'effective_period_known': False, 'interpretation_limits': fund['conditions']})
            print('parsed_and_reviewed', fund['code'], document['kind'], len(extracted['pages']), len(spans), flush=True)
    release = provider.publish(version_ids=[v['version_id'] for v in versions])
    for version in versions:
        version['published'] = True
    with provider.db() as db:
        by_key = {r['paragraph'].split('/', 1)[1]: r['id'] for r in db.execute('SELECT id,paragraph FROM chunks')}
    for row in locators:
        row['citation_id'], row['release_id'] = by_key[row['key']], release
    return provider, versions, locators


def lexical_candidates(provider, request, release):
    """同题FTS5候选对照，保留主体/维度过滤；不将候选得分当事实。"""
    terms = tokens(request['query'])[:80]
    expression = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
    with provider.db() as db:
        scored = {r['rowid']: r['score'] for r in db.execute(
            'SELECT rowid,bm25(chunks_fts,3.0,1.0) score FROM chunks_fts WHERE chunks_fts MATCH ?', (expression,))}
        rows = [r for r in provider._rows(db, release)
                if all(r[k] == request[k] for k in ('product_id', 'share_class_id') if request.get(k))
                and set(json.loads(r['facets'])) & set(request['facets']) and r['row_id'] in scored]
        rows.sort(key=lambda r: (scored[r['row_id']], r['id']))
        return [r['paragraph'].split('/', 1)[1] for r in rows[:request['limit']]]


def positives(provider, cases, locators):
    release = provider.status()['public_release_id']
    reviewed = {r['key']: r for r in locators}
    records = []
    for case in cases:
        request = case['request']
        started = time.perf_counter()
        result = KnowledgeRetrieval(provider.for_release(release)).search(**request)
        elapsed = (time.perf_counter() - started) * 1000
        keys = [h['locator']['paragraph'].split('/', 1)[1] for h in result['hits']]
        expected = set(case['expected_review_keys'])
        valid = []
        for hit in result['hits']:
            original = reviewed[hit['locator']['paragraph'].split('/', 1)[1]]
            loc = hit['locator']
            valid.append(hit['excerpt'] == original['excerpt'] and hit['source_url'] == original['source_url']
                and hit['published_on'] == original['published_on'] and hit['document_version_id'] == original['document_version_id']
                and hit['release_id'] == release and hit['product_id'] == request['product_id']
                and hit['share_class_id'] == request['share_class_id']
                and all(loc[k] == original[k] for k in ('page', 'paragraph', 'start_char', 'end_char'))
                and bool(provider.graph_trace(hit['citation_id'], release, AS_OF)['paths']))
        combined = '\n'.join(h['excerpt'] for h in result['hits'])
        fragments = all(s in combined for s in case['required_original_fragments'])
        # 空关键词列表的两题也必须命中指定原件，并有独立份额代码原文。
        identity = (case['id'] not in ('P10', 'P12') or
                    ('006211' if case['id'] == 'P10' else '021095') in combined)
        gaps = [g['code'] for g in result['coverage_gaps']]
        lexical = lexical_candidates(provider, request, release)
        passed = expected.issubset(keys) and bool(valid) and all(valid) and fragments and identity
        passed = passed and 'effective_period_unknown' in gaps and all(h['sale_status'] == 'unknown' for h in result['hits'])
        records.append({**case, 'retrieval_passed': passed, 'retrieved_review_keys': keys,
            'missing_review_keys': sorted(expected - set(keys)), 'lexical_only_review_keys': lexical,
            'lexical_only_expected_keys_covered': expected.issubset(lexical),
            'locator_hash_identity_graph_checks_passed': bool(valid) and all(valid),
            'required_original_fragments_found': fragments, 'independent_share_code_found': identity,
            'elapsed_ms': elapsed, 'result': result, 'final_answer_semantics': 'not_evaluated'})
    return records


def clone(provider, path):
    with sqlite3.connect(provider.path.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(path) as target:
        source.backup(target)
    return LocalKnowledgeProvider(path)


def negatives(provider, cases, locators, directory):
    release = provider.status()['public_release_id']
    record = next(r for r in locators if r['key'] == '006210-sales-fees')
    case = cases[0]
    output = []
    for name in ('wrong_share_parent', 'wrong_channel', 'withdrawn_version', 'cancelled_source_permission',
                 'conflicting_fees', 'publication_after_as_of', 'changed_raw_hash', 'parser_parameter_or_page_mismatch',
                 'fixed_release_new_publication'):
        p = clone(provider, directory / (name + '.sqlite3'))
        query = deepcopy(case['request'])
        consumer = KnowledgeRetrieval(p)
        consumer.search(**query)
        expected_gap, freshness_rejected, detail = None, None, {}
        if name == 'wrong_share_parent':
            query['share_class_id'] = 'ORIENT-SHARE:001198'
        elif name == 'wrong_channel':
            p.register_channel('WEBANK', name='微众银行')
            query['channel_id'] = 'WEBANK'
        elif name == 'withdrawn_version':
            p.withdraw(record['document_version_id'])
            expected_gap = 'evidence_unavailable'
        elif name == 'cancelled_source_permission':
            p.set_source_state(SOURCE, outbound_allowed=False)
            expected_gap = 'source_permission_missing'
        elif name == 'conflicting_fees':
            p.block_conflict(facets=['fees'], product_id=query['product_id'], share_class_id=query['share_class_id'],
                from_on=AS_OF, reason='isolated_conflict_probe; does not assert real financial conflict')
            expected_gap = 'conflicting_sources'
        elif name == 'publication_after_as_of':
            query['as_of'] = '2026-09-20'
            expected_gap = 'published_after_as_of'
        elif name == 'changed_raw_hash':
            with p.db() as db:
                try:
                    db.execute('UPDATE raw_artifacts SET payload=? WHERE sha256=?', (b'corruption', record['raw_sha256']))
                except sqlite3.IntegrityError as error:
                    detail['immutable_update_rejected'] = str(error) == 'immutable_raw'
                else:
                    detail['immutable_update_rejected'] = False
                # 模拟损坏磁盘绕过数据库保护，仅在一次性clone里验证回查，不触碰交付库。
                db.execute('DROP TRIGGER immutable_raw')
                db.execute('UPDATE raw_artifacts SET payload=? WHERE sha256=?', (b'corruption', record['raw_sha256']))
            expected_gap = 'evidence_unavailable'
        elif name == 'parser_parameter_or_page_mismatch':
            doc = next(v for f in json.loads((OUT/'product-review-manifest.json').read_text())['funds']
                       for v in f['documents'] if v['raw_sha256'] == record['raw_sha256'])
            raw = Path(doc['file']).read_bytes()
            extracted = extract_pdf(raw)
            detail = {}
            for probe, changed in [('false_text', None), ('wrong_page', 1)]:
                try:
                    span = {k: record[k] for k in ('page', 'paragraph', 'start_char', 'end_char', 'excerpt', 'product_id', 'share_class_id', 'facets', 'conditions')}
                    if probe == 'wrong_page': span['page'] = changed
                    p.stage_document(source_id=SOURCE, source_url=doc['source_url'], title=doc['title'], raw_bytes=raw,
                        text=extracted['text'] + ('\nmodified' if probe == 'false_text' else ''), parser_id=PARSER_ID,
                        shape=SHAPE, spans=[span], published_on=doc['published_on'], document_key='invalid-probe')
                except KnowledgeError as error:
                    detail[probe] = error.code
                else:
                    detail[probe] = 'accepted'
            with patch('local_knowledge.pdf_ingest.metadata.version', return_value='different-version'):
                try:
                    extract_pdf(raw)
                except KnowledgeError as error:
                    detail['dependency_version_change'] = error.code
                else:
                    detail['dependency_version_change'] = 'accepted'
            output.append({'scenario': name, 'passed': detail == {'false_text': 'raw_parse_mismatch',
                'wrong_page': 'pdf_span_page_mismatch', 'dependency_version_change': 'pdf_parser_version_mismatch'}, **detail})
            continue
        elif name == 'fixed_release_new_publication':
            with p.db() as db:
                before = [r[0] for r in db.execute('SELECT chunk_id FROM release_chunks WHERE release_id=? ORDER BY chunk_id', (release,))]
            another = p.publish(version_ids=[record['document_version_id']])
            fixed_result = consumer.search(**cases[8]['request'])
            with p.db() as db:
                after = [r[0] for r in db.execute('SELECT chunk_id FROM release_chunks WHERE release_id=? ORDER BY chunk_id', (release,))]
            output.append({'scenario': name, 'passed': another != release and fixed_result['public_release_id'] == release
                and bool(fixed_result['hits']) and before == after, 'old_release': release, 'new_release': another,
                'fixed_request_release': fixed_result['public_release_id'], 'old_members_unchanged': before == after})
            continue
        result = KnowledgeRetrieval(p.for_release(release)).search(**query)
        gaps = [g['code'] for g in result['coverage_gaps']]
        if name in ('withdrawn_version', 'cancelled_source_permission', 'conflicting_fees', 'changed_raw_hash'):
            try:
                consumer.assert_fresh()
            except ValueError as error:
                freshness_rejected = str(error) == 'stale_knowledge_evidence'
            else:
                freshness_rejected = False
        passed = not result['hits'] and (expected_gap is None or expected_gap in gaps)
        passed = passed and freshness_rejected is not False and detail.get('immutable_update_rejected', True)
        output.append({'scenario': name, 'passed': passed, 'hit_count': len(result['hits']), 'coverage_gap_codes': gaps,
            'consumer_freshness_rejected': freshness_rejected, **detail})
    # 历史微众时效由独立原件验收报告复核；本脚本不打开现役库。
    review = json.loads((ROOT/'local_knowledge/acceptance/latest_review_result.json').read_text())
    historical = [c for c in review['cases'] if 'expired' in c['id']]
    output.append({'scenario': 'historical_webank_expired', 'passed': bool(historical) and all(c['passed'] for c in historical),
        'kind': 'independent_provider_acceptance_artifact', 'artifact': 'local_knowledge/acceptance/latest_review_result.json',
        'artifact_sha256': sha((ROOT/'local_knowledge/acceptance/latest_review_result.json').read_bytes()),
        'cases': historical})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path, default=OUT)
    args = parser.parse_args()
    if not args.output_directory.resolve().is_relative_to(OUT.resolve()):
        raise ValueError('output_out_of_scope')
    args.output_directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((OUT/'product-review-manifest.json').read_text())
    questions = json.loads((OUT/'manual-question-set.json').read_text())
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='navren-rag-reviewed-') as temporary:
        directory = Path(temporary)
        provider, versions, locators = build(directory/'reviewed-public.sqlite3', manifest)
        positive = positives(provider, questions['cases'], locators)
        negative = negatives(provider, questions['cases'], locators, directory)
        target = args.output_directory/'reviewed-public.sqlite3'
        # 本脚本只替换自己生成的隔离验收产物，禁止现役库/任意路径。
        if target.resolve().parent != args.output_directory.resolve() or not args.output_directory.resolve().is_relative_to(OUT.resolve()):
            raise ValueError('output_out_of_scope')
        if target.exists(): target.unlink()
        clone(provider, target)
        with provider.db() as db:
            graph = {'schema_version': 1, 'release_id': provider.status()['public_release_id'],
                'entities': [dict(r) for r in db.execute('SELECT * FROM graph_entities ORDER BY id')],
                'relations': [dict(r) for r in db.execute('SELECT * FROM graph_relations ORDER BY id')],
                'provenance': [dict(r) for r in db.execute('SELECT * FROM graph_provenance ORDER BY relation_id')],
                'interpretation': '已审核原文绑定与规则引用；规则未自动结构化，不推断金融价值或因果关系'}
        save(args.output_directory/'reviewed-graph.json', graph)
        coverage = []
        for fund in manifest['funds']:
            for share in fund['shares']:
                own = [r for r in locators if r['share_class_id'] == share['share_class_id']]
                coverage.append({'product_id': fund['product_id'], **share,
                    'facets': sorted({f for r in own for f in r['facets']}), 'review_keys': [r['key'] for r in own],
                    'legal_effective_period': 'unknown', 'current_sale_status': 'unknown',
                    'channel_specific_terms': 'not_in_this_release', 'risk_grade': 'not_in_this_release',
                    'source_interpretation_limits': fund['conditions']})
        save(args.output_directory/'coverage-matrix.json', coverage)
        result = {'schema_version': 1, 'as_of': AS_OF, 'public_only': True, 'network_calls': 0,
            'cloud_extraction_or_embedding_calls': 0, 'private_database_access': False, 'live_database_written': False,
            'code_sha256': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in
                (Path(__file__).resolve(), ROOT/'local_knowledge/store.py', ROOT/'local_knowledge/graph.py',
                 ROOT/'local_knowledge/pdf_ingest.py', ROOT/'agent_runtime/knowledge_retrieval.py')},
            'annotation_method': manifest['annotation_method'], 'bank_staff_human_review': False,
            'provider_status': provider.status(), 'database': str(target), 'database_sha256': sha(target.read_bytes()),
            'manifest_sha256': sha((OUT/'product-review-manifest.json').read_bytes()),
            'question_set_sha256': sha((OUT/'manual-question-set.json').read_bytes()),
            'versions': versions, 'locators': locators, 'positive_cases': positive, 'negative_cases': negative,
            'summary': {'products': len(manifest['funds']), 'shares': sum(len(f['shares']) for f in manifest['funds']),
                'documents_parsed': len(versions), 'reviewed_spans': len(locators),
                'retrieval_passed': sum(c['retrieval_passed'] for c in positive), 'retrieval_total': len(positive),
                'lexical_only_all_expected_keys_covered': sum(c['lexical_only_expected_keys_covered'] for c in positive),
                'negative_passed': sum(c['passed'] for c in negative), 'negative_total': len(negative),
                'final_answer_semantics': 'pending_joint_agent_acceptance',
                'median_retrieval_ms': statistics.median(c['elapsed_ms'] for c in positive),
                'max_retrieval_ms': max(c['elapsed_ms'] for c in positive),
                'elapsed_seconds': time.perf_counter() - started, 'max_rss_bytes_macos': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}}
        save(args.output_directory/'retrieval-evaluation.json', result)
        print(json.dumps(result['summary'], ensure_ascii=False), flush=True)
        return 0 if all(c['retrieval_passed'] for c in positive) and all(c['passed'] for c in negative) else 1


if __name__ == '__main__':
    raise SystemExit(main())
