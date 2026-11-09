"""可信金融指标接线：银行提供事实，financial_methods只计算，不授权交易。"""
from contextlib import closing
from calendar import monthrange
from copy import copy, deepcopy
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading

from simulated_bank import BankError
from simulated_bank.agent_guard import context_hash
from financial_methods import MetricContext, CONTRACT_VERSION
from base_runtime.money_range import wire_money_metric as money_metric, project_report_money, ensure_money_ranges, policy_code_version
from financial_methods.funding import CashSnapshot, FundingEvent, Hold, evaluate_funding
from base_runtime.finance_methods import FinanceMethodAdapter, METHOD_TOOLS, code_fingerprint
from base_runtime.position_currency import position_currency


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def task_records_view(state, capabilities, provider_records):
    """One current owned read join, shared by the tool and metric issuer."""
    fields=('orchestration_tasks','fund_holds','service_quotes','service_reservations')
    result={name:deepcopy(state[name])for name in fields}
    result['currency']=(capabilities.get('currency')
        if capabilities.get('environment')=='simulation'else None)
    result['accounts']=[{key:row[key]for key in ('id','name','currency')}
                        for row in state['accounts']]
    providers={row['id']:row for row in provider_records}
    for quote in result['service_quotes']:
        provider=providers.get(quote.get('provider_id'),{})
        kind=provider.get('service_type')
        quote['type']=kind if kind in ('flowers','cake')else None
        quote['amount_minor']=quote['price_minor']
        quote['expires_on']=quote['valid_until']
    return result


def methods_fingerprint():
    methods=hashlib.sha256()
    for path in sorted((Path(__file__).parent/'financial_methods').glob('*.py')):
        methods.update(path.name.encode());methods.update(path.read_bytes())
    return methods.hexdigest()


LOADED_METHODS_CODE=methods_fingerprint()
TOOL_METRICS_CONTRACT = 'navren.financial-tool-metrics/1'
def tool_metrics_fingerprint():
    digest = hashlib.sha256(Path(__file__).read_bytes())
    digest.update((Path(__file__).parent/'base_runtime/position_currency.py').read_bytes())
    return digest.hexdigest()


LOADED_TOOL_METRICS_CODE = tool_metrics_fingerprint()
LOADED_ADAPTER_CODE = code_fingerprint()

# 表名由服务固定，不接受客户端选择数据库表或可信 kind。
_BUDGET_TABLES = ('accounts','profiles','loans','loan_installments','cards','credit_statements',
                  'subscriptions','mandates','scheduled_payments','fund_holds')
_TOOL_TABLES = {
    'presentation_view': (),
    'accounting_report': ('accounts','holdings','loans','cards','transactions','navren_demo_accounts'),
    'returns_report': ('accounts','holdings','orders','transactions','products','navren_demo_accounts'),
    'allocation_preview': (*_BUDGET_TABLES,'wealth_goals','navren_goal_drafts','goal_allocations','navren_goal_occupancy_links','orchestration_tasks','navren_demo_accounts'),
    'account_summary': ('accounts','profiles','holdings','orders','products'),
    'products': ('products','profiles'),
    'business_records': ('accounts','subscriptions','mandates','orchestration_tasks',
                         'fund_holds','service_quotes','service_reservations','service_providers'),
    'budget': _BUDGET_TABLES,
    'cashflow': (*_BUDGET_TABLES,'orders','beneficiaries','operations'),
    'monthly_statement': ('accounts','statement_periods','archived_transactions','transactions',
                          'finance_data_sources','statement_datasets','imported_statement_transactions'),
    'annual_statement': ('accounts','statement_periods','archived_transactions','transactions',
                         'finance_data_sources','statement_datasets','imported_statement_transactions'),
    'customer_records': ('accounts','loans','loan_installments','deposit_contracts','wealth_goals',
                         'budgets','advice_records','account_limits','account_restrictions',
                         'disputes','support_cases','permission_grants','consents'),
    'money_calculation': (),
    'prepare_operation': (*_BUDGET_TABLES,'operations','service_quotes'),
    'service_offer_rules': (),
    'service_offer_preview': ('service_negotiation_drafts',),
    'service_application_status': ('service_negotiation_applications',),
}
_CUSTOMER_DOMAINS = {
    'loans': ('loans','loan_installments'), 'insurance': ('insurance_policies','insurance_claims'),
    'deposits': ('deposit_contracts',), 'support': ('disputes','support_cases'),
    'permissions': ('permission_grants','consents','account_limits','account_restrictions'),
    'goals': ('wealth_goals','budgets','advice_records'),
}
_PRIVATE_FIELDS = {'workspace_id','customer_id','details','confirmation_digest','verification_code',
                   'verification_token','token','csrf','pin_hash','agent_token_hash','token_hash',
                   'verification_hash','idempotency_key','masked_mobile','masked_account'}


def _public(value):
    if isinstance(value,list): return [_public(v) for v in value]
    if isinstance(value,dict): return {k:_public(v) for k,v in value.items() if k not in _PRIVATE_FIELDS}
    return value


def _assert_observed(expected, observed):
    """正式服务投影逐项匹配；附加展示说明不能改变身份、日期或任何金融字段。"""
    if isinstance(expected,dict):
        if not isinstance(observed,dict) or any(k not in observed for k in expected):
            raise BankError('METRIC_RESULT_MISMATCH','工具结果缺少正式来源字段。',409)
        for key,value in expected.items(): _assert_observed(value,observed[key])
    elif isinstance(expected,list):
        if not isinstance(observed,list) or len(expected)!=len(observed):
            raise BankError('METRIC_RESULT_MISMATCH','工具结果的业务对象不匹配。',409)
        for a,b in zip(expected,observed): _assert_observed(a,b)
    elif type(expected) is not type(observed) or expected != observed:
        raise BankError('METRIC_RESULT_MISMATCH','工具结果与正式服务复算不一致。',409)


