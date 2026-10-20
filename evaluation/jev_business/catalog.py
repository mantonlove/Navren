"""只读取已有问句，不为实验改写输入或添加合成收益题。"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = Path(__file__).with_name('cases.json')

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def source_question(case, root=ROOT):
    source = case['origin']
    path = root / source['path']
    if source['type'] == 'json_case':
        data = json.loads(path.read_text())
        return next(c for c in data['cases'] if c['id'] == source['id'])['question']
    if source['type'] == 'jsonl_case':
        data = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return next(c for c in data if c['case_id'] == source['id'])['input_text']
    # 代码原件没有结构化 case_id：原始字符串和原方法名共同绑定。
    body = path.read_text()
    if source['method'] not in body or case['question'] not in body:
        raise ValueError('original_question_missing:' + case['id'])
    return case['question']

def load_dataset(root=ROOT):
    data = json.loads((root / 'evaluation/jev_business/cases.json').read_text())
    for path, expected in data['input_source_sha256'].items():
        if sha(root/path) != expected:
            raise ValueError('input_source_changed:' + path)
    for case in data['cases']:
        if source_question(case, root) != case['question']:
            raise ValueError('original_question_changed:' + case['id'])
    return data

def selected_cases(data, ids=None, *, regression=False):
    group = 'regression' if regression else 'development'
    available = [c for c in data['cases'] if c['group'] == group]
    if ids is None:
        return available
    requested = [x.strip() for item in ids for x in item.split(',') if x.strip()]
    if not requested or len(set(requested)) != len(requested):
        raise ValueError('empty_or_duplicate_case_ids')
    known = {c['id']: c for c in data['cases']}
    for ident in requested:
        if ident not in known:
            raise ValueError('unknown_case_id:' + ident)
        if known[ident]['group'] == 'excluded':
            raise ValueError('oracle_pending:' + ident)
        if known[ident]['group'] != group:
            raise ValueError('case_group_mismatch:' + ident)
    return [c for c in available if c['id'] in requested]
