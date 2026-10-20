"""受限金融方法适配；客户端只选择对象/期间，事实由受信服务读取。

finance_method_sources.snapshot 是维护方注入的只读服务，不是HTTP/model工具。
未接入完整来源时保留缺项；本模块不写账本，也不创建交易草案。
"""
from contextlib import closing
from dataclasses import fields, replace
from datetime import date
import hashlib
import json
from pathlib import Path

from financial_methods import MetricContext, money_metric
from financial_methods.contracts import iso_date
from financial_methods.accounting import BalanceComponent, CashMovement, balance_sheet, cash_statement
from financial_methods.allocation import AllocationGoal, ProductKey, ProductTerms, FormalRisk, arrange_funds
from simulated_bank import BankError
from base_runtime.money_range import project_report_money

METHOD_TOOLS = ('accounting_report', 'returns_report', 'allocation_preview')
CONTRACT = 'navren.finance-method-adapter/1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def code_fingerprint():
    result = hashlib.sha256()
    for name in ('finance_methods.py', 'finance_returns.py', 'finance_sources.py',
                 'finance_occupancy.py', 'finance_qualification.py', 'finance_funding.py', 'money_range.py'):
        path = Path(__file__).with_name(name)
        result.update(name.encode())
        result.update(path.read_bytes() if path.exists() else b'not_connected')
    result.update(b'goal_service.py')
    result.update((Path(__file__).parent.parent/'goal_service.py').read_bytes())
    return result.hexdigest()


def invalid(message='金融方法参数或受限来源结构无效。'):
    raise BankError('METRIC_ARGUMENTS_INVALID', message, 422)


def json_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        invalid()


def typed(cls, value):
    if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
        invalid()
    try:
        return cls(**value)
    except (TypeError, ValueError):
        invalid()


