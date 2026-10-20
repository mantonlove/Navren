"""原文句段的确定性派生检索单元；单元本身不构成新的引文。

仅使用 Python 字符位置和原文标点，不使用模型、网络或数据库写入。调用方
必须在离线建索引时提供已发布的 canonical chunks，在查询时重新校验全部
映射引文的许可、效期、身份和图路径。任一映射引文失效，整个单元不可用。

分句是有界启发式，父段保留句间原文。预算拆段会显式标记 continuation
和 issue；不会以改写、补词或无提示截断来制造完整语句。
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Set
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable


POLICY_ID = 'sentence-parent-units/1'
MAX_TEXT = 2_000_000
MAX_CANONICAL_CHUNKS = 5000
_AUTO_LOCATOR = re.compile(r'chars:(\d+)-(\d+)\Z')
_PARAGRAPH_BREAK = re.compile(r'\r?\n[ \t\r]*\n(?:[ \t\r]*\n)*')
_ABBREVIATIONS = frozenset(('mr.', 'mrs.', 'ms.', 'dr.', 'prof.', 'sr.', 'jr.',
                            'st.', 'vs.', 'no.', 'fig.', 'approx.', 'e.g.',
                            'i.e.', 'u.s.', 'u.k.', 'a.m.', 'p.m.'))
_CLOSERS = frozenset('”’\"\'）)]}»')


class UnitBuildError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BuildIssue:
    code: str
    start_char: int
    end_char: int


@dataclass(frozen=True)
class RetrievalUnit:
    id: str
    kind: str
    version_id: str
    start_char: int
    end_char: int
    text: str
    citation_ids: tuple[str, ...]
    boundary_key: str
    parent_id: str | None = None
    continuation: bool = False


@dataclass(frozen=True)
class UnitBuild:
    parents: tuple[RetrievalUnit, ...]
    children: tuple[RetrievalUnit, ...]
    issues: tuple[BuildIssue, ...]
    policy_id: str
    version_id: str
    text_sha256: str


@dataclass(frozen=True)
class _Canonical:
    id: str
    start: int
    end: int
    boundary: str


def _packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _row_dict(row):
    # sqlite3.Row provides keys()/subscription but is not a Mapping subclass.
    try:
        return {key: row[key] for key in row.keys()}
    except (AttributeError, KeyError, TypeError):
        raise UnitBuildError('invalid_canonical_row') from None


def _annotations(value, *, empty_allowed):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise UnitBuildError('invalid_canonical_annotations') from None
    if (not isinstance(value, (list, tuple)) or len(value) > 8
            or (not empty_allowed and not value)
            or any(not isinstance(item, str) or not item for item in value)):
        raise UnitBuildError('invalid_canonical_annotations')
    # Order cannot add authority. Exact strings remain exact, without stemming.
    return sorted(set(value))


def _canonical_rows(text, rows, version_id):
    collected = []
    for row in rows:
        if len(collected) >= MAX_CANONICAL_CHUNKS:
            raise UnitBuildError('canonical_chunk_limit')
        collected.append(_row_dict(row))
    rows = collected
    if not rows:
        raise UnitBuildError('canonical_chunk_limit')
    if any(not isinstance(row.get('version_id'), str) or not row['version_id']
           for row in rows):
        raise UnitBuildError('mixed_document_versions')
    versions = {row.get('version_id') for row in rows}
    if len(versions) != 1:
        raise UnitBuildError('mixed_document_versions')
    actual_version = next(iter(versions))
    if version_id is not None and version_id != actual_version:
        raise UnitBuildError('document_version_mismatch')
    output = []
    ids = set()
    required = ('id', 'start_char', 'end_char', 'excerpt', 'paragraph', 'page',
                'product_id', 'share_class_id', 'channel_id', 'facets',
                'conditions', 'data_on')
    for row in rows:
        if any(key not in row for key in required):
            raise UnitBuildError('canonical_metadata_missing')
        start, end, cid = row['start_char'], row['end_char'], row['id']
        if (not isinstance(cid, str) or not cid or cid in ids
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(text) or end - start > 1200):
            raise UnitBuildError('invalid_canonical_range')
        if row['excerpt'] != text[start:end] or not row['excerpt'].strip():
            raise UnitBuildError('canonical_quote_mismatch')
        if ('excerpt_sha256' in row
                and row['excerpt_sha256'] != _sha(row['excerpt'])):
            raise UnitBuildError('canonical_quote_hash_mismatch')
        if (not isinstance(row['paragraph'], str) or not row['paragraph']
                or (row['page'] is not None
                    and (type(row['page']) is not int or row['page'] < 1))):
            raise UnitBuildError('invalid_canonical_locator')
        for key in ('product_id', 'share_class_id', 'channel_id', 'data_on'):
            if row[key] is not None and not isinstance(row[key], str):
                raise UnitBuildError('invalid_canonical_metadata')
        locator = _AUTO_LOCATOR.fullmatch(row['paragraph'])
        is_auto = (locator is not None
                   and tuple(map(int, locator.groups())) == (start, end))
        boundary = _packed({
            'version_id': actual_version,
            'product_id': row['product_id'], 'share_class_id': row['share_class_id'],
            'channel_id': row['channel_id'], 'page': row['page'],
            'facets': _annotations(row['facets'], empty_allowed=False),
            'conditions': _annotations(row['conditions'], empty_allowed=True),
            'data_on': row['data_on'],
            # Automatic 1000-character locators are not semantic boundaries.
            # Reviewed field/PDF/paragraph locators remain strict boundaries.
            'explicit_paragraph': None if is_auto else row['paragraph'],
            **{key: row[key] for key in ('parser_id', 'shape', 'source_id') if key in row},
        })
        output.append(_Canonical(cid, start, end, boundary))
        ids.add(cid)
    return actual_version, sorted(output, key=lambda row: (row.start, row.end, row.id))


def _domains(rows):
    """Union only continuous, identically annotated canonical coverage.

    Different annotations on overlapping spans have no safe automatic choice.
    The affected full canonical chunks must be excluded: returning a safe slice
    mapped to a larger, ambiguous original citation would reintroduce the gap.
    """
    events = {}
    for index, row in enumerate(rows):
        events.setdefault(row.start, [set(), set()])[0].add(index)
        events.setdefault(row.end, [set(), set()])[1].add(index)
    points = sorted(events)
    active = set()
    domains = []
    issues = []
    ambiguous = set()
    for i, start in enumerate(points[:-1]):
        added, removed = events[start]
        active.difference_update(removed)
        active.update(added)
        end = points[i + 1]
        if not active:
            continue
        keys = {rows[index].boundary for index in active}
        if len(keys) != 1:
            issues.append(BuildIssue('ambiguous_canonical_overlap', start, end))
            ambiguous.update(active)
            continue
        key = next(iter(keys))
        if domains and domains[-1][1] == start and domains[-1][2] == key:
            domains[-1] = (domains[-1][0], end, key)
        else:
            domains.append((start, end, key))
    return domains, issues, {rows[index].id for index in ambiguous}


def _trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _paragraphs(text, start, end):
    cursor = start
    for match in _PARAGRAPH_BREAK.finditer(text, start, end):
        left, right = _trim(text, cursor, match.start())
        if left < right:
            yield left, right
        cursor = match.end()
    left, right = _trim(text, cursor, end)
    if left < right:
        yield left, right


def _period_ends_sentence(text, index, end):
    previous = text[index - 1] if index else ''
    following = text[index + 1] if index + 1 < end else ''
    if previous.isdigit() and following.isdigit():
        return False
    if following and (following.isalnum() or following in '.:/_-'):
        return False
    tail = re.search(r'[A-Za-z.]+\Z', text[max(0, index - 20):index + 1])
    if tail:
        token = tail.group().casefold()
        if token in _ABBREVIATIONS:
            return False
        # Personal initials and dotted acronyms, without guessing their meaning.
        if re.fullmatch(r'(?:[a-z]\.)+', token):
            return False
    return True


def _sentence_spans(text, start, end):
    cursor, index = start, start
    while index < end:
        char = text[index]
        ends = (char in '。！？!?；;' or char == '\n'
                or (char == '.' and _period_ends_sentence(text, index, end)))
        if ends:
            stop = index + 1
            while stop < end and text[stop] in _CLOSERS:
                stop += 1
            left, right = _trim(text, cursor, stop)
            if left < right:
                yield left, right
            cursor, index = stop, stop
        else:
            index += 1
    left, right = _trim(text, cursor, end)
    if left < right:
        yield left, right


def _bounded_sentence(text, start, end, maximum):
    if end - start <= maximum:
        yield start, end, False
        return
    cursor = start
    while cursor < end:
        stop = min(end, cursor + maximum)
        if stop < end:
            # Prefer whitespace or a clause boundary in the last quarter. Exact
            # offsets and continuation still disclose that this is a fragment.
            floor = cursor + maximum * 3 // 4
            for index in range(stop - 1, floor - 1, -1):
                if text[index].isspace() or text[index] in '，、,:：—-':
                    stop = index + 1
                    break
        left, right = _trim(text, cursor, stop)
        if left < right:
            yield left, right, True
        cursor = stop


def _citations(rows, prefix_ends, start, end, boundary):
    index = bisect_right(prefix_ends, start)
    result = []
    while index < len(rows) and rows[index].start < end:
        row = rows[index]
        if row.end > start and row.boundary == boundary:
            result.append(row.id)
        index += 1
    return tuple(result)


def _unit(kind, version, text, start, end, citations, boundary,
          *, parent_id=None, continuation=False):
    body = text[start:end]
    identity = _packed([POLICY_ID, kind, version, boundary, start, end,
                        _sha(body), citations, parent_id, continuation])
    return RetrievalUnit('ru-' + _sha(identity), kind, version, start, end, body,
                         citations, boundary, parent_id, continuation)


def build_units(parsed_text, canonical_rows: Iterable, *, version_id=None,
                max_child_chars=480, max_parent_chars=1600, max_units=20000):
    """Build exact child/parent text slices from one immutable version.

    All rows must carry the complete canonical annotations. Missing coverage is
    never filled from parsed_text alone. The returned issues are part of the
    build result; exceeding the total unit limit raises, rather than publishing
    a silently incomplete derivative. Parent IDs are distinct from citations.
    """
    if (not isinstance(parsed_text, str) or not parsed_text.strip()
            or len(parsed_text) > MAX_TEXT):
        raise UnitBuildError('invalid_parsed_text')
    if (type(max_child_chars) is not int or type(max_parent_chars) is not int
            or type(max_units) is not int or not 16 <= max_child_chars <= 1200
            or not max_child_chars <= max_parent_chars <= 6400
            or not 1 <= max_units <= 50000):
        raise UnitBuildError('invalid_unit_budget')
    version, rows = _canonical_rows(parsed_text, canonical_rows, version_id)
    domains, issues, ambiguous_ids = _domains(rows)
    if ambiguous_ids:
        issues.extend(BuildIssue('ambiguous_canonical_chunk', row.start, row.end)
                      for row in rows if row.id in ambiguous_ids)
        rows = [row for row in rows if row.id not in ambiguous_ids]
        domains, _, _ = _domains(rows)
    # Annotation boundaries and absent canonical coverage are distinct from
    # natural sentence/paragraph endings, so disclose them in the build.
    cursor = 0
    for start, end, boundary in domains:
        if start > cursor and parsed_text[cursor:start].strip():
            issues.append(BuildIssue('canonical_coverage_gap', cursor, start))
        if cursor == start and start > 0:
            issues.append(BuildIssue('canonical_boundary_split', start, start))
        cursor = end
    if cursor < len(parsed_text) and parsed_text[cursor:].strip():
        issues.append(BuildIssue('canonical_coverage_gap', cursor, len(parsed_text)))
    prefix_ends, maximum_end = [], 0
    for row in rows:
        maximum_end = max(maximum_end, row.end)
        prefix_ends.append(maximum_end)
    parents, children = [], []
    for domain_start, domain_end, boundary in domains:
        for para_start, para_end in _paragraphs(parsed_text, domain_start, domain_end):
            fragments = []
            for start, end in _sentence_spans(parsed_text, para_start, para_end):
                if end - start > max_child_chars:
                    issues.append(BuildIssue('child_budget_split', start, end))
                fragments.extend(_bounded_sentence(parsed_text, start, end, max_child_chars))
            groups, group = [], []
            for fragment in fragments:
                if group and fragment[1] - group[0][0] > max_parent_chars:
                    groups.append(group)
                    group = []
                group.append(fragment)
            if group:
                groups.append(group)
            if len(groups) > 1:
                issues.append(BuildIssue('parent_budget_split', para_start, para_end))
            domain_fragment = (domain_start > 0 or domain_end < len(parsed_text))
            for group in groups:
                start, end = group[0][0], group[-1][1]
                citations = _citations(rows, prefix_ends, start, end, boundary)
                parent = _unit('parent', version, parsed_text, start, end, citations,
                               boundary, continuation=len(groups) > 1 or domain_fragment)
                parents.append(parent)
                for child_start, child_end, continued in group:
                    citations = _citations(rows, prefix_ends, child_start, child_end, boundary)
                    children.append(_unit('child', version, parsed_text, child_start,
                                          child_end, citations, boundary,
                                          parent_id=parent.id,
                                          continuation=continued or domain_fragment))
                if len(parents) + len(children) > max_units:
                    raise UnitBuildError('unit_limit_exceeded')
    # Stable ordering prevents SQL input order or set iteration affecting hashes.
    issues = tuple(sorted(set(issues), key=lambda item: (item.start_char, item.end_char, item.code)))
    return UnitBuild(tuple(parents), tuple(children), issues, POLICY_ID, version,
                     _sha(parsed_text))


def eligible_citations(unit, eligible_ids):
    """Return the original mapping only when every mapped citation is eligible.

    This helper does not verify authority itself. eligible_ids must come from
    the caller's same-release, same-time permission/identity/graph checks.
    """
    if (not isinstance(unit, RetrievalUnit) or not isinstance(eligible_ids, Set)
            or not unit.citation_ids
            or not all(cid in eligible_ids for cid in unit.citation_ids)):
        return ()
    return unit.citation_ids
