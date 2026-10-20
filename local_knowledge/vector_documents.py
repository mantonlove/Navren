"""公共原文的确定性检索表示；标签帮助召回，原文库负责证据。

不写摘要、不翻译、不解读数字、不推断产品/份额映射。所有标签来自固定
字段表，表示只能用于本地编码；交付给模型的引文仍由 canonical resolver 读取。
"""
from __future__ import annotations

import json

from .store import PARSER, PDF_PARSER, WEBANK_PARSER, packed, sha


INPUT_POLICY = 'public-vector-documents/2'
# 微众官网公开字段标签。code 是渠道披露代码，不能据此证明底层基金代码。
FIELD_LABELS = {
    'code': '产品代码', 'name': '产品名称', 'saleStatus': '销售状态',
    'type': '产品类型', 'placement': '招募类型', 'saleType': '销售类型',
    'risk': '风险等级', 'publish': '发行机构', 'investor': '合理投资范围',
    'charge': '收费标准', 'chargeWay': '收费方式',
    'customerGroupType': '客群类别范围',
}
_REFERENCES = frozenset(('详见产品页面', '详见产品页', '详见产品页面及合同条款',
                         '详见产品合同', '以保险公司核定为准'))


def _get(row, key, default=None):
    return row[key] if key in row.keys() else default


def representation(row):
    """只附检索上下文，未知/不完整字段原样回退，绝不生成缺失值。

    身份、许可、效期、原件 hash 和图路径仍由 VectorIndex/提供器核验。
    完整单字段 JSON 加标签；大字段的续片保留原文，不能拼成另一份证据。
    """
    title, excerpt = row['title'][:120], row['excerpt']
    result = {'text': title + '\n' + excerpt, 'kind': 'plain_text',
              'field': None, 'detail_status': 'original_text'}
    parser, shape = _get(row, 'parser_id'), _get(row, 'shape')
    if parser == PDF_PARSER and shape == 'pdfplumber-pages/1':
        result['kind'] = 'reviewed_pdf_span'
    elif parser == PARSER and shape == 'market-official-full-text-v1':
        result['kind'] = 'market_full_text'
    elif (parser == WEBANK_PARSER and shape == 'webank-productpresentation-v1'
          and _get(row, 'source_id') == 'webank-direct-disclosure'
          and _get(row, 'channel_id') == 'WEBANK'
          and isinstance(_get(row, 'product_id'), str)
          and row['product_id'].startswith('WEBANK:')):
        result['kind'] = 'webank_raw_span'
        try:
            field = json.loads('{' + excerpt + '}')
        except (ValueError, TypeError, RecursionError):
            return result
        if not isinstance(field, dict) or len(field) != 1:
            return result
        key, value = next(iter(field.items()))
        code = row['product_id'][7:]
        if (key not in FIELD_LABELS or not isinstance(value, str) or not value.strip()
                or packed(key) + ':' + packed(value) != excerpt
                or _get(row, 'paragraph') != 'productPresentation:' + code + '/' + key):
            return result
        # 编码的是已披露值与真实键的字段名；不补费率、解释风险或认证购买资格。
        result.update(text=FIELD_LABELS[key] + '\n' + title + '\n渠道披露代码：' + code + '\n' + excerpt,
                      kind='webank_field', field=key,
                      detail_status='reference_only' if value.strip() in _REFERENCES else 'disclosed_value')
    return result


def input_sha(row):
    # 政策升级也使旧向量失效；无需改权威片段或 SQLite 原件版本。
    return sha(packed([INPUT_POLICY, representation(row)['text']]))
