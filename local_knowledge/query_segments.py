"""公共结构检索的本地、确定性问句多视图。

这不是意图或身份解析器。调用者必须保留原始 query 及其身份、许可、时点
门禁；这里只给最多三个附加检索视图，不改写、不翻译、不补省略主语。
queries 仅供本地检索，不能保存或出站；诊断只使用固定政策、计数及原因。
有歧义时保留原查询。有限句法规则会漏拆一些自然语言，这是保守回退。
"""
from __future__ import annotations

import re


POLICY = "public-query-segments/2"
MAX_SEGMENTS = 3
MAX_QUERY_CHARS = 2048
REASONS = frozenset({
    "single_query", "independent_questions", "shared_prefix_questions",
    "ambiguous_dependencies", "segment_limit_exceeded", "unbalanced_delimiters",
    "input_too_long",
})

_PAIRS = {"(": ")", "（": "）", "[": "]", "【": "】", "{": "}",
          "《": "》", "“": "”", "‘": "’", '"': '"', "'": "'"}
_CLOSERS = frozenset(_PAIRS.values())
_QUESTION = re.compile(
    r"是什么|是多少|为何|为什么|如何|何时|何处|是否|能否|可否|有无|有哪些|"
    r"哪(?:个|些|种|家|项|一年|一天|天|月)?|谁|什么|多少|怎么|几(?:个|时|月|年)|"
    r"\b(?:what|when|where|why|which|who|how|is|are|does|do|can|could|will|would)\b", re.I,
)
_REQUEST = re.compile(
    r"^(?:请(?:你|帮我)?\s*)?(?:分别)?(?:说明|解释|介绍|分析|列出|查询|查明|计算|"
    r"比较|核实|给出|判断|找出|评价|总结|描述|梳理|概括|查看|查找|检索)|"
    r"^(?:please\s+)?(?:explain|describe|list|calculate|compare|check|find|summarize)\b", re.I,
)
_INTRO = re.compile(
    r"(?:问题|任务|事项|请(?:你|帮我)?(?:分别)?(?:回答|说明|分析|解释|介绍|查询|核实|给出)|"
    r"我(?:想|要)(?:了解|知道))\s*$|"
    r"^(?:关于|针对|对于|就).+$|"
    r"(?:questions|tasks|please\s+(?:answer|explain|describe))\s*$", re.I,
)
_QUALIFIER = re.compile(
    r"^(?:截至|截止|在|以|按|按照|根据|依据|关于|针对|对于|就|限于|仅针对|仅依据|"
    r"从.+(?:到|至).+(?:期间|范围内)|\d{4}(?:年|[-/]\d{1,2}[-/]\d{1,2})|"
    r"as\s+of\b|according\s+to\b|under\b|for\b|within\b)", re.I,
)
_DEPENDENT = re.compile(
    r"^(?:它|他们|它们|其|该|这些|那些|上述|前者|后者|这(?:个|些)|那(?:个|些)|"
    r"与|和|及|或|相比|较|比|相对于|相较于)|"
    r"^(?:it|its|they|their|this|that|these|those|the\s+former|the\s+latter|"
    r"than|versus|compared\s+with)\b", re.I,
)
_CONDITIONAL = re.compile(
    r"如果|假如|倘若|假设|只要|只有|仅当|仅在|只在|除非|即使|一旦|要是|若(?!干)|"
    r"当.{0,60}(?:时|后|之前|以后)|前提(?:是|下)|(?:满足|符合).{0,40}(?:条件|要求)|"
    r"不要|不得|并非|不包括|不考虑|无需|不能|(?<!是)不是|不应|不允许|"
    r"\b(?:if|unless|once|provided\s+that|assuming|do\s+not|except|excluding)\b", re.I,
)
_CONTINUATION_END = re.compile(r"(?:与|和|及|或|以及|相比|相较|比较|对比|vs\.?|versus)\s*$", re.I)
_COMPARATIVE = re.compile(r"更(?!改|新|正|换)[\u3400-\u9fff]|(?:优|劣|高|低|多|少|快|慢|大|小)于|"
                          r"不如|胜过|相比|相较|\b(?:than|better|worse|cheaper|higher|lower)\b", re.I)
_NUMBERING = re.compile(r"^(?:[（(]?[0-9一二三四五六七八九十]+[）).、．]\s*)")
_LEADING = re.compile(r"^(?:另外|此外|同时|另请|并请|并且|以及|也请|请问|另一个问题是|"
                      r"and\b|also\b|additionally\b)\s*", re.I)