def monetary_structure(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith('_minor') and item is not None and not isinstance(item, (dict, list)) and type(item) is not int:
                invalid('金额必须为整数分，缺项使用null。')
            monetary_structure(item)
    elif isinstance(value, list):
        for item in value:
            monetary_structure(item)
class FinanceMethodAdapter:
    def __init__(self, metrics):
        self.metrics, self.g, self.bank = metrics, metrics.g, metrics.bank

    def arguments(self, workspace, name, raw, db):
        if name not in METHOD_TOOLS or not isinstance(raw, dict):
            invalid()
        args = json_copy(raw)
        common = {'scope', 'expected_versions'}
        allowed = {
            'accounting_report': common | {'source', 'as_of', 'period_start', 'period_end'},
            'returns_report': common | {'portfolio_ref', 'accounting_basis', 'as_of', 'period_start', 'period_end'},
            'allocation_preview': common | {'budget_metric_id', 'constraints_ref', 'product_evidence_refs'},
        }[name]
        if set(args) - allowed or not isinstance(args.get('scope'), str) or not args['scope']:
            invalid()
        scope = args['scope']
        if scope != workspace and not db.execute('SELECT 1 FROM accounts WHERE workspace_id=? AND id=?',
                                                (workspace, scope)).fetchone():
            raise BankError('METRIC_SUBJECT_MISMATCH', '所选范围不属于当前账户。', 403)
        expected = args.get('expected_versions')
        if (not isinstance(expected, dict) or any(not isinstance(k, str) or not k or
                not isinstance(v, str) or not v for k, v in expected.items())):
            invalid('expected_versions需要显式来源版本对象。')
        if name == 'allocation_preview':
            if not all(isinstance(args.get(k), str) and args[k] for k in ('budget_metric_id', 'constraints_ref')):
                invalid()
            refs = args.get('product_evidence_refs', [])
            if not isinstance(refs, list) or len(refs) > 16 or any(not isinstance(r, str) or not r for r in refs) or len(set(refs)) != len(refs):
                invalid()
            args['product_evidence_refs'] = refs
        else:
            keys = ('source',) if name == 'accounting_report' else ('portfolio_ref', 'accounting_basis')
            if not all(isinstance(args.get(k), str) and args[k] for k in keys):
                invalid()
            if name == 'accounting_report' and args['source'] not in ('simulation', 'navren_synthetic', 'imported'):
                invalid('会计来源需要明确选择。')
            if name == 'returns_report' and args['accounting_basis'] not in ('equity', 'history_moving_average'):
                invalid('收益核算口径无效。')
            try:
                if args.get('as_of') is not None:
                    if args.get('period_start') is not None or args.get('period_end') is not None:
                        raise ValueError()
                    values = [args['as_of']]
                else:
                    values = [args['period_start'], args['period_end']]
                    if values[0] > values[1]:
                        raise ValueError()
                for v in values:
                    if not isinstance(v, str) or date.fromisoformat(v).isoformat() != v:
                        raise ValueError()
            except (ValueError, TypeError, KeyError):
                invalid('需要单一时点或完整有效期间。')
        return args

    def snapshot(self, workspace, name, args, db):
        provider = getattr(self.g, 'finance_method_sources', None)
        if provider is not None:
            if not callable(getattr(provider, 'snapshot', None)):
                invalid('受信金融来源尚未提供snapshot合同。')
            value = json_copy(provider.snapshot(workspace, name, args, db=db))
        else:
            value = self._native_snapshot(workspace, name, args, db)
        if not isinstance(value, dict):
            invalid()
        required = {'subject_id', 'currency', 'data_scope', 'coverage_complete', 'input_refs',
                    'source_versions', 'records', 'catalog', 'provenance'}
        if not required <= set(value) or any(not isinstance(value[k], dict) for k in ('source_versions', 'records', 'catalog', 'provenance')):
            invalid('受信来源缺少独立目录或来源合同。')
        refs = value['input_refs']
        if (not isinstance(refs, list) or any(not isinstance(r, str) or not r for r in refs)
                or len(set(refs)) != len(refs) or type(value['coverage_complete']) is not bool
                or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                       for k, v in value['source_versions'].items())
                or any(r not in value['source_versions'] for r in refs)):
            invalid('实际输入引用和版本需要完整有效的来源合同。')
        if any(k.startswith(('finance_input/', 'finance_adapter_', 'tool_table/', 'financial_tool_metrics_'))
               for k in value['source_versions']):
            invalid('来源不得占用指标服务的保留版本名称。')
        monetary_structure(value['records'])
        if value['subject_id'] != args['scope']:
            raise BankError('METRIC_SUBJECT_MISMATCH', '来源对象与所选范围不一致。', 403)
        for key in ('as_of', 'period_start', 'period_end'):
            if name != 'allocation_preview' and value.get(key) != args.get(key):
                raise BankError('METRIC_VERSION_CONFLICT', '来源时点或期间与请求不一致。', 409)
        if name == 'returns_report' and value['records'].get('accounting_basis') != args['accounting_basis']:
            invalid('收益来源口径和请求不一致。')
        if name == 'returns_report' and any(value['records'].get(k) for k in ('opening', 'closing', 'snapshot', 'events')) and value['catalog'].get('portfolio_ref') != args['portfolio_ref']:
            raise BankError('METRIC_SUBJECT_MISMATCH', '收益原件不属于所选组合。', 403)
        for category, refs in value['provenance'].items():
            if (not isinstance(category, str) or not isinstance(refs, list) or
                    any(not isinstance(r, str) or r not in value['input_refs'] or r not in value['source_versions'] for r in refs)):
                invalid('实际输入来源必须进入MetricContext。')
        try:
            self.context(value)
        except (TypeError, ValueError):
            invalid()
        return value

    @staticmethod
    def context(value, additional=()):
        versions = dict(value['source_versions'])
        for ref, version in additional:
            if ref in versions and versions[ref] != version:
                raise BankError('METRIC_VERSION_CONFLICT', '计算原件与核验来源版本冲突。', 409)
            versions[ref] = version
        return MetricContext(value['subject_id'], value['currency'], value['data_scope'],
                             as_of=value.get('as_of'), period_start=value.get('period_start'),
                             period_end=value.get('period_end'), input_refs=tuple(value['input_refs']),
                             source_versions=tuple(versions.items()), coverage_complete=value['coverage_complete'])

    def source_versions(self, workspace, name, args, db):
        value = self.snapshot(workspace, name, args, db)
        return {**value['source_versions'], 'finance_adapter_contract': CONTRACT,
                'finance_adapter_code': code_fingerprint(), 'finance_input/'+name+'/'+workspace: digest(value)}

    def _native_snapshot(self, workspace, name, args, db):
        ws = self.bank._workspace(db, workspace)
        ref = 'finance_source_unavailable/'+name+'/'+args['scope']
        result = {'subject_id': args['scope'], 'currency': 'CNY',
                  'data_scope': 'simulation_bank_ledger', 'coverage_complete': False,
                  'input_refs': [ref], 'source_versions': {ref: str(ws['version'])},
                  'records': {}, 'catalog': {}, 'provenance': {}}
        if name == 'allocation_preview':
            funds = self.metrics.resolve_budget_metric(workspace, args['budget_metric_id'], db=db)
            result.update(data_scope=funds.data_scope, as_of=funds.as_of,
                          period_start=funds.period_start, period_end=funds.period_end)
            # 默认目标全工作区；单账户用途尚没有权威关联，不能私自贴同scope。
            selected = args['constraints_ref']
            if selected != 'goals/current' and not selected.startswith('goals/'):
                raise BankError('METRIC_ARGUMENTS_INVALID', '没有这个受信用途目录。', 422)
            if args['scope'] == workspace:
                constraints = self.g.goals.constraints(workspace, db=db)
                goals = constraints['goals']
                if selected != 'goals/current':
                    goals = [g for g in goals if g['goal_id'] == selected[len('goals/'):]]
                    if not goals:
                        raise BankError('GOAL_NOT_FOUND', '所选用途不属于当前工作区或不再有效。', 404)
                result['records']['goals'] = [{**g, 'data_scope': funds.data_scope} for g in goals]
                result['source_versions'].update(constraints['source_versions'])
                result['input_refs'] += constraints['input_refs']
                result['provenance']['goals'] = constraints['input_refs']
            else:
                result['records']['goals'] = []
            result['records'].update(products=[], risk=None,
                                     risk_source={'configuration_source':'ordinary_profile'})
            result['records']['unavailable_reasons'] = ['official_product_terms_and_formal_risk_not_connected'] if args['product_evidence_refs'] else []
            return result
        result.update({k: args.get(k) for k in ('as_of', 'period_start', 'period_end')})
        if name == 'returns_report':
            result['records']['accounting_basis'] = args['accounting_basis']
            result['records']['unavailable_reasons'] = ['quantity_price_cashflow_boundary_source_not_connected']
            return result
        # 四画像的封存聚合来自原件期末，不能和运行账户现金拼成当前财富。
        demos = self.g.demo_accounts
        if args['source'] == 'navren_synthetic' and args.get('as_of') is None:
            from .finance_sources import synthetic_cash_snapshot
            historical = synthetic_cash_snapshot(demos, workspace, args)
            if historical is not None:
                return historical
        if args['source'] == 'navren_synthetic' and args['scope'] == workspace and demos.is_demo(workspace) and args.get('as_of'):
            record = demos._record(workspace)
            source = json.loads(record['source_snapshot'])
            if args['as_of'] == source['end_date']:
                balances = source['latest_balances']
                names = ('cash', 'investment', 'real_assets', 'liability')
                kinds = ('cash', 'financial_asset', 'nonfinancial_asset', 'liability')
                bases = ('ledger_cash_balance', 'carrying_value', 'carrying_value', 'outstanding_principal')
                source_ref = 'demo_terminal_snapshot/'+record['user_id']
                components = []
                for label, kind, basis in zip(names, kinds, bases):
                    key = label+'_minor' if label != 'liability' else 'liabilities_minor'
                    # 源字段名称明确；未取得字段不填零。
                    amount = balances.get(key)
                    components.append(dict(object_id=source_ref+'/'+label, subject_id=workspace,
                        currency='CNY', as_of=args['as_of'], kind=kind, amount_minor=amount,
                        basis=basis, evidence_ref=source_ref, source_version=record['source_version']))
                result.update(data_scope='navren_synthetic', coverage_complete=True,
                    input_refs=[source_ref], source_versions={source_ref: record['source_version']},
                    records={'components': components},
                    catalog={'expected_object_ids': [source_ref+'/'+x for x in names],
                             'coverage_ref': source_ref, 'complete': True},
                    provenance={'components': [source_ref], 'coverage': [source_ref]})
        return result

    def describe(self, workspace, name, args, db, versions):
        value = self.snapshot(workspace, name, args, db)
        consumed = {**value['source_versions'], 'finance_adapter_contract': CONTRACT,
                    'finance_adapter_code': code_fingerprint(), 'finance_input/'+name+'/'+workspace: digest(value)}
        if any(versions.get(ref) != version for ref, version in consumed.items()):
            raise BankError('METRIC_VERSION_CONFLICT', '实际计算原件与本次核验快照不一致。', 409)
        ctx = self.context(value, versions.items())
        if name == 'accounting_report':
            body = accounting_report(ctx, value['records'], value['catalog'], value['provenance'])
        elif name == 'returns_report':
            from .finance_returns import returns_report
            body = returns_report(ctx, value['records'], value['catalog'], value['provenance'])
        else:
            funds = self.metrics.resolve_budget_metric(workspace, args['budget_metric_id'], db=db)
            if funds.subject_id != args['scope'] or funds.data_scope != value['data_scope'] or any(
                    getattr(funds, k) != value.get(k) for k in ('as_of', 'period_start', 'period_end')):
                raise BankError('METRIC_SUBJECT_MISMATCH', '用途和预算的对象、范围或期间不一致。', 403)
            baseline=self.metrics.resolve_budget_baseline(workspace,args['budget_metric_id'],db=db)
            body = allocation_report(funds, value['records'], value['provenance'], ctx,
                                     bank_db=db,workspace=workspace,baseline=baseline)
        body = project_report_money(body)
        result = {'schema_version': 1, 'adapter_contract': CONTRACT, 'workspace_id': workspace,
                  'scope': args['scope'], 'source_versions': versions, **body,
                  'no_financial_side_effect': True, 'execution_authority': False}
        if isinstance(result['metrics'], dict):
            result['named_metrics'] = result['metrics']
            result['metrics'] = list(result['metrics'].values())
        metrics = result['metrics']
        kinds = {m['metric_id']: 'planning_scenario' if name == 'allocation_preview' else 'recorded_fact'
                 for m in metrics}
        return result, metrics, kinds, {'adapter_contract': CONTRACT, 'ratio_reports': result.get('ratio_reports', []),
              'reasons': result.get('reasons', []), 'scope': args['scope']}


