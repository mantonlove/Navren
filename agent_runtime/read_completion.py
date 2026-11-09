"""Complete explicit personal cash queries using this turn's evidence only."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from simulated_bank import BankError


class ReadResultReferenceIncomplete(BankError):
    """Host-only state: the required reads exist, but their result is unreferenced."""
    def __init__(self, message, *, kind, required_count, covered_count):
        super().__init__('READ_RESULT_REQUIRED', message)
        self.kind = kind
        self.required_count = required_count
        self.covered_count = covered_count
        self.amount_contract_checked = False


class ReadResultPrecheckFatal(BankError):
    """Preserve a source-staleness failure discovered by the narrow precheck."""
    def __init__(self, error):
        super().__init__(error.code, error.message, error.status, error.persist)


def household_snapshot_request(text):
    """Finite whole-request read grammar; unknown clauses keep the model path.

    These are source fields, not advice, an adequacy judgement, or an operation.
    All requested fields are retained. No account or forecast period is guessed.
    """
    value = unicodedata.normalize('NFKC', text or '').strip()
    field = r'(?:全户(?:已结算)?现金|正式风险等级|投资期限|应急(?:底线|保留))'
    match = re.fullmatch(
        r'(?:请)?(?:核对|查询|查看)(?:我|本人)(?:现在|当前)?的?'
        r'(?P<fields>' + field + r'(?:[、,，和与及]+' + field + r')*)'
        r'(?:[、,，;；\s]*(?:再)?(?:看|核对|查询|查看)(?:未来|今后|接下来)'
        r'(?P<days>[1-9]\d{0,2})天(?:的)?已确认(?:支出|收支)(?:与|和|及)待核实候选)?'
        r'(?:[;；,，\s]*(?:不要操作|仅查询|只读))?[。.!！?？]*', value)
    if not match:
        return None
    days = int(match['days']) if match['days'] else None
    if days is not None and days > 365:
        return None
    names = re.findall(field, match['fields'])
    roles = [('cash' if '现金' in name else 'risk' if '风险' in name else
              'horizon' if '期限' in name else 'reserve') for name in names]
    if len(roles) != len(set(roles)):
        return None
    return {'fields': roles, 'days': days}


def household_snapshot_step(tools, attempts):
    """Use ordinary reads, signed amount fragments and full business review.

    Only exact current evidence is usable. This never creates a draft, infers
    money, changes tool permissions, skips FinalProof, or returns a PASS.
    """
    query = household_snapshot_request(tools.user_text)
    if (query is None or not tools.metric_selection_mode or tools.private_context_disabled
            or tools._history_reference is not None or tools.changed or tools.drafts
            or tools.calendar_drafts or tools.widgets):
        return None
    evidence = [e for e in tools.evidence.values() if e.get('context_hash') == tools.start_context]
    requests = [('account_summary', {})]
    if query['days'] is not None:
        requests.append(('cashflow', {'days': query['days']}))
    sources = {}
    missing = []
    for name, parameters in requests:
        rows = [e for e in evidence if e.get('tool') == name and e.get('parameters') == parameters]
        if not rows:
            if attempts.get(name, 0) or any(t.get('tool') == name and t.get('status') != 'completed'
                                          for t in tools.trace):
                return None
            missing.append((name, parameters))
        else:
            sources[name] = rows[-1]
    receipt = {'contract': 'navren.current-household-readout/1', 'requested_fields': query['fields'],
        'horizon_days': query['days'], 'financial_amount_inferred': False,
        'financial_side_effects': False, 'ordinary_full_review_and_publication_required': True}
    if missing:
        return missing, {**receipt, 'stage': 'required_current_reads'}
    tools.fresh()
    summary = sources['account_summary']; data = summary.get('data', {})
    accounts, profile = data.get('accounts'), data.get('profile')
    if (not isinstance(accounts, list) or not 1 <= len(accounts) <= 12
            or len({a['id'] for a in accounts}) != len(accounts)
            or not isinstance(profile, dict) or not isinstance(data.get('as_of'), str)):
        raise BankError('HOUSEHOLD_READ_SOURCE_REQUIRED', '全户查询原件不完整。', 422)
    records = [tools.metrics.lookup(mid) for mid in tools.metrics.metric_ids]

    def metric(source, name, subject, currency, expected=None, kind='recorded_fact'):
        rows = [r for r in records if source['id'] in r['evidence_ids'] and r['kind'] == kind
                and r['metric']['metric_name'] == name and r['metric']['subject_id'] == subject
                and r['metric']['currency'] == currency and r['metric']['unit'] == 'minor_currency'
                and r['metric']['calculation_status'] == 'calculated'
                and type(r['metric']['value_minor']) is int
                and (expected is None or r['metric']['value_minor'] == expected)]
        if len(rows) != 1:
            raise BankError('HOUSEHOLD_READ_METRIC_REQUIRED', '全户查询须引用本轮唯一正式金额指标。', 422)
        return {'metric_id': rows[0]['metric_id']}

    parts = [{'text': '当前本机模拟档案核对（截至' + data['as_of'] + '）：\n'}]
    if 'cash' in query['fields']:
        totals = data.get('cash_by_currency')
        if not isinstance(totals, list) or not totals:
            raise BankError('HOUSEHOLD_READ_SOURCE_REQUIRED', '缺少按币种全户现金原件。', 422)
        for row in totals:
            parts += [{'text': '全户已结算现金（' + row['currency'] + '）'},
                      metric(summary, 'settled_cash_total', tools.w, row['currency'], row['balance_minor']),
                      {'text': '。\n'}]
        for account in accounts:
            parts += [{'text': '账户' + account['name'] + '的已结算现金'},
                      metric(summary, 'settled_cash_balance', account['id'], account['currency'], account['balance_minor']),
                      {'text': '。\n'}]
    if 'risk' in query['fields']:
        if type(profile.get('risk_level')) is not int or not 1 <= profile['risk_level'] <= 5:
            raise BankError('HOUSEHOLD_READ_SOURCE_REQUIRED', '正式风险等级原件不完整。', 422)
        parts.append({'text': '正式风险等级R' + str(profile['risk_level']) + '。\n'})
    if 'horizon' in query['fields']:
        if type(profile.get('horizon_days')) is not int or profile['horizon_days'] < 0:
            raise BankError('HOUSEHOLD_READ_SOURCE_REQUIRED', '正式投资期限原件不完整。', 422)
        parts.append({'text': '正式投资期限' + str(profile['horizon_days']) + '天。\n'})
    if 'reserve' in query['fields']:
        reserve_metrics = [r for r in records if summary['id'] in r['evidence_ids']
                           and r['metric']['metric_name'] == 'profile_emergency_reserve']
        if len(reserve_metrics) != 1:
            raise BankError('HOUSEHOLD_READ_METRIC_REQUIRED', '缺少正式全户应急保留指标。', 422)
        parts += [{'text': '正式全户应急保留要求'}, metric(summary, 'profile_emergency_reserve',
                  tools.w, reserve_metrics[0]['metric']['currency'], profile.get('reserve_minor')),
                  {'text': '；这是画像要求，不等于某个账户已分配的底线，也不证明应急充分。\n'}]
    if query['days'] is not None:
        flow = sources['cashflow']; current = flow['data']
        if (current.get('horizon_days') != query['days'] or current.get('as_of') != data['as_of']
                or current.get('source') != 'simulated_bank_archive'):
            raise BankError('HOUSEHOLD_READ_SOURCE_REQUIRED', '现金流原件期间或来源不一致。', 422)
        parts.append({'text': '未来' + str(query['days']) + '天（至' + current['horizon_end'] + '）的已记录收支：\n'})
        for currency, totals in current['totals_by_currency'].items():
            for field, label, kind in [('confirmed_outflow', '已确认支出', 'recorded_projection'),
                                       ('confirmed_inflow', '已确认入账', 'recorded_projection'),
                                       ('candidate_outflow', '待核实候选支出', 'candidate_scenario'),
                                       ('candidate_inflow', '待核实候选入账', 'candidate_scenario')]:
                parts += [{'text': label + '（' + currency + '）'},
                          metric(flow, field, tools.w, currency, totals[field + '_minor'], kind),
                          {'text': '。\n'}]
        parts.append({'text': '候选仅是待核实情景，不能当作已确认支出或实际交易；该期间的已确认金额不能否定过去收入、贷款或其他历史流水。未录入的支出和未来收入、收入稳定性及应急充分性仍须核实。\n'})
    parts.append({'text': '本轮仅查询，没有准备草案、修改画像或执行交易。'})
    final = {'reply_parts': parts, 'claims': [{'text': '本轮全户核对使用当前正式模拟档案和对应期间原件，候选与已确认分支分开。',
              'evidence_ids': [e['id'] for e in sources.values()]}],
             'amount_claims': [], 'draft_ids': [], 'followups': []}
    return [('answer_reviewed', final)], {**receipt, 'stage': 'signed_readout',
        'evidence_ids': [e['id'] for e in sources.values()]}


def statement_fact_request(text, history, as_of):
    """A period-bound classification lookup; mixed tasks stay reviewed.

    Only the user's own persisted request supplies an inherited period. Prior
    assistant amounts are never reused as evidence; the report is read again.
    """
    value = unicodedata.normalize('NFKC', text or '').lower()
    if cash_query_kind(value) or re.search(r'顺便|同时|另外|也查|还查|再查|以及|要不要|该不该|能不能|减少|节省|优化', value):
        return None
    income = bool(re.search(r'收入|\bincome\b', value))
    expense = bool(re.search(r'支出|消费|开销|花费|\b(?:expenses?|spending)\b', value))
    if income == expense:
        return None
    largest = bool(re.search(r'最大|最多|最高|\b(?:largest|biggest|highest)\b', value))
    if expense and (not largest or re.search(r'单笔|一笔|哪笔|几笔|笔数|transaction', value)):
        return None
    if income and not re.search(r'来源|构成|组成|分类|哪(?:些|几|里)|多少|合计|总额|\b(?:sources?|breakdown|total)\b', value):
        return None
    if re.search(r'解释|说明|分析|比较|区别|原因|为什么|为何|怎么|如何|公式|定义|方法|'
                 r'建议|推荐|适合|合适|风险|规划|配置|方案|预测|预计|未来|能否|是否|'
                 r'转账|申购|赎回|买入|卖出|支付|付款|操作|执行|办理|开户|销户|'
                 r'取消|撤回|修改|更新|记住|保存|创建|确认|拒绝|假设|假如|举例|'
                 r'新闻|市场|国家|政府|公司|行业|企业|他人|别人|家庭成员|'
                 r'\b(?:explain|why|how|analy[sz]e|compare|recommend|advice|risk|plan|'
                 r'forecast|transfer|purchase|redeem|sell|pay|save|confirm|hypothetical)\b', value):
        return None
    # Account-specific aggregation is a different scope from the whole report.
    if re.search(r'银行卡|卡号|卡里|卡上|账户|帐户|USD|EUR|GBP|JPY|美元|欧元|英镑|日元', value, re.I):
        return None
    try:
        clock = date.fromisoformat(as_of)
        year = clock.year
    except (ValueError, TypeError):
        return None

    def period(content):
        if re.search(r'历史|十年|全部|所有|去年|前年|今年|最近|近\d|过去|到|至|—|~|'
                     r'日|号|周|星期|(?<!\d)\d{4}[-/]\d{1,2}[-/]\d|'
                     r'\b(?:last year|past|months|days?|weeks?)\b', content):
            return None
        relative = re.findall(r'上个月|上月|本月|这个月|\b(?:last month|this month)\b', content)
        if relative:
            if len(relative) != 1 or re.search(r'\d|年|(?:上个月|上月|本月|这个月).*月', content):
                return None
            previous = relative[0] in ('上个月', '上月', 'last month')
            ordinal = clock.year * 12 + clock.month - 1 - int(previous)
            return f'{ordinal // 12:04d}-{ordinal % 12 + 1:02d}'
        canonical = re.findall(r'(?<!\d)(\d{4})[-/年](0?[1-9]|1[0-2])(?:月|(?=[^\d]|$))', content)
        if canonical:
            periods = {f'{int(y):04d}-{int(m):02d}' for y, m in canonical}
            if len(periods) != 1:
                return None
            selected = next(iter(periods))
            mentions = re.findall(r'(?<!\d)(0?[1-9]|1[0-2])月', content)
            return selected if all(int(m) == int(selected[-2:]) for m in mentions) else None
        month = re.findall(r'(?<!\d)(0?[1-9]|1[0-2])月', content)
        if month and len(set(month)) == 1:
            return f'{year:04d}-{int(month[0]):02d}'
        return None

    # Only an unambiguous single month is admissible. Relative months use the
    # trusted business clock, never the previous assistant's date or amounts.
    if re.search(r'历史|十年|全部|所有|去年|前年|今年|最近|近\d|过去|到|至|—|~|'
                 r'日|号|周|星期|(?<!\d)\d{4}[-/]\d{1,2}[-/]\d|'
                 r'\b(?:last year|past|months|days?|weeks?)\b', value):
        return None
    month = period(value)
    if income or expense:
        # Admit only a single classification question. Additional unrecognised
        # clauses must stay on the general path, not disappear in this scope.
        plain = re.sub(r'(?<!\d)\d{4}[-/年](?:0?[1-9]|1[0-2])月?|(?<!\d)(?:0?[1-9]|1[0-2])月|'
                       r'上个月|上月|本月|这个月|\b(?:last month|this month)\b', '', value)
        plain = re.sub(r'[\s,，。.!！?？]', '', plain)
        prefix = r'(?:请|帮我)?(?:查询|查看|查一下|查查|看看|告诉我)?(?:我|我的)?'
        if expense:
            chinese = (prefix + r'(?:最大|最多|最高)的?(?:支出|消费|开销|花费)(?:项|项目|类别|分类)?'
                       r'(?:是什么|是哪(?:一)?(?:项|类|个)|有哪些|有多少|是多少)?')
            english = r'(?:what(?:is|was))?(?:my)?(?:largest|biggest|highest)(?:expense|spending)(?:category)?'
        else:
            chinese = (prefix + r'收入(?:的)?(?:主要)?(?:来源|构成|组成|分类|合计|总额)?'
                       r'(?:是什么|有哪些|有哪(?:些|几类)|有多少|是多少|多少|来自哪里|在哪)?')
            english = r'(?:(?:what(?:are|is))|howmuch)?(?:my)?(?:total)?income(?:sources|breakdown|total)?'
        if not re.fullmatch(chinese + '|' + english, plain):
            return None
    if month is None and not re.search(r'\d|月|年', value):
        for message in reversed(history or []):
            if message.get('role') != 'user':
                continue
            content = unicodedata.normalize('NFKC', message.get('content', '')).lower()
            if re.search(r'账单|收支|收入|消费|钱花|支出|statement|income|expense', content):
                month = period(content)
                if month:
                    break
                if re.search(r'\d|月|年|历史|最近|过去', content):
                    break  # An ambiguous newer period must not revive an older one.
    if not month:
        return None
    return {'kind': 'income', 'month': month} if income else {
        'kind': 'expense', 'month': month, 'selection': 'largest_category'}


def income_statement_request(text, history, as_of):
    """Compatibility entry point for callers requesting income only."""
    query = statement_fact_request(text, history, as_of)
    return query if query and query['kind'] == 'income' else None


def complete_statement_facts(query, name, final, tools):
    if query is None or query['kind'] == 'income':
        return complete_income_statement_facts(query, name, final, tools)
    if name != 'answer_facts':
        raise BankError('READ_RESULT_REQUIRED', '本轮纯支出查询须用answer_facts引用本轮月报分类事实。')
    sources = [e for e in tools.evidence.values() if e['tool'] == 'monthly_statement'
               and e['parameters'].get('month') == query['month']
               and e['data'].get('month') == query['month']]
    if not sources:
        raise BankError('READ_COMPLETION_REQUIRED', '支出分类尚未查询。请调用monthly_statement，month=' + query['month'] + '。')
    source = sources[-1]
    data = source['data']
    categories = data.get('category_expense_minor')
    # The historical report has two different bases: journal expense and cash
    # consumption. Never reconcile cash categories against journal expense.
    total = data.get('cash_expense_minor')
    if (not isinstance(categories, dict) or type(total) is not int or total < 0
            or any(not isinstance(k, str) or not k for k in categories)
            or any(type(v) is not int or v < 0 for v in categories.values())
            or sum(categories.values()) != total or data.get('currency') != 'CNY'
            or any(not isinstance(data.get(k), str) or data[k][:7] != query['month']
                   for k in ('coverage_start', 'coverage_end'))):
        raise BankError('STATEMENT_EXPENSE_INCOMPLETE', '本轮现金消费分类尚未完成同源对账，不能把流水样本当作最大支出。', 503)
    wanted = {'/cash_expense_minor', *('/category_expense_minor/' + k.replace('~', '~0').replace('/', '~1') for k in categories)}
    facts = [f for f in tools.facts.values() if f['evidence_id'] == source['id']
             and f.get('source_path') in wanted and f.get('unit') == 'minor_currency'
             and f.get('currency') == 'CNY']
    if {f['source_path'] for f in facts} != wanted or len(facts) > 24:
        raise BankError('READ_RESULT_REQUIRED', '本轮支出分类的事实引用尚不完整，不能判定最大分类。')
    if not set(final['fact_ids']).intersection(f['id'] for f in facts):
        raise BankError('READ_RESULT_REQUIRED', '请用answer_facts选择本轮cash_expense_minor或category_expense_minor事实，而非收入或其他月份。')
    # Retain the complete same-source distribution, including ties; the local
    # renderer determines the largest category without model arithmetic.
    return {**final, 'fact_ids': [f['id'] for f in facts]}


def complete_income_statement_facts(query, name, final, tools):
    """Bind the complete same-period income classification, never a row sample."""
    if query is None:
        return final
    if name != 'answer_facts':
        raise BankError('READ_RESULT_REQUIRED', '本轮纯收入查询须用answer_facts引用本轮月报收入事实。')
    sources = [e for e in tools.evidence.values() if e['tool'] == 'monthly_statement'
               and e['parameters'].get('month') == query['month']
               and e['data'].get('month') == query['month']]
    if not sources:
        raise BankError('READ_COMPLETION_REQUIRED', '收入来源尚未查询。请调用monthly_statement，month=' + query['month'] + '。不能改读历史流水第一页。')
    source = sources[-1]
    data = source['data']
    categories = data.get('category_income_minor')
    total = data.get('totals_minor', {}).get('income')
    if (data.get('income_categories_complete') is not True or not isinstance(categories, dict)
            or type(total) is not int or any(type(v) is not int for v in categories.values())
            or sum(categories.values()) != total or data.get('currency') != 'CNY'):
        raise BankError('STATEMENT_INCOME_INCOMPLETE', '本轮月报收入分类尚未完成同源对账，不能把流水样本当作收入来源。', 503)
    wanted = {'/totals_minor/income', *('/category_income_minor/' + k.replace('~', '~0').replace('/', '~1') for k in categories)}
    facts = [f for f in tools.facts.values() if f['evidence_id'] == source['id']
             and f.get('source_path') in wanted and f.get('unit') == 'minor_currency'
             and f.get('currency') == 'CNY']
    if {f['source_path'] for f in facts} != wanted:
        raise BankError('READ_RESULT_REQUIRED', '本轮收入分类的事实引用尚不完整。')
    if not set(final['fact_ids']).intersection(f['id'] for f in facts):
        raise BankError('READ_RESULT_REQUIRED', '请用answer_facts选择本轮month收入总额或category_income_minor事实，而非其他月份或支出。')
    # Full classification fits the existing 24-fact contract. Above that, retain
    # only the verified total; the view must state that categories are absent.
    if len(facts) > 24:
        facts = [f for f in facts if f['source_path'] == '/totals_minor/income']
    return {**final, 'fact_ids': [f['id'] for f in facts]}


def cash_query_kind(text):
    text = unicodedata.normalize('NFKC', text or '').lower()
    # A knowledge/example request alone is not permission or a need to read cash.
    teaching = re.search(r'什么是|是什么意思|计算公式|定义|计算方法|如何计算|怎么计算|怎么算|what is|how to calculate', text)
    personal_amount = re.search(r'(?:我的|我(?:目前|现在|能|有|还剩)).*?(?:多少|是多少|查余额)|'
                               r'(?:查|查询|查看).*?(?:我的|我现在|我目前|当前账户)|my.*?(?:balance|how much)', text)
    if teaching and not personal_amount:
        return None
    actual = re.search(r'查(?:询|看)?|帮我(?:算|计算)|我(?:目前|现在|能|有)|my\b.*?(?:balance|investable)', text)
    if not actual and re.search(r'什么是|是什么意思|如何计算|怎么计算|怎么算|举例|what is|how to calculate', text):
        return None
    if re.search(r'假设|假如|举个例子|hypothetical|suppose|for example', text) and not re.search(
            r'查(?:询|看)?.*?(?:我的|我现在|我目前|当前)|(?:read|query).*?my', text):
        return None
    if re.search(r'不要(?:查|读取)|不用(?:查|读取)|do not (?:read|query)', text):
        return None
    if re.search(r'转账|买入|申购|赎回|挂失|transfer|purchase|redeem', text) and not re.search(
            r'查(?:询|看)?|核对|多少|是否|能否|可不可以|how much|balance\?', text):
        return None
    if re.search(r'可(?:以)?(?:投资|投|安排|支配|用|花)(?:的)?(?:多少)?(?:资金|金额|余额|钱)|'
                 r'多少(?:钱|资金|金额).*?(?:投资|可投|能投)|'
                 r'(?:可以|能)(?:拿出|拿去|用|投).*?(?:投资|多少)|用于投资|investable|arrangeable', text):
        return 'budget'
    if re.search(r'(?:账户|帐户|现金|银行卡|卡里|卡上).*?余额|余额|cash balance|account balance', text):
        return 'balance'
    return None


def _explicit_read_request(text):
    value = unicodedata.normalize('NFKC', text or '').lower()
    if re.search(r'不要(?:查|读取)|不用(?:查|读取)|禁止(?:查|读取)|假设|假如|举例|什么是|'
                 r'计算公式|定义|如何计算|怎么计算|\b(?:do not read|suppose|hypothetical|for example)\b', value):
        return None
    return value


def explicit_income_month(text):
    """An explicit personal income or statement month needs its actual source."""
    value = _explicit_read_request(text)
    if (value is None or not re.search(r'核对|查询|查看|查一下', value)
            or not re.search(r'收入|工资|账单|收支', value) or not re.search(r'我|本人|本账户', value)
            or re.search(r'公司|企业|国家|政府|行业|他人|别人|朋友|对方|新闻|市场', value)):
        return None
    months = re.findall(r'(?<!\d)(\d{4})[-/年](0?[1-9]|1[0-2])(?:月|(?=$|[^\d]))', value)
    if len(months) != 1:
        return None
    year, month = map(int, months[0])
    try:
        return date(year, month, 1).strftime('%Y-%m')
    except ValueError:
        return None


def explicit_statement_read_sources(text, history=()):
    """Bound read prerequisites for a personal, explicitly dated statement query.

    These are provider reads, not an answer or an interpretation of account
    amounts. A future cash-flow projection cannot substitute for these sources.
    """
    month = explicit_income_month(text)
    if month is None:
        value = _explicit_read_request(text)
        # In an authenticated bank conversation an unqualified request to
        # analyse a dated bill means the current owner's bill, not public data.
        if (value is not None and re.search(r'账单|收支', value)
                and re.search(r'分析|查|看|核对', value)
                and not re.search(r'他人|别人|朋友|对方|公司|企业|政府|行业|新闻|市场|客户', value)):
            periods = re.findall(r'(?<!\d)(\d{4})[-/年](0?[1-9]|1[0-2])(?:月|(?=$|[^\d]))', value)
            mentions = re.findall(r'(?<!\d)(0?[1-9]|1[0-2])月', value)
            if len(periods) == 1 and all(int(m) == int(periods[0][1]) for m in mentions):
                year, number = map(int, periods[0])
                try:
                    month = date(year, number, 1).strftime('%Y-%m')
                except ValueError:
                    pass
    if month is None and explicit_current_budget_read(text) and re.search(r'账单|收支|结余', text or ''):
        value = unicodedata.normalize('NFKC', text)
        mentions = {int(m) for m in re.findall(r'(?<!\d)(0?[1-9]|1[0-2])月', value)}
        if (len(mentions) <= 1 and not re.search(r'\d{4}|去年|今年|本月|上月|最近|过去|至|到', value)
                and (mentions or re.search(r'刚才|这份|上述|这张', value))):
            for message in reversed(history or ()):
                if message.get('role') != 'user' or not re.search(r'账单|收支|结余', message.get('content', '')):
                    continue
                prior = explicit_statement_read_sources(message['content'])
                months = [args['month'] for name, args in prior if name == 'monthly_statement']
                if len(months) == 1 and (not mentions or mentions == {int(months[0][-2:])}):
                    month = months[0]
                break  # A newer ambiguous/mismatched request cannot revive an older period.
    if month is None or not re.search(r'账单|收支|结余', text or ''):
        return ()
    rows = [('statement_source', {}), ('monthly_statement', {'month': month})]
    value = unicodedata.normalize('NFKC', text)
    if re.search(r'全年|年度|(?:12|十二)\s*个?月', value):
        rows.append(('annual_statement', {'year': int(month[:4])}))
    if cash_query_kind(value) == 'budget':
        rows.append(('account_summary', {}))
    return tuple(rows)


def explicit_reserve_planning_read(text):
    """Only prerequisites for the user's own read-only reserve calculation.

    No amount, commitment link, reserve or answer is inferred here. The model
    must still choose those arguments using the actual baseline result.
    """
    value = _explicit_read_request(text)
    return bool(value is not None and re.search(r'我|本人|本账户', value)
        and not re.search(r'公司|企业|国家|政府|行业|朋友|他人|别人|对方|客户', value)
        and re.search(r'留款|预留', value)
        and re.search(r'(?:只|仅)(?:做|作|供)?(?:规划|测算)|不(?:实际)?(?:预留|付款|扣款)', value)
        and re.search(r'(?:还)?(?:能|可)(?:安排|支配|用|花)(?:的)?(?:资金|金额|钱|多少)', value))


def explicit_saved_payee_read(text):
    """An explicit saved-payee lookup, including a request not to transfer."""
    value = _explicit_read_request(text)
    return bool(value is not None
        and re.search(r'查|看|核对', value)
        and re.search(r'保存|已存|收款人列表|收款人名册', value)
        and re.search(r'转账收款人|收款人', value)
        and not re.search(r'他人|别人|朋友的|对方的|公司|企业|客户', value))


def explicit_current_budget_read(text):
    value = _explicit_read_request(text)
    return bool(value is not None and cash_query_kind(value) == 'budget'
        and re.search(r'查|核对|帮我(?:算|计算)', value)
        and re.search(r'我|本人|[账帐]户', value)
        and not re.search(r'朋友|他人|别人|对方|客户|公司|企业|他(?:的|现在|目前)|她(?:的|现在|目前)', value))


def _named_current_accounts(text, accounts):
    """Resolve an explicit short account label only against unique current rows."""
    selected = [a for a in accounts if any(value and value in text
        for value in (a.get('id'), a.get('name')))]
    for role in re.findall(r'(?:日常|备用|工资|储蓄)[账帐]户', text):
        candidates = [a for a in accounts if a.get('name', '').endswith(role)]
        if len(candidates) == 1 and candidates[0] not in selected:
            selected.append(candidates[0])
    return selected


def required_statement_reads(tools):
    """Complete requested statement or planning sources, without read retries."""
    if (tools.private_context_disabled or tools._history_reference is not None
            or tools.changed or tools.drafts):
        return None
    sources = explicit_statement_read_sources(tools.user_text,
        getattr(tools, '_statement_request_history', ()))
    saved_payees = not sources and explicit_saved_payee_read(tools.user_text)
    if saved_payees:
        sources = (('account_summary', {}), ('business_records', {'kind': 'payments'}))
    payment_draft = False
    if not sources and 'payments' in getattr(tools, '_continued_maintainer_skills', ()):
        value = _explicit_read_request(tools.user_text)
        payment_draft = bool(value is not None
            and re.search(r'收款人|转账|汇款', value)
            and re.search(r'准备|草案|改为|改成', value)
            and not re.search(r'不要转账|不转账|不要.{0,6}准备|不用.{0,6}准备|'
                              r'只(?:查|看)|仅(?:查|看)|朋友的|他人|别人|对方的|公司|企业|'
                              r'解释|什么是|方法|流程|教程|示例|举例|概念', value))
        if payment_draft:
            # A current, unique pending transfer supplies a stable account
            # reference only; every proposal still reads its current budget.
            sources = ((('business_records', {'kind': 'payments'}),)
                       if getattr(tools, '_payment_current_parent', False) else
                       (('account_summary', {}), ('business_records', {'kind': 'payments'})))
    reserve_plan = not sources and explicit_reserve_planning_read(tools.user_text)
    if reserve_plan:
        sources = (('account_summary', {}),)
    current_budget = not sources and explicit_current_budget_read(tools.user_text)
    if current_budget:
        sources = (('account_summary', {}),)
    missing = []
    for name, parameters in sources:
        completed = [e for e in tools.evidence.values() if e.get('tool') == name
                     and e.get('context_hash') == tools.start_context
                     and e.get('parameters') == parameters]
        if completed:
            continue
        if any(t.get('tool') == name and t.get('status') != 'completed' for t in tools.trace):
            return None
        missing.append((name, parameters))
    if not missing:
        return None
    loaded = reserve_plan or current_budget or saved_payees or payment_draft or 'cashflow' in getattr(tools, '_continued_maintainer_skills', ()) or any(e.get('tool') == 'load_skill'
                 and e.get('parameters') == {'name': 'cashflow'}
                 and e.get('context_hash') == tools.start_context for e in tools.evidence.values())
    if not loaded:
        if any(t.get('tool') == 'load_skill' and t.get('status') != 'completed' for t in tools.trace):
            return None
        requests = [('load_skill', {'name': 'cashflow'})]
    else:
        requests = missing
    return {'contract': ('navren.required-payee-source-reads/1' if saved_payees or payment_draft else
                         'navren.required-planning-source-reads/1' if reserve_plan
                         else 'navren.required-statement-source-reads/1'),
            'context_hash': tools.start_context,
            'requests': [{'name': name, 'arguments': parameters} for name, parameters in requests],
            'scope': ('continued_payment_current_objects_read_only_no_implicit_preparation' if payment_draft else
                      'explicit_current_budget_account_sources_read_only' if current_budget else
                      'explicit_saved_payees_and_source_accounts_read_only' if saved_payees else
                      'explicit_personal_reserve_baseline_no_amount_or_link_inference'
                      if reserve_plan else 'explicit_personal_statement_sources_no_future_projection')}


def recorded_income_window_fact_request(text):
    """Whole-request historical income versus recorded future-inflow lookup.

    The number mentioned in the question is never a trusted balance or a
    condition supplied to the provider; only the actual source values render.
    """
    month = explicit_income_month(text)
    if month is None:
        return None
    value = unicodedata.normalize('NFKC', text or '').strip()
    value, count = re.subn(r'^(?:请)?(?:核对|查询|查看|查一下)', '', value)
    if count != 1:
        return None
    value, count = re.subn(r'(?<!\d)\d{4}[-/年](?:0?[1-9]|1[0-2])月?的?收入', '', value)
    if count != 1:
        return None
    periods = re.findall(r'(?:未来|今后|接下来)\s*([1-9]\d{0,2})\s*天', value)
    if len(periods) != 1 or not 1 <= int(periods[0]) <= 365:
        return None
    days = int(periods[0])
    value = re.sub(r'(?:未来|今后|接下来)\s*[1-9]\d{0,2}\s*天', '', value)
    value, count = re.subn(r'时间轴(?:里|中)(?:的)?已确认未来入账(?:为|是)'
                           r'[-+]?\d+(?:\.\d+)?(?:元)?', '', value)
    if count != 1:
        return None
    value, count = re.subn(r'(?:是不是|是否|能否)(?:说明|证明|表示)我(?:没|没有)收入'
                           r'[，,]?\s*(?:或者|或|还是)收入档案(?:缺失|未接入)', '', value)
    if count != 1 or re.sub(r'[\s，,。.!！?？；;：:、]|以及|和|及|与', '', value):
        return None
    return {'month': month, 'days': days}


def require_income_window_fact_bundle(query, name, final, tools):
    def fail():
        raise BankError('READ_RESULT_REQUIRED',
            '收入对照须引用同轮指定月模拟月报收入及指定未来窗口已确认入账的两项原事实；'
            '不得引用无关余额、支出、其他期间或把缺失补成零。')
    if (name != 'answer_facts' or set(query) != {'month', 'days'}
            or tools.advice or tools.changed or tools.drafts
            or any(x.get('status') != 'completed' or x.get('tool') not in
                   {'monthly_statement', 'cashflow'} for x in tools.trace)):
        fail()
    current = [e for e in tools.evidence.values() if e.get('context_hash') == tools.start_context]
    monthly = [e for e in current if e.get('tool') == 'monthly_statement'
               and e.get('parameters') == {'month': query['month']}]
    flows = [e for e in current if e.get('tool') == 'cashflow'
             and e.get('parameters') == {'days': query['days']}]
    if len(monthly) != 1 or len(flows) != 1:
        fail()
    report, flow = monthly[0], flows[0]
    m, c = report['data'], flow['data']
    income_rows, future_rows = m.get('totals_minor'), c.get('totals')
    income = income_rows.get('income') if isinstance(income_rows, dict) else None
    inflow = future_rows.get('confirmed_inflow_minor') if isinstance(future_rows, dict) else None
    currency = c.get('currency')
    try:
        from datetime import timedelta
        window_valid = (type(query['days']) is int and 1 <= query['days'] <= 365
            and c['horizon_days'] == query['days']
            and date.fromisoformat(c['horizon_end']) == date.fromisoformat(c['as_of']) + timedelta(days=query['days']))
    except (KeyError, TypeError, ValueError):
        window_valid = False
    if (m.get('source') != 'simulation' or m.get('month') != query['month']
            or c.get('source') != 'simulated_bank_archive' or not window_valid
            or type(income) is not int or type(inflow) is not int
            or not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency)):
        fail()
    # The old simulation report has no currency key. Its existing trusted metric
    # issuer must attest the actual currency and observed value, never a default.
    wanted = ((report, 'income', income, 'recorded_fact'),
              (flow, 'confirmed_inflow', inflow, 'recorded_projection'))
    records = [tools.metrics.lookup(mid) for mid in tools.metrics.metric_ids]
    for source, metric_name, value, kind in wanted:
        if not any(source['id'] in row['evidence_ids'] and row['kind'] == kind
                   and row['metric']['metric_name'] == metric_name
                   and row['metric']['currency'] == currency and type(row['metric']['value_minor']) is int
                   and row['metric']['value_minor'] == value
                   and ((metric_name == 'income'
                         and isinstance(row['metric']['period_start'], str)
                         and isinstance(row['metric']['period_end'], str)
                         and row['metric']['period_start'].startswith(query['month'] + '-')
                         and row['metric']['period_end'].startswith(query['month'] + '-'))
                        or (metric_name == 'confirmed_inflow'
                            and row['metric']['period_start'] == c['as_of']
                            and row['metric']['period_end'] == c['horizon_end'])) for row in records):
            fail()
    expected = {(report['id'], '/totals_minor/income'): income,
                (flow['id'], '/totals/confirmed_inflow_minor'): inflow}
    ids = final.get('fact_ids')
    if not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2:
        fail()
    actual = {}
    for fid in ids:
        fact = tools.facts.get(fid, {})
        key = (fact.get('evidence_id'), fact.get('source_path'))
        if (key not in expected or fact.get('unit') != 'minor_currency'
                or fact.get('currency') != currency or type(fact.get('value')) is not int
                or fact['value'] != expected[key]):
            fail()
        actual[key] = fact['value']
    if actual != expected:
        fail()


def income_window_eligible_fact_ids(query, tools):
    """Schema eligibility only: never submit or fill the model's final arguments."""
    if query is None:
        return None
    selected = []
    for tool, parameters, path in (
            ('monthly_statement', {'month': query['month']}, '/totals_minor/income'),
            ('cashflow', {'days': query['days']}, '/totals/confirmed_inflow_minor')):
        sources = [e for e in tools.evidence.values() if e.get('tool') == tool
                   and e.get('context_hash') == tools.start_context and e.get('parameters') == parameters]
        if len(sources) != 1:
            return None
        facts = [fid for fid, fact in tools.facts.items()
                 if fact.get('evidence_id') == sources[0]['id'] and fact.get('source_path') == path]
        if len(facts) != 1:
            return None
        selected.append(facts[0])
    # Reuse the exact existing source/metric/current-context contract before
    # narrowing choices. The real model must still submit both IDs itself.
    require_income_window_fact_bundle(query, 'answer_facts', {'fact_ids': selected}, tools)
    return selected


