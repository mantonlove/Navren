"""正常边界投影后的工具 JSON 可逆表格传输；不发行事实或指标。

只改变已出库 tool 消息的同型记录表示，不处理模型终稿参数。原本的
每个字段、类型、嵌套值与记录顺序均可还原；碰撞或无整包收益保留原文。
"""
from __future__ import annotations

from copy import deepcopy
import json


TABLE_KEY = '__navren_tool_table_v1__'
TABLE_GUIDE = ('工具JSON中的__navren_tool_table_v1__是本地可逆传输表：'
    'columns每项是原对象的完整键路径数组，如["metric","metric_name"]；'
    'rows每行同位置是该路径的完整字段值，按路径重建每行原对象；值中的嵌套表按同法还原。'
    '所有原记录、字段、值、顺序和空值均保留，不能省略不同口径或同值记录。'
    '此格式不新增事实、指标ID、证据、签名或操作权限。终稿仍按原工具schema提交，'
    '不得提交表格标记、列号或传输引用替代原metric_id、fact_id或完整业务参数。')


class ToolTableError(ValueError):
    pass


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ToolTableError('duplicate_json_key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ToolTableError('nonfinite_json')


def _collision(value):
    if type(value) is dict:
        return TABLE_KEY in value or any(_collision(item) for item in value.values())
    if type(value) is list:
        return any(_collision(item) for item in value)
    return False


def _pack(value):
    if type(value) is dict:
        return {key: _pack(item) for key, item in value.items()}
    if type(value) is not list:
        return value
    literal = [_pack(item) for item in value]
    if len(value) < 2 or any(type(item) is not dict for item in value):
        return literal
    keys = list(value[0])
    if not keys or any(list(item) != keys for item in value):
        return literal
    columns, cells = [], []
    def column(items, path):
        if (all(type(item) is dict and item for item in items)
                and all(list(item) == list(items[0]) for item in items)):
            for key in items[0]:
                column([item[key] for item in items], path + [key])
        else:
            columns.append(path)
            cells.append([_pack(item) for item in items])
    for key in keys:
        column([item[key] for item in value], [key])
    # Every row remains distinct. Only shared dictionary key paths are moved
    # to the header; heterogeneous/empty objects remain complete leaf values.
    table = {TABLE_KEY: {'columns': columns,
                       'rows': [list(items) for items in zip(*cells)]}}
    return table if len(_encoded(table).encode('utf-8')) < len(_encoded(literal).encode('utf-8')) else literal


def decode_tool_json(value):
    """Strict local inverse. No registry, alias allocation or trust decisions."""
    if type(value) is list:
        return [decode_tool_json(item) for item in value]
    if type(value) is not dict:
        return value
    if TABLE_KEY not in value:
        return {key: decode_tool_json(item) for key, item in value.items()}
    if set(value) != {TABLE_KEY}:
        raise ToolTableError('table_shape')
    body = value[TABLE_KEY]
    if type(body) is not dict or set(body) != {'columns', 'rows'}:
        raise ToolTableError('table_shape')
    columns, rows = body['columns'], body['rows']
    if (type(columns) is not list or not columns
            or any(type(path) is not list or not path
                   or any(type(key) is not str or key == TABLE_KEY for key in path) for path in columns)
            or type(rows) is not list or len(rows) < 2
            or any(type(row) is not list or len(row) != len(columns) for row in rows)):
        raise ToolTableError('table_shape')
    # Validate the whole header before constructing any row. A leaf cannot be
    # another column's ancestor, even if the conflicting cell happens to be {}.
    trie, leaf = {}, object()
    for path in columns:
        node = trie
        for key in path:
            if leaf in node:
                raise ToolTableError('table_prefix_collision')
            node = node.setdefault(key, {})
        if node:
            raise ToolTableError('table_prefix_collision')
        node[leaf] = True
    result = []
    for row in rows:
        original = {}
        for path, item in zip(columns, row):
            node = original
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = decode_tool_json(item)
        result.append(original)
    return result


def _same_json(left, right):
    # Encoding distinguishes bool/int, null/missing, float/int and dictionary
    # member order. Equality alone would accept True as 1 or ignore key order.
    encode = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':'))
    return encode(left) == encode(right)


def pack_primary_payload(payload):
    """Use only a smaller complete wire, including its fixed decoding guide.

    The caller negotiates the exact native client. This pure function never
    mutates its input, changes tool schemas or moves source data between roles.
    """
    try:
        messages = payload.get('messages')
        if (type(messages) is not list or not messages
                or type(messages[0]) is not dict or messages[0].get('role') != 'system'
                or type(messages[0].get('content')) is not str
                or TABLE_GUIDE in messages[0]['content']):
            return payload
        candidate = deepcopy(payload)
        changed = False
        for message in candidate['messages']:
            if type(message) is not dict or message.get('role') != 'tool' or type(message.get('content')) is not str:
                continue
            raw = message['content']
            try:
                original = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
                if _collision(original):
                    # The guide is global to this request. A literal reserved
                    # key in any tool must never share it with encoded tools.
                    return payload
                packed = _pack(original)
                encoded = _encoded(packed)
                if not _same_json(decode_tool_json(packed), original):
                    continue
                if len(encoded.encode('utf-8')) < len(raw.encode('utf-8')) and packed != original:
                    message['content'] = encoded
                    changed = True
            except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError):
                continue
        if not changed:
            return payload
        candidate['messages'][0]['content'] += '\n' + TABLE_GUIDE
        if len(_encoded(candidate).encode('utf-8')) >= len(_encoded(payload).encode('utf-8')):
            return payload
        return candidate
    except (ValueError, TypeError, RecursionError, OverflowError, UnicodeError, AttributeError):
        return payload