def accounting_report(context, records, catalog, provenance):
    period = context.as_of is None
    reason = None
    diagnostics=[]
    if (catalog.get('complete') is not True or not catalog.get('coverage_ref') or
            catalog.get('coverage_ref') not in dict(context.source_versions) or not provenance.get('coverage')):
        reason = 'independent_coverage_catalog_missing'
    try:
        if period:
            rows = records.get('movements', [])
            if not isinstance(rows, list):
                invalid()
            movements = tuple(typed(CashMovement, r) for r in rows)
            if any(m.evidence_ref not in context.input_refs or dict(context.source_versions).get(m.evidence_ref) != m.source_version for m in movements):
                reason = 'cash_movement_source_not_bound'
            expected = catalog.get('expected_movement_ids')
            if (not isinstance(expected, list) or len(set(expected)) != len(expected) or
                    set(expected) != {m.movement_id for m in movements}):
                reason = 'cash_movement_coverage_incomplete'
            accounts = catalog.get('expected_account_ids')
            if (not isinstance(accounts, list) or not accounts or len(set(accounts)) != len(accounts)
                    or any(m.account_id not in accounts for m in movements)):
                reason = 'independent_cash_account_catalog_missing'
            for key in ('opening_cash', 'closing_cash', 'movements'):
                if not provenance.get(key):
                    reason = 'cash_input_provenance_missing:'+key
            refunds = [m for m in movements if m.kind == 'refund']
            refund_ids = catalog.get('expected_refund_ids')
            if (catalog.get('refunds_complete') is not True or not isinstance(refund_ids, list)
                    or len(set(refund_ids)) != len(refund_ids) or set(refund_ids) != {m.movement_id for m in refunds}):
                reason = 'independent_refund_coverage_missing'
            if refunds and (not provenance.get('refund_originals') or not provenance.get('refund_cumulative')):
                reason = 'refund_original_or_cumulative_provenance_missing'
            if not reason:
                reason = refund_gap(context, movements, records, catalog)
            ctx = replace(context, coverage_complete=context.coverage_complete and reason is None)
            values = cash_statement(ctx, records.get('opening_cash_minor'), records.get('closing_cash_minor'), movements)
            # 覆盖门仍使整表不可用；实际已绑定两腿的缺失原因不能被该门遮掉。
            pairs={}
            for movement in movements:
                try:within=context.period_start<=iso_date(movement.on).isoformat()<=context.period_end
                except (TypeError,ValueError):within=False
                if (movement.kind=='internal_transfer' and movement.related_id
                        and movement.subject_id==context.subject_id and movement.currency==context.currency
                        and movement.evidence_ref in provenance.get('movements',())
                        and movement.evidence_ref in context.input_refs
                        and dict(context.source_versions).get(movement.evidence_ref)==movement.source_version
                        and movement.account_id in (accounts if isinstance(accounts,list) else ())
                        and type(movement.delta_minor) is int
                        and within):
                    pairs.setdefault(movement.related_id,[]).append(movement)
            if any(len(pair)!=2 or pair[0].account_id==pair[1].account_id
                   or sum(m.delta_minor for m in pair)!=0 for pair in pairs.values()):
                diagnostics.append('incomplete_internal_transfer_pair')
                # 正规重建指标身份；不能改gaps却保留原metric_id。
                augmented={}
                for name,metric in values.items():
                    diagnostic_context=MetricContext(metric.subject_id,metric.currency,metric.data_scope,
                        as_of=metric.as_of,period_start=metric.period_start,period_end=metric.period_end,
                        input_refs=metric.input_refs,source_versions=metric.source_versions,
                        coverage_complete=ctx.coverage_complete)
                    augmented[name]=money_metric(diagnostic_context,metric.metric_name,metric.basis,
                        metric.formula_id,metric.value_minor,status=metric.calculation_status,
                        gaps=tuple(dict.fromkeys((*metric.coverage_gaps,*diagnostics))),
                        excluded=metric.excluded_items,assumptions=metric.assumed_inputs)
                values=augmented
            label = 'complete_recorded_cash_statement'
        else:
            rows = records.get('components', [])
            if not isinstance(rows, list):
                invalid()
            if not provenance.get('components'):
                reason = 'balance_input_provenance_missing'
            components = tuple(typed(BalanceComponent, r) for r in rows)
            if any(c.evidence_ref not in context.input_refs or dict(context.source_versions).get(c.evidence_ref) != c.source_version for c in components):
                reason = 'balance_component_source_not_bound'
            ctx = replace(context, coverage_complete=context.coverage_complete and reason is None)
            values = balance_sheet(ctx, components,
                                   expected_object_ids=catalog.get('expected_object_ids', []))
            label = 'same_source_book_balance_sheet'
    except (TypeError, ValueError):
        invalid()
    metrics = [m.to_dict() for m in values.values()]
    reasons = list(dict.fromkeys(([reason] if reason else []) + [g for m in metrics for g in m['coverage_gaps']]))
    return {'report_type': label, 'calculation_status': 'unavailable' if any(m['value_minor'] is None for m in metrics) else 'calculated',
            'metrics': metrics, 'ratio_reports': [], 'reasons': reasons,
            'historical_source_not_current_wealth': context.data_scope == 'navren_synthetic'}