def current_funding_assessment_days(text):
    """Evidence requirement only; this never exempts assessment from review."""
    value = _explicit_read_request(text)
    if (value is None or re.search(r'公司|企业|国家|政府|行业|他人|别人|新闻|市场', value)
            or not re.search(r'当前(?:资金|现金|账户)', value)
            or not re.search(r'我|本人|本账户', value)
            or not re.search(r'已记录(?:的)?(?:事项|支出|收支)', value)
            or not re.search(r'应急|缓冲|压力|充分|薄|厚', value)
            or not re.search(r'能不能|能否|是否|判断|评估|分析', value)):
        return None
    periods = re.findall(r'(?:未来|今后|接下来)\s*([1-9]\d{0,2})\s*天', value)
    return int(periods[0]) if len(periods) == 1 and 1 <= int(periods[0]) <= 365 else None


def funding_assessment_view_days(text):
    """Whole-request grammar, narrower than the existing required-read detector.

    Unknown clauses keep the complete reviewed workflow. This grants neither
    facts-only completion nor a thin/thick/adequate conclusion.
    """
    value = _explicit_read_request(text)
    days = current_funding_assessment_days(text)
    if value is None or days is None:
        return None
    lead = (r'(?:请)?(?:只|仅)(?:基于|根据)(?:我的)?当前(?:资金|现金|账户)'
            r'(?:和|与|及)(?:未来|今后|接下来)\s*([1-9]\d{0,2})\s*天(?:的)?已记录(?:的)?(?:事项|支出|收支)')
    assessment = (r'(?:能不能|能否|是否)(?:说|判断|评估)(?:我的|我)?(?:应急)?缓冲'
                  r'(?:很薄|很厚|薄|厚|足够|不足|充分|不充分)[、，,]'
                  r'(?:实际)?压力(?:肯定|一定|必然)?比账面(?:更大|更小|大|小)')
    # An optional user statement of unconfirmed dimensions remains a statement
    # of uncertainty, never a new expense or a host-generated assumption.
    dimension = r'(?:必要生活支出|收入稳定性|目标优先级)'
    unknown = (r'(?:我(?:还)?(?:没有|未)确认' + dimension +
               r'(?:[、，,或和及与]+' + dimension + r')*)?')
    match = re.fullmatch(lead + r'[\s，,；;]*' + assessment + r'[\s?？。.!！]*' +
                         unknown + r'[\s。.!！?？]*', value)
    return days if match and int(match[1]) == days else None


