"""出站金额表示边界；保留纯 Python 计算，在原发行签名之前应用。"""
from copy import deepcopy
from dataclasses import fields
import hashlib
from pathlib import Path

from financial_methods import MetricContext, money_metric as original_money_metric
from financial_methods.contracts import FinancialMetric
from simulated_bank import BankError

MAX_SAFE_MINOR = 9007199254740991
GAP = 'numeric_range_exceeded'
MONEY_MAP_FIELDS = frozenset(('totals_minor', 'category_expense_minor', 'refund_cumulative_minor'))
METRIC_FIELDS = frozenset(field.name for field in fields(FinancialMetric))
LOADED_POLICY_CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def policy_code_version():
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != LOADED_POLICY_CODE:
        raise BankError('METRIC_POLICY_RESTART_REQUIRED', '金额表示策略已更新，请重新加载服务。', 503)
    return LOADED_POLICY_CODE


def unavailable():
    raise BankError('METRIC_NUMERIC_RANGE_EXCEEDED', '金额超出当前精确表示范围，结果暂不可用。', 503)


def ensure_money_ranges(value):
    """仅明确金额叶子；日期、版本等其它整数不受此门限制。"""
    policy_code_version()
    _ensure_money_ranges(value)


def _ensure_money_ranges(value):
    if isinstance(value, dict):
        for key, item in value.items():
            # 原月/年报按流量kind、消费分类，退款来源按关联ID存储整数分。
            # 这些是明示单层金额map；不把日期/计数/constraints等整数容器当钱。
            if key in MONEY_MAP_FIELDS and item is not None:
                if not isinstance(item, dict):
                    unavailable()
                for amount in item.values():
                    if type(amount) is not int or abs(amount) > MAX_SAFE_MINOR:
                        unavailable()
            if key.endswith('_minor') and item is not None and not isinstance(item, (dict, list, tuple)):
                if type(item) is not int or abs(item) > MAX_SAFE_MINOR:
                    unavailable()
            if key == 'commitment_allocations' and isinstance(item, (list, tuple)):
                for pair in item:
                    if isinstance(pair, (list, tuple)) and len(pair) == 2:
                        if type(pair[1]) is not int or abs(pair[1]) > MAX_SAFE_MINOR:
                            unavailable()
            _ensure_money_ranges(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _ensure_money_ranges(item)


def wire_money_metric(context, name, basis, formula_id, value_minor, *, status='calculated',
                      gaps=(), excluded=(), assumptions=()):
    """仍调用原 19 字段构造器，重新计算 metric_id，绝不沿用被修改的 ID。"""
    ensure_money_ranges(assumptions)
    if type(value_minor) is int and abs(value_minor) > MAX_SAFE_MINOR:
        value_minor, status, gaps = None, 'unavailable', (*gaps, GAP)
    return original_money_metric(context, name, basis, formula_id, value_minor,
                                 status=status, gaps=gaps, excluded=excluded, assumptions=assumptions)


def _project_metric(node):
    if type(node) is not dict or set(node) != METRIC_FIELDS:
        raise BankError('METRIC_SOURCE_INVALID', '金融指标必须保留完整来源合同。', 503)
    ensure_money_ranges(node['assumed_inputs'])
    if node['value_minor'] is None or (type(node['value_minor']) is int and abs(node['value_minor']) <= MAX_SAFE_MINOR):
        return deepcopy(node)
    context = MetricContext(node['subject_id'], node['currency'], node['data_scope'],
        as_of=node['as_of'], period_start=node['period_start'], period_end=node['period_end'],
        input_refs=tuple(node['input_refs']), source_versions=tuple(node['source_versions'].items()))
    original = original_money_metric(context, node['metric_name'], node['basis'], node['formula_id'], node['value_minor'],
        status=node['calculation_status'], gaps=tuple(node['coverage_gaps']),
        excluded=tuple(node['excluded_items']), assumptions=tuple(node['assumed_inputs'])).to_dict()
    if original != node:
        raise BankError('METRIC_SOURCE_INVALID', '金融指标原件和内容标识不一致。', 503)
    return wire_money_metric(context, node['metric_name'], node['basis'], node['formula_id'], node['value_minor'],
        status=node['calculation_status'], gaps=tuple(node['coverage_gaps']),
        excluded=tuple(node['excluded_items']), assumptions=tuple(node['assumed_inputs'])).to_dict()


def project_report_money(report):
    """只改可信、未发行报告中已知的完整指标路径；未知引用不猜测同步。"""
    body = deepcopy(report)
    containers = []
    if isinstance(body, dict):
        for key in ('metrics', 'named_metrics'):
            if key in body:
                containers.append(body[key])
        for plan in body.get('plans', []):
            if isinstance(plan, dict) and 'metrics' in plan:
                containers.append(plan['metrics'])
    replaced = {}
    for container in containers:
        if not isinstance(container, (dict, list)):
            raise BankError('METRIC_SOURCE_INVALID', '金融报告指标结构无效。', 503)
        keys = list(container) if isinstance(container, dict) else range(len(container))
        for key in keys:
            original = container[key]; projected = _project_metric(original)
            if projected['metric_id'] != original['metric_id']:
                old = original['metric_id']
                if old in replaced and replaced[old] != projected:
                    raise BankError('METRIC_SOURCE_INVALID', '同一报告的指标别名原件不一致。', 503)
                replaced[old] = projected
            container[key] = projected
    def refuse_residual(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in replaced:
                    raise BankError('METRIC_UNPROJECTED_REFERENCE', '报告还含未同步的原金额引用。', 503)
                refuse_residual(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                refuse_residual(item)
        elif isinstance(value, str) and value in replaced:
            raise BankError('METRIC_UNPROJECTED_REFERENCE', '报告还含未同步的原金额引用。', 503)
    # mini/point 在原消费者中从本次已投影完整合同重新构造；此处不替换字符串或 hash。
    refuse_residual(body)
    if replaced and isinstance(body, dict):
        if body.get('calculation_status') in ('calculated', 'partial'):
            body['calculation_status'] = 'unavailable'
        if body.get('status') in ('calculated', 'partial'):
            body['status'] = 'unavailable'
        for key in ('coverage_gaps', 'reasons'):
            if isinstance(body.get(key), list):
                body[key] = list(dict.fromkeys((*body[key], GAP)))
    # 无 nullable/status 合同的 raw、timeline、checkpoint、detail 越界必须整响应失败。
    ensure_money_ranges(body)
    return body