def refund_gap(context, movements, records, catalog):
    refunds = [m for m in movements if m.kind == 'refund']
    if not refunds:
        return None
    originals = records.get('refund_originals')
    cumulative = records.get('refund_cumulative_minor')
    if not isinstance(originals, list) or not isinstance(cumulative, dict) or catalog.get('refunds_complete') is not True:
        return 'refund_original_or_cumulative_coverage_missing'
    if any(not isinstance(r, dict) for r in originals):
        invalid()
    original_ids = [r.get('movement_id') for r in originals]
    if any(not isinstance(i, str) or not i for i in original_ids) or len(set(original_ids)) != len(original_ids):
        return 'refund_original_id_missing_or_duplicate'
    originals = {r['movement_id']: r for r in originals}
    current = {}
    for refund in refunds:
        original = originals.get(refund.related_id)
        if not original or any(original.get(k) != getattr(refund, k) for k in ('subject_id', 'account_id', 'currency')):
            return 'refund_original_identity_mismatch'
        if original.get('kind') not in ('expense', 'loan_interest') or type(original.get('delta_minor')) is not int or original['delta_minor'] >= 0:
            return 'refund_original_expense_invalid'
        try:
            iso_date(original.get('on'))
        except ValueError:
            return 'refund_original_date_invalid'
        if (original['on'] > refund.on or not original.get('evidence_ref')
                or original['evidence_ref'] not in context.input_refs
                or dict(context.source_versions).get(original['evidence_ref']) != original.get('source_version')):
            return 'refund_original_date_or_evidence_invalid'
        if type(refund.delta_minor) is not int or refund.delta_minor < 0:
            return 'refund_amount_invalid'
        current[refund.related_id] = current.get(refund.related_id, 0) + refund.delta_minor
        total = cumulative.get(refund.related_id)
        if type(total) is not int or not current[refund.related_id] <= total <= -original['delta_minor']:
            return 'refund_cumulative_exceeds_original_or_incomplete'
    return None


