"""固定公共字段语义登记与按产品计算的证据覆盖。

本模块不准入资料、不解析新来源，也不判断事实真伪。调用者必须先完成
发布、身份、许可、时间及图路径检查，再传入可用原件。查询词只表示请求；
角色支持只来自已登记来源的原键与被选中的原始引文，不从公司名推断。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from types import MappingProxyType

from .store import WEBANK_PARSER, IDENTIFIER, sha
from .vector_documents import representation
from webank_catalog import URL as WEBANK_DISCLOSURE_URL


SEMANTIC_POLICY = 'public-field-semantics/1'
PRODUCT_SCOPE = 'webank_dated_channel_catalog_product'


@dataclass(frozen=True)
class FieldSemantic:
    original_key: str
    label: str
    meaning: str
    applicable_product_scope: str = PRODUCT_SCOPE
    # These are source-declared types, not a financial product taxonomy inferred
    # from a company or product name. No other catalogue/parser is registered.
    applicable_product_types: tuple[str, ...] = ('source_declared_catalog_type',)
    product_type_basis: str = 'same_version_original_type_field_only'
    evidence_requirement: str = 'registered_single_field_original_span'
    semantic_role: str | None = None
    prohibited_inference: tuple[str, ...] = ()


def _field(key, label, meaning, *, role=None, forbidden=()):
    return FieldSemantic(key, label, meaning, semantic_role=role,
                         prohibited_inference=tuple(forbidden))


FIELD_REGISTRY = MappingProxyType({
    'code': _field('code', '产品代码', '该渠道原件披露的产品代码',
                   forbidden=('underlying_fund_code', 'share_class_mapping')),
    'name': _field('name', '产品名称', '该渠道原件披露的产品名称',
                   forbidden=('product_type_from_name', 'institution_role_from_name')),
    'saleStatus': _field('saleStatus', '销售状态', '资料日期对应的渠道销售状态',
                         forbidden=('current_buyability', 'transaction_authorization')),
    'type': _field('type', '产品类型', '该渠道原件中的产品分类值',
                   forbidden=('unregistered_product_taxonomy',)),
    'placement': _field('placement', '招募类型', '该渠道原件中的募集属性值'),
    'saleType': _field('saleType', '销售类型', '该渠道原件中的销售类型值',
                       forbidden=('sales_institution_identity', 'issuer_identity',
                                  'manager_identity')),
    'risk': _field('risk', '风险等级', '该渠道原件披露的风险等级',
                   forbidden=('personal_suitability', 'loss_probability')),
    'publish': _field('publish', '发行机构', '该渠道原件在发行机构字段披露的机构名称',
                      role='issuer', forbidden=('manager_identity',
                                               'sales_institution_identity',
                                               'custodian_identity')),
    'investor': _field('investor', '合理投资范围', '该渠道原件披露的投资者范围说明',
                       forbidden=('personal_eligibility', 'transaction_authorization')),
    'charge': _field('charge', '收费标准', '该渠道原件披露的收费标准或资料指引',
                     forbidden=('undisclosed_fee_rate', 'manager_identity')),
    'chargeWay': _field('chargeWay', '收费方式', '该渠道原件披露的收费方式或资料指引',
                        forbidden=('undisclosed_fee_rate',)),
    'customerGroupType': _field('customerGroupType', '客群类别范围',
                                '该渠道原件披露的客户类别范围',
                                forbidden=('personal_eligibility',)),
})

# A finite query vocabulary is a retrieval aid, never an authoritative label.
# Keep existing exact field phrases and add source-compatible common wording.
FIELD_PHRASES = MappingProxyType({
    'risk': ('风险等级', '风险级别', '风险几级', '风险档次', 'risk level'),
    'publish': ('发行机构', '发行公司', '发行人', '哪家公司发行', '谁发行',
                '保险公司是哪家', 'issuer', 'issuing institution'),
    'charge': ('收费标准', '费用标准', '费率', '收多少', '收费金额', '收费比例',
               '管理费', 'fee rate'),
    'chargeWay': ('收费方式', '扣费方式', '扣费的做法', '怎么收费', '怎么收取',
                  '怎么交费用', 'fee collection method'),
    'investor': ('合理投资范围', '投资者范围', '风险承受', '风险偏好', '投资者要求',
                 '核保', 'investor requirements'),
    'customerGroupType': ('客群类别', '客户类别', '个人还是机构', '个人还是企业',
                          '企业客户', '机构客户', '公司名义', '企业能投保',
                          '自然人', 'customer group'),
    'saleType': ('销售类型', '销售角色', '代销', '自营', '代理销售', '替别人销售',
                 'sale type'),
    'placement': ('招募类型', '募集性质', '募集属性', '募集方式', '公募', '私募',
                  '公众募集', '公开募集', 'fundraising type', 'offering type'),
    'saleStatus': ('销售状态', '在售', '还能下单', '现在能买', 'sale status'),
    'type': ('产品类型', '什么类型', '产品大类', '产品类别', '产品分类',
             'product type', 'product category'),
    'code': ('产品代码', 'product code'),
    'name': ('产品名称', 'product name'),
})

ROLE_PHRASES = MappingProxyType({
    'issuer': FIELD_PHRASES['publish'],
    'manager': ('管理人', '管理机构', '基金管理公司', '基金管理机构', '谁管理',
                '哪家公司管理', 'fund manager', 'asset manager',
                'management company'),
    'sales_institution': ('销售机构', '代销机构', '销售银行', '代销银行',
                          '哪家银行销售', '哪家银行代销', 'sales institution',
                          'distributor'),
    'custodian': ('托管人', '托管机构', '托管银行', 'custodian'),
})
ROLE_FIELDS = MappingProxyType({'issuer': 'publish'})


def registry_metadata():
    """返回可序列化的登记快照，不含用户查询或金融事实值。"""
    return {'policy': SEMANTIC_POLICY, 'source_id': 'webank-direct-disclosure',
            'parser_id': WEBANK_PARSER, 'shape': 'webank-productpresentation-v1',
            'channel_id': 'WEBANK', 'source_url': WEBANK_DISCLOSURE_URL,
            'fields': {key: asdict(value) for key, value in FIELD_REGISTRY.items()},
            'role_fields': dict(ROLE_FIELDS)}


def _contains(query, phrase):
    # Avoid treating e.g. "subissuer" as the explicit English role "issuer".
    if phrase.isascii():
        return bool(re.search(r'(?<![a-z0-9_])' + re.escape(phrase)
                              + r'(?![a-z0-9_])', query))
    return phrase in query


def query_semantics(query):
    """识别明确请求；未知措辞由上层普通检索继续处理，不补造字段。"""
    if not isinstance(query, str):
        raise ValueError('invalid_semantic_query')
    folded = query.casefold()
    fields = [key for key, phrases in FIELD_PHRASES.items()
              if any(_contains(folded, phrase) for phrase in phrases)]
    roles = [key for key, phrases in ROLE_PHRASES.items()
             if any(_contains(folded, phrase) for phrase in phrases)]
    return {'fields': fields, 'roles': roles,
            'unsupported_roles': [role for role in roles if role not in ROLE_FIELDS]}


def _get(row, key, default=None):
    return row[key] if key in row.keys() else default


def field_evidence(row):
    """描述已登记的完整单字段原件；身份和许可仍由调用者核验。

    续片、原文拼接、其他解析器/来源、非精确键值与缺引文定位一律不支持。
    返回的 role 仅表示原件字段角色；公司名称中的“基金管理”不能改写它。
    """
    try:
        view = representation(row)
        if (view['kind'] != 'webank_field' or _get(row, 'share_class_id') is not None
                or _get(row, 'source_url') != WEBANK_DISCLOSURE_URL
                or _get(row, 'page') is not None):
            return None
        if _get(row, 'status', 'verified') != 'verified':
            return None
        citation_id, version_id = _get(row, 'id'), _get(row, 'version_id')
        if (not isinstance(citation_id, str) or not IDENTIFIER.fullmatch(citation_id)
                or not isinstance(version_id, str) or not IDENTIFIER.fullmatch(version_id)):
            return None
        start, end = _get(row, 'start_char'), _get(row, 'end_char')
        excerpt = row['excerpt']
        if (type(start) is not int or type(end) is not int or start < 0
                or end != start + len(excerpt)):
            return None
        original_hash = _get(row, 'excerpt_sha256')
        if original_hash is not None and original_hash != sha(excerpt):
            return None
        raw = json.loads('{' + excerpt + '}')
        field = view['field']
        value = raw[field]
        registered = FIELD_REGISTRY[field]
        return {'policy': SEMANTIC_POLICY, 'citation_id': citation_id,
                'document_version_id': version_id, 'product_id': row['product_id'],
                'share_class_id': None, 'channel_id': row['channel_id'],
                'original_field': field, 'label': registered.label,
                'meaning': registered.meaning, 'value': value,
                'semantic_role': registered.semantic_role,
                'applicable_product_scope': registered.applicable_product_scope,
                'product_type_basis': registered.product_type_basis,
                'detail_status': view['detail_status'],
                'locator': {'paragraph': row['paragraph'],
                            'start_char': start, 'end_char': end}}
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return None


def assess_semantic_coverage(query, rows, objects, selected_ids=None):
    """对每个已绑定产品单独计算所选原件的字段/角色覆盖。

    ``rows`` 必须已通过上层准入门禁。传入 ``selected_ids`` 后，未返回给用户
    的候选不算覆盖。只返回原件引用和覆盖状态，不保留查询，也不补缺失值。
    未绑定产品时不声称任何支持或缺口；身份歧义由上层继续报告。
    """
    if not isinstance(query, str):
        raise ValueError('invalid_semantic_query')
    rows = list(rows)
    bound = list(objects)
    if any(not isinstance(obj, str) for obj in bound):
        raise ValueError('invalid_semantic_objects')
    bound = sorted(set(bound))
    # Strip only exact names belonging to caller-bound objects so an institution
    # word in the product title does not become a question about its role.
    residual = query
    names = sorted({row['title'] for row in rows
                    if _get(row, 'product_id') in bound and isinstance(_get(row, 'title'), str)},
                   key=lambda value: (-len(value), value))
    for name in names:
        if name:
            residual = residual.replace(name, ' ')
    requests = query_semantics(residual)
    selected = set(selected_ids) if selected_ids is not None else None
    evidence = []
    for row in rows:
        if _get(row, 'product_id') not in bound:
            continue
        if selected is not None and _get(row, 'id') not in selected:
            continue
        item = field_evidence(row)
        if item is not None:
            evidence.append(item)
    supported_roles, field_support, gaps = [], [], []
    for obj in bound:
        product_items = [item for item in evidence if item['product_id'] == obj]
        for field in requests['fields']:
            items = [item for item in product_items if item['original_field'] == field]
            disclosed = [item for item in items if item['detail_status'] == 'disclosed_value']
            status = ('supported' if disclosed else 'reference_only' if items else 'uncovered')
            field_support.append({'product_id': obj, 'field': field, 'status': status,
                                  'citation_ids': sorted({item['citation_id'] for item in disclosed}),
                                  'reference_citation_ids': sorted({item['citation_id'] for item in items
                                                                    if item not in disclosed})})
            if status != 'supported':
                gaps.append({'code': 'semantic_field_reference_only' if items else 'semantic_field_uncovered',
                             'product_id': obj, 'field': field})
        for role in requests['roles']:
            field = ROLE_FIELDS.get(role)
            if field is None:
                gaps.append({'code': 'unsupported_semantic_role', 'product_id': obj,
                             'role': role, 'reason': 'no_registered_role_source'})
                continue
            items = [item for item in product_items if item['original_field'] == field
                     and item['semantic_role'] == role
                     and item['detail_status'] == 'disclosed_value']
            if items:
                supported_roles.append({'product_id': obj, 'role': role,
                                        'original_field': field,
                                        'citation_ids': sorted({item['citation_id'] for item in items}),
                                        'support_scope': 'source_disclosed_role_only'})
            else:
                gaps.append({'code': 'semantic_role_uncovered', 'product_id': obj,
                             'role': role, 'field': field})
    return {'schema_version': 1, 'policy': SEMANTIC_POLICY,
            'requested_fields': requests['fields'], 'requested_roles': requests['roles'],
            'unsupported_roles': requests['unsupported_roles'], 'objects': bound,
            'supported_roles': supported_roles, 'field_support': field_support,
            'coverage_gaps': gaps}
