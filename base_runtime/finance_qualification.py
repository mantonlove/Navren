"""受信金融来源的风险资格合同；不把画像或布尔确认升级为正式评估。

只允许基座把已核验的 records.risk_source 传给本模块。此处校验来源合同，
不认证外部机构、不接入真实生产评估，也不读取客户参数、画像或模型输出。
"""
from dataclasses import fields

from financial_methods.allocation import FormalRisk
from financial_methods.contracts import MetricContext, iso_date
from simulated_bank import BankError


CONTRACT = 'navren.finance-risk-qualification/1'
_RISK_FIELDS = {field.name for field in fields(FormalRisk)}
_SOURCE_FIELDS = {
    'configuration_source', 'not_real_risk_assessment', 'assessment_type',
    'assessment_status', 'subject_id', 'currency', 'data_scope', 'assessment_ref',
    'source_version', 'confirmation_source', 'confirmation_ref',
    'confirmation_source_version', 'confirmed_by_subject_id', 'effective_on',
    'valid_through', 'level',
}
_FORMAL_FIELDS = _SOURCE_FIELDS - {'not_real_risk_assessment'}
_CONFIGURATIONS = {'formal_assessment', 'personal_simulation', 'navren_synthetic',
                   'ordinary_profile', 'missing'}
_ASSESSMENT_STATUSES = {'completed', 'pending', 'in_progress', 'cancelled'}


def _invalid(message):
    raise BankError('METRIC_ARGUMENTS_INVALID', message, 422)


def _version_conflict():
    raise BankError('METRIC_VERSION_CONFLICT', '风险评估或本人确认的来源版本已变化。', 409)


def _string(value):
    return isinstance(value, str) and bool(value.strip())


def _refs(value):
    if (not isinstance(value, (tuple, list)) or any(not _string(ref) for ref in value)
            or len(set(value)) != len(value)):
        _invalid('风险证据引用必须为无重复的非空字符串序列。')
    return tuple(value)


def _versions(value):
    pairs = tuple(value.items()) if isinstance(value, dict) else value
    if not isinstance(pairs, (tuple, list)):
        _invalid('风险来源版本必须为明确的引用与版本对应关系。')
    result = {}
    for pair in pairs:
        if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or not all(_string(item) for item in pair)):
            _invalid('风险来源版本必须为非空字符串键值对。')
        ref, version = pair
        if ref in result and result[ref] != version:
            _version_conflict()
        result[ref] = version
    return result


def _day(value):
    if value is None:
        return None
    if not isinstance(value, str):
        _invalid('风险评估日期必须为ISO公历日期或明确缺项。')
    try:
        return iso_date(value)
    except (TypeError, ValueError):
        _invalid('风险评估日期必须为ISO公历日期或明确缺项。')


def _level(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 5):
        _invalid('风险等级必须为1至5的整数或明确缺项。')


def _risk_row(risk):
    if risk is None:
        return None
    if isinstance(risk, FormalRisk):
        row = {field.name: getattr(risk, field.name) for field in fields(FormalRisk)}
    elif isinstance(risk, dict):
        row = dict(risk)
    else:
        _invalid('风险原件必须为对象或明确缺项。')
    if set(row) - _RISK_FIELDS:
        _invalid('风险原件不得携带画像或未登记的资格字段。')
    for name in ('subject_id', 'currency', 'data_scope'):
        if name in row and not _string(row[name]):
            _invalid('风险原件的主体、币种和数据范围必须明确。')
    if 'confirmed' in row and type(row['confirmed']) is not bool:
        _invalid('风险原件的本人确认标记必须为布尔值。')
    if 'level' in row:
        _level(row['level'])
    for name in ('effective_on', 'valid_through'):
        if name in row:
            _day(row[name])
    if 'input_refs' in row:
        row['input_refs'] = _refs(row['input_refs'])
    if 'source_versions' in row:
        row['source_versions'] = tuple(_versions(row['source_versions']).items())
    return row