def _bound_record_sources(row, context, category, provenance):
    refs = row.get('input_refs')
    versions = row.get('source_versions')
    if isinstance(versions, dict):
        versions = tuple(versions.items())
    if (not isinstance(refs, (tuple, list))
            or any(not isinstance(r, str) or r not in context.input_refs for r in refs)
            or not isinstance(versions, (tuple, list))
            or any(not isinstance(p, (tuple, list)) or len(p) != 2
                   or any(not isinstance(v, str) or not v for v in p) for p in versions)):
        invalid('用途、条款和风险原件的实际来源必须进入MetricContext。')
    if provenance.get(category) and any(r not in provenance[category] for r in refs):
        invalid('原件来源不属于本项用途、条款或风险的证明目录。')
    bound = dict(context.source_versions)
    if any(bound.get(r) != v for r, v in versions):
        raise BankError('METRIC_VERSION_CONFLICT', '用途、条款或风险原件的来源版本已变化。', 409)
    if any(r not in dict(versions) for r in refs):
        invalid('原件缺少输入引用的精确来源版本。')
    occupied = row.get('occupancy_refs', ())
    if not isinstance(occupied, (tuple, list)) or any(r not in context.input_refs for r in occupied):
        invalid('占用关联缺少受信来源。')
    row['input_refs'], row['source_versions'] = tuple(refs), tuple(versions)


