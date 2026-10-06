"""隔离验证建库设计；所有测试数据为明确标记的合成夹具，不接银行或模型。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parent
SCHEMA = (ROOT / 'schema.sql').read_text(encoding='utf-8')
HASH = hashlib.sha256(b'isolated-schema-fixture').hexdigest()
NOW = '2026-10-05T00:00:00Z'


def insert(db, table, **values):
    columns = ','.join(values)
    marks = ','.join('?' for _ in values)
    db.execute(f'INSERT INTO {table} ({columns}) VALUES ({marks})', tuple(values.values()))


def verify():
    db = sqlite3.connect(':memory:')
    db.executescript(SCHEMA)
    checks = []

    def succeeds(name, check):
        assert check(), name
        checks.append({'name': name, 'passed': True})

    def rejects(name, action):
        db.execute('SAVEPOINT rejection_case')
        try:
            action()
        except sqlite3.IntegrityError:
            checks.append({'name': name, 'passed': True})
        else:
            raise AssertionError(f'未拦截：{name}')
        finally:
            db.execute('ROLLBACK TO rejection_case')
            db.execute('RELEASE rejection_case')

    insert(db, 'sources', source_id='fixture-source', publisher='合成夹具，非真实金融资料',
           canonical_url='https://example.invalid/schema-fixture', read_scope='full_text',
           usage_policy_json='{}', enabled=1)
    insert(db, 'products', product_id='fixture-product', issuer_id='fixture-issuer',
           registry_scheme='fixture', registry_code='TEST-ONLY', name='仅建库验证',
           asset_type='fixture', data_origin='simulated', identity_status='verified')
    for share in ('A', 'C'):
        insert(db, 'share_classes', share_id=share, product_id='fixture-product', variant_key=share,
               registry_scheme='fixture', share_code='TEST-' + share, currency='CNY')
    for number, scope in ((1, 'full_text'), (2, 'full_text'), (3, 'metadata_only')):
        doc, version, parse = f'd{number}', f'v{number}', f'p{number}'
        insert(db, 'documents', document_id=doc, source_id='fixture-source',
               canonical_url=f'https://example.invalid/{doc}', document_type='fixture',
               visibility='public', workspace_id=None, outbound_policy='deny', data_origin='simulated')
        insert(db, 'document_versions', version_id=version, document_id=doc, version_no=1,
               raw_sha256=HASH, raw_local_path=f'fixtures/{doc}.txt', published_at=NOW,
               fetched_at=NOW, read_scope=scope, lifecycle='verified')
        insert(db, 'document_parses', parse_id=parse, version_id=version, parser_name='fixture',
               parser_revision='1', normalization_revision='1', parsed_sha256=HASH,
               parsed_local_path=f'fixtures/{doc}.json', completeness='complete', quality_report_json='{}')
        insert(db, 'document_subjects', version_id=version, share_id='A', channel_id=None, relation='terms')
        insert(db, 'evidence_spans', span_id=number, parse_id=parse, version_id=version,
               ordinal=0, kind='paragraph', section_path='合成测试/赎回', locator_json='{}',
               original_text='合成夹具：持有期限以条款为准。', structured_json='{}')

    def chunk(number, parse='p1', version='v1'):
        insert(db, 'chunks', chunk_id=number, parse_id=parse, version_id=version,
               chunker_revision='fixture-1', ordinal=number, original_text='合成检索夹具。',
               embedding_text='合成检索夹具。', embedding_text_sha256=HASH,
               title_terms='合成 测试', body_terms='持有 期限 赎回 条款', token_count=8)

    chunk(1)
    insert(db, 'chunk_spans', chunk_id=1, span_id=1, parse_id='p1', version_id='v1', ordinal=0)
    succeeds('中文预分词全文索引可召回', lambda: db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '赎回' ORDER BY bm25(chunks_fts)"
    ).fetchall() == [(1,)])
    rejects('跨解析版本的片段原文映射被拒绝', lambda: insert(db, 'chunk_spans',
        chunk_id=1, span_id=2, parse_id='p1', version_id='v1', ordinal=1))
    rejects('元数据来源不能生成正文索引', lambda: chunk(3, 'p3', 'v3'))
    rejects('原件哈希不可覆盖', lambda: db.execute(
        "UPDATE document_versions SET raw_sha256=? WHERE version_id='v1'", ('b' * 64,)))
    rejects('解析版本不可覆盖', lambda: db.execute(
        "UPDATE document_parses SET parser_revision='2' WHERE parse_id='p1'"))

    for share in ('A', 'C'):
        insert(db, 'fact_assertions', fact_id='f' + share, share_id=share,
               predicate='fixture-only', value_type='text', value_text='条款', unit='text',
               recorded_at=NOW, nature='observed', conditions_json='{}', verification='pending')
    quote = '持有期限'
    start = '合成夹具：持有期限以条款为准。'.index(quote)
    evidence = dict(span_id=1, relation='supports', start_char=start, end_char=start + len(quote))
    insert(db, 'fact_evidence', fact_id='fA', quote_text=quote, **evidence)
    succeeds('正确原文跨度引用可入库', lambda: db.execute('SELECT count(*) FROM fact_evidence').fetchone()[0] == 1)
    rejects('伪造引文被拒绝', lambda: insert(db, 'fact_evidence', fact_id='fA',
        quote_text='不存在的保本承诺', **evidence))
    rejects('原文不能支持其他份额的事实', lambda: insert(db, 'fact_evidence',
        fact_id='fC', quote_text=quote, **evidence))
    rejects('已记录的事实数值不能原地覆盖', lambda: db.execute(
        "UPDATE fact_assertions SET value_text='其他值' WHERE fact_id='fA'"))

    insert(db, 'embedding_profiles', embedding_profile_id='fixture-not-a-real-model',
           model_name='fixture-only', model_revision='0', weights_sha256=HASH,
           tokenizer_revision='0', dimensions=2, max_input_tokens=16,
           distance='cosine', normalize=1, query_prefix='')
    insert(db, 'releases', release_id='r1', scope_key='fixture',
           embedding_profile_id='fixture-not-a-real-model', vector_collection='fixture-only-r1',
           chunker_revision='fixture-1', status='staging', expected_chunk_count=1,
           manifest_sha256=HASH, check_report_json='{}', created_at=NOW)
    rejects('空发布清单不能变成 ready', lambda: db.execute("UPDATE releases SET status='ready' WHERE release_id='r1'"))
    insert(db, 'release_chunks', release_id='r1', chunk_id=1, vector_point_id='fixture-point',
           embedding_text_sha256=HASH, lexical_ready=1, vector_ready=0)
    rejects('向量未完成不能发布', lambda: db.execute("UPDATE releases SET status='ready' WHERE release_id='r1'"))
    rejects('当前指针不能指向 staging', lambda: insert(db, 'current_releases', scope_key='fixture', release_id='r1'))

    # 这里只模拟工作进程的完成标志以测试 SQL 发布约束；没有生成任何真实向量。
    db.execute("UPDATE release_chunks SET vector_ready=1, embedding_text_sha256=? WHERE release_id='r1'", ('c' * 64,))
    rejects('片段哈希不一致不能发布', lambda: db.execute("UPDATE releases SET status='ready' WHERE release_id='r1'"))
    db.execute("UPDATE release_chunks SET embedding_text_sha256=? WHERE release_id='r1'", (HASH,))
    db.execute("UPDATE releases SET status='ready' WHERE release_id='r1'")
    insert(db, 'current_releases', scope_key='fixture', release_id='r1')
    succeeds('完整模拟清单可切换发布指针', lambda: db.execute('SELECT release_id FROM current_releases').fetchall() == [('r1',)])
    rejects('已发布清单不能覆盖', lambda: db.execute(
        "UPDATE release_chunks SET vector_ready=0 WHERE release_id='r1'"))
    rejects('已发布向量集合不能替换', lambda: db.execute(
        "UPDATE releases SET vector_collection='another-collection' WHERE release_id='r1'"))
    succeeds('已发布片段可见', lambda: db.execute('SELECT count(*) FROM available_release_chunks').fetchone()[0] == 1)
    db.execute("UPDATE document_versions SET lifecycle='withdrawn' WHERE version_id='v1'")
    succeeds('原文撤回后旧索引片段立即不可用', lambda: db.execute('SELECT count(*) FROM available_release_chunks').fetchone()[0] == 0)

    chunk(2, 'p2', 'v2')
    db.execute('DELETE FROM chunks WHERE chunk_id=2')
    succeeds('删除片段后全文索引同步删除', lambda: db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '赎回'"
    ).fetchall() == [(1,)])
    succeeds('所有外键完整', lambda: not db.execute('PRAGMA foreign_key_check').fetchall())
    succeeds('数据库完整性检查', lambda: db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok')
    db.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")
    checks.append({'name': '全文索引与内容表一致', 'passed': True})
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'chunks_fts_%' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    db.close()
    return {'status': 'schema_checks_passed', 'sqlite_version': sqlite3.sqlite_version,
            'tables': tables, 'checks': checks, 'check_count': len(checks),
            'fixtures': '仅内存中的合成测试资料', 'real_financial_documents_ingested': 0,
            'embedding_inference_run': False, 'qdrant_collection_created': False,
            'production_connected': False, 'rag_end_to_end_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    report = verify()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / 'schema-validation.sqlite3'
    if target.exists():
        raise FileExistsError(f'不覆盖已有数据库：{target}')
    with sqlite3.connect(target) as db:
        db.executescript(SCHEMA)
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    report['schema_database'] = str(target.resolve())
    report_path = args.output_dir / 'schema-validation.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': report['status'], 'checks': report['check_count'],
                      'schema_database': str(target), 'report': str(report_path),
                      'rag_end_to_end_verified': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
