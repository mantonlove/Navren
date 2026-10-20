"""仅消费请求内真实 Base 守卫添加的公开证据范围说明。"""
from __future__ import annotations

from copy import deepcopy
import math

from base_runtime.evidence import EvidenceGuardedClient
from simulated_bank import BankError


BASE_NOTICE = '本轮只核验选中公开对象的目录字段，不能据此推断即时可买、完整费率、收益或底层份额。'
GAP_NOTICE = '仍有对象、维度或时效未核实；只能回答已核实部分并说明缺口，比较对象不齐须追问。'
SCOPE_KEY = 'public_evidence_limits'
_NOTICES = (BASE_NOTICE, BASE_NOTICE + GAP_NOTICE)


class ReviewScopeError(BankError):
    def __init__(self):
        # Never include the rejected packet/notice in an error or log.
        super().__init__('AGENT_REVIEW_SCOPE_INVALID', '公开证据审核范围合同无效，答复未放行。', 503)


def validate_review_scope(value):
    """精确值校验；本函数不证明或授予该字段的受信来源。"""
    if type(value) is not str or value not in _NOTICES:
        raise ReviewScopeError()
    return value


def _json(value):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json(item) for key, item in value.items())
    return False


def _equal(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(_equal(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def trusted_review_packet(reviewer, packet):
    """预算和隐私投影前捕捉真实 `_request_args` 的唯一许可变换。

    仅 exact Base Guard 可以派生范围。普通客户端原件不变；它或模型
    自报的同名字段不被接受。完整输入和输出均深拷贝，非范围字段类型
    及成员必须逐值相同。发送前的 Base fresh 门仍由原守卫执行。
    """
    if type(packet) is not dict or not _json(packet):
        raise ReviewScopeError()
    original = deepcopy(packet)
    if type(reviewer) is not EvidenceGuardedClient:
        if SCOPE_KEY in original or 'catalog_information' in original:
            raise ReviewScopeError()
        return original

    if 'notice' not in vars(reviewer):
        raise ReviewScopeError()
    notice = vars(reviewer)['notice']
    if notice is not None:
        validate_review_scope(notice)
    if SCOPE_KEY in original:
        if notice is None or not _equal(original[SCOPE_KEY], notice):
            raise ReviewScopeError()
    expected = deepcopy(original)
    if notice is not None:
        expected[SCOPE_KEY] = notice
    catalog=vars(reviewer).get('_catalog_information')
    if catalog is not None:
        from local_knowledge.catalog_information import validate_snapshot_shape
        catalog=validate_snapshot_shape(catalog)
        if 'catalog_information' in original and not _equal(original['catalog_information'],catalog):raise ReviewScopeError()
        expected['catalog_information']=deepcopy(catalog)
    elif 'catalog_information' in original:raise ReviewScopeError()
    try:
        transformed = reviewer._request_args((deepcopy(original),))
    except Exception:
        raise ReviewScopeError() from None
    if (type(transformed) is not tuple or len(transformed) != 1
            or type(transformed[0]) is not dict or not _json(transformed[0])
            or not _equal(transformed[0], expected)):
        raise ReviewScopeError()
    return deepcopy(transformed[0])