def allocation_report(funds, records, provenance, context, *, bank_db=None,workspace=None,baseline=None):
    rows = records.get('goals', [])
    if not isinstance(rows, list):
        invalid()
    goals = [];occupancy_gaps=[]
    for row in rows:
        if not isinstance(row, dict):
            invalid()
        row = dict(row)
        if row.get('priority') is not None and type(row['priority']) is not int:
            invalid('用途优先级需要整数或null。')
        if 'confirmed' in row and type(row['confirmed']) is not bool:
            invalid('本人确认状态必须是布尔值。')
        # 后面由真实确认关联与已发行基线派生；绝不采信输入的verified布尔值。
        row['occupancy_verified_in_baseline'] = False
        if row.get('product_key') is not None:
            row['product_key'] = typed(ProductKey, row['product_key'])
        # source native constraints留缺项；不能用默认排序补本人确认。
        row.setdefault('subject_id', funds.subject_id)
        row.setdefault('currency', funds.currency)
        row.setdefault('data_scope', funds.data_scope)
        row.setdefault('input_refs', ())
        row.setdefault('source_versions', ())
        if isinstance(row['source_versions'], dict):
            row['source_versions'] = tuple(row['source_versions'].items())
        _bound_record_sources(row, context, 'goals', provenance)
        row['confirmed'] = row.get('confirmed') is True and 'product_key' in row and bool(row['input_refs']) and bool(provenance.get('goals'))
        if bank_db is not None and workspace is not None:
            from .finance_occupancy import verify_goal_occupancy
            occupancy,gaps=verify_goal_occupancy(bank_db,workspace,funds,row)
            # 未核验声明只作为诊断输入保留；verified=False始终阻断任何免扣信用。
            # 这样重复同一个实际ref仍能被纯引擎标记occupancy_reused，不能被缺项遮掉。
            if occupancy['already_occupied_minor'] is None and type(row.get('already_occupied_minor')) is int:
                occupancy['already_occupied_minor']=row['already_occupied_minor']
            if occupancy['occupancy_verified_in_baseline']:
                included={'fund_holds/'+h['id'] for h in (baseline or {}).get('holds',[])}
                if ((baseline or {}).get('baseline_kind')!='bank_ledger'
                        or not set(occupancy['occupancy_refs'])<=included):
                    occupancy['occupancy_verified_in_baseline']=False
                    gaps=[*gaps,'goal_occupancy_not_deducted_in_issued_baseline']
            row.update(occupancy)
            _bound_record_sources(row,context,'goals',provenance)
            occupancy_gaps.extend(gap+':'+str(row.get('goal_id')) for gap in gaps)
        goals.append(typed(AllocationGoal, row))
    products = []
    product_rows = records.get('products', [])
    if not isinstance(product_rows, list):
        invalid()
    for row in product_rows:
        if not isinstance(row, dict):
            invalid()
        row = dict(row)
        row['key'] = typed(ProductKey, row.get('key'))
        if isinstance(row.get('source_versions'), dict):
            row['source_versions'] = tuple(row['source_versions'].items())
        _bound_record_sources(row, context, 'products', provenance)
        if not provenance.get('products') or not row['input_refs']:
            row['terms_complete'] = False
        products.append(typed(ProductTerms, row))
    from .finance_qualification import qualify_risk
    risk,qualification_reasons,classification=qualify_risk(
        records.get('risk'),records.get('risk_source'),context,provenance)
    # 全部实际消费来源进入每一个规划指标。不是重造预算金额或预算公式。
    merged = dict(funds.source_versions)
    for ref, ver in context.source_versions:
        if ref in merged and merged[ref] != ver:
            raise BankError('METRIC_VERSION_CONFLICT', '预算与用途来源代次冲突。', 409)
        merged[ref] = ver
    bound_funds = replace(funds, input_refs=tuple(dict.fromkeys((*funds.input_refs, *context.input_refs))),
                          source_versions=tuple(merged.items()))
    body = arrange_funds(bound_funds, tuple(goals), tuple(products), risk)
    # 输出原始已发行预算，规划结果使用扩展后的依赖；预算ID绝不重造。
    body['funds'] = funds.to_dict()
    metrics = [m for p in body['plans'] for m in p['metrics'].values()]
    reasons = list(dict.fromkeys([g for p in body['plans'] for g in p['coverage_gaps']]))
    reasons=list(dict.fromkeys([*reasons,*occupancy_gaps]))
    if any(g.product_key is not None for g in goals):
        reasons=list(dict.fromkeys([*reasons,*qualification_reasons]))
    return {'report_type': 'confirmed_uses_planning_only', **body, 'metrics': metrics,
            'ratio_reports': [], 'reasons': reasons,
            'risk_qualification':{'classification':classification,'reasons':qualification_reasons,
                                  'formal_qualification_verified':risk is not None}}
