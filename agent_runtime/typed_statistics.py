"""本轮正式账户统计原子；不推断、改写或证明银行操作状态。"""
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import hashlib
import json
import re
import secrets
import unicodedata

from simulated_bank import BankError
from .metric_claims import MetricClaimError, _metric_snapshot, monetary_occurrences


_ROLES = {
    'holding_principal': ('holding_book_principal_not_market_value',
        '截至{date}，账户持仓账面本金统计为{amount}（不是市值或已结算现金）。'),
    'pending_redemption': ('pending_redemption_not_settled_cash',
        '截至{date}，账户待结算赎回金额统计为{amount}（不是已到账现金）。'),
}

# Source statements use the same explicit selector and span proof, never a
# fabricated financial metric. The statement kind is host-owned, not model text.
_SOURCE_KINDS = frozenset(('simulation_product_terms', 'current_budget_cash_scope', 'research_scope'))
_PRODUCT_FIELDS = {'term_days': '锁定期限', 'redemption_delay_days': '赎回到账目录时延'}
_BUDGET_CASH = '当前预算的cash只取所选账户已结算现金,可安排资金再扣应急底线、生效预留及未来30天已知到期款'
_BUDGET_EXCLUSION = '持仓本金及未到账赎回款未加进cash或可安排资金'
_BUDGET_UNLOCK = '解除持仓锁定仍不等于已结算或到账'
_RESEARCH_TEXT = '若继续研究候选方案,应核对期限、赎回与到账条件及费用;这只是后续研究方法,不说明研究已完成或任何交易已执行。'
_SOURCE_CONTRACT = 'navren.source-statements/1'


def _explicit_financial_product_research(user_text):
    """Only expose product-specific guidance when the current request names it."""
    if type(user_text) is not str:
        return False
    value=unicodedata.normalize('NFKC',user_text)
    product=r'基金|债券|证券|股票|金融产品|投资产品|银行理财|理财产品|定期存款|结构性存款|赎回|申购'
    inquiry=r'选择|挑选|选购|怎么选|如何选|选(?:一[只个款])?(?:基金|债券|股票|理财产品)|购买|买入|持有|配置|投资|理财|赎回|申购|期限|费率|费用|到账|到期|流动性|条款|比较'
    if re.search(r'学习|培训|课程|考试|教学',value) and not re.search(r'赎回|申购|到账|到期|条款|费用|费率|流动性|购买|买入|持有',value):
        return False
    return bool(re.search(product,value) and re.search(inquiry,value))

def _label(value):
    if type(value) is not str or not 1 <= len(value) <= 160 or any(ord(c) < 32 for c in value):
        _invalid()
    return json.dumps(unicodedata.normalize('NFKC', value), ensure_ascii=False)


def _source_statement(item):
    kind = item['statement_kind']
    if kind == 'simulation_product_terms':
        row, field = item['product'], item['source_field']
        if field not in _PRODUCT_FIELDS or type(row.get(field)) is not int or not 0 <= row[field] <= 100000:
            _invalid()
        return ('模拟目录产品' + _label(row['name']) + '的' + _PRODUCT_FIELDS[field]
                + '为' + str(row[field]) + '天;这是目录条件,不是任何一笔交易已到账的回执。')
    if kind == 'current_budget_cash_scope':
        return ('当前预算账户' + _label(item['account_name'])
                + '的口径只取已结算现金,再扣应急底线、生效预留和未来30天已知到期款;'
                  '持仓本金和未到账赎回不计入现金或可安排资金;解除锁定不证明赎回成功、已结算或到账。')
    if kind == 'research_scope':
        return _RESEARCH_TEXT
    _invalid()


def _invalid():
    raise BankError('STATISTIC_BINDING_INVALID', '本轮统计原子引用、原文位置或正式来源不一致。', 422)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        _invalid()


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _field(final, location):
    if type(final) is not dict or type(location) is not str:
        _invalid()
    if location == 'text':
        value = final.get('text')
    else:
        match = re.fullmatch(r'claims\[(0|[1-9][0-9]*)\]\.text', location)
        if match is None or type(final.get('claims')) is not list:
            _invalid()
        index = int(match.group(1))
        if index >= len(final['claims']) or type(final['claims'][index]) is not dict:
            _invalid()
        value = final['claims'][index].get('text')
    if type(value) is not str:
        _invalid()
    return value