def funding_assessment_metric_choices(name, data, records, evidence_id, days):
    """Select existing source-bound metrics, never calculate money or advice."""
    from datetime import timedelta
    def fail():
        raise BankError('READ_RESULT_REQUIRED', '资金判断选择视图须绑定当前预算及指定期间原最低余量指标。')
    wanted = []
    try:
        if not isinstance(data, dict) or not isinstance(evidence_id, str):
            fail()
        start = data['as_of']
        date.fromisoformat(start)
        if name == 'budget':
            if (data.get('budget_scope') != 'current_budget' or data.get('planning_reserves') != []
                    or type(data.get('additional_reserve_minor')) is not int or data['additional_reserve_minor'] != 0):
                fail()
            wanted.append(('current_budget', data['account_id'], 'arrangeable_now',
                           data['investable_minor'], data['currency'], 'recorded_fact',
                           start, (date.fromisoformat(start) + timedelta(days=30)).isoformat()))
        elif name == 'cashflow':
            end = data['horizon_end']
            if (data.get('source') != 'simulated_bank_archive' or type(days) is not int
                    or not 1 <= days <= 365 or data.get('horizon_days') != days
                    or date.fromisoformat(end) != date.fromisoformat(start) + timedelta(days=days)
                    or not isinstance(data.get('accounts'), list) or not data['accounts']):
                fail()
            accounts = data['accounts']
            if len({a['account_id'] for a in accounts}) != len(accounts):
                fail()
            for account in accounts:
                for role, field, flag, kind in (
                        ('confirmed_minimum_floor_surplus', 'minimum_available_after_confirmed_minor',
                         'confirmed_floor_breached', 'recorded_projection'),
                        ('candidate_minimum_floor_surplus', 'minimum_available_if_candidates_occur_minor',
                         'candidate_floor_breached', 'candidate_scenario')):
                    amount = account[field]
                    if type(amount) is not int or type(account.get(flag)) is not bool or account[flag] != (amount < 0):
                        fail()
                    wanted.append((role, account['account_id'], field.removesuffix('_minor'),
                                   amount, account['currency'], kind, start, end))
        else:
            fail()
        selected = []
        for role, subject, metric_name, amount, currency, kind, begin, end in wanted:
            if (not isinstance(subject, str) or not subject or type(amount) is not int
                    or not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency)):
                fail()
            rows = [r for r in records if isinstance(r, dict) and r.get('kind') == kind
                    and evidence_id in r.get('evidence_ids', []) and isinstance(r.get('metric'), dict)
                    and r['metric'].get('subject_id') == subject and r['metric'].get('metric_name') == metric_name
                    and r['metric'].get('currency') == currency and r['metric'].get('unit') == 'minor_currency'
                    and type(r['metric'].get('value_minor')) is int and r['metric']['value_minor'] == amount
                    and r['metric'].get('as_of') is None and r['metric'].get('period_start') == begin
                    and r['metric'].get('period_end') == end]
            if len(rows) != 1:
                fail()
            selected.append((role, rows[0]))
        return selected
    except (KeyError, TypeError, ValueError):
        fail()


