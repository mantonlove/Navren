"""仅真实审核分支的本地最终回复回查；无模型/HTTP注册入口或新签名。"""
from collections import OrderedDict
from copy import deepcopy
import inspect
import hashlib
import json
import math
import threading
import time

from financial_metrics import fingerprint
from simulated_bank import BankError
from user_memory import privacy_revision
from .metric_claims import monetary_occurrences
from .provider_review_carrier import (LITERAL as CARRIER_LITERAL,
    unpack_review_carrier, transport_record as carrier_transport_record,
    version as carrier_version)

CONTRACT = 'navren.agent-final-proof/1'
_TRANSPORT = frozenset({'financial_metric_batches', 'financial_metric_snapshot', 'knowledge_snapshot',
    'knowledge_preparation', 'memory_proposals', 'speech_receipt', '_approved_tts_text', '_answer_context'})


def fail(code='AGENT_FINAL_PROOF_INVALID', status=409):
    raise BankError(code, '本轮原审核回复或受信发布绑定无效，已停止发布。', status)


def language_value(value):
    if type(value) is not str or value not in ('zh', 'en'):
        fail('INVALID_LANGUAGE', 400)
    return value


def capture_publication_sources(reader, *, private_context_disabled=False):
    if type(private_context_disabled) is not bool:
        fail('AGENT_FINAL_SOURCE_READER_INVALID', 400)
    if reader is None:
        return None
    if not callable(reader):
        fail('AGENT_FINAL_SOURCE_READER_INVALID', 400)
    try:
        value = deepcopy(reader())
        if (type(value) is not dict or set(value) != {'knowledge_preparation', 'financial_metric_snapshot'}
                or type(value['knowledge_preparation']) is not dict
                or (type(value['financial_metric_snapshot']) is not dict
                    and not (private_context_disabled and value['financial_metric_snapshot'] is None))):
            fail('AGENT_FINAL_SOURCE_READER_INVALID', 503)
        if 'catalog_information' in value['knowledge_preparation']:
            from local_knowledge.catalog_information import validate_snapshot_shape
            validate_snapshot_shape(value['knowledge_preparation']['catalog_information'])
        fingerprint(value)
    except BankError:
        raise
    except Exception:
        fail('AGENT_FINAL_SOURCE_READER_INVALID', 503)
    return value


def presentation_call(consumer, method, *args, language='zh', **kwargs):
    """新consumer正式参数可接线；旧无图默认兼容，旧有图不得借旧zh状态。"""
    language_value(language)
    function = getattr(consumer, method)
    try:
        parameters = inspect.signature(function).parameters
        supported = 'language' in parameters and parameters['language'].kind != inspect.Parameter.POSITIONAL_ONLY
    except (ValueError, TypeError):
        supported = False
    if supported:
        return function(*args, language=language, **kwargs)
    selected = args[1] if method == 'prepare_final' else []
    has_view = (consumer.prepared is not None or any(isinstance(w, dict)
                and w.get('type') == 'financial_presentation' for w in selected))
    if has_view:
        fail('PRESENTATION_LANGUAGE_WIRING_REQUIRED', 503)
    return function(*args, **kwargs)


def contains(value, original):
    if value == original:
        return True
    if type(value) is dict:
        return any(contains(v, original) for v in value.values())
    if type(value) is list:
        return any(contains(v, original) for v in value)
    return False


def require_complete(value):
    if type(value) is dict:
        boundary = value.get('boundary')
        if type(boundary) is dict and boundary.get('status') in ('withheld', 'gap'):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        if value.get('metadata_withheld') or value.get('metrics_omitted'):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        for item in value.values():
            require_complete(item)
    elif type(value) is list:
        for item in value:
            require_complete(item)