def _statement(metric):
    # Same fixed minor-unit rendering as the original metric-parts renderer.
    value = metric['value_minor']
    major, fraction = divmod(value, 100)
    amount = f'{major:,}.{fraction:02d}' + ('元' if metric['currency'] == 'CNY' else ' ' + metric['currency'])
    text = unicodedata.normalize('NFKC', _ROLES[metric['metric_name']][1].format(
        date=metric['as_of'], amount=amount))
    occurrences = monetary_occurrences(text)
    if (len(occurrences) != 1
            or (occurrences[0]['value_minor'], occurrences[0]['currency'])
                != (value, metric['currency'])):
        _invalid()
    return text


class TypedStatisticSession:
    """Host-only issuer tied to one BankTools object and its formal registry."""

    def __init__(self, tools):
        # Delayed import avoids a BankTools -> issuer -> BankTools import cycle.
        from .bank_tools import BankTools
        if not isinstance(tools, BankTools):
            _invalid()
        self._tools = tools
        self._gateway = tools.g
        self._workspace = tools.w
        self._request = tools.request_id
        self._context = tools.start_context
        self._service = getattr(tools.g, 'financial_metrics', None)
        self._nonce = secrets.token_hex(24)
        self._items = {}
        self._proof = None

    @property
    def ids(self):
        return list(self._items)

    def _owner(self):
        tools = self._tools
        if (getattr(tools, '_statistic_session', None) is not self
                or tools.g is not self._gateway or tools.w != self._workspace
                or tools.request_id != self._request or tools.start_context != self._context
                or getattr(tools.g, 'financial_metrics', None) is not self._service
                or getattr(tools, 'private_context_disabled', False)
                or not callable(getattr(self._service, 'validate', None))):
            _invalid()

    def _evidence(self, evidence_id):
        row = self._tools.evidence.get(evidence_id)
        if (type(row) is not dict or row.get('id') != evidence_id
                or row.get('tool') != 'account_summary' or row.get('source') != 'simulation'
                or row.get('context_hash') != self._context):
            _invalid()
        return row

    def _replace_slot(self, slot, pending):
        retained = {sid: item for sid, item in self._items.items() if item.get('_slot', ('account_summary',)) != slot}
        for item in pending.values():
            item['_slot'] = slot
        self._items = {**retained, **pending}
        self._proof = None

    def _source_evidence(self, item):
        evidence = self._tools.evidence.get(item['evidence_id'])
        expected_tool = {'simulation_product_terms': 'products', 'current_budget_cash_scope': 'budget', 'research_scope': 'load_skill'}[item['statement_kind']]
        expected_source = 'maintainer_skill' if expected_tool == 'load_skill' else 'simulation'
        if (type(evidence) is not dict or evidence.get('id') != item['evidence_id']
                or evidence.get('tool') != expected_tool or evidence.get('source') != expected_source
                or evidence.get('context_hash') != self._context or _digest(evidence) != item['evidence_digest']
                or not any(row.get('tool') == expected_tool and row.get('evidence_id') == item['evidence_id']
                           and row.get('status') == 'completed' for row in self._tools.trace)):
            _invalid()
        return evidence

    def register_source(self, name, evidence_id):
        """Consume existing completed products/skill originals, without a read."""
        if name not in ('products', 'load_skill'):
            return
        self._owner()
        evidence = self._tools.evidence.get(evidence_id)
        if (type(evidence) is not dict or evidence.get('id') != evidence_id
                or evidence.get('tool') != name or evidence.get('context_hash') != self._context):
            _invalid()
        if name == 'load_skill':
            if evidence.get('source') != 'maintainer_skill':
                _invalid()
            params, data = evidence.get('parameters'), evidence.get('data')
            if (type(params) is not dict or type(data) is not dict
                    or params.get('name') not in ('asset-allocation', 'goal-planning')
                    or data.get('skill') != params['name'] or data.get('reference') != params.get('reference')):
                return
            if params['name'] == 'goal-planning' and not _explicit_financial_product_research(getattr(self._tools,'user_text',None)):
                return
            # Repeated loading must not expire an already delivered guidance ID.
            if any(item.get('statement_kind') == 'research_scope' for item in self._items.values()):
                return
            from .skills import load_advisor
            instructions = load_advisor(params['name'], params.get('reference'))['instructions']
            instructions_sha = hashlib.sha256(instructions.encode()).hexdigest()
            delivered_sha = (hashlib.sha256(data['instructions'].encode()).hexdigest()
                             if type(data.get('instructions')) is str else data.get('instructions_sha256'))
            if instructions_sha != delivered_sha:
                _invalid()
            item = {'statement_kind': 'research_scope', 'evidence_id': evidence_id,
                    'evidence_digest': _digest(evidence), 'skill': params['name'],
                    'instructions_sha256': instructions_sha,
                    'contract_sha256': _digest((_SOURCE_CONTRACT, _RESEARCH_TEXT))}
            item['text'] = _source_statement(item)
            self._replace_slot(('research_scope',), {'stat_' + secrets.token_hex(16): item})
            return
        if evidence.get('source') != 'simulation' or evidence.get('parameters') != {} or type(evidence.get('data')) is not list:
            _invalid()
        rows = evidence['data'];by_id = {}
        for row in rows:
            if type(row) is not dict or type(row.get('id')) is not str or row['id'] in by_id:
                _invalid()
            by_id[row['id']] = row
        pending = {}
        for fact_id, fact in self._tools.facts.items():
            if fact.get('evidence_id') != evidence_id or fact.get('field') not in _PRODUCT_FIELDS:
                continue
            match = re.fullmatch(r'/(0|[1-9][0-9]*)/(term_days|redemption_delay_days)', fact.get('source_path', ''))
            if match is None or int(match[1]) >= len(rows):
                _invalid()
            row, field = rows[int(match[1])], match[2]
            if (fact.get('id') != fact_id or fact.get('unit') != 'scalar' or fact.get('currency') is not None
                    or fact.get('field') != field or type(fact.get('value')) is not int
                    or fact['value'] != row.get(field)):
                _invalid()
            # Optional statement eligibility must not reject an otherwise valid
            # catalog read. Historical rows keep their missing/null originals;
            # neither a version nor a zero-day term may be inferred for them.
            if (not row['id'] or type(row.get('version')) is not int or row['version'] < 1
                    or type(row.get(field)) is not int or not 0 <= row[field] <= 100000):
                continue
            item = {'statement_kind': 'simulation_product_terms', 'evidence_id': evidence_id,
                    'evidence_digest': _digest(evidence), 'source_fact_id': fact_id,
                    'fact': deepcopy(fact), 'product': deepcopy(row), 'source_field': field}
            item['text'] = _source_statement(item)
            pending['stat_' + secrets.token_hex(16)] = item
        self._replace_slot(('simulation_product_terms',), pending)

    def _planning_preserves_budget_scope(self, slot, evidence, batch):
        """A read-only scenario does not change a revalidated baseline definition.

        Retain the same issuer identity only while its original signed source and
        all scope inputs still hold. This never restores a retired boundary slot.
        """
        tools = self._tools
        data, params = evidence.get('data'), evidence.get('parameters')
        if (tools.changed is not False or type(tools.drafts) is not dict or tools.drafts
                or evidence.get('source') != 'simulation' or evidence.get('tool') != 'budget'
                or evidence.get('context_hash') != self._context
                or type(data) is not dict or type(params) is not dict
                or data.get('budget_scope') != 'planning_scenario'
                or data.get('account_id') != slot[1] or params.get('account_id') != slot[1]
                or set(params) != {'account_id', 'reserves'}
                or type(params['reserves']) is not list or not params['reserves']
                or type(data.get('planning_reserves')) is not list
                or len(params['reserves']) != len(data['planning_reserves'])
                or batch.get('workspace_id') != self._workspace or batch.get('tool_name') != 'budget'):
            return False
        prior = [item for item in self._items.values() if item.get('_slot') == slot]
        if len(prior) != 1:
            return False
        try:
            original = self._source_evidence(prior[0])
            base, arguments = original['data'], original['parameters']
            fixed = ('account_id', 'currency', 'as_of', 'version', 'amount_basis', 'name',
                     'cash_minor', 'reserve_minor', 'held_minor', 'upcoming_due_minor',
                     'baseline_investable_minor', 'commitments')
            if (base.get('budget_scope') != 'current_budget' or base.get('planning_reserves') != []
                    or arguments != {'account_id': slot[1]} or type(base.get('version')) is not int
                    or any(key not in base or key not in data or type(base[key]) is not type(data[key])
                        or base[key] != data[key] for key in fixed)
                    or any(type(want) is not dict or type(got) is not dict
                        or set(want) != {'label', 'amount_minor', 'commitment_ids'}
                        or set(got) != set(want) | {'already_budgeted_minor', 'additional_reserve_minor'}
                        or any(type(want[key]) is not type(got[key]) or want[key] != got[key] for key in want)
                        for want, got in zip(params['reserves'], data['planning_reserves']))):
                return False
            self._service.validate(self._workspace, batch)
            self._validate_source(prior[0], None)
        except (BankError, MetricClaimError, KeyError, TypeError, ValueError):
            return False
        self._proof = None
        return True

    def _register_budget(self, evidence_id, batch, registered_records):
        if type(batch) is not dict or type(registered_records) is not list:
            _invalid()
        evidence = self._tools.evidence.get(evidence_id)
        if (type(evidence) is not dict or evidence.get('source') != 'simulation'
                or evidence.get('tool') != 'budget' or evidence.get('context_hash') != self._context):
            _invalid()
        data, params = evidence.get('data'), evidence.get('parameters')
        if (type(data) is not dict or type(params) is not dict or not isinstance(data.get('account_id'), str)
                or params.get('account_id') != data['account_id']):
            _invalid()
        slot = ('current_budget_cash_scope', data['account_id'])
        if (params.get('reserves', []) != [] or set(params) - {'account_id', 'reserves'}
                or data.get('budget_scope') != 'current_budget' or data.get('planning_reserves') != []):
            if self._planning_preserves_budget_scope(slot, evidence, batch):
                return
            self._replace_slot(slot, {})
            return
        self._service.validate(self._workspace, batch)
        pending = {}
        for record in registered_records:
            metric = record.get('metric', {})
            if metric.get('metric_name') != 'arrangeable_now':
                continue
            if (record.get('kind') != 'recorded_fact' or metric.get('subject_id') != data['account_id']
                    or metric.get('currency') != data.get('currency') or metric.get('as_of') is not None
                    or metric.get('period_start') != data.get('as_of')
                    or metric.get('period_end') != (date.fromisoformat(data['as_of']) + timedelta(days=30)).isoformat()
                    or metric.get('formula_id') != 'tools.budget_current/1'
                    or metric.get('basis') != 'floor_and_holds_and_30_day_obligations_deducted'
                    or metric.get('assumed_inputs') or metric.get('calculation_status') != 'calculated'
                    or evidence_id not in record.get('evidence_ids', [])
                    or self._tools.metrics.lookup(metric['metric_id']) != record
                    or metric not in batch.get('metrics', []) or batch.get('workspace_id') != self._workspace
                    or batch.get('tool_name') != 'budget'):
                _invalid()
            basis = unicodedata.normalize('NFKC', data.get('amount_basis', ''))
            if any(required not in basis for required in (_BUDGET_CASH, _BUDGET_EXCLUSION, _BUDGET_UNLOCK)):
                _invalid()
            item = {'statement_kind': 'current_budget_cash_scope', 'evidence_id': evidence_id,
                    'evidence_digest': _digest(evidence), 'metric': deepcopy(metric),
                    'kind': 'recorded_fact', 'batch': deepcopy(batch), 'account_name': data['name'], 'as_of': data['as_of']}
            item['text'] = _source_statement(item)
            pending['stat_' + secrets.token_hex(16)] = item
        if len(pending) > 1:
            _invalid()
        self._replace_slot(slot, pending)

    def register(self, name, evidence_id, batch, registered_records):
        """Consume a signed account_summary batch after original registry commit."""
        if name == 'budget':
            self._owner()
            self._register_budget(evidence_id, batch, registered_records)
            return
        if name != 'account_summary':
            return
        self._owner()
        if (type(batch) is not dict or batch.get('tool_name') != name
                or batch.get('batch_contract') != 'navren.financial-tool-metrics/1'
                or batch.get('workspace_id') != self._workspace):
            self._replace_slot(('account_summary',), {})
            return
        if type(registered_records) is not list:
            _invalid()
        self._service.validate(self._workspace, batch)
        evidence = self._tools.evidence.get(evidence_id)
        if (type(evidence) is not dict or evidence.get('id') != evidence_id
                or evidence.get('tool') != name or evidence.get('source') != 'simulation'
                or evidence.get('context_hash') != self._context):
            self._replace_slot(('account_summary',), {})
            return
        evidence_digest = _digest(evidence)
        signed = {m['metric_id']: m for m in batch.get('metrics', [])}
        pending, seen_metrics = {}, set()
        for record in registered_records:
            if type(record) is not dict or type(record.get('metric')) is not dict:
                _invalid()
            raw = record['metric']
            if raw.get('metric_name') not in _ROLES:
                continue
            try:
                metric = _metric_snapshot(raw)
                current = self._tools.metrics.lookup(metric['metric_id'])
            except MetricClaimError:
                _invalid()
            if (metric != signed.get(metric['metric_id'])
                    or current['metric'] != metric or current['kind'] != 'recorded_fact'
                    or record != current or evidence_id not in current['evidence_ids']
                    or batch.get('metric_kinds', {}).get(metric['metric_id']) != 'recorded_fact'
                    or metric['subject_id'] != self._workspace
                    or metric['formula_id'] != 'tools.account_principal/1'
                    or metric['basis'] != _ROLES[metric['metric_name']][0]
                    or metric['unit'] != 'minor_currency'
                    or metric['data_scope'] not in ('simulation_bank_ledger', 'navren_synthetic_runtime_ledger')
                    or metric['calculation_status'] != 'calculated'
                    or type(metric['value_minor']) is not int or metric['value_minor'] < 0
                    or metric['as_of'] is None or metric['period_start'] is not None or metric['period_end'] is not None
                    or metric['assumed_inputs'] or metric['coverage_gaps']
                    or metric['currency'] not in ('CNY', 'USD', 'EUR', 'GBP', 'JPY')):
                # Existing metrics retain their original contract; a different
                # kind/scope/basis is simply ineligible for this new selector.
                continue
            if metric['metric_id'] in seen_metrics:
                _invalid()
            seen_metrics.add(metric['metric_id'])
            sid = 'stat_' + secrets.token_hex(16)
            pending[sid] = {'metric': deepcopy(metric), 'kind': 'recorded_fact',
                'evidence_id': evidence_id, 'evidence_digest': evidence_digest,
                'batch': deepcopy(batch), 'text': _statement(metric)}
        # A new complete summary replaces earlier selector capabilities, even
        # for equal values/metric IDs. Old selectors and render proof expire.
        self._replace_slot(('account_summary',), pending)

    def _validate_source(self, item, bank_db):
        evidence = self._source_evidence(item)
        kind = item['statement_kind']
        if _source_statement(item) != item['text'] or monetary_occurrences(item['text']):
            _invalid()
        if kind == 'simulation_product_terms':
            fact = self._tools.facts.get(item['source_fact_id'])
            if fact != item['fact']:
                _invalid()
            match = re.fullmatch(r'/(0|[1-9][0-9]*)/' + item['source_field'], fact['source_path'])
            if (match is None or type(evidence['data']) is not list or int(match[1]) >= len(evidence['data'])
                    or evidence['data'][int(match[1])] != item['product']):
                _invalid()
            if bank_db is not None:
                bank = self._tools.g.bank
                bank._workspace(bank_db, self._workspace)
                profile = bank._profile(bank_db, self._workspace)
                raw = bank_db.execute('SELECT data FROM products WHERE id=?', (item['product']['id'],)).fetchone()
                if raw is None:
                    _invalid()
                product = json.loads(raw['data'])
                if product.get('workspace_id') and product['workspace_id'] != self._workspace:
                    _invalid()
                reasons = bank._eligibility(product, profile)
                current = {**product, 'profile_eligible': not reasons, 'exclusion_reasons': reasons}
                if current != item['product']:
                    _invalid()
        elif kind == 'current_budget_cash_scope':
            metric = item['metric']
            current = self._tools.metrics.lookup(metric['metric_id'])
            if (current['metric'] != metric or current['kind'] != 'recorded_fact'
                    or item['evidence_id'] not in current['evidence_ids']):
                _invalid()
            self._service.validate(self._workspace, deepcopy(item['batch']), db=bank_db)
        elif kind == 'research_scope':
            from .skills import load_advisor
            params = evidence['parameters']
            instructions = load_advisor(params['name'], params.get('reference'))['instructions']
            if (params['name'] != item['skill']
                    or hashlib.sha256(instructions.encode()).hexdigest() != item['instructions_sha256']
                    or item['contract_sha256'] != _digest((_SOURCE_CONTRACT, _RESEARCH_TEXT))):
                _invalid()
        else:
            _invalid()

    def _validate_items(self, ids, bank_db=None):
        self._owner()
        if bank_db is not None:
            try:
                database = next((row[2] for row in bank_db.execute('PRAGMA database_list')
                                 if row[1] == 'main'), None)
            except (AttributeError, TypeError):
                _invalid()
            if not database or Path(database).resolve() != Path(self._tools.g.bank.database).resolve():
                _invalid()
        validated = set()
        for sid in ids:
            if type(sid) is not str or sid not in self._items:
                _invalid()
            item = self._items[sid]
            if 'statement_kind' in item:
                self._validate_source(item, bank_db)
                continue
            try:
                current = self._tools.metrics.lookup(item['metric']['metric_id'])
            except MetricClaimError:
                _invalid()
            if (current['metric'] != item['metric'] or current['kind'] != item['kind']
                    or item['evidence_id'] not in current['evidence_ids']
                    or _digest(self._evidence(item['evidence_id'])) != item['evidence_digest']
                    or _statement(item['metric']) != item['text']):
                _invalid()
            digest = _digest(item['batch'])
            if digest not in validated:
                # Publication reuses exactly the caller's bank DB; no fresh(),
                # new connection, transaction, metric issuance or re-signing.
                self._service.validate(self._workspace, deepcopy(item['batch']), db=bank_db)
                validated.add(digest)

    def catalog(self, evidence_id=None):
        ids = [sid for sid, item in self._items.items()
               if evidence_id is None or item['evidence_id'] == evidence_id]
        if not ids:
            return []
        self._validate_items(ids)
        output = []
        for sid in ids:
            item = self._items[sid]
            row = {'statistic_id': sid, 'canonical_statement': item['text']}
            if 'statement_kind' not in item:
                row.update(metric_name=item['metric']['metric_name'], metric_id=item['metric']['metric_id'])
            else:
                row.update(statement_kind=item['statement_kind'], source_evidence_id=item['evidence_id'])
                if item['statement_kind'] == 'simulation_product_terms':
                    row.update(source_fact_id=item['source_fact_id'], subject_id=item['product']['id'],
                               source_version=item['product']['version'], source_field=item['source_field'],
                               value=item['product'][item['source_field']], unit='days')
                elif item['statement_kind'] == 'current_budget_cash_scope':
                    metric = item['metric']
                    row.update(metric_id=metric['metric_id'], subject_id=metric['subject_id'],
                               currency=metric['currency'], as_of=item['as_of'])
                else:
                    row.update(skill=item['skill'], instructions_sha256=item['instructions_sha256'],
                               contract_sha256=item['contract_sha256'])
            output.append(row)
        return output

    def expand(self, statistic_id):
        self._validate_items([statistic_id])
        item = self._items[statistic_id]
        if 'statement_kind' in item:
            return {'text': item['text'], 'statement_kind': item['statement_kind'],
                    'evidence_ids': [item['evidence_id']]}
        return {'text': item['text'], 'metric_id': item['metric']['metric_id'],
                'evidence_ids': [item['evidence_id']]}

    def _entries(self, final, ranges_by_field, selected_ids):
        if (type(ranges_by_field) is not dict or type(selected_ids) is not dict
                or set(ranges_by_field) != set(selected_ids)):
            _invalid()
        entries = []
        for location, spans in ranges_by_field.items():
            value = _field(final, location)
            ids = selected_ids[location]
            if type(spans) not in (list, tuple) or type(ids) is not list or len(spans) != len(ids):
                _invalid()
            last = 0
            for span, sid in zip(spans, ids):
                if (type(span) not in (list, tuple) or len(span) != 2
                        or any(type(n) is not int for n in span)
                        or type(sid) is not str or sid not in self._items):
                    _invalid()
                start, end = span
                if not (last <= start < end <= len(value)) or value[start:end] != self._items[sid]['text']:
                    _invalid()
                if location != 'text':
                    index = int(re.fullmatch(r'claims\[(0|[1-9][0-9]*)\]\.text', location).group(1))
                    if final['claims'][index].get('evidence_ids') != [self._items[sid]['evidence_id']]:
                        _invalid()
                entries.append((location, start, end, sid))
                last = end
        return tuple(entries)

    def bind_render(self, final, ranges_by_field, selected_ids):
        self._proof = None
        entries = self._entries(final, ranges_by_field, selected_ids)
        if not entries:
            return
        self._validate_items([entry[3] for entry in entries])
        self._proof = (self._nonce, _digest(final), entries)

    def _validated(self, final, bank_db=None):
        if self._proof is None:
            return {}
        self._owner()
        nonce, digest, entries = self._proof
        if nonce != self._nonce or digest != _digest(final):
            _invalid()
        ranges, ids = {}, {}
        for location, start, end, sid in entries:
            ranges.setdefault(location, []).append((start, end))
            ids.setdefault(location, []).append(sid)
        if self._entries(final, ranges, ids) != entries:
            _invalid()
        self._validate_items([entry[3] for entry in entries], bank_db=bank_db)
        return ranges

    def rebind_host_transform(self, before, after):
        """Only verified NFKC/offset, followup subset and interaction lineage."""
        if self._proof is None:
            return
        self._validated(before)
        old_entries = self._proof[2]
        expected = deepcopy(before)
        if type(after) is not dict:
            _invalid()
        if _json(before) == _json(after):
            return
        original = before.get('text')
        normalize_body = 'amount_claims' in before or 'ratio_claims' in before
        if normalize_body:
            if type(original) is not str:
                _invalid()
            expected['text'] = unicodedata.normalize('NFKC', original)
        if 'amount_claims' in before:
            if type(before['amount_claims']) is not list:
                _invalid()
            for source, target in zip(before['amount_claims'], expected['amount_claims']):
                if type(source) is not dict or type(source.get('text')) is not str:
                    _invalid()
                target['text'] = unicodedata.normalize('NFKC', source['text'])
                if 'occurrence_start' in source:
                    start = source['occurrence_start']
                    if (type(start) is not int or start < 0
                            or original[start:start + len(source['text'])] != source['text']):
                        _invalid()
                    target['occurrence_start'] = len(unicodedata.normalize('NFKC', original[:start]))
                    offset = target['occurrence_start']
                    if expected['text'][offset:offset + len(target['text'])] != target['text']:
                        _invalid()
        old_followups, new_followups = before.get('followups'), after.get('followups')
        if old_followups is not None or new_followups is not None:
            if type(old_followups) is not list or type(new_followups) is not list:
                _invalid()
            cursor = iter(old_followups)
            if any(not any(_json(old) == _json(new) for old in cursor) for new in new_followups):
                _invalid()
            expected['followups'] = deepcopy(new_followups)
        if 'interaction' in before:
            from base_runtime.reply_interaction import validate_interaction
            expected['interaction'] = validate_interaction(before['interaction'])
        if _json(expected) != _json(after):
            _invalid()
        ranges, ids = {}, {}
        for location, start, end, sid in old_entries:
            if location == 'text' and normalize_body:
                start, end = (len(unicodedata.normalize('NFKC', original[:offset]))
                              for offset in (start, end))
            ranges.setdefault(location, []).append((start, end))
            ids.setdefault(location, []).append(sid)
        entries = self._entries(after, ranges, ids)
        self._proof = (self._nonce, _digest(after), entries)


def validated_statistic_atom_ranges(tools, final, bank_db=None):
    """Local proof only; model JSON and equal text/value never issue authority."""
    session = getattr(tools, '_statistic_session', None)
    if session is None:
        return {}
    if type(session) is not TypedStatisticSession or session._tools is not tools:
        _invalid()
    return session._validated(final, bank_db=bank_db)