# 复用 F 已有的审核无损编码；原 primary 编码逐字保留。
_REVIEW_SIGNED_FIELDS = frozenset(('contract_version','metric_id','subject_id','metric_name',
    'currency','unit','value_minor','as_of','period_start','period_end','data_scope',
    'calculation_status','basis','formula_id','input_refs','source_versions','excluded_items',
    'coverage_gaps','assumed_inputs'))

_REVIEW_LITERAL_KEYS = frozenset(('metric_batch','amount_contract','body_amount_contract',
    'ratio_contract','amount_claims','ratio_claims','source_versions','source_version_sets',
    'financial_metric_catalog','review_metric_catalog'))

def _review_literal_subtree(value):
    if type(value)is dict:
        return (_REVIEW_SIGNED_FIELDS<=set(value)or bool(_REVIEW_LITERAL_KEYS.intersection(value))
            or any(_review_literal_subtree(v)for v in value.values()))
    return type(value)is list and any(_review_literal_subtree(v)for v in value)

def restore_review_data_tables(packet,system):
    """Only actual review wire with this exact guide may use these known slots."""
    if type(system)is not str or not system.endswith('\n'+TABLE_GUIDE):return packet
    if type(packet)is not dict or not (_collision(packet)or _LITERAL_KEY in packet):raise ToolTableError('review_table_missing')
    result=deepcopy(packet)
    if _LITERAL_KEY in result:
        if not system.endswith('\n'+_LITERAL_GUIDE+'\n'+TABLE_GUIDE):raise ToolTableError('review_literal_guide')
        catalog=result.pop(_LITERAL_KEY)
        roots,slots=_review_literal_roots(result,restoring=True)
        restored=_restore_literal_values(roots,catalog)
        for (parent,key),value in zip(slots,restored):parent[key]=value
    for entry in result.get('evidence',[]):
        if type(entry)is not dict:raise ToolTableError('review_table_evidence')
        if _collision(entry.get('data')):
            if _review_literal_subtree(entry['data']):raise ToolTableError('review_table_financial_literal')
            entry['data']=decode_tool_json(entry['data'])
            if _review_literal_subtree(entry['data']):raise ToolTableError('review_table_financial_literal')
    if _collision(result.get('monetary_evidence')):
        result['monetary_evidence']=decode_tool_json(result['monetary_evidence'])
    if _collision(result):raise ToolTableError('review_table_wrong_slot')
    return result

