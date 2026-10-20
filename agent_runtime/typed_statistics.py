"""本轮正式账户统计原子；不推断、改写或证明银行操作状态。"""
from copy import deepcopy
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

    def register(self, name, evidence_id, batch, registered_records):
        """Consume a signed account_summary batch after original registry commit."""
        if name != 'account_summary':
            return
        self._owner()
        if (type(batch) is not dict or batch.get('tool_name') != name
                or batch.get('batch_contract') != 'navren.financial-tool-metrics/1'
                or batch.get('workspace_id') != self._workspace):
            self._items, self._proof = {}, None
            return
        if type(registered_records) is not list:
            _invalid()
        self._service.validate(self._workspace, batch)
        evidence = self._tools.evidence.get(evidence_id)
        if (type(evidence) is not dict or evidence.get('id') != evidence_id
                or evidence.get('tool') != name or evidence.get('source') != 'simulation'
                or evidence.get('context_hash') != self._context):
            self._items, self._proof = {}, None
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
        self._items = pending
        self._proof = None

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
        return [{'statistic_id': sid, 'canonical_statement': self._items[sid]['text'],
                 'metric_name': self._items[sid]['metric']['metric_name'],
                 'metric_id': self._items[sid]['metric']['metric_id']} for sid in ids]

    def expand(self, statistic_id):
        self._validate_items([statistic_id])
        item = self._items[statistic_id]
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