def funding_assessment_eligible_metric_ids(tools):
    """Narrow the primary schema only after the original required reads pass."""
    days = funding_assessment_view_days(tools.user_text)
    if days is None or tools.changed or tools.drafts or any(
            t.get('status') != 'completed' or t.get('tool') not in
            {'load_skill', 'account_summary', 'budget', 'cashflow'} for t in tools.trace):
        return None
    try:
        require_explicit_source_completion('answer_reviewed', {}, tools)
    except BankError as error:
        if error.code == 'READ_COMPLETION_REQUIRED':
            return None
        raise
    evidence = [e for e in tools.evidence.values() if e.get('context_hash') == tools.start_context]
    summaries = [e for e in evidence if e.get('tool') == 'account_summary' and e.get('parameters') == {}]
    accounts = summaries[-1]['data']['accounts']
    explicit = [a['id'] for a in accounts if a['id'] in tools.user_text]
    named = [a['id'] for a in accounts if a.get('name') and a['name'] in tools.user_text]
    targets = set(explicit or named or [a['id'] for a in accounts])
    records = [tools.metrics.lookup(mid) for mid in tools.metrics.metric_ids]
    selected = []
    for account_id in sorted(targets):
        source = [e for e in evidence if e.get('tool') == 'budget'
                  and e.get('parameters', {}).get('account_id') == account_id
                  and e['parameters'].get('floor_minor', 0) == 0 and e['parameters'].get('reserves', []) == []
                  and e.get('data', {}).get('budget_scope') == 'current_budget']
        if not source:
            return None
        selected += funding_assessment_metric_choices('budget', source[-1]['data'], records, source[-1]['id'], days)
    flows = [e for e in evidence if e.get('tool') == 'cashflow' and e.get('parameters') == {'days': days}]
    selected += [(role, row) for role, row in funding_assessment_metric_choices(
        'cashflow', flows[-1]['data'], records, flows[-1]['id'], days)
        if row['metric']['subject_id'] in targets]
    return [row['metric_id'] for _, row in selected]