def qualify_risk(risk, source, context, provenance):
    """返回 (FormalRisk或None, 缺项原因列表, 来源分类字符串)。

    formal_assessment 仅表示来源分类；状态未完成、未本人确认、原件缺失或
    评估过期时仍返回 None。正式来源必须证明评估及本人确认两个实际引用，
    二者可以指向同一份同时包含评估与确认的受信原件。
    """
    if not isinstance(context, MetricContext) or not isinstance(provenance, dict):
        _invalid('风险资格核验需要受信MetricContext和证明目录。')
    risk_provenance = _refs(provenance.get('risk', ()))
    row = _risk_row(risk)
    if source is None:
        return None, ['risk_source_missing'], 'missing'
    if not isinstance(source, dict) or set(source) - _SOURCE_FIELDS:
        _invalid('风险来源必须为已登记来源合同对象。')
    if not source:
        return None, ['risk_source_missing'], 'missing'
    configuration = source.get('configuration_source')
    if configuration is not None and (not isinstance(configuration, str) or configuration not in _CONFIGURATIONS):
        _invalid('风险配置来源分类无效。')
    if ('not_real_risk_assessment' in source
            and type(source['not_real_risk_assessment']) is not bool):
        _invalid('非真实风险评估标记必须为布尔值。')
    for name in _SOURCE_FIELDS - {'configuration_source', 'not_real_risk_assessment',
                                  'level', 'effective_on', 'valid_through'}:
        if name in source and not _string(source[name]):
            _invalid('风险来源字段必须为非空字符串，不能嵌入客户画像。')
    if 'assessment_status' in source and source['assessment_status'] not in _ASSESSMENT_STATUSES:
        _invalid('风险来源的完成状态无效。')
    if 'level' in source:
        _level(source['level'])
    for name in ('effective_on', 'valid_through'):
        if name in source:
            _day(source[name])
    nonformal = (configuration in {'personal_simulation', 'navren_synthetic', 'ordinary_profile'}
                 or source.get('not_real_risk_assessment') is True)
    formal = configuration == 'formal_assessment' or source.get('assessment_type') == 'formal_assessment'
    if formal and nonformal:
        _invalid('正式评估标记与模拟画像或非真实评估标记冲突。')
    if nonformal:
        classification = 'ordinary_profile' if configuration == 'ordinary_profile' else 'personal_simulation'
        return None, ['risk_source_not_formal'], classification
    if configuration is None or configuration == 'missing':
        if formal:
            _invalid('正式风险评估必须明确登记配置来源。')
        return None, ['risk_source_missing'], 'missing'

    classification = 'formal_assessment'
    if not _FORMAL_FIELDS <= set(source):
        _invalid('正式风险来源缺少评估原件或本人确认的明确来源合同。')
    if source['assessment_type'] != 'formal_assessment':
        _invalid('正式风险来源的实际评估类别与配置分类不一致。')
    for name in _FORMAL_FIELDS - {'level', 'effective_on', 'valid_through'}:
        if not _string(source[name]):
            _invalid('正式风险来源字段必须明确，不能只提供confirmed布尔值。')
    _level(source['level'])
    effective, through = _day(source['effective_on']), _day(source['valid_through'])
    if effective is not None and through is not None and through < effective:
        _invalid('正式风险评估有效期间倒置。')
    if any(source[name] != getattr(context, name) for name in ('subject_id', 'currency', 'data_scope')):
        _invalid('正式风险评估来源与受信主体、币种或数据范围不一致。')

    versions = dict(context.source_versions)
    references = ((source['assessment_ref'], source['source_version']),
                  (source['confirmation_ref'], source['confirmation_source_version']))
    for ref, version in references:
        if ref not in context.input_refs or ref not in risk_provenance or ref not in versions:
            _invalid('风险评估及本人确认原件必须进入MetricContext和风险证明目录。')
        if versions[ref] != version:
            _version_conflict()
    if row is None:
        return None, ['formal_risk_missing'], classification
    if set(row) != _RISK_FIELDS:
        _invalid('正式风险原件结构不完整。')
    if any(row[name] != source[name] for name in
           ('subject_id', 'currency', 'data_scope', 'level', 'effective_on', 'valid_through')):
        _invalid('正式风险原件与来源记录的主体、日期或等级不一致。')
    row_versions = dict(row['source_versions'])
    if set(row_versions) != set(row['input_refs']):
        _invalid('正式风险原件需要逐项绑定输入引用和精确版本。')
    for ref, version in row_versions.items():
        if ref not in context.input_refs or ref not in risk_provenance or ref not in versions:
            _invalid('正式风险原件的实际来源不属于受信风险证明目录。')
        if versions[ref] != version:
            _version_conflict()
    if any(ref not in row['input_refs'] for ref, _ in references):
        _invalid('正式风险原件未实际引用评估记录或本人确认记录。')

    reasons = []
    if source['assessment_status'] != 'completed':
        reasons.append('formal_risk_not_completed')
    if (row['confirmed'] is not True or source['confirmation_source'] != 'subject_confirmation'
            or source['confirmed_by_subject_id'] != context.subject_id):
        reasons.append('formal_risk_not_subject_confirmed')
    if source['level'] is None or effective is None or through is None:
        reasons.append('formal_risk_assessment_details_missing')
    elif not effective <= iso_date(context.as_of or context.period_start) <= through:
        reasons.append('formal_risk_not_current')
    if reasons:
        return None, reasons, classification
    return FormalRisk(**row), [], classification
