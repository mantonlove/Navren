"""从公共原件派生精确的、纯书目指针范围；不推断财务事实。

第三版刻意只支持独立、无主题的英文 ``For more/further information, see``。
任何 on/about 或其他自由主题均不分类，以免将主题内的事实和条件吞入角色。
每个条目必须同时具备带明确 initials 的个人作者、括号年份、成对引号标题、明确的引用日期
和句点终止。尾部仅支持固定出版类型、明确的编号/卷号/页码，或显式的
Publisher/Journal/Series/Proceedings 标签加配对引号名称。不能凭大写猜测
裸机构名称、刊物名称或其他自由字段；这类常见但不明确的格式也保留原文。
一般的 See、法律交叉引用、引文旁的事实句以及不明确的结尾均不分类。
调用者仍负责来源、版本、许可及事实门禁，角色不是事实或机构身份的证明。

范围是原始 Python 字符串的半开 [start, end)，不做 Unicode/空白归一化。
输出只含固定政策、固定角色和整数范围；不输出原文、主题或推断摘要。
"""
from __future__ import annotations

import datetime as _datetime
import re


POLICY = "public-reference-pointer-roles/3"
ROLE = "reference_pointer"
MAX_TEXT_CHARS = 2_000_000
MAX_POINTER_CHARS = 8192
MAX_HEADER_CHARS = 800
MAX_AUTHOR_CHARS = 240
MAX_TITLE_CHARS = 1200
MAX_ENTRY_TAIL_CHARS = 1800
MAX_ENTRIES_PER_POINTER = 16
MAX_SPANS = 512

_START = re.compile(r"(?<!\w)For\s+(?:more|further)\s+information\b", re.I)
_SEE = re.compile(r"\s*(?:please\s+)?see\b\s+", re.I)
_YEAR = re.compile(r"\(([12]\d{3})(?:[a-z])?\)", re.I)
_QUOTES = {'"': '"', "“": "”", "‘": "’", "'": "'"}
_PAIRS = {**_QUOTES, "(": ")", "[": "]", "{": "}"}
_CLOSERS = frozenset(_PAIRS.values())
# 不用 TitleCase 兜底或谓词黑名单猜测名称。作者必须符合个人姓名语法；
# 自由机构名称和其他多词大写段落均不分类，宁可漏识别。
_NAME_ATOM = r"[^\W\d_]+(?:[-'’][^\W\d_]+)*"
_NAME_PARTICLES = r"(?:(?:de|del|van|von|der|da|di|du|la|le)\s+)*"
_INITIALS = r"[A-Z]\.(?:-?[A-Z]\.)*(?:\s+[A-Z]\.(?:-?[A-Z]\.)*)*"
_PERSON = re.compile(
    r"(?:" + _INITIALS + r"\s+" + _NAME_PARTICLES + r"(?P<first>" + _NAME_ATOM + r")|"
    + _NAME_PARTICLES + r"(?P<last>" + _NAME_ATOM + r")(?:\s*,?\s*" + _INITIALS + r"))",
    re.UNICODE,
)
_AUTHOR_SEPARATOR = re.compile(r"\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)")
_ET_AL = re.compile(r"\s+et\s+al\.")
_PUBLICATION_TYPES = frozenset({
    "working paper", "working paper series", "discussion paper", "discussion paper series",
    "research paper", "research bulletin", "bulletin", "research report", "report",
    "journal", "newsletter", "speech", "interview", "press release", "blog post",
    "conference paper", "proceedings", "research note", "technical note", "technical report",
    "policy brief",
})
_NAMED_PUBLICATION = re.compile(r"(?:publisher|journal|series|proceedings)\s*:\s*", re.I)
_ABBREVIATIONS = frozenset({
    "no", "nos", "vol", "pp", "p", "ed", "eds", "edn", "rev", "jr", "sr",
    "dr", "prof", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "al",
})
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_PATTERN = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DATE_PATTERNS = (
    re.compile(r"(?<![\w-])(?P<year>[12]\d{3})-(?P<month>\d{2})-(?P<day>\d{2})\s*$"),
    re.compile(r"(?<!\w)(?P<day>\d{1,2})\s+(?P<month>" + _MONTH_PATTERN +
               r")(?:\s+(?P<year>[12]\d{3}))?\s*$", re.I),
    re.compile(r"(?<!\w)(?P<month>" + _MONTH_PATTERN + r")\s+(?P<day>\d{1,2})"
               r"(?:\s*,?\s+(?P<year>[12]\d{3}))?\s*$", re.I),
    re.compile(r"(?<!\w)(?P<month>" + _MONTH_PATTERN + r")\s+(?P<year>[12]\d{3})\s*$", re.I),
)
_NUMBER_FIELD = re.compile(
    r"\b(?:nos?\.?|vol(?:ume)?\.?|issue|number|pp?\.?|pages?|edition)\s*"
    r"\d+(?:\s*[-–—/]\s*\d+)*", re.I,
)