def required_monthly_income_read(tools, *, monthly_attempts=0):
    month = explicit_income_month(tools.user_text)
    if month is None:
        return None
    completed = [e for e in tools.evidence.values() if e.get('tool') == 'monthly_statement'
                 and e.get('context_hash') == tools.start_context
                 and e.get('parameters') == {'month': month} and e.get('data', {}).get('month') == month
                 and type(e.get('data', {}).get('totals_minor', {}).get('income')) is int]
    if completed:
        return None
    trace = [row for row in tools.trace if row.get('tool') == 'monthly_statement']
    if (type(monthly_attempts) is not int or monthly_attempts > len(trace)
            or any(row.get('status') != 'completed' for row in trace)):
        return None
    return {'contract': 'navren.required-monthly-income-read/1', 'month': month,
            'context_hash': tools.start_context, 'scope': 'explicit_month_income_no_future_income_inference'}



def allocation_base_read_obligation(tools):
    """Current personal allocation method prerequisites; no amount output rule.

    Loading a method is not itself permission to read a hypothetical person's
    accounts. Bind this obligation only to an actual personal/current-data flow
    in this turn. Source failure is distinct from a read that was never made.
    """
    if tools.private_context_disabled or tools._history_reference is not None:
        return None
    value = _explicit_read_request(tools.user_text)
    if (value is None or re.search(r'不是本人|非本人|他人|别人|朋友|对方|客户|'
            r'他(?:的|现在|目前)|她(?:的|现在|目前)', value)):
        return None
    evidence = [e for e in tools.evidence.values()
                if e.get('context_hash') == tools.start_context]
    if not any(e.get('tool') == 'load_skill' and e.get('data', {}).get('skill') == 'asset-allocation'
               for e in evidence):
        return None
    summaries = [e for e in evidence if e.get('tool') == 'account_summary'
                 and e.get('parameters') == {}]
    if not summaries and not re.search(r'我|本人|本账户', value):
        return None
    result = {'contract': 'navren.allocation-base-read-obligation/1',
              'context_hash': tools.start_context, 'complete': False,
              'account_summary_evidence_id': None, 'account_ids': [],
              'completed_account_ids': [], 'missing_account_ids': [],
              'missing': ['account_summary'], 'unavailable': False}
    if not summaries:
        result['unavailable'] = any(row.get('tool') == 'account_summary'
            and row.get('status') == 'rejected' and type(row.get('status_code')) is int
            and row['status_code'] >= 500 for row in tools.trace)
        return result
    summary = summaries[-1]
    accounts = summary.get('data', {}).get('accounts')
    if (not isinstance(accounts, list) or not accounts
            or any(not isinstance(a, dict) or not isinstance(a.get('id'), str) or not a['id']
                   or not isinstance(a.get('name'), str) or not a['name']
                   or not isinstance(a.get('currency'), str) for a in accounts)
            or len({a['id'] for a in accounts}) != len(accounts)):
        result['missing'] = ['valid_account_summary']
        return result
    result['account_summary_evidence_id'] = summary['id']
    explicit = [a for a in accounts if a['id'] in tools.user_text]
    named = _named_current_accounts(tools.user_text, accounts)
    if not explicit and any(sum(a['name'] == b['name'] for b in accounts) != 1 for a in named):
        result['missing'] = ['unambiguous_account']
        return result
    selected = explicit or named
    if not selected and re.search(r'主[账帐]户', value):
        selected = [a for a in accounts if a['id'].endswith('-main')]
        if len(selected) != 1:
            result['missing'] = ['unambiguous_account']
            return result
    selected = selected or accounts
    targets = [a['id'] for a in selected]
    currencies = {a['id']: a['currency'] for a in selected}
    completed = set()
    for e in evidence:
        if e.get('tool') != 'budget':
            continue
        data, parameters = e.get('data', {}), e.get('parameters', {})
        account_id = data.get('account_id')
        if (account_id in targets and account_id == parameters.get('account_id')
                and data.get('budget_scope') == 'current_budget'
                and data.get('planning_reserves') == []
                and data.get('currency') == currencies[account_id]
                and data.get('as_of') == summary['data'].get('as_of')
                and data.get('version') == summary['data'].get('version')
                and type(parameters.get('floor_minor', 0)) is int
                and parameters.get('floor_minor', 0) == 0
                and parameters.get('reserves', []) == []
                and type(data.get('investable_minor')) is int):
            completed.add(account_id)
    missing = [account_id for account_id in targets if account_id not in completed]
    failed = {row.get('account_id') for row in tools.trace
              if row.get('tool') == 'budget' and row.get('status') == 'rejected'
              and type(row.get('status_code')) is int and row['status_code'] >= 500}
    result.update(account_ids=targets, completed_account_ids=sorted(completed),
        missing_account_ids=missing, missing=['budget'] if missing else [],
        complete=not missing, unavailable=bool(missing) and set(missing) <= failed)
    return result


def explicit_liquidity_horizon_days(text):
    """Finite personal use-by constraint; no money, date guess or authority."""
    value = _explicit_read_request(text)
    if (value is None
            or not re.search(r'(?:我|本人|本账户)[^。；;\n]{0,24}(?:资金|现金|钱|余额)', value)
            or re.search(r'不是本人|非本人|他人|别人|朋友|对方|客户', value)
            or not re.search(r'配置|投资|理财|收益|产品|资金用途|资金安排', value)):
        return None
    digits = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9}
    def count(token):
        if token.isascii() and token.isdigit():
            return int(token)
        if token in digits:
            return digits[token]
        if token.count('十') == 1:
            left, right = token.split('十')
            if (not left or left in digits) and (not right or right in digits):
                return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
        return None
    horizons = set()
    pattern = (r'(?<![0-9一二两三四五六七八九十百千万])'
               r'(?P<number>[1-9][0-9]{0,2}|[一二两三四五六七八九十]{1,3})'
               r'(?:个)?(?P<unit>天|日|周|星期)(?:之后|后|之内|以内|内)'
               r'[ \t,，]{0,2}(?:(?:届时|全部|全额|都|必须|一定|确定|要|需要|将)){0,4}'
               r'(?:使用|用|取出|支付|花)')
    for clause in re.split(r'[。；;\n]', value):
        if re.search(r'可能|大概|也许|如果|假如|不确定|不需要|不必|不用|并非', clause):
            continue
        for match in re.finditer(pattern, clause):
            number = count(match['number'])
            if number is None:
                return None
            days = number * (7 if match['unit'] in ('周', '星期') else 1)
            if not 1 <= days <= 365:
                return None
            horizons.add(days)
    return next(iter(horizons)) if len(horizons) == 1 else None


def liquidity_product_read_obligation(tools):
    """Current simulated rule read only; it cannot certify cash availability."""
    if (tools.private_context_disabled or tools._history_reference is not None
            or tools.changed or tools.drafts):
        return None
    days = explicit_liquidity_horizon_days(tools.user_text)
    if days is None:
        return None
    current = [e for e in tools.evidence.values()
               if e.get('context_hash') == tools.start_context]
    if not any(e.get('tool') == 'load_skill'
               and e.get('data', {}).get('skill') in ('asset-allocation', 'product-research')
               for e in current):
        return None
    result = {'contract': 'navren.liquidity-product-read-obligation/1',
              'context_hash': tools.start_context, 'horizon_days': days,
              'complete': False, 'products_evidence_id': None,
              'scope': 'current_simulated_product_rules_not_public_sale_or_arrival_guarantee'}
    for entry in reversed(current):
        if (entry.get('tool') != 'products' or entry.get('parameters') != {}
                or entry.get('source') != 'simulation'
                or not isinstance(entry.get('id'), str)
                or tools.evidence.get(entry['id']) is not entry):
            continue
        rows = entry.get('data')
        if (not isinstance(rows, list)
                or any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
                       or not row['id'] or not isinstance(row.get('name'), str)
                       or not row['name'] for row in rows)
                or len({row['id'] for row in rows}) != len(rows)):
            return result
        # Missing rule fields remain visible source gaps for the full reviewer;
        # a successful catalogue read does not make unknown terms executable.
        result.update(complete=True, products_evidence_id=entry['id'])
        return result
    return result


def required_short_horizon_account_read(tools, *, summary_attempts=0):
    """Only fill the current-account prerequisite of an explicit all-use plan.

    Keep method selection, portfolio parameters and the complete review model
    path unchanged. This returns an ordinary read request, never an answer.
    """
    allocation = allocation_base_read_obligation(tools)
    liquidity = liquidity_product_read_obligation(tools)
    if (allocation is None or liquidity is None
            or allocation.get('missing') != ['account_summary']
            or allocation.get('unavailable') is not False
            or type(summary_attempts) is not int or summary_attempts != 0
            or any(row.get('tool') == 'account_summary' for row in tools.trace)):
        return None
    value = _explicit_read_request(tools.user_text)
    clauses = re.split(r'[。；;\n]', value or '')
    if not any(
            re.search(r'(?:我|本人|本账户)(?:当前|现在|目前)可安排的?(?:资金|现金|钱|余额)', clause)
            and re.search(r'(?:全部|全额|都)(?:要|需要|将|必须|一定|确定){0,3}(?:使用|用|取出|支付|花)', clause)
            and not re.search(r'可能|大概|也许|如果|假如|不确定|不需要|不必|不用|并非', clause)
            for clause in clauses):
        return None
    return {'contract': 'navren.short-horizon-current-account-prerequisite/1',
            'context_hash': tools.start_context,
            'horizon_days': liquidity['horizon_days'],
            'requests': [{'name': 'account_summary', 'arguments': {}}],
            'scope': 'current_allocation_baseline_existing_read_contract',
            'financial_side_effects': False,
            'ordinary_full_review_and_publication_required': True}


def required_liquidity_product_read(tools, *, product_attempts=0):
    """At most one ordinary products request before terminal budget admission."""
    obligation = liquidity_product_read_obligation(tools)
    if obligation is None or obligation['complete']:
        return None
    if (type(product_attempts) is not int or product_attempts != 0
            or any(row.get('tool') == 'products' for row in tools.trace)):
        return None
    return {**obligation, 'requests': [{'name': 'products', 'arguments': {}}]}