def strict_equal(left, right):
    """JSON合同类型严格相等；True/1、1/1.0不能替换。"""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left) == set(right) and all(strict_equal(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def _expand_review(boundary, projection, *, review_carrier_version=CARRIER_LITERAL,
                   review_carrier_witness=None, transport=None):
    from .review_evidence import unpack_review_evidence,unpack_financial_evidence
    from .provider_evidence_transport import unpack_provider_evidence
    carrier_version(review_carrier_version)
    # Decode only the existing exact reversible protocols before independently
    # comparing every original member; decoding never replaces Base HMAC.
    packed = unpack_review_evidence(unpack_financial_evidence(
        unpack_provider_evidence(projection,review=True),review=True))
    full = unpack_review_carrier(packed, allowed_version=review_carrier_version,
        witness=review_carrier_witness)
    if transport is not None:
        if type(transport) is not dict:fail('AGENT_FINAL_PARTIAL_PROJECTION',503)
        transport.update(carrier_transport_record(packed, allowed_version=review_carrier_version,
            witness=review_carrier_witness))
    return boundary.expand_metric_sources(full)


def _restore_body_offsets(boundary, source, target, key, *, rows=False):
    """只在已检查的typed正文槽逆算；不补回任何缺失字段/成员。"""
    if key not in source:
        return
    claims = source[key]['amount_claims'] if key == 'body_amount_contract' and rows else source[key]
    restored = target[key]['amount_claims'] if key == 'body_amount_contract' and rows else target[key]
    if type(claims) is not list or type(restored) is not list or len(claims) != len(restored):
        fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
    text, local_text = source.get('text'), target.get('text')
    for old, new in zip(claims, restored):
        old, new = (old['claim'], new['claim']) if rows else (old, new)
        if 'occurrence_start' not in old:
            continue
        start, fragment = old['occurrence_start'], old.get('text')
        if (type(start) is not int or start < 0 or type(text) is not str or type(fragment) is not str
                or text[start:start + len(fragment)] != fragment):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        local_start = len(boundary._restore(text[:start], strict=False))
        if type(local_text) is not str or local_text[local_start:local_start + len(new['text'])] != new['text']:
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        new['occurrence_start'] = local_start


def restore_review_projection(boundary, projection, *, raw_projection=None,
                              historical_quote_market=None, historical_quote_session=None,
                              historical_quote_proof=None):
    # Tool/formula literals are schema identifiers, not user prose. Unknown
    # turn aliases still fail; restored private identities remain local only.
    from .data_boundary import _ALIAS, BoundaryError
    full = _expand_review(boundary, projection)
    quotes = historical_quote_session is not None or historical_quote_proof is not None
    if quotes:
        from .historical_quotes import finalize_review_projection
        checked = finalize_review_projection(boundary, raw_projection, full,
            historical_quote_market, historical_quote_session, historical_quote_proof)
        if not strict_equal(full, checked):
            fail('AGENT_FINAL_HISTORY_QUOTE_CHANGED')
    def check(value):
        if type(value) is dict:
            for key, item in value.items(): check(key); check(item)
        elif type(value) is list:
            for item in value: check(item)
        elif type(value) is str and any(token not in boundary._reverse for token in _ALIAS.findall(value)):
            raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
    check(full)
    local = boundary._restore(full, strict=False)
    draft = full.get('draft', {})
    target = local.get('draft', {})
    # Ordinary local glyph restoration is permitted only after its complete
    # normalized safe projection equals the actual reviewed text. No arbitrary
    # raw fields are copied; typed offsets and issued history keep their paths.
    if not quotes and type(raw_projection) is dict:
        raw_draft = raw_projection.get('draft')
        if (type(raw_draft) is dict and all(k not in raw_draft
                or (type(raw_draft[k]) is list and raw_draft[k] == [])
                for k in ('amount_claims', 'ratio_claims'))):
            import unicodedata
            if (type(draft) is not dict or type(target) is not dict
                    or type(raw_draft.get('text')) is not str):
                fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
            safe_text = boundary._plain_reply_text(raw_draft['text'])
            if unicodedata.normalize('NFKC', safe_text) != draft.get('text'):
                fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
            target['text'] = boundary._restore(safe_text, strict=False)
    if type(draft) is dict and type(target) is dict:
        for key in ('amount_claims', 'ratio_claims'):
            _restore_body_offsets(boundary, draft, target, key)
        for key, field in (('amount_contract', 'amount_claims'), ('ratio_contract', 'ratio_claims')):
            if key in full:
                source_body = {'text': draft.get('text'), field: full[key][field]}
                local_body = {'text': target.get('text'), field: local[key][field]}
                _restore_body_offsets(boundary, source_body, local_body, field, rows=True)
    if 'presentation_review' in full and 'text' in full['presentation_review']:
        _restore_body_offsets(boundary, full['presentation_review'], local['presentation_review'],
                              'body_amount_contract', rows=True)
    if 'provider_review' in full:
        source = full['provider_review']['projection']['complete_review']
        target = local['provider_review']['projection']['complete_review']
        _restore_body_offsets(boundary, source, target, 'body_amount_contract', rows=True)
    if quotes and not strict_equal(local.get('draft'), historical_quote_proof.final):
        fail('AGENT_FINAL_HISTORY_QUOTE_CHANGED')
    return local


def expected_review_projection(boundary, raw, *, historical_quote_market=None,
                               historical_quote_session=None, historical_quote_proof=None):
    """独立顶层组合器；绝不调用/复用被审核的review_packet。

    叶级Boundary合同是共同受信的隐私实现：history只保留16条合法角色；
    server_context只保留typed字段；tool_result按领域/敏感字段白名单；
    operations/calendar按其领域过滤私字段；原件typed财务/Provider完整。
    某个原证据整包不可安全投影时拒proof，不能静默删掉这个成员。
    """
    from .data_boundary import _TASK_DOMAINS
    from .presentation_consumer import project_review
    from .provider_projection import project_provider_receipt
    if type(raw) is not dict:
        fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
    allowed = {'question','history','server_context','evidence','draft','monetary_evidence',
        'amount_contract','ratio_contract','operations','calendar_widgets','policy',
        'financial_evidence_transport','presentation_review','provider_review','public_evidence_limits','catalog_information',
        'saved_payee_record_basis'}
    if set(raw) - allowed:
        fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
    b = deepcopy(boundary)  # local expected aliases/counts cannot mutate the real turn
    out = {}
    # The independent composer consumes the exact new host contract too. It
    # must never copy the reviewed result or call the reviewed review_packet.
    from .payment_completion import validate_saved_payee_review_basis
    payee_basis = None
    query_ref = None
    if 'saved_payee_record_basis' in raw:
        try:
            payee_basis = validate_saved_payee_review_basis(raw)
        except (ValueError, TypeError, KeyError):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        if payee_basis['query_name'] is not None:
            query_ref = b._alias(payee_basis['query_name'], 'person')
    if 'financial_evidence_transport' in raw:
        if raw['financial_evidence_transport'] != 'catalog-v1': fail()
        out['financial_evidence_transport'] = 'catalog-v1'
    if 'public_evidence_limits' in raw:
        from .review_scope import validate_review_scope
        out['public_evidence_limits']=validate_review_scope(raw['public_evidence_limits'])
    if 'catalog_information' in raw:
        from local_knowledge.catalog_information import validate_snapshot_shape
        out['catalog_information']=validate_snapshot_shape(raw['catalog_information'])
    for key, method in (('question', b.user_text), ('history', b.history), ('server_context', b.server_context)):
        if key in raw: out[key] = method(raw[key])
    evidence = raw.get('evidence', [])
    if type(evidence) is not list or len(evidence) > 48: fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
    out['evidence'] = []
    for row in evidence:
        if type(row) is not dict or not {'id','tool','data','source'} <= set(row): fail()
        projected = b.tool_result(row['tool'], {'evidence_id': row['id'], 'data': row['data'], 'facts': []})
        require_complete(projected)
        if 'boundary' in projected or type(projected.get('data')) not in (dict,list):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        if row['tool'] == 'cashflow':
            from .read_completion import funding_assessment_view_days
            assessment_days = funding_assessment_view_days(raw.get('question'))
            if assessment_days is not None:
                projected['data'].update(b._cashflow_scenario_sources(row['data'], assessment_days))
        out['evidence'].append({'id': projected['evidence_id'], 'tool': row['tool'],
            'data': projected['data'], 'source': b._text(row['source'], maximum=100)})
    if payee_basis is not None:
        source_id = b._alias(payee_basis['evidence_id'])
        source = next((e for e in out['evidence'] if e['id'] == source_id), None)
        rows = source.get('data', {}).get('beneficiaries') if source else None
        if type(rows) is not list:
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        by_record = {}
        for row in rows:
            if type(row) is not dict or 'id' not in row:
                fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
            if row['id'] in by_record and not strict_equal(by_record[row['id']], row):
                fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
            by_record[row['id']] = row
        source_ids = [b._alias(i) for i in payee_basis['source_record_ids']]
        matched_ids = [b._alias(i) for i in payee_basis['matched_record_ids']]
        if set(source_ids) != set(by_record) or len(set(source_ids)) != len(source_ids):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        facts = []
        for row in payee_basis['row_facts']:
            identifier = b._alias(row['record_id'])
            if any(not strict_equal(by_record[identifier].get(k), row[k]) for k in ('verified', 'status')):
                fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
            facts.append({**row, 'record_id': identifier})
        out['saved_payee_record_basis'] = {
            'contract': payee_basis['contract'], 'evidence_id': source_id,
            'query_ref': query_ref, 'query_scope': payee_basis['query_scope'],
            'source_record_ids': source_ids, 'matched_record_ids': matched_ids,
            'source_record_count': len(source_ids), 'matched_record_count': len(matched_ids),
            'row_facts': facts, 'record_identity_key': 'id',
            'local_current_query_binding': True, 'execution_authority': False}
    out['operations'] = b._project(raw.get('operations', []), 'operations') if 'operations' in _TASK_DOMAINS[b.task] else []
    out['calendar_widgets'] = b._project(raw.get('calendar_widgets', []), 'calendar') if 'calendar' in _TASK_DOMAINS[b.task] else []
    if 'presentation_review' in raw:
        if 'calendar' not in _TASK_DOMAINS[b.task]: fail()
        out['presentation_review'] = project_review(b, raw['presentation_review'])
    if 'provider_review' in raw:
        if 'calendar' not in _TASK_DOMAINS[b.task]: fail()
        out['provider_review'] = project_provider_receipt(b, raw['provider_review'])
    draft = raw.get('draft', {})
    draft_fields = {'text','claims','draft_ids','widget_ids','followups','amount_claims','ratio_claims','interaction'}
    if type(draft) is not dict or set(draft) - draft_fields: fail()
    out['draft'] = {k: b._text(v) if k == 'text' else b._project(v, 'profile', key=k)
                    for k,v in draft.items() if k not in ('amount_claims','ratio_claims','interaction')}
    if 'interaction' in draft:
        out['draft']['interaction']=b._reply_interaction(draft['interaction'],project=True)
    for key, method in (('amount_claims', b._amount_claims), ('ratio_claims', b._ratio_claims)):
        if key in draft:
            out['draft'][key] = method(draft[key], source_text=draft.get('text'), target_text=out['draft'].get('text'))
    for key, method in (('amount_contract', b._amount_contract), ('ratio_contract', b._ratio_contract)):
        if key in raw:
            out[key] = method(raw[key], source_text=draft.get('text'), target_text=out['draft'].get('text'))
    # A flattened monetary fact is visible only when its actual projected local
    # evidence carries the same scalar/public canonical source. No new authority.
    facts = raw.get('monetary_evidence', [])
    if type(facts) is not list or len(facts) > 48: fail()
    by_id = {row['id']: row for row in out['evidence']}
    out['monetary_evidence'] = []
    for fact in facts:
        if type(fact) is not dict: fail()
        evidence_id, value = b._alias(fact.get('evidence_id')), fact.get('value')
        row = by_id.get(evidence_id, {})
        source = b._public_fact_source(row.get('tool',''), row.get('data',{}), fact)
        if (type(value) not in (int,float) or not math.isfinite(value)
                or (fact.get('unit') == 'public_minor_currency' and source is None)
                or (value not in b._scalars(row.get('data',{})) and source is None)):
            continue
        fields = {k: fact[k] for k in ('id','evidence_id','value','currency','unit','scope','citation_id','article_id') if k in fact}
        if source is not None: fields.update(scope='public_statement_not_account_balance', text=source)
        out['monetary_evidence'].append(b._project_fact(fields, public=source is not None))
    policy = raw.get('policy', {})
    if type(policy) is not dict: fail()
    out['policy'] = b._project(policy, 'general')
    if historical_quote_session is not None or historical_quote_proof is not None:
        from .historical_quotes import finalize_review_projection
        out = finalize_review_projection(b, raw, out, historical_quote_market,
            historical_quote_session, historical_quote_proof)
    require_complete(out)
    return out


def _unique_json(pairs):
    out = {}
    for key, value in pairs:
        if key in out: fail('AGENT_FINAL_REVIEW_WIRE_INVALID', 503)
        out[key] = value
    return out


def _review_wire_parameters_match(wire, reviewer_model, reviewer):
    """新官方模型仅接受实际标准审核器的完整发送参数；旧模型保留原1400合同。"""
    from .model_adapters import GLM_OFFICIAL_MODEL, GLM_OFFICIAL_URL, OpenAICompatibleJsonClient
    if reviewer_model != GLM_OFFICIAL_MODEL:
        return type(wire.get('max_tokens')) is int and wire['max_tokens'] == 1400
    from .conversation import GLMConversationReviewer, _repair_input_reviewer
    try:
        inner = _repair_input_reviewer(reviewer)
        if inner is None:
            return False
        methods = ('_payload', '_request_parts', 'request_payload', 'serialized_request',
                   'review_system', '_catalog_route_supported')
        if any(name in vars(inner) or getattr(type(inner), name, None)
                is not getattr(GLMConversationReviewer, name) for name in methods):
            return False
        client = vars(inner).get('client')
        if (type(client) is not OpenAICompatibleJsonClient
                or any(name in vars(client) for name in ('_request_payload', 'complete_json'))
                or vars(inner).get('model_route') != 'official'
                or inner.model_id != reviewer_model or client.model_id != reviewer_model
                or client.provider != 'zhipu' or client.url != GLM_OFFICIAL_URL
                or client.env_name != 'GLM_API_KEY' or client.keychain_service != 'CC/GLM_API_KEY'
                or type(client.max_retries) is not int or client.max_retries != 0 or client.timeout != 35):
            return False
        expected = OpenAICompatibleJsonClient._request_payload(
            client, system='', user='', stage='reviewer.conversation')
        if (type(inner.output_token_limit) is not int
                or not strict_equal(inner.output_token_limit, expected['max_tokens'])):
            return False
        return strict_equal({k: v for k, v in wire.items() if k != 'messages'},
                            {k: v for k, v in expected.items() if k != 'messages'})
    except (AttributeError, TypeError, ValueError):
        return False


def validate_review_projection(boundary, raw, actual, *, review_wire, reviewer_model, reviewer=None,
                               review_carrier_version=CARRIER_LITERAL,
                               review_carrier_witness=None, transport=None,
                               historical_quote_market=None, historical_quote_session=None,
                               historical_quote_proof=None):
    """实际标准适配器Request.data→lossless包→独立预期，逐键逐成员比对。"""
    if type(review_wire) is not bytes:
        fail('AGENT_FINAL_REVIEW_WIRE_REQUIRED', 503)
    try:
        carrier_version(review_carrier_version)
        wire = json.loads(review_wire, object_pairs_hook=_unique_json)
        if (type(wire) is not dict or wire.get('model') != reviewer_model or not _review_wire_parameters_match(wire, reviewer_model, reviewer)
                or type(wire.get('messages')) is not list or len(wire['messages']) != 2
                or wire['messages'][0].get('role') != 'system' or wire['messages'][1].get('role') != 'user'
                or type(wire['messages'][1].get('content')) is not str):
            fail('AGENT_FINAL_REVIEW_WIRE_INVALID', 503)
        sent = json.loads(wire['messages'][1]['content'], object_pairs_hook=_unique_json)
        # Only actual wire table slots are decoded. The logical packet, Base
        # originals and both independent complete comparisons below stay intact.
        from .primary_tool_transport import restore_review_data_tables
        sent = restore_review_data_tables(sent, wire['messages'][0].get('content'))
        expanded = _expand_review(boundary, actual, review_carrier_version=review_carrier_version,
            review_carrier_witness=review_carrier_witness)
        if not strict_equal(_expand_review(boundary, sent, review_carrier_version=review_carrier_version,
                review_carrier_witness=review_carrier_witness,transport=transport), expanded):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        quote_context = dict(historical_quote_market=historical_quote_market,
            historical_quote_session=historical_quote_session, historical_quote_proof=historical_quote_proof)
        expected = expected_review_projection(boundary, raw, **quote_context)
        require_complete(expanded)
        if not strict_equal(expanded, expected):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        return restore_review_projection(boundary, expanded, raw_projection=raw, **quote_context)
    except BankError:
        raise
    except Exception:
        # No sensitive raw value/path enters the error, UI or audit.
        fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)


def observed_provider_batch(raw, projection, metric_batches):
    """唯一实际已审Provider object→原完整批次；合法签名集合不能替对象绑定。"""
    receipt = projection.get('provider_review')
    if receipt is None:
        return None
    if type(receipt) is not dict or not strict_equal(receipt, raw.get('provider_review')):
        fail('AGENT_FINAL_PROVIDER_WIRING_REQUIRED', 503)
    try:
        ref = receipt['projection']['object_ref']
        complete = receipt['projection']['complete_review']
        found = []
        for row in raw['evidence']:
            obj = row.get('data', {}).get('provider_object') if type(row.get('data')) is dict else None
            if type(obj) is dict and strict_equal(obj.get('object_ref'), ref): found.append((row['id'],obj))
        if len(found) != 1: fail('AGENT_FINAL_PROVIDER_WIRING_REQUIRED', 503)
        evidence_id, obj = found[0]
        visible = [row for row in projection['evidence'] if row.get('id') == evidence_id]
        if (len(visible) != 1 or not strict_equal(visible[0].get('data', {}).get('provider_object'), obj)
                or type(obj.get('result')) is not dict
                or not strict_equal(obj['result'].get('presentation'), complete['presentation'])):
            fail('AGENT_FINAL_PROVIDER_WIRING_REQUIRED', 503)
        batch = obj['result']['metric_batch']
        if type(batch) is not dict or not any(strict_equal(batch, item) for item in metric_batches):
            fail('AGENT_FINAL_PROVIDER_WIRING_REQUIRED', 503)
        return deepcopy(batch)
    except BankError:
        raise
    except Exception:
        fail('AGENT_FINAL_PROVIDER_WIRING_REQUIRED', 503)


class FinalResponseProofs:
    """单Agent实例有界、线程安全、105秒原deadline内使用一次的局部批准记录。"""
    def __init__(self, *, limit=64, clock=time.monotonic):
        self._entries = OrderedDict()
        self._lock = threading.RLock()
        self._limit, self._clock = limit, clock

    def approved(self, *, gateway, workspace, request_id, language, round_number, reviewer_model,
                 verdict, projection, raw_projection, actual_projection, metric_batches, observer_batches,
                 publication_sources, deadline, boundary, review_wire, reviewer=None,
                 review_carrier_version=CARRIER_LITERAL, review_carrier_witness=None,
                 private_context_disabled=False, generated_code_session=None, operation_tools=None,
                 historical_quote_session=None, historical_quote_proof=None):
        language_value(language)
        operation_bindings=None
        if operation_tools is not None:
            from .bank_tools import BankTools
            from .operation_claims import validate_operation_wording
            if (not isinstance(operation_tools,BankTools) or operation_tools.g is not gateway
                    or operation_tools.w != workspace or operation_tools.request_id != request_id):
                fail('AGENT_FINAL_OPERATION_BINDING_INVALID')
            operation_bindings=validate_operation_wording(raw_projection['draft'],operation_tools)
        quote_market=None
        if historical_quote_session is not None or historical_quote_proof is not None:
            from .bank_tools import BankTools
            from .historical_quotes import HistoryQuoteSession, QuoteProof, assert_answer
            if (type(operation_tools) is not BankTools
                    or type(historical_quote_session) is not HistoryQuoteSession
                    or type(historical_quote_proof) is not QuoteProof
                    or operation_tools.history_quote_session is not historical_quote_session
                    or operation_tools.history_quote_proof is not historical_quote_proof
                    or historical_quote_session.snapshot._owner is not gateway.page_contexts
                    or not private_context_disabled):
                fail('AGENT_FINAL_HISTORY_QUOTE_BINDING_INVALID')
            quote_market=operation_tools._market_store()
            assert_answer(quote_market,historical_quote_session,historical_quote_proof,raw_projection['draft'])
        if generated_code_session is not None:
            from .generated_code import GeneratedCodeSession
            if (type(generated_code_session) is not GeneratedCodeSession
                    or generated_code_session.g is not gateway
                    or generated_code_session.w != workspace
                    or generated_code_session.tools.request_id != request_id):
                fail('AGENT_FINAL_SANDBOX_BINDING_INVALID')
            generated_code_session.verify_fresh()
        catalog_information=getattr(gateway,'catalog_information_observer',None)
        if catalog_information is not None:
            if raw_projection.get('catalog_information')!=catalog_information.snapshot():fail('AGENT_FINAL_SOURCE_CHANGED')
            if operation_tools is None:fail('AGENT_FINAL_SOURCE_CHANGED')
            ready=[e['data'].get('ready') for e in operation_tools.evidence.values() if e.get('tool')=='knowledge_status']
            if len(ready)!=1:fail('AGENT_FINAL_SOURCE_CHANGED')
            catalog_information.assert_answer(raw_projection['draft'],operation_tools.user_text,ready[0])
        elif 'catalog_information' in raw_projection:fail('AGENT_FINAL_SOURCE_CHANGED')
        if type(private_context_disabled) is not bool:
            fail()
        if private_context_disabled and (metric_batches or observer_batches
                or (publication_sources is not None and publication_sources.get('financial_metric_snapshot') is not None)):
            fail('AGENT_FINAL_SOURCE_CHANGED')
        if type(verdict) is not dict or verdict.get('verdict') != 'approve':
            fail()
        if (type(round_number) is not int or round_number < 1 or type(reviewer_model) is not str
                or not reviewer_model or type(projection) is not dict
                or type(raw_projection) is not dict):
            fail()
        require_complete(raw_projection)
        require_complete(projection)
        review_transport={}
        restored = validate_review_projection(boundary, raw_projection, actual_projection,
            review_wire=review_wire, reviewer_model=reviewer_model, reviewer=reviewer,
            review_carrier_version=review_carrier_version,review_carrier_witness=review_carrier_witness,
            transport=review_transport,historical_quote_market=quote_market,
            historical_quote_session=historical_quote_session,historical_quote_proof=historical_quote_proof)
        if not strict_equal(projection, restored):
            fail('AGENT_FINAL_PARTIAL_PROJECTION', 503)
        provider_batch = observed_provider_batch(raw_projection, restored, metric_batches)
        token = object()
        entry = dict(gateway=gateway, workspace=workspace, request_id=request_id, language=language,
            round_number=round_number, reviewer_model=reviewer_model, verdict=deepcopy(verdict),
            projection=deepcopy(projection), raw_projection=deepcopy(raw_projection),
            actual_projection=deepcopy(actual_projection), actual_projection_hash=fingerprint(actual_projection),
            review_wire_hash=hashlib.sha256(review_wire).hexdigest(),
            review_transport=deepcopy(review_transport),
            metric_batches=deepcopy(metric_batches), observer_batches=deepcopy(observer_batches),
            provider_batch=provider_batch,
            private_context_disabled=private_context_disabled,
            publication_sources=deepcopy(publication_sources), deadline=deadline,
            response=None, response_hash=None, used=False, speech_check=None,
            generated_code_session=generated_code_session,
            historical_quote_session=historical_quote_session, historical_quote_proof=historical_quote_proof,
            operation_tools=operation_tools, operation_bindings=deepcopy(operation_bindings))
        with self._lock:
            self._expire()
            if any(e['workspace'] == workspace and e['request_id'] == request_id for e in self._entries.values()):
                fail('AGENT_FINAL_PROOF_REPLAY')
            self._entries[token] = entry
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)
        return token

    def _expire(self):
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e['deadline'] <= now]:
            del self._entries[key]

    def _entry(self, token):
        entry = self._entries.get(token)
        if entry is None or entry['deadline'] <= self._clock():
            fail('AGENT_FINAL_PROOF_EXPIRED')
        return entry

    def stage(self, token, response):
        with self._lock:
            entry = self._entry(token)
            if entry.get('staged') is not None:
                fail('AGENT_FINAL_PROOF_REPLAY')
            self._identity(entry, response, entry['language'])
            if response.get('presentation') is not None or response.get('metric_batch') is not None:
                # Frozen old component loop has no actual Provider receipt in
                # Native/review; no metadata can create that missing approval.
                receipt = entry['projection'].get('provider_review')
                if (type(receipt) is not dict or entry['provider_batch'] is None
                        or not strict_equal(response.get('metric_batch'), entry['provider_batch'])):
                    fail('AGENT_FINAL_PROVIDER_WIRING_REQUIRED', 503)
            entry['staged'] = deepcopy(response)

    def _identity(self, entry, response, language):
        if (type(response) is not dict or language_value(language) != entry['language']
                or response.get('language') != language or response.get('workspace_id') != entry['workspace']
                or response.get('bank_response_id') != entry['request_id'] or response.get('isFallback') is not False
                or response.get('reply') != entry['projection']['draft']['text']
                or type(response.get('agent_review')) is not dict
                or response['agent_review'].get('status') != 'approved'
                or response['agent_review'].get('model_id') != entry['reviewer_model']
                or response['agent_review'].get('review_id') != entry['request_id']):
            fail()

    def _metrics(self, entry, gateway, bank_db, batches):
        if bank_db is None:
            fail('AGENT_FINAL_GUARD_DB_REQUIRED', 503)
        if type(batches) is not list:
            fail()
        for batch in batches:
            gateway.financial_metrics.validate(entry['workspace'], deepcopy(batch), db=bank_db)

    def _operations(self, entry, bank_db):
        tools=entry.get('operation_tools')
        if tools is None:return
        if entry['operation_bindings']['bindings'] and bank_db is None:
            fail('AGENT_FINAL_GUARD_DB_REQUIRED',503)
        from .operation_claims import validate_operation_wording
        current=validate_operation_wording(entry['raw_projection']['draft'],tools,bank_db=bank_db)
        if not strict_equal(current,entry['operation_bindings']):
            fail('AGENT_FINAL_OPERATION_SOURCE_CHANGED')

    def _history_quotes(self, entry, bank_db):
        session,proof=entry.get('historical_quote_session'),entry.get('historical_quote_proof')
        if session is None and proof is None:return
        tools=entry['operation_tools']
        if (bank_db is None or tools.history_quote_session is not session
                or tools.history_quote_proof is not proof):
            fail('AGENT_FINAL_HISTORY_QUOTE_BINDING_INVALID')
        spans=tools.validated_history_quote_spans(entry['raw_projection']['draft'],bank_db=bank_db)
        if spans != proof.spans or not strict_equal(entry['projection']['draft'],proof.final):
            fail('AGENT_FINAL_HISTORY_QUOTE_CHANGED')

    def seal(self, token, response, *, bank_db=None, language='zh', server_gateway=None, session_token=None):
        """仅commit_history闭包调用；来源闭包在process入口已捕获，不读响应自报批准。"""
        with self._lock:
            entry = self._entry(token)
            self._identity(entry, response, language)
            # The HTTP callback already sealed and validated under the original
            # publication transaction. Its return-path check has no DB and can
            # only confirm that exact sealed response; it creates no new grant.
            if bank_db is None and entry['response_hash'] is not None:
                if fingerprint(response) != entry['response_hash']:
                    fail('AGENT_FINAL_RESPONSE_CHANGED')
                return True
            self._history_quotes(entry,bank_db)
            self._operations(entry,bank_db)
            sandbox=entry['generated_code_session']
            if sandbox is not None:
                if bank_db is None:fail('AGENT_FINAL_GUARD_DB_REQUIRED',503)
                sandbox.verify_for_publication(bank_db)
            if entry['response_hash'] is not None:
                if fingerprint(response) != entry['response_hash']:
                    fail('AGENT_FINAL_RESPONSE_CHANGED')
                return True
            original = entry.get('staged')
            if original is None:
                fail('AGENT_FINAL_PROOF_UNSEALED')
            keys = set(original) | set(response)
            changed = {k for k in keys if original.get(k) != response.get(k) or (k in original) != (k in response)}
            if changed - _TRANSPORT:
                fail('AGENT_FINAL_RESPONSE_CHANGED')
            gateway = server_gateway or entry['gateway']
            if gateway.bank is not entry['gateway'].bank:
                fail()
            additions = changed - {'_approved_tts_text', '_answer_context'}
            if additions and bank_db is None:
                fail('AGENT_FINAL_GUARD_DB_REQUIRED', 503)
            if 'financial_metric_batches' in changed:
                if response['financial_metric_batches'] != entry['observer_batches']:
                    fail('AGENT_FINAL_SOURCE_CHANGED')
                self._metrics(entry, gateway, bank_db, response['financial_metric_batches'])
            if {'financial_metric_snapshot', 'knowledge_preparation'} & changed:
                sources = entry['publication_sources']
                if sources is None:
                    fail('AGENT_FINAL_TRANSPORT_WIRING_REQUIRED', 503)
                for key in ('financial_metric_snapshot', 'knowledge_preparation'):
                    if key in changed and response.get(key) != sources[key]:
                        fail('AGENT_FINAL_SOURCE_CHANGED')
                if 'financial_metric_snapshot' in changed:
                    if response['financial_metric_snapshot'] is None:
                        if (entry['private_context_disabled'] is not True or entry['metric_batches']
                                or entry['observer_batches'] or sources['financial_metric_snapshot'] is not None):
                            fail('AGENT_FINAL_SOURCE_CHANGED')
                    else:self._metrics(entry, gateway, bank_db, [response['financial_metric_snapshot']])
            if 'knowledge_snapshot' in changed:
                observer = getattr(entry['gateway'], 'knowledge_provider', None)
                snapshot = getattr(observer, 'snapshot', None)
                if not callable(snapshot) or response.get('knowledge_snapshot') != snapshot(original.get('knowledge_snapshot')):
                    fail('AGENT_FINAL_TRANSPORT_WIRING_REQUIRED', 503)
            if 'memory_proposals' in changed:
                self._memory_candidates(entry, gateway, response['memory_proposals'])
            if 'speech_receipt' in changed:
                checker = getattr(gateway, 'assert_speech_evidence', None)
                if not callable(checker):
                    fail('AGENT_FINAL_TRANSPORT_WIRING_REQUIRED', 503)
                receipt = response['speech_receipt']
                expected = original.get('_approved_tts_text')
                def checked(db):
                    value = checker(entry['workspace'], receipt, session_token, db=db)
                    if value != expected:
                        fail('AGENT_FINAL_SOURCE_CHANGED')
                checked(bank_db)
                entry['speech_check'] = checked  # token never enters a JSON/proof/hash/log.
            for key in ('_approved_tts_text', '_answer_context'):
                if key in changed and key in response:
                    fail('AGENT_FINAL_RESPONSE_CHANGED')
            entry['validation_gateway'] = gateway
            entry['response'] = deepcopy(response)
            entry['response_hash'] = fingerprint(response)
            return True

    def _memory_candidates(self, entry, gateway, proposals):
        if type(proposals) is not list or len(proposals) > 32:
            fail()
        if not proposals:
            return
        w, rid = entry['workspace'], entry['request_id']
        if gateway.memories.state(w)['enabled'] is not True:
            fail()
        with gateway.personal_data.connection(w) as db:
            for proposal in proposals:
                if type(proposal) is not dict or set(proposal) != {'proposal_id', 'category', 'value', 'source_type',
                        'source_ref', 'confirmation_token', 'expected_revision', 'notice'}:
                    fail()
                row = db.execute('SELECT * FROM memory_candidates WHERE workspace_id=? AND proposal_id=?',
                                 (w, proposal['proposal_id'])).fetchone()
                if (row is None or row['status'] != 'candidate' or row['source_ref'] != rid
                        or row['expires_at'] <= gateway.personal_data.clock()
                        or proposal['source_type'] != 'conversation_user_statement' or proposal['source_ref'] != rid
                        or proposal['notice'] != '候选来自本人原话，确认后才生效。'
                        or any(proposal[k] != row[k] for k in ('category', 'value', 'confirmation_token', 'expected_revision'))
                        or proposal['expected_revision'] != gateway.personal_data.revision(w)):
                    fail('AGENT_FINAL_SOURCE_CHANGED')

    def validate(self, workspace, response, *, bank_db, language):
        with self._lock:
            candidates = [e for e in self._entries.values() if e['workspace'] == workspace
                and e['request_id'] == (response.get('bank_response_id') if type(response) is dict else None)]
            if len(candidates) != 1:
                fail('AGENT_FINAL_PROOF_MISSING', 503)
            entry = candidates[0]
            if entry['deadline'] <= self._clock():fail('AGENT_FINAL_PROOF_EXPIRED')
            if entry['used']:fail('AGENT_FINAL_PROOF_REPLAY')
            if entry['response_hash'] is None:fail('AGENT_FINAL_PROOF_UNSEALED')
            self._identity(entry, response, language)
            if fingerprint(response) != entry['response_hash']:fail('AGENT_FINAL_RESPONSE_CHANGED')
            gateway = entry.get('validation_gateway', entry['gateway'])
            self._history_quotes(entry,bank_db)
            self._operations(entry,bank_db)
            sandbox=entry['generated_code_session']
            if sandbox is not None:sandbox.verify_for_publication(bank_db)
            self._metrics(entry, gateway, bank_db, entry['metric_batches'])
            observer = getattr(entry['gateway'], 'knowledge_provider', None)
            if callable(getattr(observer, 'assert_fresh', None)):observer.assert_fresh()
            if (response.get('privacy_revision') != privacy_revision(bank_db, workspace)
                    or response.get('personal_revision') != gateway.personal_data.revision(workspace)):
                fail('AGENT_FINAL_SOURCE_CHANGED')
            if entry['speech_check'] is not None:entry['speech_check'](bank_db)
            body = len(monetary_occurrences(response['reply']))
            total = body
            for q in response.get('follow_up_questions', []):
                text = q if type(q) is str else q.get('text', '')
                total += len(monetary_occurrences(text))
                if type(q) is dict:total += len(monetary_occurrences(q.get('label', '')))
            receipt = entry['projection'].get('provider_review')
            if response.get('presentation') is not None:
                if (type(receipt) is not dict or entry['provider_batch'] is None
                        or not strict_equal(response.get('metric_batch'),entry['provider_batch'])):
                    fail('AGENT_FINAL_PROVIDER_WIRING_REQUIRED',503)
                checked = gateway.presentation_provider.verify(workspace, deepcopy(receipt),
                    trusted_current_request=entry['request_id'], trusted_current_message=entry['request_id'],
                    language=language, bank_db=bank_db)
                complete = receipt['projection']['complete_review']
                if (complete['text'] != response['reply']
                        or complete['presentation'] != response['presentation']):fail()
                self._metrics(entry,gateway,bank_db,[response['metric_batch']])
                body, total = checked['bodyAmountOccurrenceCount'], complete['visible_amount_occurrence_count']
            elif receipt is not None:fail()
            if type(body) is not int or type(total) is not int or not 0 <= body <= total <= 24:fail('AGENT_FINAL_AMOUNT_LIMIT')
            proof = {'contract':CONTRACT,'request_id':entry['request_id'],'message_id':entry['request_id'],
                'workspace_id':workspace,'language':language,'approved_content':response['reply'],
                'approved_response_hash':entry['response_hash'],'approved_review_projection':deepcopy(entry['projection']),
                'approved_projection_hash':fingerprint(entry['projection']),'reviewer_model':entry['reviewer_model'],
                'review_id':entry['request_id'],'verdict':deepcopy(entry['verdict']),
                'body_amount_occurrence_count':body,'total_amount_occurrence_count':total,
                'original_provider_receipt':deepcopy(receipt)}
            entry['used'] = True
            return proof