def pack_review_data_payload(payload):
    """Actual GLM serializer and reserve share one full-wire choose-shorter path."""
    try:
        if (type(payload)is not dict or type(payload.get('messages'))is not list or len(payload['messages'])!=2
                or payload['messages'][0].get('role')!='system'or payload['messages'][1].get('role')!='user'
                or type(payload['messages'][0].get('content'))is not str
                or payload['messages'][0]['content'].endswith('\n'+TABLE_GUIDE)):
            return payload
        original=json.loads(payload['messages'][1]['content'],object_pairs_hook=_unique,parse_constant=_reject_constant)
        if type(original)is not dict or _collision(original):return payload
        packed=deepcopy(original);changed=False
        for entry in packed.get('evidence',[]):
            if type(entry)is not dict or 'data'not in entry:continue
            value=entry['data']
            if _review_literal_subtree(value):continue
            organized=_pack(value)
            if not _same_json(decode_tool_json(organized),value):return payload
            if not _same_json(organized,value):entry['data']=organized;changed=True
        rows=packed.get('monetary_evidence')
        # These are the original unsigned scalar compatibility facts, never
        # metric signatures or amount claim parameters. Unknown rows stay literal.
        allowed=frozenset(('evidence_id','value','currency','unit','scope','article_id','citation_id'))
        if (type(rows)is list and rows and all(type(r)is dict and set(r)<=allowed
                and all(v is None or type(v)in(str,int,float,bool)for v in r.values())for r in rows)):
            organized=_pack(rows)
            if not _same_json(decode_tool_json(organized),rows):return payload
            if not _same_json(organized,rows):packed['monetary_evidence']=organized;changed=True
        if not changed:return _pack_review_literals(payload)
        candidate=deepcopy(payload);candidate['messages'][0]['content']+='\n'+TABLE_GUIDE
        candidate['messages'][1]['content']=json.dumps(packed,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        if not _same_json(restore_review_data_tables(packed,candidate['messages'][0]['content']),original):return payload
        if len(json.dumps(candidate,ensure_ascii=False).encode())>=len(json.dumps(payload,ensure_ascii=False).encode()):return _pack_review_literals(payload)
        return _pack_review_literals(candidate)
    except (AttributeError,KeyError,ValueError,TypeError,OverflowError,RecursionError,UnicodeError):
        return payload

_LITERAL_KEY='__navren_input_literals_v1__'

_LITERAL_PREFIX='~L:'

_LITERAL_GUIDE=('本包__navren_input_literals_v1__按0起始索引逐字列出重复字符串。'
    '仅tool.data/tool.facts或审核evidence.data/monetary_evidence中的完整字符串"~L:N"'
    '引用目录第N项，先逐字还原再读取；不用于其他字段，不新增事实、签名或权限。'
    '原字段、类型、顺序和全部值均保留，终稿仍按原schema与原ID提交。')

def _literal_catalog(values):
    counts={}
    def scan(value):
        if type(value)is str:
            if value.startswith(_LITERAL_PREFIX):raise ToolTableError('literal_collision')
            if len(_encoded(value).encode())>=12:counts[value]=counts.get(value,0)+1
        elif type(value)is list:
            for item in value:scan(item)
        elif type(value)is dict:
            if _LITERAL_KEY in value:raise ToolTableError('literal_collision')
            for item in value.values():scan(item)
    for value in values:scan(value)
    catalog=[s for s,n in counts.items()if n>1 and (n-1)*len(_encoded(s).encode())>n*10+3]
    if not catalog:return values,None
    aliases={s:_LITERAL_PREFIX+str(i)for i,s in enumerate(catalog)}
    def encode(value):
        if type(value)is str:return aliases.get(value,value)
        if type(value)is list:return [encode(x)for x in value]
        if type(value)is dict:return {k:encode(v)for k,v in value.items()}
        return value
    packed=[encode(v)for v in values]
    if not _same_json(_restore_literal_values(packed,catalog),values):raise ToolTableError('literal_roundtrip')
    return packed,catalog

def _restore_literal_values(values,catalog):
    if (type(catalog)is not list or not catalog or any(type(s)is not str or s.startswith(_LITERAL_PREFIX)for s in catalog)
            or len(set(catalog))!=len(catalog)):raise ToolTableError('literal_catalog')
    used=set()
    def restore(value):
        if type(value)is str and value.startswith(_LITERAL_PREFIX):
            suffix=value[len(_LITERAL_PREFIX):]
            if not suffix.isascii()or not suffix.isdigit()or str(int(suffix))!=suffix:raise ToolTableError('literal_ref')
            index=int(suffix)
            if not 0<=index<len(catalog):raise ToolTableError('literal_ref')
            used.add(index);return catalog[index]
        if type(value)is list:return [restore(x)for x in value]
        if type(value)is dict:
            if _LITERAL_KEY in value:raise ToolTableError('literal_collision')
            return {k:restore(v)for k,v in value.items()}
        return value
    result=[restore(v)for v in values]
    if used!=set(range(len(catalog))):raise ToolTableError('literal_unused')
    return result

def _review_literal_roots(packet,*,restoring=False):
    roots=[];slots=[]
    for entry in packet.get('evidence',[]):
        if type(entry)is dict and 'data'in entry and not _review_literal_subtree(entry['data']):
            roots.append(entry['data']);slots.append((entry,'data'))
    rows=packet.get('monetary_evidence');decoded=None if restoring else decode_tool_json(rows)
    allowed={'evidence_id','value','currency','unit','scope','article_id','citation_id'}
    if (restoring or type(decoded)is list and all(type(row)is dict and set(row)<=allowed
            and all(v is None or type(v)in(str,int,float,bool)for v in row.values())for row in decoded)):
        roots.append(rows);slots.append((packet,'monetary_evidence'))
    return roots,slots

def _pack_review_literals(payload):
    try:
        original=json.loads(payload['messages'][1]['content'],object_pairs_hook=_unique,parse_constant=_reject_constant)
        if type(original)is not dict or _LITERAL_KEY in original:return payload
        packed=deepcopy(original);roots,slots=_review_literal_roots(packed)
        values,catalog=_literal_catalog(roots)
        if catalog is None:return payload
        for (parent,key),value in zip(slots,values):parent[key]=value
        packed[_LITERAL_KEY]=catalog;candidate=deepcopy(payload)
        system=candidate['messages'][0]['content'];tail='\n'+TABLE_GUIDE
        if system.endswith(tail):system=system[:-len(tail)]
        candidate['messages'][0]['content']=system+'\n'+_LITERAL_GUIDE+tail
        candidate['messages'][1]['content']=json.dumps(packed,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        # Compare the entire logical packet after both independent transports.
        decoded=restore_review_data_tables(packed,candidate['messages'][0]['content'])
        expected=(restore_review_data_tables(original,payload['messages'][0]['content'])
            if _collision(original)else original)
        if not _same_json(decoded,expected):return payload
        if len(json.dumps(candidate,ensure_ascii=False).encode())>=len(json.dumps(payload,ensure_ascii=False).encode()):return payload
        return candidate
    except (AttributeError,KeyError,ValueError,TypeError,OverflowError,RecursionError,UnicodeError):
        return payload
