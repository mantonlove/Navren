"""复用原资金服务的展示投影；不计算资金总额、义务合计或比例。"""
from copy import copy, deepcopy
from datetime import date, timedelta
import hashlib
from pathlib import Path
import uuid

from base_runtime.presentation import FinancialPresentations, reject
from financial_methods import MetricContext, money_metric
from financial_metrics import fingerprint

CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class FundingPresentations(FinancialPresentations):
    def source_versions(self, w, db):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != CODE:
            reject('FUNDING_VIEW_RESTART_REQUIRED', '资金展示代码已变化，请重新加载。', 503)
        from base_runtime.funding_cards import FundingCards
        return {**super().source_versions(w, db), 'funding_view_code': CODE,
                'funding_view_input_rows': fingerprint(self.g.financial_metrics._funding_table_versions(w, db)),
                'funding_view_extra_rows': fingerprint(FundingCards._source_rows(w, db))}

    def _params(self, w, p, g):
        if type(p) is not dict or p.get('intent') != 'funds':
            return super()._params(w, p, g)
        allowed = {'intent', 'days', 'section', 'offset', 'limit', 'from', 'to',
                   'source', 'currency', 'portfolio', 'accounting_basis', 'account_id',
                   'granularity', 'measure', 'kinds', 'include_life_events', 'layout'}
        if set(p) - allowed:
            reject('FUNDING_VIEW_QUERY', '资金展示参数无效。')
        days = p.get('days', 60)
        if type(days) is not int or not 1 <= days <= 365:
            reject('FUNDING_VIEW_QUERY', '资金期间须为1至365天。')
        # 原funds.current_budget是整个工作区，不伪造分账户预算。
        if (p.get('account_id') is not None or p.get('source', 'simulation') != 'simulation'
                or p.get('currency', 'CNY') != 'CNY' or p.get('portfolio', 'current') != 'current'
                or p.get('accounting_basis', 'funds.current_budget/1') != 'funds.current_budget/1'):
            reject('FUNDING_VIEW_SCOPE', '本资金服务只发行当前工作区整体人民币规则。', 422)
        section = p.get('section', 'summary')
        offset, limit = p.get('offset', 0), p.get('limit', 8)
        if (section not in {'summary', 'obligations', 'holds'} or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 8):
            reject('FUNDING_VIEW_PAGE', '资金明细分页无效。')
        begin = g.bank.customer_archive(w)['as_of']
        end = (date.fromisoformat(begin) + timedelta(days=days)).isoformat()
        if p.get('from', begin) != begin or p.get('to', end) != end:
            reject('FUNDING_VIEW_RANGE', '资金依据须保留原业务日和完整期限。')
        if p.get('layout') not in (None, 'funding_blocks', 'funding_items'):
            reject('PRESENTATION_LAYOUT', '资金图不支持无受信几何依据的图型。')
        return {'intent': 'funds', 'days': days, 'section': section, 'offset': offset, 'limit': limit,
                'from': begin, 'to': end, 'source': 'simulation', 'currency': 'CNY',
                'portfolio': 'current', 'accounting_basis': 'funds.current_budget/1',
                'account_id': None, 'granularity': 'day', 'measure': 'arrangeable_now',
                'kinds': [], 'include_life_events': False, 'layout': p.get('layout'),
                'as_of': begin, 'clock_basis': 'simulation_business_date'}

    def _build(self, g, w, p, versions, request_id, message_id, parent=None, selected_items=None, details=False):
        if p['intent'] != 'funds':
            return super()._build(g, w, p, versions, request_id, message_id, parent, selected_items, details)
        fm = copy(self.g.financial_metrics)
        g.personal_data = g.memories.store
        fm.g, fm.bank = g, g.bank  # 原算法沿现有query_only同银行连接。
        original = fm.snapshot(w, p['days'])
        fm.validate(w, original, g.bank.connect())
        vid = uuid.uuid4().hex
        metrics, kinds, points = [], {}, []
        scope = {k: p[k] for k in ('source', 'currency', 'portfolio', 'accounting_basis', 'account_id', 'kinds', 'include_life_events')}
        scope['funding_rule'] = {'subject': 'whole_workspace', 'emergency_policy': 'workspace_profile_once',
                                 'future_inflows_pre_spent': False, 'holds_deduplicated_by': 'explicit_identity_links',
                                 'per_account_budget_issued': False}
        scope['funding_query'] = {'days': p['days'], 'method_id': 'funds.current_budget/1',
                                  'rule_mode': original['funding'].get('mode'),
                                  'subject': 'whole_workspace', 'as_of': p['as_of'],
                                  'period_start': p['from'], 'period_end': p['to'],
                                  'coverage_scope': original['coverage_scope'],
                                  'source_scope': original['funding']['metrics']['current_budget']['data_scope'],
                                  'source_versions': deepcopy(original['source_versions'])}
        pres = {'schema_version': 1, 'contract': 'navren.presentation/1', 'view_id': vid, 'parent_view_id': parent,
                'request_id': request_id, 'message_id': message_id, 'workspace_binding': w,
                'authorization_revision': {'personal': int(versions['personal']), 'privacy': None},
                'source_versions': versions, 'scope': scope, 'range': {'from': p['from'], 'to': p['to']},
                'granularity': 'day', 'timezone': 'Asia/Shanghai', 'clock_basis': p['clock_basis'], 'as_of': p['as_of'],
                'series_kind': 'funding_breakdown', 'data_scope': original['funding']['metrics']['current_budget']['data_scope'],
                'measure': 'arrangeable_now', 'measure_basis': 'funds.current_budget/1', 'title': '资金拆解',
                'points': points, 'missing': deepcopy(original.get('coverage_gaps', [])), 'coverage': 'recorded_items_only',
                'readonly_actions': ['select_point', 'ask'], 'important_states': [original['coverage_scope']],
                'authority_reports': [], 'allowed_layouts': ['funding_blocks'] if p['section'] == 'summary' else ['funding_items'],
                'geometry': {'available': False, 'reason': 'no_ratio_or_composition_denominator_issued'},
                'funding_reference': {'snapshot_id': original['snapshot_id'], 'snapshot_hash': fingerprint(original)},
                'funding_status': original['funding']['status'], 'funding_mode': original['funding'].get('mode'),
                'funding_limits': deepcopy(original['funding'].get('floor_judgments', {}))}
        timeline = g.cashflow_timeline(w, p['days'])
        items = []
        for slot in timeline['timeline']:
            for event in slot['events']:
                if event['certainty'] == 'confirmed' and event['cash_delta_minor'] < 0:
                    items.append({'id': 'obligation/' + event['id'], 'date': slot['date'], 'section': 'obligations',
                                  'title': event.get('title', event.get('kind', '已记录付款')), 'currency': event['currency'],
                                  'amount_minor': event.get('amount_minor', -event['cash_delta_minor']),
                                  'basis': 'confirmed_recorded_outflow', 'kind': 'recorded_projection',
                                  'evidence_ref': event['evidence_ref'], 'state': 'confirmed'})
        archive = g.bank.customer_archive(w)
        for row in archive['records']['fund_holds']:
            if row['status'] == 'held' and row['created_on'] <= p['as_of'] <= row['expires_on']:
                items.append({'id': 'hold/' + row['id'], 'date': p['as_of'], 'section': 'holds',
                              'title': '已生效预留', 'currency': 'CNY', 'amount_minor': row['amount_minor'],
                              'basis': 'held_cash_subset_not_additional_asset', 'kind': 'recorded_fact',
                              'evidence_ref': 'fund_holds/' + row['id'], 'state': row['status']})
        items.sort(key=lambda i: (i['date'], i['id']))
        if original['funding_baseline_kind'] != 'bank_ledger':
            # 注入来源没有原银行逐项映射，不能混入银行明细或紧急保留。
            items = []
        def add(metric, label, block, kind='recorded_fact'):
            metrics.append(deepcopy(metric)); kinds[metric['metric_id']] = kind
            points.append({'point_ref': vid + ':funds:' + block, 'date': p['as_of'], 'start': p['from'], 'end': p['to'],
                           'label': label, 'block': block, 'coverage': metric['calculation_status'],
                           'value': {k: metric[k] for k in ('metric_id', 'value_minor', 'currency', 'calculation_status')},
                           'item_count': len([i for i in items if i['section'] == block]) if original['funding_baseline_kind'] == 'bank_ledger' else None,
                           'preview': [], 'missing': metric['coverage_gaps']})
        if p['section'] == 'summary':
            for key, label in (('cash_balance', '已结算现金'), ('current_available', '扣除底线及占用后的当前余额'), ('current_budget', '当前可安排预算')):
                add(original['funding']['metrics'][key], label, key)
            if original['funding_baseline_kind'] == 'bank_ledger':
                ctx = MetricContext(w, 'CNY', pres['data_scope'], as_of=p['as_of'],
                                    input_refs=('profiles/' + w,), source_versions=tuple(original['source_versions'].items()))
                reserve = money_metric(ctx, 'emergency_floor', 'workspace_profile_once', 'source.profiles.reserve_minor/1', archive['profile']['reserve_minor']).to_dict()
                add(reserve, '应急保留', 'emergency_floor')
            for section, label in (('obligations', '近期已确认付款'), ('holds', '已生效预留')):
                # 原服务没有发行拆解合计，不以另算差值冒充原指标。
                ctx = MetricContext(w, 'CNY', pres['data_scope'], period_start=p['from'], period_end=p['to'],
                                    input_refs=tuple(original['funding']['metrics']['current_budget']['input_refs']),
                                    source_versions=tuple(original['source_versions'].items()), coverage_complete=False)
                m = money_metric(ctx, section + '_aggregate', 'aggregate_not_issued', 'presentation.unavailable_component/1', None,
                                 status='unavailable', gaps=('component_aggregate_not_issued_by_source',)).to_dict()
                add(m, label, section)
            total = len(points)
        else:
            selected = [i for i in items if i['section'] == p['section'] and (selected_items is None or i['id'] in selected_items)]
            total = len(selected)
            if p['offset'] > total: reject('FUNDING_VIEW_PAGE', '明细偏移超出原范围。')
            for item in selected[p['offset']:p['offset'] + p['limit']]:
                ctx = MetricContext(w, item['currency'], pres['data_scope'], as_of=item['date'],
                                    input_refs=(item['evidence_ref'],), source_versions=tuple(original['source_versions'].items()))
                m = money_metric(ctx, item['section'] + '_item', item['basis'], 'source.funding_item/1', item['amount_minor']).to_dict()
                add(m, item['title'], item['id'], item['kind'])
                points[-1].update(item_ref=item['id'], state=item['state'], date=item['date'], evidence_ref=item['evidence_ref'])
        pres['layout'] = pres['allowed_layouts'][0]
        pres['page'] = {'offset': p['offset'], 'limit': p['limit'], 'total': total, 'has_more': p['offset'] + len(points) < total}
        pres['followups'] = []  # 正式建议由整轮受信发行/审核提供，不自造available。
        return pres, metrics, kinds, items

    def _stored(self, w, vid, db, *, current=True):
        stored = super()._stored(w, vid, db, current=current)
        ref = stored['presentation'].get('funding_reference')
        if ref and current:
            from financial_metrics import fingerprint
            fm = self.g.financial_metrics
            with fm._tool_lock: entry = deepcopy(fm._funding_issuances.get(ref['snapshot_id']))
            if not entry or fingerprint(entry[0]) != ref['snapshot_hash']:
                reject('METRIC_BUDGET_NOT_ISSUED', '资金原件已失效，请重新查询。', 409)
            fm.validate(w, entry[0], db)
        return stored

    def detail(self, w, vid, selector, *, request_id, message_id, bank_db=None):
        with self._reissue_atomic(bank_db) as db:
            stored = self._stored(w, vid, db)
            if stored['parameters']['intent'] != 'funds':
                return super().detail(w, vid, selector, request_id=request_id, message_id=message_id, bank_db=db)
            self.resolve(w, vid, request_id=request_id, message_id=message_id, bank_db=db)
            if type(selector) is not dict or set(selector) - {'point_ref', 'offset', 'limit'} or 'point_ref' not in selector:
                reject('PRESENTATION_SELECTOR', '请选择资金卡亲发的明细组。')
            point = next((x for x in stored['presentation']['points'] if x['point_ref'] == selector['point_ref']), None)
            if not point or point['block'] not in {'obligations', 'holds'}:
                reject('PRESENTATION_SELECTOR', '该资金块没有逐项明细。')
            p = deepcopy(stored['parameters']); p.update(section=point['block'], offset=selector.get('offset', 0), limit=selector.get('limit', 8), layout=None)
            p = {k:v for k,v in p.items() if k not in {'as_of', 'clock_basis'}}
            return self._query(w, p, request_id=request_id, message_id=message_id, parent=vid,
                               parent_projection_hash=stored['presentation']['projection_hash'], bank_db=db)

    def item_page(self, w, vid, point_ref, *, request_id, message_id, offset=0, limit=8, bank_db=None):
        with self._reissue_atomic(bank_db) as db:
            stored = self._stored(w, vid, db)
            if stored['parameters']['intent'] != 'funds':
                return super().item_page(w, vid, point_ref, request_id=request_id, message_id=message_id,
                                         offset=offset, limit=limit, bank_db=db)
            return self.detail(w, vid, {'point_ref': point_ref, 'offset': offset, 'limit': limit},
                               request_id=request_id, message_id=message_id, bank_db=db)

    def reissue_selected_detail(self, w, old_view_id, selection_ref, expected_parent_projection_hash, *,
                                trusted_current_request, trusted_current_message, bank_db=None):
        with self._reissue_atomic(bank_db) as db:
            stored = self._stored(w, old_view_id, db)
            if stored['parameters']['intent'] != 'funds':
                return super().reissue_selected_detail(w, old_view_id, selection_ref, expected_parent_projection_hash,
                    trusted_current_request=trusted_current_request, trusted_current_message=trusted_current_message, bank_db=db)
            self._identity(trusted_current_request, trusted_current_message)
            original = stored['presentation']
            if trusted_current_request == original['request_id'] or expected_parent_projection_hash != original['projection_hash']:
                reject('PRESENTATION_BINDING', '旧批准不能用于当前轮。', 403)
            if type(selection_ref) is not dict or set(selection_ref) not in ({'point_ref'}, {'item_ref'}):
                reject('PRESENTATION_SELECTOR', '跨轮只能选亲发资金项。')
            p = deepcopy(stored['parameters']); selected = None
            if 'point_ref' in selection_ref:
                point = next((x for x in original['points'] if x['point_ref'] == selection_ref['point_ref']), None)
                if not point: reject('PRESENTATION_SELECTOR', '资金图点没有发行。', 403)
                if point['block'] in {'obligations', 'holds'}: p['section'] = point['block']
            else:
                visible = {x.get('item_ref') for x in original['points']}
                item = next((x for x in stored['items'] if x['id'] == selection_ref['item_ref']), None)
                if not item or item['id'] not in visible: reject('PRESENTATION_SELECTOR', '资金事项没有实际显示。', 403)
                p['section'] = item['section']; selected = [item['id']]
            p.update(offset=0, layout=None)
            p = {k:v for k,v in p.items() if k not in {'as_of', 'clock_basis'}}
            result = self._query(w, p, request_id=trusted_current_request, message_id=trusted_current_message,
                                 parent=old_view_id, parent_projection_hash=original['projection_hash'], selected_items=selected,
                                 _lineage={'old_view_id': old_view_id, 'parent_projection_hash': original['projection_hash'],
                                           'selection_ref': deepcopy(selection_ref)}, bank_db=db)
            self._stored(w, old_view_id, db)
            self.g.financial_metrics.validate(w, result['metric_batch'], db)
            return result