def _apostrophe(text, index):
    if text[index] not in ("'", "’") or index == 0 or index + 1 >= len(text):
        return False
    if text[index - 1].isalnum() and text[index + 1].isalnum():
        return True
    # participants' choices：只处理明确的复数所有格，不把任意引号当撇号。
    if text[index - 1] in "sS" and text[index + 1].isspace():
        following = index + 1
        while following < len(text) and text[following].isspace():
            following += 1
        return following < len(text) and text[following].islower()
    return False


def _standalone_start(text, start):
    previous = start - 1
    while previous >= 0 and text[previous].isspace():
        previous -= 1
    if previous < 0:
        return True
    if text[previous] not in ".!?":
        return False
    if text[previous] == "." and _abbreviation_at(text, previous):
        return False
    return start > 0 and text[start - 1].isspace()


def _candidate_starts(text):
    """被引号/括号包裹的指针属于引文或条件，不能视作独立元信息。"""
    positions = {match.start() for match in _START.finditer(text)}
    if not positions:
        return []
    found, stack = [], []
    for index, char in enumerate(text):
        if index in positions and not stack and _standalone_start(text, index):
            found.append(index)
        if _apostrophe(text, index):
            continue
        if stack and char == stack[-1]:
            stack.pop()
            continue
        if stack and stack[-1] in ('"', "'", "”", "’"):
            continue
        if char in _PAIRS:
            stack.append(_PAIRS[char])
        elif char in _CLOSERS:
            # 无配对的右引号/括号意味着无法确认后续的引用上下文。
            stack.append("\0")
    return found


def _skip_space(text, position, limit):
    while position < limit and text[position].isspace():
        position += 1
    return position


def _header_end(text, start, limit):
    header_limit = min(limit, start + MAX_HEADER_CHARS)
    match = _START.match(text, start, header_limit)
    if match is None:
        return None
    position = _skip_space(text, match.end(), header_limit)
    if position >= header_limit or text[position] != ",":
        return None  # 自由主题/从句一律 unknown，不按主题词或谓词列表猜测。
    see = _SEE.match(text, position + 1, header_limit)
    return see.end() if see is not None else None


def _personal_authors(value):
    position = 0
    count = 0
    while position < len(value) and count < MAX_ENTRIES_PER_POINTER:
        match = _PERSON.match(value, position)
        if match is None:
            return False
        surname = match.group("first") or match.group("last")
        if len(surname) < 2 or not surname[0].isupper():
            return False
        position = match.end()
        count += 1
        et_al = _ET_AL.match(value, position)
        if et_al:
            position = et_al.end()
        if position == len(value):
            return True
        separator = _AUTHOR_SEPARATOR.match(value, position)
        if separator is None:
            return False
        position = separator.end()
    return False


def _metadata_fields(value):
    fields, stack, start = [], [], 0
    for index, char in enumerate(value):
        if _apostrophe(value, index):
            continue
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
        if char == "," and not stack:
            fields.append(value[start:index].strip())
            start = index + 1
    if stack:
        return None
    fields.append(value[start:].strip())
    return fields if all(fields) else None


def _publication_metadata(value):
    if not value:
        return True
    fields = _metadata_fields(value)
    if fields is None:
        return False
    for index, field in enumerate(fields):
        normalized = " ".join(field.lower().split())
        if normalized in _PUBLICATION_TYPES or _NUMBER_FIELD.fullmatch(field):
            continue
        if normalized in {"on", "accessed", "accessed on", "retrieved", "retrieved on"}:
            # 只能作为最后一个明确的引用日期标签。
            if index != len(fields) - 1:
                return False
            continue
        named = _NAMED_PUBLICATION.match(field)
        if named and _quoted_title_end(field, named.end(), len(field)) == len(field):
            continue
        # 无标记专有名词、任意大写字段、裸机构/刊物名均无法证明是元信息。
        return False
    return True


