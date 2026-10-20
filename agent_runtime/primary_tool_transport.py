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