def require_explicit_source_completion(name, final, tools):
    """Actual current-turn evidence is mandatory even if a local plan did not run."""
    if (name not in ('answer_facts', 'answer_reviewed') or tools.private_context_disabled
            or tools._history_reference is not None):
        return
    liquidity = liquidity_product_read_obligation(tools)
    if liquidity is not None and not liquidity['complete']:
        raise BankError('READ_COMPLETION_REQUIRED',
            '明确本人资金用款期限的配置或产品用途规划须取得本轮products模拟规则原件；'
            '未读取、失败或旧上下文不能当作已核实，不能以稍后可查或类别流动性保证替代。'
            '不得据此新增购买、改写期限或绕过原预算；真实缺项仍须如实保留。')
    allocation = allocation_base_read_obligation(tools)
    if allocation is not None:
        if name != 'answer_reviewed':
            raise BankError('REVIEW_REQUIRED', '本人当前资料的资产配置判断仍须本轮独立审核。')
        if not allocation['complete']:
            source = ('account_summary' if allocation['missing'] == ['account_summary'] else 'budget')
            missing = set(allocation['missing_account_ids']) if source == 'budget' else None
            if (not allocation['unavailable'] or not _gap_answer(source, final, tools.trace, missing)):
                raise BankError('READ_COMPLETION_REQUIRED',
                    '本人当前资产配置须先完成本轮account_summary及所问账户的current baseline budget；'
                    '未指定单账户时逐一核对当前账户，不猜默认账户。可读取但尚未读取不是资料不可得，'
                    '不能仅以“可以再查询”结束；若真实服务不可用则明确该缺项及可作判断的范围。')
    missing_statement = [tool for tool, parameters in explicit_statement_read_sources(tools.user_text)
        if not any(e.get('tool') == tool and e.get('parameters') == parameters
                   and e.get('context_hash') == tools.start_context for e in tools.evidence.values())]
    if missing_statement:
        raise BankError('READ_COMPLETION_REQUIRED',
            '本轮明确要求的账单来源尚未读取：' + '、'.join(missing_statement) +
            '。未来现金流不能替代指定月份及年度覆盖来源。')
    comparison = getattr(tools, 'recorded_income_window_fact_query', None)
    if comparison is not None:
        require_income_window_fact_bundle(comparison, name, final, tools)
        return
    month = explicit_income_month(tools.user_text)
    if month is not None and not any(
            e.get('tool') == 'monthly_statement' and e.get('context_hash') == tools.start_context
            and e.get('parameters') == {'month': month} and e.get('data', {}).get('month') == month
            and type(e.get('data', {}).get('totals_minor', {}).get('income')) is int
            for e in tools.evidence.values()):
        raise BankError('READ_COMPLETION_REQUIRED',
            '用户明确要求核对的月份账单尚未读取：monthly_statement，month=' + month +
            '。其他期间的查询不能替代该月账单，也不能以本次未核实结束已可开展的查询。')
    days = current_funding_assessment_days(tools.user_text)
    if days is None:
        return
    if name != 'answer_reviewed':
        raise BankError('REVIEW_REQUIRED', '当前资金充分性判断仍须原独立审核，不能用事实终结工具免审。')
    summaries = [e for e in tools.evidence.values() if e.get('tool') == 'account_summary'
                 and e.get('context_hash') == tools.start_context and e.get('parameters') == {}]
    if not summaries:
        raise BankError('READ_COMPLETION_REQUIRED', '当前资金判断须先读取本轮account_summary。')
    accounts = summaries[-1]['data'].get('accounts', [])
    explicit_ids = [a.get('id') for a in accounts if isinstance(a.get('id'), str) and a['id'] in tools.user_text]
    named = [a for a in accounts if isinstance(a.get('name'), str) and a['name'] and a['name'] in tools.user_text]
    if not explicit_ids and any(sum(a.get('name') == b.get('name') for b in accounts) != 1 for a in named):
        raise BankError('READ_COMPLETION_REQUIRED', '所问账户名称不唯一，不能猜测账户。')
    targets = set(explicit_ids or [a['id'] for a in named] or [a['id'] for a in accounts])
    currencies = {a['id']: a['currency'] for a in accounts}
    completed = {e['data']['account_id'] for e in tools.evidence.values()
                 if e.get('tool') == 'budget' and e.get('context_hash') == tools.start_context
                 and e.get('data', {}).get('account_id') in targets
                 and e['data']['account_id'] == e.get('parameters', {}).get('account_id')
                 and e['data'].get('budget_scope') == 'current_budget'
                 and e['data'].get('planning_reserves') == []
                 and e['data'].get('currency') == currencies[e['data']['account_id']]
                 and type(e.get('parameters', {}).get('floor_minor', 0)) is int
                 and e['parameters'].get('floor_minor', 0) == 0 and e['parameters'].get('reserves', []) == []
                 and type(e['data'].get('investable_minor')) is int}
    if not targets or not targets <= completed:
        raise BankError('READ_COMPLETION_REQUIRED',
            '当前资金及已记录事项的充分性判断须读取本轮所问账户的current baseline budget；'
            '未指定单账户时按本轮账户结果分别查询，不猜主账户，不以余额或未消费资料替代。')
    if not any(e.get('tool') == 'cashflow' and e.get('context_hash') == tools.start_context
               and e.get('parameters') == {'days': days} and e.get('data', {}).get('horizon_days') == days
               for e in tools.evidence.values()):
        raise BankError('READ_COMPLETION_REQUIRED', '当前资金判断须读取用户指定期间的cashflow，days=' + str(days) + '。')


def required_budget_reads(tools, *, budget_attempts=0):
    """本轮明确账户的缺失基线读取；不选默认账户、不生成答复或规划。"""
    allocation = allocation_base_read_obligation(tools)
    assessment = current_funding_assessment_days(tools.user_text) is not None
    reserve_plan = (explicit_reserve_planning_read(tools.user_text)
        and not tools.private_context_disabled and tools._history_reference is None
        and not tools.changed and not tools.drafts)
    if cash_query_kind(tools.user_text) != 'budget' and not assessment and not reserve_plan and allocation is None:
        return None
    summaries = [e for e in tools.evidence.values() if e.get('tool') == 'account_summary'
                 and e.get('context_hash') == tools.start_context and e.get('parameters') == {}]
    if not summaries:
        return None
    summary = summaries[-1]
    accounts = summary.get('data', {}).get('accounts')
    if (not isinstance(accounts, list) or not accounts
            or any(not isinstance(a, dict) or not isinstance(a.get('id'), str)
                   or not a['id'] or not isinstance(a.get('name'), str) or not a['name'] for a in accounts)
            or len({a['id'] for a in accounts}) != len(accounts)):
        return None
    explicit_ids = [a['id'] for a in accounts if a['id'] in tools.user_text]
    if reserve_plan and re.search(r'主[账帐]户', tools.user_text):
        # The trusted account provider's primary ID suffix is also preserved
        # by CloudDataBoundary. Require one actual account; never default.
        primary = [a['id'] for a in accounts if a['id'].endswith('-main')]
        if len(primary) != 1:
            return None
        explicit_ids = list(dict.fromkeys(explicit_ids + primary + [
            a['id'] for a in accounts if a['name'] in tools.user_text]))
    if explicit_ids:
        targets = explicit_ids
    else:
        named = _named_current_accounts(tools.user_text, accounts)
        if not named and not assessment and allocation is None:
            value = unicodedata.normalize('NFKC', tools.user_text or '')
            if (not re.search(r'我|本人', value)
                    or re.search(r'朋友|他人|别人|对方|客户|他(?:的|现在|目前)|她(?:的|现在|目前)', value)):
                return None
        if any(sum(a['name'] == b['name'] for b in accounts) != 1 for a in named):
            return None
        # The existing final-read contract requires each current account when
        # a personal budget query names none. Read those same targets locally;
        # do not select a default account or ask the model to repeat the lookup.
        targets = [a['id'] for a in (named or accounts)]
    if allocation is not None:
        targets = allocation['account_ids']
        if not targets:
            return None
    budget_trace = [r for r in tools.trace if r.get('tool') == 'budget']
    # An invalid/failed prior attempt is not permission to silently try again.
    if type(budget_attempts) is not int or budget_attempts > len(budget_trace):
        return None
    if any(r.get('status') != 'completed' and
           (r.get('account_id') is None or r.get('account_id') in targets) for r in budget_trace):
        return None
    completed = set()
    for e in tools.evidence.values():
        if e.get('tool') != 'budget' or e.get('context_hash') != tools.start_context:
            continue
        data, args = e.get('data', {}), e.get('parameters', {})
        if (data.get('account_id') in targets and data.get('account_id') == args.get('account_id')
                and data.get('budget_scope') == 'current_budget'
                and type(args.get('floor_minor', 0)) is int and args.get('floor_minor', 0) == 0
                and args.get('reserves', []) == [] and type(data.get('investable_minor')) is int):
            completed.add(data['account_id'])
    if allocation is not None:
        completed = set(allocation['completed_account_ids'])
    missing = [account_id for account_id in targets if account_id not in completed]
    if not missing:
        return None
    return {'contract': 'navren.required-budget-read/1',
            'account_summary_evidence_id': summary['id'], 'account_ids': missing,
            'context_hash': tools.start_context, 'scope': 'current_budget_without_additional_reserves'}


def _gap_answer(name, final, trace, missing=None):
    # Invalid arguments/unloaded tools are repairable protocol errors, not proof
    # of unavailable data. A genuine service failure may end with an honest gap.
    failed = [item for item in trace if item.get('tool') == name and item.get('status') == 'rejected'
              and type(item.get('status_code')) is int and item['status_code'] >= 500]
    if missing is not None:
        failed = bool(missing) and missing <= {item.get('account_id') for item in failed}
    return (failed and not final.get('interaction')
            and bool(re.search(r'无法|未能|不可用|缺少|缺失|失败|未接入', final.get('text', ''))))


def recorded_cashflow_fact_request(text, accounts):
    """A whole-request grammar for existing-record budget/floor comparisons.

    Unknown clauses, custom amounts/conditions and advice never disappear into
    this scope. Account identity comes from the trusted current archive only.
    """
    value = unicodedata.normalize('NFKC', text or '').strip()
    if cash_query_kind(value) != 'budget' or not isinstance(accounts, list):
        return None
    matches = [a for a in accounts if isinstance(a, dict) and any(
        isinstance(a.get(k), str) and a[k] and a[k] in value for k in ('id', 'name'))]
    if len(matches) != 1:
        return None
    account = matches[0]
    if (not isinstance(account.get('id'), str) or not account['id']
            or not isinstance(account.get('currency'), str)
            or not re.fullmatch(r'[A-Z]{3}', account['currency'])):
        return None
    aliases = [re.escape(account[k]) for k in ('id', 'name')
               if isinstance(account.get(k), str) and account[k]]
    value, count = re.subn(r'(?:只看|仅看|查询|查看|看)?(?:' + '|'.join(aliases) + r')', '', value)
    if count != 1:
        return None
    periods = re.findall(r'(?:未来|今后|接下来)\s*([1-9]\d{0,2})\s*天', value)
    if len(periods) != 1 or not 1 <= int(periods[0]) <= 365:
        return None
    days = int(periods[0])
    value = re.sub(r'(?:未来|今后|接下来)\s*[1-9]\d{0,2}\s*天', '', value)
    value, count = re.subn(r'(?:当前|现在|目前)(?:可安排|可支配)(?:多少钱|多少资金|多少金额|资金有多少|金额有多少)', '', value)
    if count != 1:
        return None
    scenario = (r'已确认的?(?:支出|事项)(?:以及|和|及|与)'
                r'(?:续费|周期续费|代扣)?候选(?:事项)?(?:都|也|均)?发生(?:时|后)?'
                r'[，,]?\s*(?:会不会|是否会?|会否)(?:跌破|低于|破)'
                r'已设置的?(?:应急底线|应急保留底线)')
    value, count = re.subn(scenario, '', value)
    if count != 1:
        return None
    value = re.sub(r'(?:请)?(?:简短|简要)(?:说明|解释)', '', value)
    if re.sub(r'[\s，,。.!！?？；;、]|以及|和|及|与', '', value):
        return None
    return {'account_id': account['id'], 'currency': account['currency'], 'days': days}


