"""受控公共机构范围与来源覆盖；只重排已准入的市场原文候选。

元数据是固定政策，不从发布者、国家简称或模型输出猜测机构。这里不召回、
不改分数、不决定事实支持，也不替代权限、时点、原件和图路径核验。
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
import re
from urllib.parse import urlsplit

from .vector_documents import representation


POLICY = 'public-institution-coverage/2'
_COMPARISON = re.compile(r'比较|对比|相比|区别|各自|分别|不同|compare|versus', re.I)
_CONNECTORS = re.compile(r'与|和|及')
_COMPOUND = re.compile(r'(?P<codes>[\u3400-\u9fff]+)央行')
_DNS_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', re.I | re.ASCII)
_COUNTRIES = (
    ('中', 'CN'), ('美', 'US'), ('英', 'GB'), ('日', 'JP'),
    ('德', 'DE'), ('法', 'FR'), ('俄', 'RU'),
)


@dataclass(frozen=True)
class _Institution:
    institution_id: str
    country_code: str
    role: str | None
    publisher_aliases: tuple[str, ...]
    query_aliases: tuple[str, ...]
    allowed_source_domains: tuple[str, ...]
    evidence_urls: tuple[str, ...]


_INSTITUTIONS = (
    _Institution(
        'FEDERAL_RESERVE', 'US', 'central_bank',
        ('美联储', '美国联邦储备系统', 'Federal Reserve', 'Federal Reserve System'),
        ('美联储', '美国央行', '美国联邦储备系统', '联邦储备系统',
         'Federal Reserve System', 'Federal Reserve'),
        ('federalreserve.gov',),
        ('https://www.federalreserve.gov/aboutthefed.htm',),
    ),
    _Institution(
        'PEOPLE_BANK_OF_CHINA', 'CN', 'central_bank',
        ('中国人民银行', "People's Bank of China", 'People’s Bank of China'),
        ('中国人民银行', '中国央行', "People's Bank of China", 'People’s Bank of China'),
        ('pbc.gov.cn',),
        ('https://www.pbc.gov.cn/rmyh/105208/2025080818581331589/2025032615440044720.pdf',
         'https://www.npc.gov.cn/zgrdw/npc/flsyywd/jingji/2004-10/26/content_337725.htm'),
    ),
    _Institution(
        'EUROPEAN_CENTRAL_BANK', 'euro_area', 'central_bank',
        ('欧洲央行', '欧洲中央银行', 'European Central Bank', 'ECB'),
        ('欧洲央行', '欧洲中央银行', '欧元区央行', 'European Central Bank', 'ECB'),
        ('ecb.europa.eu',),
        ('https://www.ecb.europa.eu/ecb/orga/escb/html/index.en.html',),
    ),
    # 此项只登记现有 canonical publisher，不赋予中央银行角色。
    _Institution('NATIONAL_BUREAU_OF_STATISTICS', 'CN', None,
                 ('国家统计局',), ('国家统计局',), ('stats.gov.cn',), ()),
)


def institution_policy_metadata():
    """返回可冻结的元数据副本；调用方不能修改运行政策。"""
    return {
        'policy': POLICY,
        'authority': 'source_coverage_only',
        'country_short_names': dict(_COUNTRIES),
        'institutions': [
            {'institution_id': item.institution_id, 'country_code': item.country_code,
             'role': item.role, 'publisher_aliases': list(item.publisher_aliases),
             'query_aliases': list(item.query_aliases),
             'allowed_source_domains': list(item.allowed_source_domains),
             'evidence_urls': list(item.evidence_urls)}
            for item in _INSTITUTIONS
        ],
    }


def _alias_pattern(alias):
    # ASCII 名称必须是完整词，避免 ECBank 等子串误绑。
    edge_left = r'(?<![A-Za-z0-9_])' if alias[0].isascii() else ''
    edge_right = r'(?![A-Za-z0-9_])' if alias[-1].isascii() else ''
    return re.compile(edge_left + re.escape(alias) + edge_right, re.I)


def resolve_public_institutions(query):
    """只解析明确机构名或已登记角色下的完整国别缩写组合。

    无法确定整个组合时全范围回退；不猜测未知前缀后的国家简称后缀。
    输出只有公共政策信息和枚举原因，不包含原问题或其文本片段。
    """
    result = {'policy': POLICY, 'authority': 'source_coverage_only', 'comparison_intent': False,
              'institution_ids': [], 'compound_used': False, 'ready': False,
              'reason': 'invalid_query'}
    if not isinstance(query, str):
        return result
    result['comparison_intent'] = bool(_COMPARISON.search(query))
    spans = []
    for item in _INSTITUTIONS:
        for alias in item.query_aliases:
            for match in _alias_pattern(alias).finditer(query):
                spans.append((match.start(), match.end(), item.institution_id))
    spans.sort(key=lambda span: (span[0], -span[1], span[2]))
    for index, (start, end, iid) in enumerate(spans):
        if any(other_start < end and start < other_end and iid != other_id
               for other_start, other_end, other_id in spans[index + 1:]):
            result['reason'] = 'ambiguous_institution_alias'
            return result
    ids = list(dict.fromkeys(span[2] for span in spans))
    masked = list(query)
    for start, end, _ in spans:
        masked[start:end] = ' ' * (end - start)
    parse_copy = _CONNECTORS.sub(' ', _COMPARISON.sub(' ', ''.join(masked)))
    countries = dict(_COUNTRIES)
    for match in _COMPOUND.finditer(parse_copy):
        codes = match.group('codes')
        if '欧' in codes:
            result['reason'] = 'ambiguous_country_compound'
            return result
        if any(code not in countries for code in codes):
            result['reason'] = 'unregistered_country_compound'
            return result
        if len(codes) < 2 or len(set(codes)) != len(codes):
            result['reason'] = 'invalid_country_compound'
            return result
        compound_ids = []
        for code in codes:
            registered = [item.institution_id for item in _INSTITUTIONS
                          if item.country_code == countries[code] and item.role == 'central_bank']
            if len(registered) != 1:
                result['reason'] = 'unregistered_country_role'
                return result
            compound_ids.append(registered[0])
        ids.extend(iid for iid in compound_ids if iid not in ids)
        result['compound_used'] = True
    result['institution_ids'] = ids
    if not result['comparison_intent']:
        result['reason'] = 'comparison_intent_missing'
    elif len(ids) < 2:
        result['reason'] = 'multiple_explicit_institutions_missing'
    else:
        result.update(ready=True, reason=None)
    return result


def _get(row, key, default=None):
    try:
        return row[key] if key in row.keys() else default
    except (AttributeError, KeyError, TypeError, IndexError):
        return default


def _has(row, key):
    try:
        return key in row.keys()
    except (AttributeError, TypeError):
        return False


def _official_origin(source_url, domains):
    if not isinstance(source_url, str) or any(char.isspace() for char in source_url):
        return False
    try:
        url = urlsplit(source_url)
        hostname = url.hostname
        # urlsplit 不校验 DNS 标签，也会把显式空端口视为 None。
        # 原始 URL 必须是可解释的官方主机，不能只靠字符串后缀取得路由资格。
        if (hostname is None or len(hostname) > 253 or url.netloc.endswith(':')
                or any(not _DNS_LABEL.fullmatch(label) for label in hostname.split('.'))):
            return False
        return (url.scheme == 'https' and url.username is None and url.password is None
                and url.port in (None, 443)
                and any(hostname == domain or hostname.endswith('.' + domain)
                        for domain in domains))
    except (ValueError, TypeError):
        return False


def _publisher_institution(publisher, source_url):
    if not isinstance(publisher, str):
        return None, False
    normalized = publisher.strip().casefold()
    exact = [item for item in _INSTITUTIONS
             if normalized in {alias.casefold() for alias in item.publisher_aliases}]
    if len(exact) == 1:
        item = exact[0]
        return (item.institution_id if _official_origin(source_url, item.allowed_source_domains) else None), False
    if len(exact) > 1:
        return None, True
    # 组合发布者不允许靠其中一个名称取得机构资格。
    mentioned = {item.institution_id for item in _INSTITUTIONS
                 if any(_alias_pattern(alias).search(publisher) for alias in item.publisher_aliases)}
    return None, len(mentioned) > 1


def order_public_institution_coverage(query, sorted_matches, eligible_rows):
    """给至少两个明确机构的比较题形成机构→文档首片段覆盖前缀。

    所有输入 tuple 对象和分数保持；前缀以原排序首次出现的机构和文档为序，
    其余候选原序放在尾部。任何已选候选结构或资格异常都整批保持原序。
    """
    scope = resolve_public_institutions(query)
    diagnostics = {'policy': POLICY, 'authority': 'source_coverage_only', 'used': False, 'reason': scope['reason'],
                   'institution_ids': scope['institution_ids'],
                   'participating_institution_ids': [], 'prefix_candidate_count': 0,
                   'missing_institution_ids': [], 'compound_used': scope['compound_used']}
    # 非比较或机构范围不明确时只是恒等 selector；准入仍由调用方原门负责。
    if not scope['ready']:
        return sorted_matches, diagnostics

    def fallback(reason):
        diagnostics['reason'] = reason
        return sorted_matches, diagnostics

    if not isinstance(sorted_matches, (list, tuple)):
        return fallback('invalid_candidate_structure')
    ids = []
    for match in sorted_matches:
        if (not isinstance(match, tuple) or len(match) != 3
                or isinstance(match[0], bool) or not isinstance(match[0], Real)
                or not math.isfinite(match[0])
                or not isinstance(match[1], str) or not match[1]
                or not isinstance(match[2], (set, frozenset))
                or any(not isinstance(facet, str) for facet in match[2])):
            return fallback('invalid_candidate_structure')
        ids.append(match[1])
    if len(ids) != len(set(ids)):
        return fallback('duplicate_candidate_id')
    if not isinstance(eligible_rows, (list, tuple)):
        return fallback('invalid_eligible_rows')
    wanted = set(ids)
    by_id = {}
    for row in eligible_rows:
        cid = _get(row, 'id')
        if not isinstance(cid, str) or cid not in wanted:
            continue
        if cid in by_id:
            return fallback('ambiguous_candidate_row')
        by_id[cid] = row
    if wanted != set(by_id):
        return fallback('unknown_candidate_id')
    institutions = {}
    for cid in ids:
        row = by_id[cid]
        try:
            market = representation(row)['kind'] == 'market_full_text'
        except (KeyError, TypeError, AttributeError, IndexError):
            market = False
        if not market:
            return fallback('non_market_candidate')
        if (any(_get(row, key) != value for key, value in
                (('visibility', 'public'), ('data_origin', 'real'), ('status', 'verified')))
                or any(_get(row, key) not in (1, True) for key in
                       ('enabled', 'read_allowed', 'outbound_allowed'))):
            return fallback('ineligible_candidate')
        if any(not isinstance(_get(row, key), str) or not _get(row, key)
               for key in ('document_id', 'version_id')):
            return fallback('invalid_document_identity')
        if any(not _has(row, key) or (_get(row, key) is not None and not isinstance(_get(row, key), str))
               for key in ('product_id', 'share_class_id', 'channel_id')):
            return fallback('invalid_document_identity')
        if not isinstance(_get(row, 'publisher'), str) or not _get(row, 'publisher').strip():
            return fallback('invalid_publisher_identity')
        iid, ambiguous = _publisher_institution(_get(row, 'publisher'), _get(row, 'source_url'))
        if ambiguous:
            return fallback('ambiguous_publisher_alias')
        institutions[cid] = iid
    requested = set(scope['institution_ids'])
    groups = {}
    for match in sorted_matches:
        iid = institutions[match[1]]
        if iid not in requested:
            continue
        row = by_id[match[1]]
        group_key = tuple(_get(row, key) for key in
                          ('document_id', 'version_id', 'product_id', 'share_class_id', 'channel_id'))
        groups.setdefault(iid, {}).setdefault(group_key, match)
    diagnostics['participating_institution_ids'] = list(groups)
    diagnostics['missing_institution_ids'] = [iid for iid in scope['institution_ids'] if iid not in groups]
    if diagnostics['missing_institution_ids']:
        return fallback('requested_institution_not_recalled')
    documents = [list(group.values()) for group in groups.values()]
    prefix = [matches[index] for index in range(max(map(len, documents), default=0))
              for matches in documents if index < len(matches)]
    prefix_ids = {match[1] for match in prefix}
    ordered = prefix + [match for match in sorted_matches if match[1] not in prefix_ids]
    diagnostics.update(used=True, reason=None, prefix_candidate_count=len(prefix))
    return ordered, diagnostics