def _quoted_title_end(text, start, limit):
    opener = text[start] if start < limit else None
    if opener not in _QUOTES:
        return None
    stack = [_QUOTES[opener]]
    for index in range(start + 1, min(limit, start + MAX_TITLE_CHARS + 2)):
        char = text[index]
        if _apostrophe(text, index):
            continue
        if char == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1 if text[start + 1:index].strip() else None
            continue
        if char in _QUOTES and char != opener:
            stack.append(_QUOTES[char])
        elif char in ("”", "’"):
            return None
    return None


def _abbreviation_at(text, dot):
    start = dot - 1
    while start >= 0 and text[start].isalpha():
        start -= 1
    word = text[start + 1:dot]
    if not word:
        return False
    return ((len(word) == 1 and word.isupper()) or word.lower() in _ABBREVIATIONS)


def _dated_metadata(value, citation_year):
    for pattern in _DATE_PATTERNS:
        match = pattern.search(value)
        if match is None:
            continue
        groups = match.groupdict()
        month_text = groups["month"].rstrip(".").lower()
        month = int(month_text) if month_text.isdigit() else _MONTHS[month_text]
        year = int(groups.get("year") or citation_year)
        day = int(groups.get("day") or 1)
        try:
            _datetime.date(year, month, day)
        except ValueError:
            continue
        raw_prefix = value[:match.start()].rstrip()
        prefix = raw_prefix.rstrip(",").strip()
        # 日期单独占一个元信息字段；不能将无效“31 February 2024”降级为
        # “February 2024”，也不能在较大数字或随后事实句里寻找日期。
        if raw_prefix and not raw_prefix.endswith(","):
            last_field = raw_prefix.rsplit(",", 1)[-1].strip().lower()
            if last_field not in {"on", "accessed", "accessed on", "retrieved", "retrieved on"}:
                continue
        if _publication_metadata(prefix):
            return True
    return False


def _entry_end(text, start, limit):
    start = _skip_space(text, start, limit)
    year_match = _YEAR.search(text, start, min(limit, start + MAX_AUTHOR_CHARS))
    if year_match is None:
        return None
    if not _personal_authors(text[start:year_match.start()].strip()):
        return None
    position = _skip_space(text, year_match.end(), limit)
    if position >= limit or text[position] != ",":
        return None
    position = _skip_space(text, position + 1, limit)
    title_end = _quoted_title_end(text, position, limit)
    if title_end is None:
        return None
    position = _skip_space(text, title_end, limit)
    if position >= limit or text[position] != ",":
        return None
    tail_start = position + 1
    stack = []
    for index in range(tail_start, min(limit, tail_start + MAX_ENTRY_TAIL_CHARS)):
        char = text[index]
        if _apostrophe(text, index):
            continue
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
        if char in "!?":
            return None
        if char in ".;" and not (char == "." and _abbreviation_at(text, index)):
            if _dated_metadata(text[tail_start:index], year_match.group(1)):
                return index + 1, char
            return None
    return None


def _pointer_end(text, start):
    limit = min(len(text), start + MAX_POINTER_CHARS)
    position = _header_end(text, start, limit)
    if position is None:
        return None
    last_complete = None
    for _ in range(MAX_ENTRIES_PER_POINTER):
        entry = _entry_end(text, position, limit)
        if entry is None:
            return last_complete
        end, delimiter = entry
        if delimiter == ".":
            last_complete = end
        position = _skip_space(text, end, limit)
        # 分号连接的条目必须完整闭合；不能在不明确条目前半截分类。
        if delimiter == ";":
            last_complete = None
            conjunction = re.match(r"and\s+", text[position:limit], re.I)
            if conjunction:
                position += conjunction.end()
            continue
        # 句点后仅继续完整的独立书目条目；普通正文会在作者语法处失败。
        if position >= limit:
            return last_complete
    return None  # 超限代表无法确认整个引用列表的结束位置。


def detect_reference_roles(text):
    """返回受限派生角色；不明确、超限或非字符串输入返回空范围。"""
    result = {"policy": POLICY, "spans": []}
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
        return result
    previous_end = 0
    for start in _candidate_starts(text):
        if start < previous_end:
            continue
        end = _pointer_end(text, start)
        if end is None or end <= start:
            continue
        result["spans"].append({"role": ROLE, "start": start, "end": end})
        previous_end = end
        if len(result["spans"]) == MAX_SPANS:
            break
    return result