def require_recorded_cashflow_facts(request, name, final, tools):
    """Require the complete current result bundle, never model-authored prose."""
    def fail():
        raise BankError('READ_RESULT_REQUIRED',
            '记录情景事实须选齐同一账户当前budget.investable_minor及所问天数cashflow的两项minimum_available事实；'
            '不能用余额、其他账户或其他期间代替，也不能混入自设留款、建议或操作。')
    if (name != 'answer_facts' or not isinstance(request, dict)
            or set(request) != {'account_id', 'currency', 'days'}
            or type(request['days']) is not int or not 1 <= request['days'] <= 365
            or tools.advice or tools.changed or tools.drafts
            or any(row.get('status') != 'completed' or row.get('tool') not in
                   {'account_summary', 'budget', 'cashflow'} for row in tools.trace)):
        fail()
    current = [e for e in tools.evidence.values()
               if e.get('context_hash') == tools.start_context]
    summaries = [e for e in current if e.get('tool') == 'account_summary' and e.get('parameters') == {}]
    if len(summaries) != 1:
        fail()
    summary = summaries[0]['data']
    if type(summary.get('version')) is not int or not isinstance(summary.get('as_of'), str):
        fail()
    accounts = [a for a in summary.get('accounts', []) if a.get('id') == request['account_id']
                and a.get('currency') == request['currency']]
    if len(accounts) != 1:
        fail()
    budgets = [e for e in current if e.get('tool') == 'budget'
               and e.get('parameters', {}).get('account_id') == request['account_id']]
    flows = [e for e in current if e.get('tool') == 'cashflow']
    if len(budgets) != 1 or len(flows) != 1:
        fail()
    budget, flow = budgets[0], flows[0]
    b, c = budget['data'], flow['data']
    params = budget['parameters']
    if (params.get('floor_minor', 0) != 0 or params.get('reserves', []) != []
            or b.get('budget_scope') != 'current_budget' or b.get('planning_reserves') != []
            or b.get('account_id') != request['account_id'] or b.get('currency') != request['currency']
            or type(b.get('investable_minor')) is not int
            or flow.get('parameters') != {'days': request['days']}
            or c.get('horizon_days') != request['days']
            or any(d.get('as_of') != summary.get('as_of') or d.get('version') != summary.get('version')
                   for d in (b, c))):
        fail()
    try:
        start = date.fromisoformat(c['as_of'])
        from datetime import timedelta
        if date.fromisoformat(c['horizon_end']) != start + timedelta(days=request['days']):
            fail()
    except (KeyError, TypeError, ValueError):
        fail()
    matches = [(i, a) for i, a in enumerate(c.get('accounts', []))
               if a.get('account_id') == request['account_id'] and a.get('currency') == request['currency']]
    if len(matches) != 1:
        fail()
    index, account = matches[0]
    fields = (('minimum_available_after_confirmed_minor', 'confirmed_floor_breached'),
              ('minimum_available_if_candidates_occur_minor', 'candidate_floor_breached'))
    if any(type(account.get(field)) is not int or type(account.get(flag)) is not bool
           or account[flag] != (account[field] < 0) for field, flag in fields):
        fail()
    expected = {(budget['id'], '/investable_minor'): b['investable_minor']}
    expected.update({(flow['id'], '/accounts/' + str(index) + '/' + field): account[field]
                     for field, _ in fields})
    selected = final.get('fact_ids')
    if not isinstance(selected, list) or len(selected) != 3 or len(set(selected)) != 3:
        fail()
    actual = {}
    for fid in selected:
        fact = tools.facts.get(fid, {})
        key = (fact.get('evidence_id'), fact.get('source_path'))
        if (key not in expected or fact.get('currency') != request['currency']
                or fact.get('unit') != 'minor_currency' or type(fact.get('value')) is not int
                or fact['value'] != expected[key]):
            fail()
        actual[key] = fact['value']
    if actual != expected:
        fail()


def _require_statement_budget_comparison(kind, name, final, tools, targets, budgets):
    """Require separately bound results when a statement is compared to cash.

    Only current issuer records establish coverage. This chooses no metric for
    the model and never copies amounts from history or computes a new amount.
    """
    if (kind!='budget' or name!='answer_reviewed'
            or not explicit_current_budget_read(tools.user_text)
            or not re.search(r'账单|结余',tools.user_text)):
        return
    requests=explicit_statement_read_sources(tools.user_text,
        getattr(tools,'_statement_request_history',()))
    months={args['month']for tool,args in requests if tool=='monthly_statement'}
    if len(months)!=1:return
    month=next(iter(months))
    reports=[row for row in tools.evidence.values()if row.get('tool')=='monthly_statement'
        and row.get('context_hash')==tools.start_context and row.get('parameters')=={'month':month}]
    if len(reports)!=1:
        raise BankError('READ_COMPLETION_REQUIRED','本轮比较尚缺所问月份的当前账单来源；不能复用旧助手金额。')
    report=reports[0]
    required={('statement',month,a['currency'])for a in targets}
    required.update((role,a['id'],a['currency'])for a in targets for role in ('cash','budget'))
    covered=set()
    for claim in final.get('amount_claims',()):
        mid=claim.get('metric_id')
        if (mid not in tools.metrics.metric_ids or not claim.get('text')
                or claim['text']not in final.get('text','')):continue
        record=tools.metrics.lookup(mid);metric=record['metric']
        if record['kind']!='recorded_fact' or type(metric['value_minor'])is not int:continue
        evidence=set(record['evidence_ids']);currency=metric['currency']
        if (metric['metric_name']=='income_expense_surplus'and report['id']in evidence
                and isinstance(metric['period_start'],str)and isinstance(metric['period_end'],str)
                and metric['period_start'].startswith(month+'-')and metric['period_end'].startswith(month+'-')):
            covered.add(('statement',month,currency))
        for account in targets:
            if (metric['subject_id']!=account['id']or currency!=account['currency']):continue
            budget=budgets[account['id']]
            if metric['metric_name']=='arrangeable_now'and budget['id']in evidence:
                covered.add(('budget',account['id'],currency))
            if ((metric['metric_name']=='cash'and budget['id']in evidence)
                    or (metric['metric_name']=='settled_cash_balance'and any(
                        tools.evidence.get(eid,{}).get('tool')=='account_summary'
                        and tools.evidence[eid].get('context_hash')==tools.start_context for eid in evidence))):
                covered.add(('cash',account['id'],currency))
    if not required<=covered:
        raise ReadResultReferenceIncomplete(
            '比较账单与当前额度时须分别引用本轮账单结余、对应账户现金余额及可安排金额。',
            kind='statement_budget_comparison',required_count=len(required),
            covered_count=len(required&covered))


def _budget_advice_without_result_query(text):
    """A finite advice-only request; unknown or explicit queries keep coverage.

    This changes no read, amount, permission or review requirement. Cash context
    is declarative; a request to look up/calculate cash is never advice-only.
    """
    value = unicodedata.normalize('NFKC', text or '').strip()
    if re.search(r'查|核对|计算|算一?下|多少|几(?:元|块|笔)|余额|结余|剩余|'
                 r'告诉|列出|报出|显示|读出|读一下|读取|数额|具体金额|可用额|'
                 r'是否|能否|可否|够不够|够用|怎样|怎么样|什么|哪|吗|呢|[?？]|'
                 r'\b(?:query|read|check|calculate|balance|how much)\b', value, re.I):
        return False
    number = r'[0-9一二三四五六七八九十百千万两]+'
    period = (r'(?:' + number + r'(?:个)?(?:天|周|星期|月|年)|'
              r'明天|后天|月底|年底|下周|下个月|'
              r'\d{4}[-/]\d{1,2}[-/]\d{1,2})')
    context = (r'(?:我|本人)的?(?:当前|目前|现在)?可(?:以)?(?:安排|支配|用|投资)的?'
               r'(?:钱|资金|金额)(?:(?:在)?' + period + r'(?:后|内|前)?'
               r'(?:都|全|全部)?(?:要|需要|将)?(?:用|使用|用掉|支出))?')
    objective = (r'(?:为了|为|以)(?:收益|回报|利息|风险|流动性|安全性|安全)'
                 r'(?:更|较)?(?:高|低|好|优|最大|最小|稳健)?')
    request = (r'(?:' + objective + r')?(?:你)?(?:请)?(?:直接)?'
               r'(?:给(?:我|出)?|提供|制定|设计|帮我(?:做|制定)|需要|想要|希望得到)'
               r'(?:一?(?:个|份)|些|点)?(?:稳健|合理|可行|最优|具体|投资|资金|资产|短期|长期|保守|的)*'
               r'(?:建议|规划|配置|方案)(?:建议|规划|配置|方案|组合|比例|及|和|与|预期收益率?|预计收益率?|期望收益率?|预期回报率?|风险|说明)*')
    qualifier = (r'(?:(?:这|本)(?:一?轮|次))?(?:我)?(?:只|仅)(?:问|要|求|讨论)'
                 r'(?:方案|建议|规划)(?:不(?:购买|买入|操作|交易|执行))?')
    no_action = r'(?:不|不要|暂不|先不|无需)(?:购买|买入|操作|交易|执行)'
    uptake = r'(?:我|本人)(?:先|会|想|打算|准备)?(?:照着|按照(?:方案|建议)?)(?:做|执行|安排)'
    clauses = [part.strip() for part in re.split(r'[，,；;。.!！\n]+', value) if part.strip()]
    return bool(clauses and any(re.fullmatch(request, part) for part in clauses)
                and all(re.fullmatch(context, part) or re.fullmatch(request, part)
                        or re.fullmatch(qualifier, part) or re.fullmatch(objective, part)
                        or re.fullmatch(no_action, part) or re.fullmatch(uptake, part)
                        for part in clauses))