_URL = re.compile(r"https?://[^\s，；。！？]+", re.I)
_ENTITY_WORD = re.compile(r"产品|基金|银行|公司|账户|政策|条例|规则|办法|方案|指数|"
                          r"指标|法规|报告|合同|城市|国家|企业|学校|部门|项目|"
                          r"\b(?:product|policy|rule|bank|fund|company|account|index|report|city)\b", re.I)
_PROPERTY_ONLY = re.compile(r"(?:等级|费用|风险|收益|期限|范围|机构|方式|类型|日期|时间|"
                            r"条件|限制|标准|属性|费率|对象|职责|依据|来源|出处|引文|"
                            r"证据|答案|格式|链接|引用)$")
_ELLIPTIC_ACTION = re.compile(
    r"^(?:(?:最终|最后|随后|之后|接着|再|仍|还|分别|总共|实际|则|将|会|已|已经|正在)\s*)*"
    r"(?:选择|选取|选出|录取|接纳|批准|资助|拨款|申请|申报|增加|减少|完成|启动|确定|"
    r"参与|接受|获得|入选|中选|选中|相关|有关|对应|相应|负责|符合|满足|筛选|执行|"
    r"开展|推出|实施|招募|收到|公布|支付|被|受|获|列入|列在)"
)
_LINKED_QUESTION = re.compile(
    r"是否(?:就|也|因此|由此|能|能够|可以|足以|可能)?(?:意味着|表明|说明|证明|反映|代表|导致)|"
    r"(?:能否|可否)(?:据此|由此|说明|证明|推断)|"
    r"(?:这|那)(?:是否|是不是|就|能够|可以|足以)?(?:意味着|表明|说明|证明)|因此|由此|据此|"
    r"\b(?:imply|implies|implied|therefore|hence)\b|\bdoes\s+this\s+mean\b", re.I,
)
_BACK_REFERENCE = re.compile(r"上述|前述|以上|其中|此类|这些|那些|前者|后者|"
                             r"\b(?:above|previous|aforementioned)\b", re.I)
_TOPIC_MODIFIER = re.compile(r"^(?:新|旧|现行|新版|旧版|公开的|修订后的|已发布的)\s*")
_ACRONYM_TOPIC = re.compile(r"^[A-Z][A-Z0-9_-]{1,}(?![A-Za-z0-9_-])")


def _top_positions(query):
    """忽略引号、括号、URL 和数字内部标点；不完整配对直接回退。"""
    protected = set()
    for match in _URL.finditer(query):
        protected.update(range(match.start(), match.end()))
    stack, positions = [], []
    for index, char in enumerate(query):
        if index in protected:
            continue
        if char == "'" and index > 0 and index + 1 < len(query):
            if query[index - 1].isalnum() and query[index + 1].isalnum():
                continue  # English possessives are not quoted passages.
        if stack and char == stack[-1]:
            stack.pop()
            continue
        if stack and stack[-1] in ('"', "'", "”", "’"):
            continue
        if char in _PAIRS:
            stack.append(_PAIRS[char])
            continue
        if char in _CLOSERS:
            return None
        if stack:
            continue
        if char in "：:，,；;？?":
            if (char in ":,，" and index > 0 and index + 1 < len(query)
                    and query[index - 1].isdigit() and query[index + 1].isdigit()):
                continue
            positions.append((index, char))
    return None if stack else positions


def _parts(body, positions, delimiters, *, keep=False):
    result, start = [], 0
    for index, char in positions:
        if char not in delimiters:
            continue
        result.append(body[start:index + 1 if keep else index].strip())
        start = index + 1
    tail = body[start:].strip()
    if tail:
        result.append(tail)
    return result


def _probe(segment):
    value = _NUMBERING.sub("", segment.strip())
    while True:
        clean = _LEADING.sub("", value, count=1)
        if clean == value:
            return value
        value = clean


def _explicit_topic(value):
    value = value.strip().lstrip("《“\"'")
    value = _TOPIC_MODIFIER.sub("", value, count=1)
    return bool(_ENTITY_WORD.match(value) or _ACRONYM_TOPIC.match(value))


