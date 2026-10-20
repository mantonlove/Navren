"""Explicit successor implementation binding; immutable casebook and gold stay pinned."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/product-insurance-implementation-20261018/acceptance'
ORIGINAL_DIGESTS = {
    'casebook.json': 'cab79b695bf1d1a1d5185caf05b2e1d48ad43392cf3e91b4f6da95565666251e',
    'api-map-v1.json': '9caef5264fc4d130260cda346d9919e77520beeb4bfbe512f077413d592a6df9',
    'security-preservation.json': '876d673214a622609c13fb5500bac18f2ca6d4458aa4a02d93730a5c0a3cccf1',
}
# Explicit T01 successor adoption, retaining pre-T01 manifest and all gold.
ADOPTED_API = 'ed642af824d7859f5a6bf90ea0a789a4105499bad0e7edba036b00164c9b82c3'
ADOPTED_STATUS = 'FROZEN_V1_FINAL_CONSUMER_ENGINEERING_PASSED_PENDING_JOINT_ADOPTION'


def current_adoption():
    path = OUT / 'implementation-input-adoption.json'
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if value.get('original_files') != ORIGINAL_DIGESTS:
        raise ValueError('original_freeze_identity_changed')
    if (value.get('api_contract', {}).get('sha256') != ADOPTED_API
            or value.get('api_contract', {}).get('status') != ADOPTED_STATUS):
        raise ValueError('successor_requires_new_explicit_adoption')
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    for name, expected in value['original_files'].items():
        original = OUT / 'frozen-original-inputs' / name
        current = ROOT / 'evaluation/product_insurance' / name
        if digest(original) != expected or digest(current) != expected:
            raise ValueError('original_acceptance_input_changed:' + name)
    casebook = json.loads((ROOT / 'evaluation/product_insurance/casebook.json').read_text())
    for key, expected in value['unchanged_gold_subtrees'].items():
        actual = hashlib.sha256(json.dumps(casebook[key], ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if actual != expected:
            raise ValueError('acceptance_gold_changed:' + key)
    api = ROOT / value['api_contract']['path']
    if digest(api) != value['api_contract']['sha256']:
        raise ValueError('adopted_implementation_drifted')
    if json.loads(api.read_text())['status'] != value['api_contract']['status']:
        raise ValueError('adopted_status_drifted')
    return value