def require_cash_query_complete(kind, name, final, tools):
    request = getattr(tools, 'recorded_cashflow_fact_query', None)
    if request is not None:
        require_recorded_cashflow_facts(request, name, final, tools)
        return
    if kind is None:
        return
    summaries = [item for item in tools.evidence.values() if item['tool'] == 'account_summary']
    if not summaries:
        if name == 'answer_reviewed' and _gap_answer('account_summary', final, tools.trace):
            return
        raise BankError('READ_COMPLETION_REQUIRED',
                        '本轮账户查询尚未完成。直接调用account_summary；用户已要求只读查询，不再征求查询许可。')
    accounts = summaries[-1]['data']['accounts']
    if not accounts:
        if name == 'answer_reviewed' and not final.get('interaction') and re.search(
                r'没有|未接入|暂无|未找到|不可用', final.get('text', '')):
            return
        raise BankError('READ_COMPLETION_REQUIRED', '本轮没有可查询账户，请明确说明资料缺口，不能将缺失说成零。')
    explicit = _named_current_accounts(tools.user_text, accounts)
    targets = explicit or accounts
    target_ids = {a['id'] for a in targets}
    if kind == 'budget':
        budgets = {item['data']['account_id']: item for item in tools.evidence.values()
                   if item['tool'] == 'budget' and item['data'].get('budget_scope') == 'current_budget'
                   and not item['parameters'].get('floor_minor')
                   and item['data'].get('account_id') in target_ids
                   and type(item['data'].get('investable_minor')) is int}
        missing = target_ids - budgets.keys()
        if missing:
            if name == 'answer_reviewed' and _gap_answer('budget', final, tools.trace, missing):
                return
            raise BankError('READ_COMPLETION_REQUIRED',
                            '可投资查询尚未完成。对本轮账户结果中所查询的账户调用不带额外留款的budget，'
                            '各币种分列；不能拿余额代替预算，不能只回答“我来查”。')
        # A user-requested planning reserve may follow the same-account baseline;
        # the answer must then bind its scenario metric, never pretend it is cash.
        planning = bool(re.search(r'额外|另(?:外)?留|保留|预留|留出|留(?:下|款)|reserve|set aside', tools.user_text))
        if planning:
            for item in tools.evidence.values():
                if (item['tool'] == 'budget' and item['data'].get('account_id') in target_ids
                        and (item['parameters'].get('reserves') or item['parameters'].get('floor_minor'))):
                    budgets[item['data']['account_id']] = item
        required_evidence = {item['id'] for item in budgets.values()}
        field = 'investable_minor'
        metric_names = {'arrangeable_now', 'planning_arrangeable_now'} if planning else {'arrangeable_now'}
        if not planning:
            _require_statement_budget_comparison(kind,name,final,tools,targets,budgets)
    else:
        required_evidence = {summaries[-1]['id']}
        field, metric_names = 'balance_minor', {'settled_cash_balance'}
    # Advice still requires the original current reads above and the full
    # downstream amount/source/permission/GLM contract; only cash-result
    # enumeration is inapplicable to this finite advice-only request.
    if (kind == 'budget' and name == 'answer_reviewed'
            and _budget_advice_without_result_query(tools.user_text)):
        return
    if name == 'answer_facts':
        selected = [tools.facts[i] for i in final['fact_ids'] if i in tools.facts]
        covered, subjects = set(), set()
        for fact in selected:
            if fact.get('field') != field or fact['evidence_id'] not in required_evidence:
                continue
            if kind == 'budget':
                covered.add(fact['evidence_id'])
                continue
            path = fact.get('source_path', '')
            match = re.fullmatch(r'/accounts/(\d+)/balance_minor', path)
            if match and int(match[1]) < len(accounts):
                subjects.add(accounts[int(match[1])]['id'])
            match = re.fullmatch(r'/cash_by_currency/(\d+)/balance_minor', path)
            rows = summaries[-1]['data'].get('cash_by_currency', [])
            if not explicit and match and int(match[1]) < len(rows):
                subjects.update(a['id'] for a in targets if a['currency'] == rows[int(match[1])]['currency'])
        if (kind == 'budget' and required_evidence <= covered) or (kind == 'balance' and target_ids <= subjects):
            return
    else:
        covered, subjects = set(), set()
        for claim in final.get('amount_claims', []):
            mid = claim.get('metric_id')
            if mid not in tools.metrics.metric_ids:
                continue
            record = tools.metrics.lookup(mid)
            metric = record['metric']
            matches = (metric['metric_name'] in metric_names and any(
                a['id'] == metric['subject_id'] and a['currency'] == metric['currency'] for a in targets))
            if kind == 'balance' and not explicit and metric['metric_name'] == 'settled_cash_total':
                matches = metric['subject_id'] == tools.w
            if (matches
                    and type(metric['value_minor']) is int
                    and record['kind'] in ({'recorded_fact', 'planning_scenario'} if kind == 'budget' and planning else {'recorded_fact'})
                    and claim.get('text') and claim['text'] in final.get('text', '')):
                covered.update(record['evidence_ids'])
                if kind == 'balance' and required_evidence.intersection(record['evidence_ids']):
                    subjects.update(a['id'] for a in targets if
                        a['id'] == metric['subject_id'] or (not explicit and metric['metric_name'] == 'settled_cash_total'
                                                         and a['currency'] == metric['currency']))
        if (kind == 'budget' and required_evidence <= covered) or (kind == 'balance' and target_ids <= subjects):
            return
    message = ('最终回答未引用本轮所查结果。纯查询用answer_facts选择余额或investable_minor的事实；'
               '解释用answer_reviewed并在正文与amount_claims引用对应余额或arrangeable_now正式指标。'
               '不能只承诺稍后查询，也不再要求用户确认只读查询。')
    if name == 'answer_reviewed':
        required = required_evidence if kind == 'budget' else target_ids
        completed = covered if kind == 'budget' else subjects
        raise ReadResultReferenceIncomplete(message, kind=kind,
            required_count=len(required), covered_count=len(required.intersection(completed)))
    raise BankError('READ_RESULT_REQUIRED', message)


def _holding_horizon_facts(summary, user_text):
    """Pure calendar-day arithmetic over one summary; never a cash conclusion."""
    from calendar_events import day
    from datetime import timedelta
    result = {'as_of': None, 'profile_days': None, 'profile_end': None,
              'use_days': explicit_liquidity_horizon_days(user_text) if isinstance(user_text, str) else None,
              'lock_days': {}, 'invalid_lock_date': False}
    if not isinstance(summary, dict):
        return result
    try:
        result['as_of'] = day(summary.get('as_of'))
    except BankError:
        return result
    profile = summary.get('profile')
    horizon = profile.get('horizon_days') if isinstance(profile, dict) else None
    if type(horizon) is int and 0 <= horizon <= 3650:
        try:
            result['profile_end'] = result['as_of'] + timedelta(days=horizon)
            result['profile_days'] = horizon
        except OverflowError:
            pass
    holdings = summary.get('holdings')
    if type(holdings) is not list:
        result['invalid_lock_date'] = True
        return result
    for holding in holdings:
        try:
            locked = day(holding.get('locked_until') if isinstance(holding, dict) else None)
        except BankError:
            result['invalid_lock_date'] = True
            continue
        result['lock_days'][locked.isoformat()] = max(0, (locked - result['as_of']).days)
    return result


def holding_horizon_basis_text(summary, user_text):
    """Short deterministic explanation appended to existing scope prose only."""
    facts = _holding_horizon_facts(summary, user_text)
    if facts['as_of'] is None:
        return '\n持仓期限比较未知：本轮业务日期缺失或无效，不能推断剩余锁定天数。'
    pieces = ['\n期限比较以本轮业务日期' + facts['as_of'].isoformat() + '为起点。']
    if facts['profile_days'] is None:
        pieces.append('正式画像投资期限缺失或无效，不能判断与其是否冲突。')
    else:
        pieces.append('正式画像' + str(facts['profile_days']) + '天截止' + facts['profile_end'].isoformat() + '。')
    for locked, remaining in sorted(facts['lock_days'].items()):
        sentence = '锁定截止' + locked + '：剩余锁定' + str(remaining) + '天'
        if facts['profile_days'] is not None:
            sentence += '，' + ('超过' if remaining > facts['profile_days'] else '未超过') + '正式画像期限'
        if facts['use_days'] is not None:
            sentence += '，' + ('超过' if remaining > facts['use_days'] else '未超过') + '本轮明确用款期限' + str(facts['use_days']) + '天'
        pieces.append(sentence + '。')
    if facts['invalid_lock_date']:
        pieces.append('部分持仓锁定日期缺失或无效，对这些持仓不能推断期限关系。')
    pieces.append('上述比较仅说明锁定截止与期限；不证明解锁即赎回或到账，也不证明适当性或资金足额。')
    return ''.join(pieces)


def validate_holding_horizon_wording(final, tools):
    """Hard-check one finite lock-date/horizon relation without new bank reads.

    Normal OP and later metric/publication checks retain their original scope.
    Month-only references require one complete matching lock date in this
    turn's trusted summary; no host date, model horizon or guessed year is used.
    """
    from calendar_events import day
    from .operation_claims import OperationWordingError
    if not isinstance(final, dict):
        return
    fields = []
    text = final.get('text', final.get('reply', ''))
    if isinstance(text, str):
        fields.append(('text', text))
    for index, claim in enumerate(final.get('claims') or ()):
        if isinstance(claim, dict) and isinstance(claim.get('text'), str):
            fields.append(('claims[' + str(index) + '].text', claim['text']))
    for index, followup in enumerate(final.get('followups') or ()):
        if isinstance(followup, str):
            fields.append(('followups[' + str(index) + ']', followup))
    relation = re.compile(
        r'(?:^|[。；;！!\n])\s*在\s*(?P<days>[0-9]{1,4})\s*天(?:的)?\s*'
        r'(?P<basis>最长投资期限|正式画像期限|明确用款期限|用款期限)\s*(?:下|内)\s*[，,]\s*'
        r'锁定(?:到|至)\s*(?P<date>[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}|[0-9]{1,2}\s*月)\s*'
        r'(?:的)?(?:那笔|这笔|该笔|持仓)与(?:该|上述)?(?:期限|用款)(?:约束)?'
        r'(?P<verdict>不存在冲突|没有冲突|不冲突|无冲突|存在冲突|有冲突|冲突)(?=$|[，,。；;！!\n])')
    found = [(location, match) for location, value in fields
             for match in relation.finditer(unicodedata.normalize('NFKC', value))]
    if not found:
        return
    evidence, trace = getattr(tools, 'evidence', None), getattr(tools, 'trace', None)
    context = getattr(tools, 'start_context', None)
    summaries = []
    if type(evidence) is dict and type(trace) is list and isinstance(context, str) and context:
        for evidence_id, item in evidence.items():
            if (isinstance(item, dict) and item.get('id') == evidence_id and item.get('tool') == 'account_summary'
                    and item.get('source') == 'simulation' and item.get('context_hash') == context
                    and item.get('parameters') == {} and isinstance(item.get('data'), dict)
                    and any(isinstance(row, dict) and row.get('tool') == 'account_summary'
                        and row.get('status') == 'completed' and row.get('evidence_id') == evidence_id for row in trace)):
                summaries.append(item['data'])
    unique = summaries and all(all(item.get(key) == summaries[-1].get(key)
        for key in ('as_of', 'profile', 'holdings')) for item in summaries)
    facts = _holding_horizon_facts(summaries[-1], getattr(tools, 'user_text', '')) if unique else None
    def reason_for(match):
        if facts is None or facts['as_of'] is None or facts['invalid_lock_date']:
            return 'current_summary_dates_missing_invalid_or_ambiguous'
        expected = facts['profile_days'] if match['basis'] in ('最长投资期限', '正式画像期限') else facts['use_days']
        if expected is None or int(match['days']) != expected:
            return 'horizon_not_bound_to_current_profile_or_explicit_use_by'
        target = match['date'].replace(' ', '')
        if target.endswith('月'):
            month = int(target[:-1])
            candidates = [value for value in facts['lock_days'] if int(value[5:7]) == month]
            if not 1 <= month <= 12 or len(candidates) != 1:
                return 'month_reference_does_not_resolve_unique_current_lock_date'
            target = candidates[0]
        else:
            try:
                target = day(target).isoformat()
            except BankError:
                return 'claimed_lock_date_invalid'
            if target not in facts['lock_days']:
                return 'claimed_lock_date_not_in_current_holdings'
        conflict = facts['lock_days'][target] > expected
        claimed = not match['verdict'].startswith(('不', '没', '无'))
        return 'lock_days_and_bound_horizon_relation_mismatch' if claimed != conflict else None
    failures = [(location, match, reason) for location, match in found
                if (reason := reason_for(match)) is not None]
    if failures:
        locations = tuple(dict.fromkeys(location for location, _, _ in failures))
        error = OperationWordingError('OPERATION_STATUS_EVIDENCE_REQUIRED',
            '持仓锁定与期限比较未通过本轮原件日期复算；只修正该比较，不改写画像、不虚构到账或资金状态。',
            422, location=locations[0], locations=locations,
            assertion_details=tuple((location, match.group(0).lstrip('。；;！!\n').strip(), ('unsupported',))
                                    for location, match, _ in failures))
        error.operation_wording_instruction = '按本轮业务日期、原持仓锁定截止及正式画像或明确用款期限复算，只修正错误的期限关系。'
        error.holding_horizon_reasons = tuple(reason for _, _, reason in failures)
        raise error