def _subject(value):
    """保守的独立主题线索；仅帮助拆句，绝不证明实体身份。"""
    if not value or _DEPENDENT.match(value):
        return False
    if _ELLIPTIC_ACTION.match(value):
        return False  # 动作/关系从句不是主体，不能从字数或“企业”等宾语补主体。
    value = re.sub(r"^(?:请(?:你|帮我)?|我想知道|我想了解)\s*", "", value)
    value = value.strip(" ，,；;。.!！?？:：")
    value = re.sub(r"(?:是|为|有|应|会|将|能|可以)$", "", value).rstrip()
    if not value or _DEPENDENT.match(value):
        return False
    compact = re.sub(r"[\W_]+", "", value)
    if _PROPERTY_ONLY.search(compact) and not _explicit_topic(value):
        return False  # 通用属性/作答要求不是独立对象，不能继承上一问主体。
    if "的" in value:
        # “项目甲的…”保留了主体；“扶持的企业…”只有关系对象，不能补主体。
        return _explicit_topic(value.split("的", 1)[0])
    if _ACRONYM_TOPIC.match(value):
        return True
    if _explicit_topic(value) and len(compact) >= 3:
        return True
    if _PROPERTY_ONLY.search(compact):
        return False  # 单独属性不能继承上一句的对象，明确主题头除外。
    return False  # 长度不能证明这是名词主语；未知主题留给完整原问句。


def _independent(segment, *, shared_subject):
    value = _probe(segment)
    if not value or _CONTINUATION_END.search(value.rstrip("?？。.!！")):
        return False
    if _LINKED_QUESTION.search(value) or _BACK_REFERENCE.search(value):
        return False
    if _DEPENDENT.match(value) and not shared_subject:
        return False
    question = _QUESTION.search(value)
    request = _REQUEST.match(value)
    if question is None and re.search(r"[吗么]\s*[?？]?$", value):
        return shared_subject or _subject(value.rstrip("?？").rstrip("吗么"))
    if question is None and request is None:
        return False
    if shared_subject:
        return True  # 原文中明确给出的共同主题仍被完整复制到每一视图。
    if request is not None:
        return _subject(value[request.end():])
    before = value[:question.start()]
    if _subject(before):
        return True
    # 谁/什么/which 起句时，必须还有明确主题，不能靠“它/该产品”补主体。
    after = value[question.end():].lstrip()
    after = re.sub(r"^(?:是|为|管理|负责|属于|is\b|are\b|does\b|do\b)\s*", "", after, flags=re.I)
    return _subject(after)


def _prefix(query, positions):
    start, last = 0, None
    for index, char in positions:
        if char in "？?；;":
            break
        if char in "：:" and _INTRO.search(query[:index].strip()):
            prefix = query[:index + 1]
            shared = bool(re.search(r"(?:关于|针对|对于|就).+|.+的(?:以下|下列)?(?:问题|任务|事项)", prefix))
            return prefix, query[index + 1:], shared
    # 没有冒号时只搬运明确限定前缀；不搬运第一个问句的主题。
    for index, char in positions:
        if char not in "，,":
            if char in "？?；;":
                break
            continue
        leading = query[start:index].strip()
        if _QUALIFIER.match(leading) and not _QUESTION.search(leading) and not _REQUEST.match(leading):
            last, start = index, index + 1
            continue
        break
    if last is not None:
        return query[:last + 1], query[last + 1:], False
    return "", query, False


def _result(query, reason, segments=None):
    queries = [query] if segments is None else segments
    return {"queries": queries, "policy": POLICY, "used": segments is not None,
            "count": len(queries), "reason": reason}


def build_query_segments(query):
    """最多返回三个原文问题；默认仅返回未改写的原 query。

    多视图不能取代原始查询的门禁。查询原文仅在返回的 queries 中供本地
    使用；policy/count/reason/used 是唯一可记录的诊断。
    """
    if not isinstance(query, str):
        raise ValueError("invalid_query_segments_input")
    if len(query) > MAX_QUERY_CHARS:
        return _result(query, "input_too_long")
    positions = _top_positions(query)
    if positions is None:
        return _result(query, "unbalanced_delimiters")
    prefix, body, shared_subject = _prefix(query, positions)
    body_positions = _top_positions(body)
    candidates = [
        _parts(body, body_positions, "？?", keep=True),
        _parts(body, body_positions, "，,；;"),
    ]
    possible = False
    for segments in candidates:
        if len(segments) < 2:
            continue
        possible = True
        if _CONDITIONAL.search(query) or _COMPARATIVE.search(query):
            return _result(query, "ambiguous_dependencies")
        if all(_independent(part, shared_subject=shared_subject) for part in segments):
            if len(segments) > MAX_SEGMENTS:
                return _result(query, "segment_limit_exceeded")
            views = [prefix + part for part in segments]
            if len(set(views)) < 2:
                return _result(query, "single_query")
            return _result(query, "shared_prefix_questions" if prefix else "independent_questions", views)
    return _result(query, "ambiguous_dependencies" if possible else "single_query")