class FinancialMetrics:
    def __init__(self, gateway):
        self.g, self.bank = gateway, gateway.bank
        # 只在当前服务生命周期发行；重启后旧工具批次失败关闭。
        self._tool_key = secrets.token_bytes(32)
        self._tool_lock = threading.RLock()
        # 只序列化CF准备，不用于发行或银行事务内的末端回查。
        self._prepare_lock = threading.RLock()
        self._budget_observations = {}
        self._cashflow_prepare_transitions = {}
        self._prepared_operation_rows = {}
        self._funding_issuances = {}
        with closing(self.bank.connect()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS base_operation_metrics(
                operation_id TEXT PRIMARY KEY REFERENCES operations(id),workspace_id TEXT NOT NULL,
                snapshot TEXT NOT NULL,created_at REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS base_operation_metric_requirements(
                operation_id TEXT PRIMARY KEY REFERENCES operations(id),workspace_id TEXT NOT NULL)''')
            db.commit()
        self.bank.base_metrics_guard = self.execution_guard
        self.bank.base_metrics_register = self.register_required

    def personal_revision(self, workspace, db=None):
        """已有银行连接内核主体/私库路径，再只读私库版本，不重入银行连接。"""
        if db is None:return self.g.personal_data.revision(workspace)
        main=next((r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'),None)
        if not main or Path(main).resolve()!=Path(self.bank.database).resolve():
            raise BankError('METRIC_SUBJECT_MISMATCH','金融版本连接不属于当前银行。',403)
        self.bank._workspace(db,workspace)
        store=self.g.personal_data
        binding=db.execute('SELECT database_path FROM personal_data_binding WHERE id=1').fetchone()
        if (store.bank is not self.bank or not binding
                or Path(binding[0]).resolve()!=Path(store.database).resolve()):
            raise BankError('METRIC_SUBJECT_MISMATCH','个人资料库绑定与金融来源不一致。',403)
        with closing(store.connect()) as private:
            row=private.execute('SELECT revision FROM personal_versions WHERE workspace_id=?',(workspace,)).fetchone()
        return row[0] if row else 0

    def versions(self, workspace, db=None):
        if db is None:
            with closing(self.bank.connect()) as connection:
                return self.versions(workspace,connection)
        ws = self.bank._workspace(db,workspace)
        result = {'bank':str(ws['version']), 'business_date':ws['as_of'],
                  'private_context':context_hash(db,workspace),
                  'personal':str(self.personal_revision(workspace,db)),
                  'financial_methods_contract':CONTRACT_VERSION}
        if methods_fingerprint()!=LOADED_METHODS_CODE:
            raise BankError('METHODS_RESTART_REQUIRED','金融方法代码已更新，服务需加载新版本后再计算。',503)
        result['financial_methods_code'] = LOADED_METHODS_CODE
        result['money_range_policy_code'] = policy_code_version()
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='navren_demo_accounts'").fetchone():
            demo=db.execute("SELECT source_version FROM navren_demo_accounts WHERE workspace_id=? AND status='ready'",(workspace,)).fetchone()
            if demo:result['demo_dataset']=demo[0]
        return result

    def snapshot(self, workspace, days=60):
        if type(days) is not int or not 1<=days<=365:
            raise BankError('INVALID_HORIZON','指标期限需要1至365天。')
        if getattr(self.g,'finance_funding_sources',None) is not None:
            return self._trusted_funding_snapshot(workspace,days)
        versions = self.versions(workspace)
        funding_tables_before = self._funding_table_versions(workspace)
        archive = self.bank.customer_archive(workspace)
        timeline = self.g.cashflow_timeline(workspace,days)
        accounts = archive['accounts']
        if {a['currency'] for a in accounts} != {'CNY'}:
            raise BankError('METRIC_CURRENCY_UNSUPPORTED','当前指标仅支持单一人民币账本，不能跨币种相加。',422)
        begin, end = archive['as_of'], timeline['horizon_end']
        records = archive['records']
        refs = tuple(['accounts/'+a['id'] for a in accounts]+['profiles/'+workspace])
        ctx = MetricContext(workspace,'CNY','recorded_items_projection',period_start=begin,period_end=end,
                            input_refs=refs,source_versions=tuple(versions.items()))
        cash = CashSnapshot(workspace,'CNY',begin,sum(a['balance_minor'] for a in accounts),0,
                            archive['profile']['reserve_minor'],begin,end,'bank_snapshot/'+workspace,versions['bank'])
        events, assumptions = [], []
        for slot in timeline['timeline']:
            for event in slot['events']:
                delta = event['cash_delta_minor']
                if delta == 0:continue  # 预留释放在Hold中记录一次。
                events.append(FundingEvent(event['id'],workspace,event['currency'],event['date'],abs(delta),
                              'inflow' if delta>0 else 'outflow',event['certainty'],event['evidence_ref'],versions['bank'],event['kind']))
                if event['kind']=='recorded_forecast':
                    assumptions.append({'event_id':event['id'],'type':'recorded_forecast_candidate',
                                        'notice':'已记录的未付预测，仅用于候选分支；不是确认义务。'})
        holds = []
        baseline_refs=[]
        for row in records['fund_holds']:
            if row['status']!='held' or row['created_on']>begin or row['expires_on']<begin:continue
            # 已有schema没有明确付款释放/义务分配，不根据同额猜测抵扣关系。
            holds.append(Hold(row['id'],workspace,'CNY',row['amount_minor'],'fund_holds/'+row['id'],versions['bank'],
                              release_on=(date.fromisoformat(row['expires_on'])+timedelta(days=1)).isoformat()))
            baseline_refs.append('fund_holds/'+row['id'])
        funding = project_report_money(evaluate_funding(ctx,cash,tuple(events),tuple(holds),assumptions=tuple(assumptions)).to_dict())
        gaps = list(archive.get('integrity',{}).get('coverage_gaps',[]))
        result = {'workspace_id':workspace,'bank_business_date':begin,'horizon_days':days,
                  'source_versions':versions,'funding':funding,'metrics':list(funding['metrics'].values()),
                  'coverage_scope':'仅已记录事项；没有预支历史工资或假定未来收入。预测/候选不进入确认分支。',
                  'coverage_gaps':gaps,'no_financial_side_effect':True,
                  'research_portfolio_included':False,'funding_baseline_kind':'bank_ledger',
                  'funding_baseline_refs':baseline_refs}
        from user_memory import privacy_revision
        with closing(self.bank.connect()) as db:result['privacy_revision']=privacy_revision(db,workspace)
        if self.g.demo_accounts.is_demo(workspace):
            current = self.g.demo_accounts.current(workspace)
            original = current.get('source_snapshot',current)
            point = MetricContext(workspace,'CNY','navren_synthetic',as_of=original['end_date'],
                                 input_refs=('demo_snapshot/'+current['user_id'],),source_versions=tuple(versions.items()))
            for key,basis in (('investment_minor','synthetic_investment_book_value'),
                              ('real_assets_minor','synthetic_nonfinancial_book_value'),
                              ('net_worth_minor','synthetic_terminal_net_worth')):
                result['metrics'].append(money_metric(point,key,basis,'source.terminal_snapshot/1',
                       original['latest_balances'][key],excluded=('historical_research_portfolio','cost_not_extra_asset')).to_dict())
            result['source'] = 'navren_synthetic'
            result['source_snapshot_is_historical'] = True
            result['coverage_gaps'] += ['真实基金份额/净值/分红拆分和当前交易资格没有来源映射。']
        if versions != self.versions(workspace):
            raise BankError('METRIC_VERSION_CONFLICT','计算期间输入已变化，请重新读取。',409)
        ensure_money_ranges(result)
        result['snapshot_id'] = 'base-metrics-'+fingerprint(result)
        # 保存实际发行原件；客户端不能只拼一个数字或重算公开摘要变成预算。
        funding_tables = self._funding_table_versions(workspace)
        if versions != self.versions(workspace) or funding_tables_before != funding_tables:
            raise BankError('METRIC_VERSION_CONFLICT','预算发行期间来源变化。',409)
        with self._tool_lock:
            self._funding_issuances[result['snapshot_id']] = (deepcopy(result), funding_tables)
            while len(self._funding_issuances)>512:
                self._funding_issuances.pop(next(iter(self._funding_issuances)))
        return result

    def _funding_source(self, workspace, days, db):
        """维护方来源只在服务端注入，完整原件与银行身份/日期一起核验。"""
        from base_runtime.finance_funding import read_funding_source
        provider=getattr(self.g,'finance_funding_sources',None)
        if provider is None:
            raise BankError('METRIC_VERSION_CONFLICT','预算的受信来源已撤回。',409)
        query_only=db.execute('PRAGMA query_only').fetchone()[0]
        changes=db.total_changes
        db.execute('PRAGMA query_only=ON')
        try:
            try:source=read_funding_source(provider,workspace,days,db)
            except sqlite3.OperationalError as error:
                if 'readonly' in str(error).lower():
                    raise BankError('METRIC_SOURCE_NOT_READONLY','资金来源读取不得修改账本。',422) from error
                raise
        finally:db.execute('PRAGMA query_only='+str(int(query_only)))
        if db.total_changes!=changes:
            raise BankError('METRIC_SOURCE_NOT_READONLY','资金来源读取发生了数据库写入。',422)
        if source['period_start']!=self.bank._workspace(db,workspace)['as_of']:
            raise BankError('METRIC_VERSION_CONFLICT','资金来源期初与本次银行业务日期不一致。',409)
        return source

    def _funding_source_versions(self, workspace, source, db):
        from base_runtime.finance_funding import CONTRACT as FUNDING_SOURCE_CONTRACT
        if code_fingerprint()!=LOADED_ADAPTER_CODE:
            raise BankError('METHODS_RESTART_REQUIRED','资金来源适配代码已变化，需重新加载。',503)
        versions=self.versions(workspace,db)
        supplied=source['source_versions']
        if any(k in versions for k in supplied):
            raise BankError('METRIC_ARGUMENTS_INVALID','资金来源不能覆盖银行及方法的权威版本。',422)
        return {**versions,**supplied,'finance_funding_input/'+workspace:source['envelope_digest'],
                'finance_funding_adapter_code':code_fingerprint(),
                'finance_funding_source_contract':FUNDING_SOURCE_CONTRACT}

    def _trusted_funding_snapshot(self, workspace, days):
        from base_runtime.finance_funding import evaluate_source
        # 只读来源与发行使用同一真实银行事务；不在内存锁里获取数据库锁。
        with self.bank.transaction() as db:
            source=self._funding_source(workspace,days,db)
            versions=self._funding_source_versions(workspace,source,db)
            tables=self._funding_table_versions(workspace,db)
            funding=evaluate_source(source,additional_versions=versions.items())
            result={'workspace_id':workspace,'bank_business_date':source['period_start'],'horizon_days':days,
                    'source_versions':versions,'funding':funding,'metrics':list(funding['metrics'].values()),
                    'coverage_scope':'受信来源原件；缺金额保持未知，不补为银行账本余额。',
                    'coverage_gaps':list(funding['coverage_gaps']),'no_financial_side_effect':True,
                    'research_portfolio_included':False,'funding_baseline_kind':'trusted_source',
                    'funding_baseline_refs':[],
                    'funding_source_binding':{'days':days,'envelope_digest':source['envelope_digest']}}
            from user_memory import privacy_revision
            result['privacy_revision']=privacy_revision(db,workspace)
            current=self._funding_source(workspace,days,db)
            if (source!=current or versions!=self._funding_source_versions(workspace,current,db)
                    or tables!=self._funding_table_versions(workspace,db)):
                raise BankError('METRIC_VERSION_CONFLICT','预算发行期间受信原件已变化。',409)
            ensure_money_ranges(result)
            result['snapshot_id']='base-metrics-'+fingerprint(result)
        with self._tool_lock:
            self._funding_issuances[result['snapshot_id']]=(deepcopy(result),tables)
            while len(self._funding_issuances)>512:
                self._funding_issuances.pop(next(iter(self._funding_issuances)))
        return result

    def _funding_table_versions(self, workspace, db=None):
        if db is None:
            with closing(self.bank.connect()) as connection:
                return self._funding_table_versions(workspace,connection)
        return {table:fingerprint([dict(r) for r in db.execute(
            'SELECT * FROM '+table+' WHERE workspace_id=? ORDER BY rowid',(workspace,))])
            for table in _BUDGET_TABLES}

    def resolve_budget_metric(self, workspace, metric_id, *, db=None):
        """只读取当前服务曾发行且仍有效的funds.current_budget/1原件。"""
        if not isinstance(metric_id,str) or not metric_id:
            raise BankError('METRIC_ARGUMENTS_INVALID','预算需要真实已发行指标ID。',422)
        if db is None:
            with closing(self.bank.connect()) as connection:
                connection.execute('BEGIN')
                return self.resolve_budget_metric(workspace,metric_id,db=connection)
        from financial_methods.contracts import FinancialMetric
        with self._tool_lock:
            matches=[]
            for snapshot,tables in self._funding_issuances.values():
                metric=snapshot['funding']['metrics'].get('current_budget')
                if snapshot['workspace_id']==workspace and metric and metric['metric_id']==metric_id:
                    matches.append((snapshot,tables,metric))
            if not matches:
                raise BankError('METRIC_BUDGET_NOT_ISSUED','预算ID未由当前正式服务发行，请先读取资金结果。',409)
            snapshot,tables,metric=deepcopy(matches[-1])
        # 指标锁只保护内存复制；银行来源回查在锁外，不能与BEGIN形成倒置。
        self.validate(workspace,snapshot,db)
        if tables!=self._funding_table_versions(workspace,db):
            raise BankError('METRIC_VERSION_CONFLICT','预算的原始资金来源已变化。',409)
        if metric['basis']!='floor_surplus' or metric['formula_id']!='funds.current_budget/1':
            raise BankError('METRIC_BUDGET_INVALID','用途安排需要当前底线外预算。',422)
        return FinancialMetric(**{**metric,'source_versions':tuple(metric['source_versions'].items())})

    def resolve_budget_baseline(self, workspace, metric_id, *, db):
        """实际已发行银行预算中已扣占用；来源自报hold不获得免扣信用。"""
        funds=self.resolve_budget_metric(workspace,metric_id,db=db)
        with self._tool_lock:
            snapshots=[deepcopy(snapshot) for snapshot,_ in self._funding_issuances.values()
                       if snapshot['workspace_id']==workspace and
                       snapshot['funding']['metrics']['current_budget']['metric_id']==metric_id]
        if not snapshots:
            raise BankError('METRIC_BUDGET_NOT_ISSUED','预算原件已被淘汰，请重新读取。',409)
        original=snapshots[-1]
        kind=original.get('funding_baseline_kind')
        holds=[]
        if kind=='bank_ledger' and funds.calculation_status=='calculated':
            for row in db.execute('SELECT * FROM fund_holds WHERE workspace_id=? ORDER BY id',(workspace,)):
                ref='fund_holds/'+row['id']
                if (ref in original.get('funding_baseline_refs',[]) and ref in funds.input_refs
                        and dict(funds.source_versions).get(ref)==original['source_versions']['bank']):
                    holds.append(dict(row))
        return {'baseline_kind':kind,'holds':holds,'snapshot_id':original['snapshot_id'],
                'source_versions':original['source_versions']}

    def method_report(self, workspace, tool_name, arguments):
        """受限只读入口，返回实际签名批次供原观察者登记/末端回查。"""
        if tool_name not in METHOD_TOOLS:
            raise BankError('METRIC_ARGUMENTS_INVALID','没有这个金融方法入口。',422)
        with self.bank.transaction() as db:
            adapter=FinanceMethodAdapter(self)
            args=adapter.arguments(workspace,tool_name,arguments,db)
            binding={'method_arguments':args}
            before=self._tool_versions(workspace,tool_name,binding,db)
            self._check_expected_versions(args,before)
            expected,_,_,_=self._describe_tool(workspace,tool_name,args,binding,before,db=db)
            batch=self._issue_method_batch(workspace,tool_name,args,binding,expected,db)
        return {**expected,'metric_batch':batch}

    def accounting_report(self, workspace, **arguments):
        return self.method_report(workspace,'accounting_report',arguments)

    def returns_report(self, workspace, **arguments):
        return self.method_report(workspace,'returns_report',arguments)

    def allocation_preview(self, workspace, **arguments):
        return self.method_report(workspace,'allocation_preview',arguments)

    @staticmethod
    def _check_expected_versions(args,current):
        if any(current.get(k)!=v for k,v in args['expected_versions'].items()):
            raise BankError('METRIC_VERSION_CONFLICT','所期待的金融来源版本已变化。',409)

    def _issue_method_batch(self,workspace,name,args,binding,data,db):
        before=self._tool_versions(workspace,name,binding,db)
        self._check_expected_versions(args,before)
        expected,metrics,kinds,metadata=self._describe_tool(workspace,name,args,binding,before,db=db)
        ensure_money_ranges(expected); ensure_money_ranges(metrics); ensure_money_ranges(metadata)
        _assert_observed(_public(expected),data)
        if before!=self._tool_versions(workspace,name,binding,db):
            raise BankError('METRIC_VERSION_CONFLICT','金融方法计算期间来源变化。',409)
        batch={'batch_contract':TOOL_METRICS_CONTRACT,'workspace_id':workspace,'tool_name':name,
            'arguments':args,'binding':binding,'source_versions':before,
            'source_fingerprint':fingerprint(_public(expected)),'metrics':metrics,
            'metric_kinds':kinds,'coverage':metadata,'no_financial_side_effect':True}
        batch['service_signature']=self._tool_signature(batch)
        batch['snapshot_id']='base-tool-metrics-'+fingerprint(batch)
        return batch

    def validate(self, workspace, snapshot, db=None):
        if not isinstance(snapshot,dict) or snapshot.get('workspace_id')!=workspace:
            raise BankError('METRIC_SUBJECT_MISMATCH','金融结果不属于当前账户。',403)
        if snapshot.get('batch_contract') == TOOL_METRICS_CONTRACT:
            return self._validate_tool_batch(workspace,snapshot,db)
        body={k:v for k,v in snapshot.items() if k!='snapshot_id'}
        if snapshot.get('snapshot_id')!='base-metrics-'+fingerprint(body):
            raise BankError('METRIC_TAMPERED','金融结果摘要不一致。',409)
        with self._tool_lock:
            issued=deepcopy(self._funding_issuances.get(snapshot['snapshot_id']))
        if issued is None:
            raise BankError('METRIC_BUDGET_NOT_ISSUED','金融结果未由当前服务发行，请重新读取。',409)
        original,tables=issued
        if snapshot!=original:
            raise BankError('METRIC_TAMPERED','金融结果与实际发行原件不一致。',409)
        if db is None:
            with closing(self.bank.connect()) as connection:
                connection.execute('BEGIN')
                return self.validate(workspace,snapshot,connection)
        if tables!=self._funding_table_versions(workspace,db):
            raise BankError('METRIC_VERSION_CONFLICT','已发行金融结果的原始资金来源已变化。',409)
        if snapshot.get('funding_source_binding') is not None:
            binding=snapshot['funding_source_binding']
            if (not isinstance(binding,dict) or set(binding)!={'days','envelope_digest'}
                    or type(binding['days']) is not int or not 1<=binding['days']<=365):
                raise BankError('METRIC_TAMPERED','预算来源绑定格式不一致。',409)
            source=self._funding_source(workspace,binding['days'],db)
            if binding['envelope_digest']!=source['envelope_digest']:
                raise BankError('METRIC_VERSION_CONFLICT','已发行预算的受信来源原件已变化。',409)
            current=self._funding_source_versions(workspace,source,db)
        else:
            current=self.versions(workspace,db)
        if snapshot['source_versions'] != current:
            raise BankError('METRIC_VERSION_CONFLICT','金融结果的账本、资料或算法版本已变化，请重新准备。',409)

    def _tool_versions(self, workspace, name, binding, db=None):
        """已有银行事务内只读，不能递归调用开启写事务的 archive/overview。"""
        if db is None:
            with closing(self.bank.connect()) as connection:
                return self._tool_versions(workspace,name,binding,connection)
        result = self.versions(workspace,db)
        if tool_metrics_fingerprint()!=LOADED_TOOL_METRICS_CODE:
            raise BankError('TOOL_METRICS_RESTART_REQUIRED','逐工具指标代码已更新，服务需重新加载。',503)
        result.update(financial_tool_metrics_contract=TOOL_METRICS_CONTRACT,
                      financial_tool_metrics_code=LOADED_TOOL_METRICS_CODE)
        if name=='presentation_view':
            service=self.g.presentations
            stored=service._stored(workspace,binding['view_id'],db)
            p=stored['presentation']
            if any(p[k]!=binding[k] for k in ('request_id','message_id')):
                raise BankError('PRESENTATION_BINDING','展示不属于该服务响应。',403)
            result.update(service.source_versions(workspace,db))
            result['presentation_view/'+p['view_id']]=fingerprint(stored)
            return result
        if name in METHOD_TOOLS:
            if code_fingerprint()!=LOADED_ADAPTER_CODE:
                raise BankError('METHODS_RESTART_REQUIRED','金融适配代码已变化，需重新加载。',503)
            adapter=FinanceMethodAdapter(self)
            args=adapter.arguments(workspace,name,binding.get('method_arguments'),db)
            supplied=adapter.source_versions(workspace,name,args,db)
            if any(k in result and result[k]!=v for k,v in supplied.items()):
                raise BankError('METRIC_VERSION_CONFLICT','金融来源不能覆盖已有权威版本。',409)
            result.update(supplied)
        if name in ('monthly_statement','annual_statement') and binding.get('report_source'):
            source=binding['report_source']
            if (not isinstance(source,dict) or set(source)!={'workspace_id','source','source_version'}
                    or source['workspace_id']!=workspace or source['source'] not in ('simulation','imported')
                    or not isinstance(source['source_version'],str)):
                raise BankError('METRIC_SUBJECT_MISMATCH','报告来源绑定不属于当前工作区。',403)
            if source['source']=='simulation':
                current=str(self.bank._workspace(db,workspace)['version'])
            else:
                dataset=db.execute('SELECT version FROM statement_datasets WHERE workspace_id=?',(workspace,)).fetchone()
                current=str(dataset['version']) if dataset else None
            if current is None or current!=source['source_version']:
                raise BankError('SOURCE_VERSION_CONFLICT','报告引用的所选来源已变化，请重新读取。',409)
            result['report_source/'+source['source']+'/'+workspace]=current
        for table in _TOOL_TABLES[name]:
            columns = [r['name'] for r in db.execute('PRAGMA table_info('+table+')')]
            if not columns:
                result['tool_table/'+table+'/'+workspace] = 'not_connected'
                continue
            if table=='operations':
                # 认证凭据不改变草案业务语义；金额、状态、确认内容与业务期限仍受保护。
                rows=self._operation_business_rows(db,workspace)
                result[self._operation_version_key(workspace,name)]=fingerprint(rows)
                continue
            if 'workspace_id' in columns:
                rows = db.execute('SELECT * FROM '+table+' WHERE workspace_id=? ORDER BY rowid',(workspace,))
            elif table=='products':
                rows = db.execute('SELECT * FROM products ORDER BY id')
            else:
                raise BankError('METRIC_SOURCE_UNSUPPORTED','指标来源表没有可信对象范围。',503)
            result['tool_table/'+table+'/'+workspace] = fingerprint([dict(r) for r in rows])
        if name.startswith('service_'):
            result['service_rules'] = fingerprint(self.g.service_negotiations.rules())
            if name=='service_offer_preview' and binding.get('draft_id'):
                draft = db.execute('SELECT * FROM service_negotiation_drafts WHERE workspace_id=? AND id=?',
                                   (workspace,binding['draft_id'])).fetchone()
                if not draft or draft['status']!='previewed' or self.bank.clock()>=draft['expires_at']:
                    raise BankError('STALE_SERVICE_EVIDENCE','服务预览已被替代、撤回或过期。',409)
        if name=='prepare_operation':
            operation=self.bank._operation(db,workspace,binding['operation_id'])
            if operation['state']!='pending_confirmation' or self.bank.clock()>=operation['expires_at']:
                raise BankError('STALE_OPERATION_METRICS','操作已变化、被替代或过期，请重新准备。',409)
            policy=db.execute("SELECT details FROM audit WHERE workspace_id=? AND operation_id=? AND event='liquidity_policy' ORDER BY id LIMIT 1",
                              (workspace,binding['operation_id'])).fetchone()
            result['operation_liquidity_policy/'+binding['operation_id']]=fingerprint(dict(policy) if policy else None)
        return result

    @staticmethod
    def _operation_version_key(workspace, name):
        # CF 是读取时排除说明；prepare 是当前草案。不同语义不能合并成同名版本。
        prefix='cashflow_operations/' if name=='cashflow' else 'operation_business/'
        return prefix+workspace

    @staticmethod
    def _operation_business_rows(db, workspace):
        return [{k:v for k,v in dict(r).items()
                 if k not in ('verification_hash','verification_expires_at')}
                for r in db.execute('SELECT * FROM operations WHERE workspace_id=? ORDER BY rowid',(workspace,))]

    def prepare_bank_operation(self, workspace, payload, *, agent_origin=False,
                               metric_required=False, metric_batches=()):
        """请求 Observer 独占调用：只授权实际 bank.prepare 的精确业务转换。

        不接受模型的差分或新版本。先验证本轮服务签名批次，实际调用银行准备后，
        必须只有该操作新增和该调用明确替代的旧 Agent 草案；资金或其它来源仍需原版本。
        许可按原 CF 批次 ID 留在服务内存；不能使另一请求的 CF 快照自动获得许可。
        """
        batches=list(metric_batches)
        cashflows=[b for b in batches if isinstance(b,dict) and b.get('tool_name')=='cashflow']
        if not cashflows:
            return self.bank.prepare(workspace,payload,agent_origin=agent_origin,metric_required=metric_required)
        if not metric_required:
            raise BankError('METRIC_BINDING_REQUIRED','现金流到准备转换必须走银行指标保护入口。',409)
        with self._prepare_lock:
            with closing(self.bank.connect()) as db:
                db.execute('BEGIN')  # 版本与操作前状态必须来自同一读快照。
                for batch in batches:self.validate(workspace,batch,db)
                before=self._operation_business_rows(db,workspace)
                prior={r['id']:r for r in before}
                reviews={r['operation_id']:dict(r) for r in db.execute(
                    'SELECT * FROM agent_draft_reviews WHERE workspace_id=?',(workspace,))}
                audit_id=db.execute('SELECT coalesce(max(id),0) FROM audit WHERE workspace_id=?',
                                    (workspace,)).fetchone()[0]
            operation=self.bank.prepare(workspace,payload,agent_origin=agent_origin,metric_required=metric_required)
            operation_id=operation['id']
            with closing(self.bank.connect()) as db:
                db.execute('BEGIN')
                after=self._operation_business_rows(db,workspace)
                current={r['id']:r for r in after}
                row=current.get(operation_id)
                expected=dict(prior)
                if operation_id not in prior:
                    captured=self._prepared_operation_rows.get((workspace,operation_id))
                    if (not row or row['state']!='pending_confirmation'
                            or not captured or row!=captured or self.bank._public_operation(row)!=operation
                            or json.loads(row['payload'])!=payload or self.bank.clock()>=row['expires_at']):
                        raise BankError('METRIC_VERSION_CONFLICT','准备期间操作内容变化，请重新读取。',409)
                    prepared=db.execute("SELECT 1 FROM audit WHERE workspace_id=? AND operation_id=? "
                                        "AND event='operation_prepared' AND id>?",
                                        (workspace,operation_id,audit_id)).fetchone()
                    if not prepared:
                        raise BankError('METRIC_VERSION_CONFLICT','准备转换缺少银行原始审计。',409)
                    expected[operation_id]=captured
                    for old_id,old in prior.items():
                        if old==current.get(old_id):continue
                        review=reviews.get(old_id)
                        superseded=db.execute("SELECT details FROM audit WHERE workspace_id=? AND operation_id=? "
                                              "AND event='agent_draft_superseded' AND id>? ORDER BY id",
                                              (workspace,old_id,audit_id)).fetchall()
                        actual_review=db.execute('SELECT status FROM agent_draft_reviews WHERE workspace_id=? AND operation_id=?',
                                                 (workspace,old_id)).fetchone()
                        replacement=any(json.loads(r['details']).get('replacement_id')==operation_id for r in superseded)
                        if (not agent_origin or old['state']!='pending_confirmation' or not review
                                or not actual_review or actual_review['status']!='superseded' or not replacement):
                            raise BankError('METRIC_VERSION_CONFLICT','准备期间其它操作发生变化，请重新读取。',409)
                        expected[old_id]={**old,'state':'invalidated'}
                if expected!=current:
                    raise BankError('METRIC_VERSION_CONFLICT','准备期间发生非本轮的操作新增或修改，请重新读取。',409)
                pending={}
                key=self._operation_version_key(workspace,'cashflow')
                for batch in batches:
                    if batch in cashflows:
                        versions=self._tool_versions(workspace,'cashflow',batch['binding'],db)
                        versions[key]=batch['source_versions'][key]
                        if versions!=batch['source_versions']:
                            raise BankError('METRIC_VERSION_CONFLICT','准备期间账本、资料或算法变化，请重新读取。',409)
                        pending[(workspace,batch['snapshot_id'])]={
                            'from':batch['source_versions'][key],'to':fingerprint(after),
                            'operation_id':operation_id}
                    else:self.validate(workspace,batch,db)
                self._cashflow_prepare_transitions.update(pending)
            return operation

    def _tool_signature(self, body):
        return hmac.new(self._tool_key,fingerprint(body).encode(),hashlib.sha256).hexdigest()

    def bound_gateway(self, gateway):
        """可信网关报告视图；共享发行密钥和锁，不迁移库或替换操作 guard。"""
        if gateway.bank is not self.bank:
            raise BankError('METRIC_SUBJECT_MISMATCH','不能把金融指标绑定到另一账本。',403)
        view=copy(self)
        view.g=gateway
        return view

    def _validate_tool_batch(self, workspace, batch, db=None):
        try:
            unsigned={k:v for k,v in batch.items() if k not in ('snapshot_id','service_signature')}
            signed={k:v for k,v in batch.items() if k!='snapshot_id'}
            name,binding=batch['tool_name'],batch['binding']
            if (name not in _TOOL_TABLES or not isinstance(binding,dict)
                    or batch.get('snapshot_id')!='base-tool-metrics-'+fingerprint(signed)
                    or not hmac.compare_digest(str(batch.get('service_signature','')),self._tool_signature(unsigned))):
                raise ValueError('invalid_signature')
        except (ValueError,TypeError,KeyError):
            raise BankError('METRIC_TAMPERED','逐工具指标没有当前正式服务的有效签名。',409) from None
        current=self._tool_versions(workspace,name,binding,db)
        if name=='cashflow':
            transition=self._cashflow_prepare_transitions.get((workspace,batch['snapshot_id']))
            key=self._operation_version_key(workspace,name)
            if (transition and transition['from']==batch['source_versions'].get(key)
                    and transition['to']==current.get(key)):
                current[key]=transition['from']
        if batch['source_versions'] != current:
            raise BankError('METRIC_VERSION_CONFLICT','逐工具指标的对象、来源或代码版本已变化。',409)

    def metrics_for_tool(self, workspace, tool_name, arguments, data, *, db=None):
        """发行完整 FinancialMetric；data 只用于核对，绝不成为可信金额输入。

        正式服务重读/复算发生在发行阶段。最终发布可以在已有银行写锁中
        validate(db=...)：只回查固定来源版本/表摘要及草案状态，不开第二个写锁。
        """
        if tool_name not in _TOOL_TABLES: return None
        if db is not None and tool_name!='presentation_view':
            raise TypeError('已有发布连接仅用于受控展示批次发行。')
        if not isinstance(arguments,dict) or not (isinstance(data,dict)
                or tool_name=='products' and isinstance(data,list)):
            raise BankError('METRIC_ARGUMENTS_INVALID','逐工具指标需要明确参数和正式结果。',422)
        try:
            args=json.loads(json.dumps(arguments,ensure_ascii=False,allow_nan=False))
        except (ValueError,TypeError):
            raise BankError('METRIC_ARGUMENTS_INVALID','逐工具参数必须是有效JSON。',422) from None
        if tool_name=='business_records' and args.get('kind')not in ('subscriptions','tasks'):
            return None
        if tool_name in METHOD_TOOLS:
            with self.bank.transaction() as db:
                args=FinanceMethodAdapter(self).arguments(workspace,tool_name,args,db)
                return self._issue_method_batch(workspace,tool_name,args,{'method_arguments':args},data,db)
        binding={}
        if tool_name=='presentation_view':
            binding=self.g.presentations.binding(workspace,args)
        if tool_name in ('monthly_statement','annual_statement'):
            scoped=getattr(self.g,'_request_report_source',None)
            if scoped:
                owner,source,expected_version=scoped
                binding['report_source']={'workspace_id':owner,'source':source,'source_version':str(expected_version)}
        elif tool_name=='prepare_operation':
            operation=data.get('operation')
            if not isinstance(operation,dict) or not isinstance(operation.get('id'),str):
                raise BankError('METRIC_ARGUMENTS_INVALID','准备操作缺少当前草案ID。',422)
            binding['operation_id']=operation['id']
        elif tool_name=='service_offer_preview':
            # ID仅定位服务库原件；金额、条款、状态与期限全部回读。
            if data.get('status')!='previewed' or not data.get('draft_id'): return None
            binding['draft_id']=data['draft_id']
        before=self._tool_versions(workspace,tool_name,binding,db)
        expected,metrics,kinds,metadata=self._describe_tool(workspace,tool_name,args,binding,before,db=db)
        ensure_money_ranges(expected); ensure_money_ranges(metrics); ensure_money_ranges(metadata)
        _assert_observed(_public(expected),data)
        after=self._tool_versions(workspace,tool_name,binding,db)
        if before!=after:
            raise BankError('METRIC_VERSION_CONFLICT','计算期间正式来源变化，请重新读取。',409)
        if tool_name=='budget' and not args.get('reserves'):
            self._budget_observations[(workspace,args['account_id'])]={
                'versions':before,'commitment_ids':metadata['observed_commitment_ids']}
        batch={'batch_contract':TOOL_METRICS_CONTRACT,'workspace_id':workspace,
               'tool_name':tool_name,'arguments':args,'binding':binding,'source_versions':before,
               'source_fingerprint':fingerprint(_public(expected)),'metrics':metrics,
               'metric_kinds':kinds,'coverage':metadata,'no_financial_side_effect':True}
        if tool_name=='cashflow':
            # 同额/同对象/同期间的并行读取也属于不同请求发行，不能共享转换许可。
            batch['issuance_id']=secrets.token_hex(16)
        batch['service_signature']=self._tool_signature(batch)
        batch['snapshot_id']='base-tool-metrics-'+fingerprint(batch)
        return batch

    def _describe_tool(self, workspace, name, args, binding, versions, db=None):
        if name=='presentation_view':
            return self.g.presentations.describe(workspace,args,binding,versions,db)
        if name in METHOD_TOOLS:
            if db is None:
                with closing(self.bank.connect()) as connection:
                    return self._describe_tool(workspace,name,args,binding,versions,db=connection)
            return FinanceMethodAdapter(self).describe(workspace,name,args,db,versions)
        metrics,kinds,metadata=[],{},{}
        call_ref='tool_evidence/'+name+'/'+fingerprint({'arguments':args,'binding':binding})
        call_version=fingerprint(versions)
        scope='navren_synthetic_runtime_ledger' if self.g.demo_accounts.is_demo(workspace) else 'simulation_bank_ledger'

        def emit(subject,label,value,currency,basis,formula,refs, *, on=None,begin=None,end=None,
                 kind='recorded_fact',data_scope=None,gaps=(),excluded=(),assumptions=(),partial=False,input_versions=()):
            if value is not None and type(value) is not int:
                raise BankError('METRIC_SOURCE_INVALID','正式来源金额必须为整数分。',503)
            refmap={**versions,call_ref:call_version}
            for ref in refs: refmap.setdefault(ref,versions['bank'])
            refmap.update(input_versions)
            context=MetricContext(subject,currency,data_scope or scope,as_of=on,
                                  period_start=begin,period_end=end,input_refs=tuple(refs)+(call_ref,),
                                  source_versions=tuple(refmap.items()))
            metric=money_metric(context,label,basis,formula,value,
                                status='unavailable' if value is None else 'partial' if partial else 'calculated',
                                gaps=gaps,excluded=excluded,assumptions=assumptions).to_dict()
            metrics.append(metric);kinds[metric['metric_id']]=kind
            return metric['metric_id']

        if name=='account_summary':
            state=self.g.overview(workspace);totals={};principal={};positions={}
            products={p['id']:p for p in self.bank.products(workspace)}
            accounts={a['id']:a for a in state['accounts']}
            for account in state['accounts']:
                currency=account['currency'];totals[currency]=totals.get(currency,0)+account['balance_minor']
                emit(account['id'],'settled_cash_balance',account['balance_minor'],currency,'ledger_cash_balance',
                     'tools.account_balance/1',('accounts/'+account['id'],),on=state['as_of'])
            for row in state['holdings']:
                currency=position_currency(workspace,row,accounts,products)
                bucket=principal.setdefault(currency,{'currency':currency,'holding_principal_minor':0,'pending_redemption_minor':0})
                bucket['holding_principal_minor']+=row['amount_minor']
                product_bucket=positions.setdefault((row['product_id'],currency),{'value':0,'refs':[]})
                product_bucket['value']+=row['amount_minor']
                product_bucket['refs'].append('holdings/'+workspace+'/'+row['account_id']+'/'+row['product_id'])
            for row in state['orders']:
                if row['status']=='pending_settlement':
                    currency=position_currency(workspace,row,accounts,products)
                    bucket=principal.setdefault(currency,{'currency':currency,'holding_principal_minor':0,'pending_redemption_minor':0})
                    bucket['pending_redemption_minor']+=row['amount_minor']
            expected={k:state[k] for k in ('as_of','version','accounts','profile','holdings')}
            expected['cash_by_currency']=[{'name':'现金合计','currency':c,'balance_minor':v} for c,v in totals.items()]
            expected['principal_summary']=list(principal.values())
            for currency,value in totals.items():
                refs=tuple('accounts/'+a['id'] for a in state['accounts'] if a['currency']==currency)
                emit(workspace,'settled_cash_total',value,currency,'same_currency_workspace_cash_total',
                     'tools.account_cash_total/1',refs,on=state['as_of'],excluded=('credit_limit_is_not_cash',))
            reserve=state['profile'].get('reserve_minor')
            reserve_currency=self.bank.capabilities().get('currency')
            if type(reserve)is int and reserve>=0 and isinstance(reserve_currency,str):
                emit(workspace,'profile_emergency_reserve',reserve,reserve_currency,
                     'formal_profile_emergency_requirement_not_account_allocation',
                     'tools.profile_emergency_reserve/1',('bank_emergency_requirement_record',),on=state['as_of'],
                     excluded=('not_extra_cash','not_assigned_account_floor','adequacy_not_established'))
            for row in principal.values():
                for field,basis in (('holding_principal_minor','holding_book_principal_not_market_value'),
                                    ('pending_redemption_minor','pending_redemption_not_settled_cash')):
                    emit(workspace,field.removesuffix('_minor'),row[field],row['currency'],basis,
                         'tools.account_principal/1',('holdings/'+workspace,'orders/'+workspace),on=state['as_of'],
                         excluded=('not_total_wealth','not_available_cash','historical_valuation_not_added'))
            for (product_id,currency),bucket in positions.items():
                emit(product_id,'product_holding_principal',bucket['value'],currency,
                     'aggregate_book_principal_per_product_not_cash','tools.product_holding_principal/1',
                     tuple(bucket['refs']),on=state['as_of'],
                     excluded=('not_individual_lot','not_market_value','not_available_cash','not_redemption_receipt'))
        elif name=='products':
            expected=self.g.bank.products(workspace)
            for product in expected:
                currency=product.get('currency')
                if not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency):
                    continue
                subject=product['id'];refs=('products/'+subject,)
                if 'min_purchase_minor' in product:
                    emit(subject,'minimum_purchase',product['min_purchase_minor'],currency,
                         'simulated_product_minimum_not_cash_or_investment_advice',
                         'tools.simulated_product_minimum/1',refs,on=versions['business_date'],
                         excluded=('not_current_sale_quote','not_execution_permission'))
                nominal=re.fullmatch(r'固定([0-9]+(?:\.[0-9]{1,2})?)元，仅演示本金流转',
                                     product.get('nav_model',''))
                if nominal and product.get('source')=='simulation' and currency=='CNY':
                    emit(subject,'simulated_unit_principal',int(Decimal(nominal.group(1))*100),currency,
                         'declared_fixed_principal_unit_not_market_nav',
                         'tools.simulated_unit_principal/1',refs,on=versions['business_date'],
                         excluded=('not_real_nav','not_return_quote','not_real_issuer'))
        elif name=='business_records':
            # Re-read the actual records and their owning accounts. A matching
            # amount or another account's currency is never evidence.
            state=self.g.overview(workspace)
            expected=({key:state[key] for key in ('mandates','subscriptions')}
                      if args['kind']=='subscriptions'else
                      task_records_view(state,self.bank.capabilities(),
                          self.bank.customer_archive(workspace)['records']['service_providers']))
            accounts={row['id']:row for row in state['accounts']}
            for table,label in (('subscriptions','subscription_amount'),('mandates','mandate_amount')):
                for row in expected.get(table,[]):
                    account=accounts.get(row.get('account_id'))
                    currency=(account or {}).get('currency')
                    if (not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency)
                            or type(row.get('amount_minor')) is not int or row['amount_minor']<0):
                        continue
                    emit(row['id'],label,row['amount_minor'],currency,
                         'recorded_periodic_amount_not_cash_savings_or_payment',
                         'tools.current_subscription_record/1',
                         (table+'/'+row['id'],'accounts/'+account['id']),
                         on=versions['business_date'],
                         excluded=('not_current_cash_balance','not_executed_payment','not_guaranteed_savings'))
            if args['kind']=='tasks':
                currency=expected.get('currency')
                if (not isinstance(currency,str)or not re.fullmatch(r'[A-Z]{3}',currency)
                        or {row.get('currency')for row in state['accounts']}!={currency}):
                    raise BankError('TASK_CURRENCY_SOURCE_REQUIRED','任务币种与当前模拟银行合同不一致。',422)
                def task_amount(row,field,label,ccy,refs,basis):
                    value=row.get(field)
                    if type(value)is int and value>=0 and isinstance(ccy,str)and re.fullmatch(r'[A-Z]{3}',ccy):
                        emit(row['id'],label,value,ccy,basis,'tools.current_task_record/1',refs,
                             on=versions['business_date'],excluded=('not_executed_payment','not_real_service_booking'))
                for row in expected['orchestration_tasks']:
                    task_amount(row,'budget_minor','task_budget',currency,
                        ('orchestration_tasks/'+row['id'],'bank_capabilities/currency'),
                        'registered_task_budget_in_declared_simulation_currency')
                quotes={row['id']:row for row in expected['service_quotes']}
                for row in quotes.values():
                    task_amount(row,'price_minor','service_quote_amount',row.get('currency'),
                        ('service_quotes/'+row['id'],'service_providers/'+row['provider_id']),
                        'current_simulated_service_quote_not_real_market_price')
                for row in expected['fund_holds']:
                    account=accounts.get(row.get('account_id'))
                    if account:
                        task_amount(row,'amount_minor','task_hold_amount',account.get('currency'),
                            ('fund_holds/'+row['id'],'accounts/'+account['id']),
                            'registered_hold_record_status_must_be_read_separately')
                for row in expected['service_reservations']:
                    quote=quotes.get(row.get('quote_id'))
                    if quote:
                        task_amount(row,'amount_minor','service_reservation_amount',quote.get('currency'),
                            ('service_reservations/'+row['id'],'service_quotes/'+quote['id']),
                            'registered_service_record_not_confirmation_or_payment')
                current=[q for q in quotes.values()if q.get('status')=='illustrative'
                    and q.get('currency')==currency and isinstance(q.get('valid_until'),str)
                    and q['valid_until']>=versions['business_date']
                    and type(q.get('price_minor'))is int and q['price_minor']>=0]
                if current and len(current)==len(quotes)and len(current)<=6:
                    emit(workspace,'current_service_quote_total',sum(q['price_minor']for q in current),currency,
                        'sum_of_exact_entire_current_simulated_quote_set','tools.current_service_quote_set/1',
                        tuple('service_quotes/'+q['id']for q in current),on=versions['business_date'],
                        excluded=('not_subset_total','not_executed_payment','not_real_service_booking'))
        elif name=='budget':
            expected,metadata=self._describe_budget(workspace,args,versions)
            account_id=args['account_id'];currency=expected['currency'];begin=expected['as_of']
            end=(date.fromisoformat(begin)+timedelta(days=30)).isoformat()
            refs=('accounts/'+account_id,'profiles/'+workspace,*('commitments/'+c['evidence_id'] for c in expected['commitments']))
            floor_assumptions=({'type':'user_requested_emergency_floor','amount_minor':args['floor_minor']},) if metadata['floor_override_is_planning'] else ()
            for field,basis in (('cash_minor','ledger_cash_balance'),('reserve_minor','effective_emergency_floor_setting'),
                                ('held_minor','effective_fund_holds')):
                overridden=field=='reserve_minor' and bool(floor_assumptions)
                emit(account_id,field.removesuffix('_minor'),expected[field],currency,
                     'user_requested_emergency_floor_not_recorded_setting' if overridden else basis,'tools.budget_inputs/1',refs,on=begin,
                     kind='planning_scenario' if overridden else 'recorded_fact',assumptions=floor_assumptions if overridden else ())
            for field,basis in (('upcoming_due_minor','known_30_day_obligations'),
                                ('baseline_investable_minor','floor_and_holds_and_30_day_obligations_deducted')):
                emit(account_id,field.removesuffix('_minor'),expected[field],currency,
                     'planning_floor_and_holds_and_30_day_obligations_deducted' if floor_assumptions and field=='baseline_investable_minor' else basis,
                     'tools.budget_baseline/1',
                     refs,begin=begin,end=end,kind='planning_scenario' if floor_assumptions and field=='baseline_investable_minor' else 'recorded_fact',
                     assumptions=floor_assumptions if field=='baseline_investable_minor' else ())
            planning=bool(args.get('reserves')) or bool(floor_assumptions)
            assumptions=tuple({'type':'user_planning_reserve','label':r['label'],'amount_minor':r['amount_minor'],
                               'commitment_ids':','.join(r['commitment_ids']),
                               'already_budgeted_minor':r['already_budgeted_minor'],
                               'additional_reserve_minor':r['additional_reserve_minor']} for r in expected['planning_reserves'])+floor_assumptions
            emit(account_id,'planning_arrangeable_now' if planning else 'arrangeable_now',expected['investable_minor'],currency,
                 'planning_floor_surplus' if planning else 'floor_and_holds_and_30_day_obligations_deducted',
                 'tools.budget_planning/1' if planning else 'tools.budget_current/1',refs,begin=begin,end=end,
                 kind='planning_scenario' if planning else 'recorded_fact',assumptions=assumptions,
                 excluded=('no_real_reservation_created','future_income_not_prespent'))
            if args.get('reserves'):
                commitments={c['evidence_id']:c for c in expected['commitments'] if c['account_id']==account_id}
                source_tables={'loan':'loan_installments','credit_card':'credit_statements',
                               'subscription':'subscriptions','mandate':'mandates',
                               'scheduled_transfer':'scheduled_payments'}
                metadata['planning_reserve_metrics']=[]
                for index,reserve in enumerate(expected['planning_reserves']):
                    linked_refs=tuple(ref for cid in reserve['commitment_ids'] for ref in
                        ('commitments/'+cid,source_tables[commitments[cid]['kind']]+'/'+cid))
                    proof_refs=tuple(dict.fromkeys((*refs,*linked_refs)))
                    reserve_ref='planning_reserve/'+fingerprint({'call_ref':call_ref,'reserve_index':index})
                    target_id=emit(account_id,'planning_reserve_target',reserve['amount_minor'],currency,
                        'user_requested_planning_reserve_target_not_actual_hold','tools.budget_planning/1',(*proof_refs,reserve_ref),
                        begin=begin,end=end,kind='planning_scenario',
                        assumptions=(assumptions[index],),
                        input_versions=((reserve_ref,call_version),),
                        excluded=('not_recorded_goal','no_real_reservation_created','not_payment'))
                    linked_id=emit(account_id,'already_budgeted_linked_commitment',reserve['already_budgeted_minor'],currency,
                        'linked_30_day_obligations_already_deducted_in_budget','tools.budget_inputs/1',proof_refs,
                        begin=begin,end=end,excluded=('not_additional_reserve','not_held_funds','not_payment'))
                    metadata['planning_reserve_metrics'].append({'reserve_index':index,
                        'commitment_ids':list(reserve['commitment_ids']),
                        'target_metric_id':target_id,'already_budgeted_metric_id':linked_id})
                emit(account_id,'additional_planning_reserve',expected['additional_reserve_minor'],currency,
                     'sum_max_zero_reserve_minus_explicit_linked_obligations','tools.budget_reserve_dedup/1',refs,
                     begin=begin,end=end,kind='planning_scenario',assumptions=assumptions)
        elif name=='cashflow':
            expected=self.g.cashflow_timeline(workspace,args['days']);begin=expected['as_of'];end=expected['horizon_end']
            for account in expected['accounts']:
                aid=account['account_id'];events=[e for slot in expected['timeline'] for e in slot['events'] if e['account_id']==aid]
                refs=('accounts/'+aid,*tuple(e['evidence_ref'] for e in events))
                assumptions=tuple({'type':'recorded_forecast_candidate','event_id':e['id'],
                                   'notice':'已记录未付预测，仅用于候选分支。'} for e in events if e['kind']=='recorded_forecast')
                point_fields={'opening_balance_minor':'ledger_cash_balance','reserve_floor_minor':'assigned_emergency_floor',
                              'held_minor':'effective_fund_holds','current_available_minor':'floor_surplus_before_future_obligations'}
                for field,basis in point_fields.items():
                    emit(aid,field.removesuffix('_minor'),account[field],account['currency'],basis,
                         'tools.cashflow_point/1',refs,on=begin)
                for field in ('projected_balance_after_confirmed_minor','projected_available_after_confirmed_minor',
                              'projected_balance_if_candidates_occur_minor','projected_available_if_candidates_occur_minor',
                              'minimum_available_after_confirmed_minor','minimum_available_if_candidates_occur_minor'):
                    candidate='candidates' in field
                    emit(aid,field.removesuffix('_minor'),account[field],account['currency'],
                         'candidate_floor_surplus' if candidate and 'available' in field else
                         'confirmed_floor_surplus' if 'available' in field else 'projected_cash_balance',
                         'tools.cashflow_candidates/1' if candidate else 'tools.cashflow_confirmed/1',refs,
                         begin=begin,end=end,kind='candidate_scenario' if candidate else 'recorded_projection',
                         assumptions=assumptions if candidate else (),
                         excluded=('unrecorded_living_expenses','adequacy_not_established','pending_operations_not_counted'))
                for branch in ('confirmed','candidate'):
                    for direction in ('inflow','outflow'):
                        value=sum(abs(e['cash_delta_minor']) for e in events
                                  if e['certainty']==branch and (e['cash_delta_minor']>=0)==(direction=='inflow'))
                        emit(aid,branch+'_'+direction,value,account['currency'],'requested_period_recorded_future_cash_flow',
                             'tools.cashflow_account_flow/1',refs,begin=begin,end=end,
                             kind='candidate_scenario' if branch=='candidate' else 'recorded_projection',
                             assumptions=assumptions if branch=='candidate' else ())
            for currency,totals in expected['totals_by_currency'].items():
                for field,value in totals.items():
                    emit(workspace,field.removesuffix('_minor'),value,currency,'requested_period_recorded_future_cash_flow',
                         'tools.cashflow_flow_total/1',('cashflow/'+workspace,),begin=begin,end=end,
                         kind='candidate_scenario' if field.startswith('candidate') else 'recorded_projection')
            metadata={'requested_horizon_days':args['days'],'period_start':begin,'period_end':end,
                      'scope':'recorded_items_only','adequacy':'unknown'}
        elif name in ('monthly_statement','annual_statement'):
            expected,metadata=self._describe_statement(workspace,name,args)
            currency=metadata['currency'];begin,end=metadata['period_start'],metadata['period_end']
            data_scope=metadata['data_scope'];gaps=tuple(metadata['coverage_gaps'])
            if currency:
                fields={key:(value,'recorded_'+key+'_flow_not_cash_balance') for key,value in expected['totals_minor'].items()}
                for field in ('income_expense_surplus_minor','cash_delta_minor','journal_net_expense_minor','cash_expense_minor'):
                    if field in expected:fields[field]=(expected[field],'recorded_statement_'+field.removesuffix('_minor')+'_not_current_cash')
                for field,(value,basis) in fields.items():
                    if metadata['no_records']: value=None
                    emit(workspace,field.removesuffix('_minor'),value,currency,basis,'tools.statement_observed_totals/1',
                         ('statement/'+workspace+'/'+metadata['requested_period'],),begin=begin,end=end,
                         data_scope=data_scope,gaps=gaps,partial=not metadata['complete_period'],
                         excluded=('not_current_account_balance','borrowing_and_investment_principal_not_income',
                                   'missing_periods_not_zero','depreciation_not_cash_expense'))
                for category,value in expected.get('category_expense_minor',{}).items():
                    emit(workspace,'category_expense:'+category,value,currency,'recorded_category_cash_expense',
                         'tools.statement_category/1',('statement/'+workspace+'/'+metadata['requested_period'],),
                         begin=begin,end=end,data_scope=data_scope,gaps=gaps,partial=not metadata['complete_period'],
                         excluded=('not_current_cash','depreciation_not_cash_expense'))
        elif name=='customer_records':
            domain=args.get('domain');fields=_CUSTOMER_DOMAINS.get(domain)
            if fields is None: raise BankError('METRIC_ARGUMENTS_INVALID','未知档案领域。',422)
            archive=self.bank.customer_archive(workspace);records=archive['records'];on=archive['as_of']
            expected={'as_of':on,'records':{k:records.get(k,[]) for k in fields},
                      'unavailable_entities':[k for k in fields if k not in records]}
            metadata={'unavailable_entities':expected['unavailable_entities'],'as_of':on}
            money_fields={
                'loans': {'outstanding_minor':('recorded_outstanding_debt','recorded_fact'),
                          'original_principal_minor':('historical_origination_principal_not_current_cash','recorded_fact'),
                          'overdue_minor':('recorded_overdue_debt','recorded_fact')},
                'loan_installments':{k:('recorded_installment_'+k,'recorded_fact') for k in ('principal_minor','interest_minor','fees_minor','paid_minor')},
                'deposit_contracts':{'principal_minor':('recorded_deposit_contract_principal','recorded_fact'),
                                     'interest_paid_minor':('historically_recorded_interest_paid_not_current_cash','recorded_fact')},
                'wealth_goals':{k:('recorded_goal_'+k+'_not_cash','planning_scenario') for k in ('target_minor','saved_minor')},
                'budgets':{'limit_minor':('recorded_category_plan_not_cash','planning_scenario')},
                'advice_records':{'liquidity_floor_minor':('draft_advice_floor_not_current_funds','planning_scenario')},
                'account_limits':{k:('recorded_account_'+k+'_not_cash','recorded_fact') for k in ('limit_minor','used_minor')},
            }
            accounts={a['id']:a for a in archive['accounts']}
            single_currency=next(iter({a['currency'] for a in accounts.values()})) if len({a['currency'] for a in accounts.values()})==1 else None
            for table in fields:
                for row in records.get(table,[]):
                    aid=row.get('account_id',row.get('repayment_account_id'))
                    if table=='loan_installments':
                        loan=next((r for r in records['loans'] if r['id']==row['loan_id']),None)
                        aid=loan['repayment_account_id'] if loan else None
                    currency=row.get('currency') or (accounts.get(aid) or {}).get('currency') or single_currency
                    if not currency:continue
                    for field,(basis,kind) in money_fields.get(table,{}).items():
                        if field not in row:continue
                        kwargs={'on':on}
                        if field=='original_principal_minor':kwargs={'on':row['started_on']}
                        if field=='interest_paid_minor':kwargs={'begin':row['started_on'],'end':min(on,row['matures_on'])}
                        if table=='loan_installments':kwargs={'on':row['due_on']}
                        if table=='budgets':
                            kwargs={'begin':row['period']+'-01','end':row['period']+'-'+str(monthrange(*map(int,row['period'].split('-')))[1])}
                        emit(row['id'],field.removesuffix('_minor'),row[field],currency,basis,'tools.customer_record/1',
                             (table+'/'+row['id'],),kind=kind,excluded=('not_current_cash_balance',),**kwargs)
        elif name=='money_calculation':
            expected=self._calculate_hypothesis(args)
            assumptions=tuple({'type':'user_hypothesis','input_index':i,'value_minor':v} for i,v in enumerate(args['values_minor']))
            if 'factor' in args: assumptions+=({'type':'user_hypothesis','factor':args['factor']},)
            emit(workspace,'hypothetical_'+args['operation']+'_result',expected['result_minor'],args['currency'],
                 'user_input_arithmetic_not_account_data','tools.money_hypothesis/1',(call_ref,),
                 on=versions['business_date'],kind='hypothesis',data_scope='user_input_scenario',assumptions=assumptions,
                 excluded=('not_account_balance','not_formal_risk_profile','not_return_forecast'))
        elif name=='prepare_operation':
            expected=self._describe_operation(workspace,args,binding)
            account=expected['account'];operation=expected['operation'];payload=operation['payload'];on=versions['business_date']
            refs=('operations/'+operation['id'],'accounts/'+account['id'])
            emit(account['id'],'pre_confirmation_settled_cash',account['balance_minor'],account['currency'],
                 'current_ledger_cash_before_pending_operation','tools.pending_operation_current/1',refs,on=on)
            if type(payload.get('amount_minor')) is int:
                emit(operation['id'],'pending_operation_amount',payload['amount_minor'],account['currency'],
                     'pending_draft_amount_not_executed','tools.pending_operation_amount/1',refs,on=on,
                     kind='planning_scenario',excluded=('not_executed','not_deducted_from_cash'))
            for field,value in expected.get('projected_after_confirmation',{}).items():
                if field.endswith('_minor'):
                    emit(account['id'],'after_confirmation_'+field.removesuffix('_minor'),value,account['currency'],
                         'pending_operation_conditional_projection','tools.pending_operation_projection/1',refs,on=on,
                         kind='planning_scenario',assumptions=({'type':'pending_user_confirmation','operation_id':operation['id']},),
                         excluded=('not_execution_receipt','confirmation_rechecks_required'))
        else:
            expected,metadata=self._describe_service(workspace,name,binding)
            for row in metadata.pop('metric_terms'):
                term,subject=row['terms'],row['subject_id']
                for field in ('monthly_value_minor','max_cost_minor','minimum_monthly_salary_minor'):
                    if field not in term:continue
                    emit(subject,field.removesuffix('_minor'),term[field],term['currency'],
                         'approved_conditional_service_terms_not_cash','tools.service_terms/1',('service_terms/'+subject,),
                         begin=term['valid_from'],end=term['valid_through'],data_scope='navren_synthetic_service_terms_not_cash',
                         kind='planning_scenario',
                         assumptions=({'type':'conditional_service_terms','status':row['status'],
                                       'duration_months':term['duration_months']},),
                         excluded=('not_account_cash','not_awarded_benefit','eligibility_not_established'))
        return expected,metrics,kinds,metadata

    def _describe_budget(self, workspace, args, versions):
        account_id=args.get('account_id');floor=args.get('floor_minor',0);reserves=args.get('reserves',[])
        if not isinstance(account_id,str) or type(floor) is not int or floor<0 or not isinstance(reserves,list) or len(reserves)>8:
            raise BankError('METRIC_ARGUMENTS_INVALID','预算需要真实账户、非负整数底线和有效留款列表。',422)
        state=self.g.overview(workspace)
        account=next((a for a in state['accounts'] if a['id']==account_id),None)
        if account is None:raise BankError('METRIC_SUBJECT_MISMATCH','预算账户不属于当前客户。',403)
        data=self.g.budget(workspace,account_id,floor)
        data.update(account_id=account_id,currency=account['currency'],name=account['name'])
        commitments={c['evidence_id']:c for c in data['commitments'] if c['account_id']==account_id}
        observation=self._budget_observations.get((workspace,account_id))
        if reserves and (not observation or observation['versions']!=versions):
            raise BankError('BUDGET_BASELINE_REQUIRED','请先重新读取同账户当前预算和已计入义务ID。',409)
        extra,used,applied=0,set(),[]
        for reserve in reserves:
            if (not isinstance(reserve,dict) or set(reserve)!={'label','amount_minor','commitment_ids'}
                    or not isinstance(reserve['label'],str) or not reserve['label'].strip()
                    or type(reserve['amount_minor']) is not int or reserve['amount_minor']<0
                    or not isinstance(reserve['commitment_ids'],list)
                    or any(not isinstance(i,str) for i in reserve['commitment_ids'])):
                raise BankError('METRIC_ARGUMENTS_INVALID','规划留款必须包含说明、整数金额和明确义务ID。',422)
            ids=reserve['commitment_ids']
            if (len(set(ids))!=len(ids) or used.intersection(ids) or set(ids)-set(commitments)
                    or set(ids)-set(observation['commitment_ids'])):
                raise BankError('INVALID_RESERVE_LINK','规划留款的义务ID没有被观察、错属账户或重复。',422)
            used.update(ids);linked=sum(commitments[i]['amount_minor'] for i in ids)
            additional=max(0,reserve['amount_minor']-linked);extra+=additional
            applied.append({**reserve,'already_budgeted_minor':linked,'additional_reserve_minor':additional})
        data.update(baseline_investable_minor=data['investable_minor'],additional_reserve_minor=extra,
                    investable_minor=max(0,data['investable_minor']-extra),
                    budget_scope='planning_scenario' if reserves else 'current_budget',planning_reserves=applied)
        floor_override=floor>self.g.budget(workspace,account_id,0)['reserve_minor'] if floor else False
        return data,{'observed_commitment_ids':sorted(commitments),'floor_override_is_planning':floor_override,
                     'period_start':state['as_of'],'period_end':state['commitment_window_end'],
                     'reserve_basis':data['reserve_basis'],'real_reservation_created':False}

    def _describe_statement(self, workspace, name, args):
        annual=name=='annual_statement'
        if annual:
            year=args.get('year')
            if type(year) is not int or not 1900<=year<=2100:
                raise BankError('METRIC_ARGUMENTS_INVALID','年度参数必须为有效整数年份。',422)
            period=str(year);start,end=period+'-01-01',period+'-12-31'
            report=self.g.annual_analysis(workspace,year)
        else:
            period=args.get('month')
            if not isinstance(period,str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',period):
                raise BankError('METRIC_ARGUMENTS_INVALID','月份参数必须为YYYY-MM。',422)
            year,month=map(int,period.split('-'));start=period+'-01';end=period+'-'+str(monthrange(year,month)[1])
            report=self.g.analysis(workspace,period)
        state=self.g.source(workspace);imported=state['active_source']=='imported'
        count=report.get('transaction_count',report.get('archived_count',0)+report.get('live_count',0))
        no_records=count==0 and not report.get('source_summaries')
        scope='imported_statement_records' if imported else 'navren_synthetic' if self.g.demo_accounts.is_demo(workspace) else 'simulation_statement_records'
        coverage_start,coverage_end=report.get('coverage_start'),report.get('coverage_end')
        recorded=report.get('recorded_range')
        if coverage_start is None and recorded:coverage_start=recorded[0][:10]
        if coverage_end is None and recorded:coverage_end=recorded[-1][:10]
        covered=report.get('covered_months',[])
        complete=False;gaps=[]
        if imported:
            currency=(state.get('dataset') or {}).get('currency')
            if annual:
                rows=self.g.transactions(workspace)
                dates=[r['occurred_at'][:10] for r in rows if r['occurred_at'][:4]==period]
                if dates:coverage_start,coverage_end=min(dates),max(dates)
            gaps.append('imported_records_do_not_prove_complete_period')
        else:
            with closing(self.bank.connect()) as db:
                accounts=self.bank._accounts(db,workspace)
                rows=[dict(r) for r in db.execute('SELECT * FROM statement_periods WHERE workspace_id=? AND period_end>=? AND period_start<=? ORDER BY period_start',
                                                 (workspace,start,end))]
                observed_dates=[]
                for table in ('transactions','archived_transactions') if annual else ('transactions',):
                    observed_dates.extend(r[0][:10] for r in db.execute('SELECT occurred_at FROM '+table+
                        ' WHERE workspace_id=? AND substr(occurred_at,1,10) BETWEEN ? AND ?',(workspace,start,end)))
            currencies={a['currency'] for a in accounts};currency=next(iter(currencies)) if len(currencies)==1 else None
            if scope=='navren_synthetic':
                complete=(coverage_start==start and coverage_end==end and not report.get('partial_period',True))
            elif rows:
                coverage_start=min(r['period_start'] for r in rows);coverage_end=max(r['period_end'] for r in rows)
                # 必须每个账户均有连续的正式期间；有流水不表示完整覆盖。
                complete=True
                for account in accounts:
                    cursor=date.fromisoformat(start)
                    for row in sorted((r for r in rows if r['account_id']==account['id']),key=lambda r:r['period_start']):
                        left,right=date.fromisoformat(row['period_start']),date.fromisoformat(row['period_end'])
                        if left>cursor:break
                        if right>=cursor:cursor=right+timedelta(days=1)
                    if cursor<=date.fromisoformat(end):complete=False
                covered=sorted({r['period_start'][:7] for r in rows})
            if scope!='navren_synthetic' and observed_dates:
                coverage_start=min([*observed_dates,*([coverage_start] if coverage_start else [])])
                coverage_end=max([*observed_dates,*([coverage_end] if coverage_end else [])])
                covered=sorted(set(covered)|{d[:7] for d in observed_dates})
            if not currency:gaps.append('mixed_currency_statement_cannot_be_aggregated')
        if no_records:complete=False
        if not complete:gaps.append('requested_period_not_fully_covered')
        if annual:
            missing=sorted({period+'-'+f'{m:02d}' for m in range(1,13)}-set(covered))
            gaps.extend('uncovered_month:'+m for m in missing)
        if no_records:gaps.append('no_observed_records_for_requested_period')
        # 指标绑定实际已覆盖时间；请求边界单独保留，不能把局部年份写成全年。
        actual_start=max(start,coverage_start) if coverage_start else start
        actual_end=min(end,coverage_end) if coverage_end else end
        if actual_start>actual_end:actual_start,actual_end=start,end;no_records=True
        metadata={'requested_period':period,'requested_period_start':start,'requested_period_end':end,
                  'period_start':actual_start,'period_end':actual_end,'source_coverage_start':coverage_start,
                  'source_coverage_end':coverage_end,'covered_months':covered,'complete_period':complete,
                  'no_records':no_records,'currency':currency,'data_scope':scope,'coverage_gaps':gaps}
        return {k:v for k,v in report.items() if k!='notice'},metadata

    @staticmethod
    def _calculate_hypothesis(args):
        operation=args.get('operation');values=args.get('values_minor');currency=args.get('currency')
        if (operation not in ('sum','subtract','multiply') or not isinstance(values,list) or not 1<=len(values)<=20
                or any(type(v) is not int or abs(v)>100000000 for v in values)
                or currency not in ('CNY','USD','EUR','GBP','JPY')):
            raise BankError('METRIC_ARGUMENTS_INVALID','假设计算需要整数金额、明确币种和支持的算式。',422)
        if operation=='multiply':
            if len(values)!=1 or type(args.get('factor')) is not int or not 1<=args['factor']<=1200:
                raise BankError('INVALID_CALCULATION','乘法需要一项金额和合法整数倍数。',422)
            value=values[0]*args['factor']
        else:
            if 'factor' in args:raise BankError('INVALID_CALCULATION','只有乘法允许倍数。',422)
            value=sum(values) if operation=='sum' else values[0]-sum(values[1:])
        return {'currency':currency,'result_minor':value,'source':'user_input_scenario'}

    def _describe_operation(self, workspace, args, binding):
        view=self.g.operation_view(workspace,binding['operation_id']);operation=view['operation'];payload=operation['payload']
        # 幂等键为服务生成的敏感执行元数据，不出现在模型参数或指标合同中。
        compare={k:v for k,v in args.items() if k!='liquidity_floor_minor'}
        expected_payload={k:v for k,v in payload.items() if k!='idempotency_key'}
        if compare!=expected_payload or (args.get('liquidity_floor_minor',0)!=view['liquidity_floor_minor']):
            raise BankError('METRIC_OPERATION_MISMATCH','当前草案与请求的操作、对象或金额不一致。',409)
        if operation['action'] in ('purchase','transfer','reserve_funds'):
            amount=payload['amount_minor']
            view['projected_after_confirmation']={
                'currency':view['account']['currency'],
                'balance_minor':view['account']['balance_minor']-(amount if operation['action']!='reserve_funds' else 0),
                'investable_minor':max(0,view['budget']['investable_minor']-amount)}
        return view

    def _describe_service(self, workspace, name, binding):
        service=self.g.service_negotiations;rules=service.rules();terms=[]
        if name=='service_offer_rules':
            expected=rules
            terms=[{'terms':r,'subject_id':r['offer_id'],'status':'approved_rule_conditional'} for r in rules['offers']]
        elif name=='service_application_status':
            expected=service.applications(workspace)
            terms=[{'terms':r['terms'],'subject_id':r['application_id'],'status':r['status']} for r in expected['applications']]
        else:
            with closing(self.bank.connect()) as db:
                row=db.execute('SELECT * FROM service_negotiation_drafts WHERE workspace_id=? AND id=?',
                               (workspace,binding['draft_id'])).fetchone()
                if not row:raise BankError('STALE_SERVICE_EVIDENCE','服务预览来源不存在。',409)
                selected=json.loads(row['offer_ids_json']);conditions=json.loads(row['conditions_json'])
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='base_service_negotiation_terms'").fetchone():
                    bound=db.execute('SELECT * FROM base_service_negotiation_terms WHERE draft_id=?',(row['id'],)).fetchone()
                    if not bound or bound['rules_sha256']!=service._rules_digest(rules):
                        raise BankError('STALE_SERVICE_EVIDENCE','已展示服务条款与当前获准规则不一致。',409)
            offers=[]
            for oid in selected:
                rule=next((r for r in rules['offers'] if r['offer_id']==oid),None)
                if not rule or row['rule_version']!=rules['schema_version']:
                    raise BankError('STALE_SERVICE_EVIDENCE','服务预览中的条款已撤回。',409)
                # 签名仅绑定库中的确切条件预览，不确认资格或兑现优惠。
                offers.append({**rule,'eligibility_status':'conditional_application','review_status':'rules_checked',
                               'status_label':'可提交条件申请，资格待模拟规则核验',
                               'investment_purchase_required':bool(rule.get('requires_investment_purchase'))})
                terms.append({'terms':rule,'subject_id':row['id']+'/'+oid,'status':row['status']})
            expected={'status':'previewed','draft_id':row['id'],'conditions':conditions,'offers':offers,
                      'rule_version':row['rule_version'],'memory_revision':row['memory_revision']}
        for row in terms:
            term=row['terms']
            if hasattr(service,'_validate_rule'):service._validate_rule(term)
            if (type(term.get('monthly_value_minor')) is not int or type(term.get('duration_months')) is not int
                    or type(term.get('max_cost_minor')) is not int
                    or term['monthly_value_minor']*term['duration_months']!=term['max_cost_minor']):
                raise BankError('METRIC_SOURCE_INVALID','服务条款费用算式无有效来源。',503)
        return expected,{'scope':'conditional_service_terms_not_cash','metric_terms':terms}

    def bind_operation(self, workspace, operation_id, snapshot):
        with self.bank.transaction() as db:
            self.bank._operation(db,workspace,operation_id)
            existing=db.execute('SELECT snapshot FROM base_operation_metrics WHERE operation_id=?',(operation_id,)).fetchone()
            if existing:return  # 幂等重试不能替换原草案证据。
            self.validate(workspace,snapshot,db)
            db.execute('INSERT INTO base_operation_metrics VALUES(?,?,?,?)',
                       (operation_id,workspace,json.dumps(snapshot,ensure_ascii=False,sort_keys=True),self.bank.clock()))
            self.bank._audit(db,workspace,'financial_metrics_bound',{'snapshot_id':snapshot['snapshot_id']},operation_id)

    def register_required(self, db, workspace, operation):
        db.execute('INSERT INTO base_operation_metric_requirements VALUES(?,?)',(operation,workspace))
        # 银行 INSERT 所在原事务捕获完整业务原件，不能把准备返回后的新行修改洗进许可。
        row=self.bank._operation(db,workspace,operation)
        self._prepared_operation_rows[(workspace,operation)]={k:v for k,v in dict(row).items()
            if k not in ('verification_hash','verification_expires_at')}

    def execution_guard(self, db, workspace, operation):
        row=db.execute('SELECT workspace_id,snapshot FROM base_operation_metrics WHERE operation_id=?',(operation['id'],)).fetchone()
        required=db.execute('SELECT workspace_id FROM base_operation_metric_requirements WHERE operation_id=?',(operation['id'],)).fetchone()
        if required and row is None:
            raise BankError('METRIC_BINDING_REQUIRED','草案尚未完成指标绑定，请重新准备。',409)
        if row:
            if row['workspace_id']!=workspace:
                raise BankError('METRIC_SUBJECT_MISMATCH','草案指标不属于当前账户。',403)
            self.validate(workspace,json.loads(row['snapshot']),db)
