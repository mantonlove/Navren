"""Bounded source-credit checks; no fetching, clock inference or publication writes."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

ATTRIBUTION_CONTRACT = 'xinhua-visible-source-v1'
MEDIA_IDS = {'xinhua', 'xinhua_money'}
PLATFORM_NAMES = ('新华网', '新华社')
CCTV_CONTRACT = 'cctv-visible-source-v1'
CCTV_SELF_NAMES = ('央视财经', '央视网', '央视网财经')
YICAI_CONTRACT = 'yicai-visible-source-v1'


def yicai_publication_time(soup):
    values = {node.get_text(' ', strip=True) for node in soup.select('.m-text .title p.f-cb > em.f-fl')}
    if len(values) != 1:
        return None
    value = next(iter(values))
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Shanghai')).isoformat()
    except ValueError:
        return None


def _yicai_attribution(soup, published_at, body):
    names = sorted({node.get_text(' ', strip=True) for node in
                    soup.select('.m-text .title p.f-cb > span.f-fl') if node.get_text(strip=True)})
    bylines = []
    for node in soup.select('.m-text .title p.names'):
        value = re.sub(r'\s+', ' ', node.get_text(' ', strip=True))
        match = re.search(r'(?:^|\s)作者\s*[：:]\s*(.+?)(?=\s*责编\s*[：:]|$)', value)
        if match and match[1].strip():
            bylines.append(match[1].strip())
    authors = sorted(set(bylines))
    origin = names[0] if len(names) == 1 else None
    status = ('conflicting_source' if len(names) > 1 or len(authors) > 1 else 'unknown' if not origin
              else 'self_published' if origin == '第一财经' else 'republication')
    # An owned-site wrapper around another outlet's dispatch is still a reprint.
    if status == 'self_published' and re.match(r'^(?:据|来源[：:]|转自|转载自)\s*(?:新华社|新华网|央视|财联社|证券时报|路透|彭博)', body):
        status = 'republication'
    visible = yicai_publication_time(soup)
    bound_date = False
    try:
        actual = datetime.fromisoformat(published_at)
        bound_date = bool(actual.tzinfo is not None and visible and actual == datetime.fromisoformat(visible))
    except (ValueError, TypeError):
        pass
    original = published_at if (status == 'self_published' and len(authors) == 1
                                and bound_date and len(body) >= 120) else None
    return {
        'publication_platform': '第一财经', 'origin_publisher': origin,
        'reporting_body': origin if status == 'self_published' and len(authors) == 1 else None,
        'byline_text': authors[0] if len(authors) == 1 else None,
        'original_published_at': original,
        'first_publication_status': 'verified_original_page' if original else 'unknown',
        'publication_time_kind': 'original_page_publication' if original else 'platform_page_publication',
        'source_attribution': {'contract': YICAI_CONTRACT, 'status': status,
                               'visible_source_credits': names,
                               'evidence_selector': '.m-text .title p.f-cb > span.f-fl',
                               'publication_selector': '.m-text .title p.f-cb > em.f-fl',
                               'byline_selector': '.m-text .title p.names',
                               'visible_date_matches_published_at': bound_date},
    }


def _cctv_attribution(soup, published_at, body):
    """Use the article header, never a syndicator credit or an editor's name."""
    rows = []
    for node in soup.select('#title_area .info'):
        value = re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip()
        match = re.fullmatch(
            r'来源\s*[：:]\s*([^|｜<>\n]{1,60})\s*[|｜]\s*'
            r'(\d{4}年\d{2}月\d{2}日\s+\d{2}:\d{2})', value)
        if match:
            rows.append((match[1].strip(), match[2]))
    names = sorted({name for name, _ in rows})
    dates = {date for _, date in rows}
    meta_names = {str(node.get('content', '')).strip()
                  for node in soup.select('meta[name="source"],meta[property="source"]')
                  if str(node.get('content', '')).strip()}
    conflict = len(names) > 1 or len(dates) > 1 or bool(meta_names and meta_names != set(names))
    origin = names[0] if len(names) == 1 and not conflict else None
    status = ('conflicting_source' if conflict else 'unknown' if not origin
              else 'self_published' if origin in CCTV_SELF_NAMES else 'republication')
    bound_date = None
    try:
        observed = datetime.fromisoformat(published_at)
        if observed.tzinfo is not None and len(dates) == 1:
            visible = datetime.strptime(next(iter(dates)), '%Y年%m月%d日 %H:%M')
            if observed.astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None, second=0, microsecond=0) == visible:
                bound_date = published_at
    except (TypeError, ValueError):
        pass
    original = bound_date if status == 'self_published' and len(body) >= 120 else None
    byline = re.search(r'[（(]((?:总台)?央视(?:财经)?记者\s+[^()（）]{1,100})[）)]', body)
    return {
        'publication_platform': '央视网财经', 'origin_publisher': origin,
        'reporting_body': origin if byline else None,
        'byline_text': byline[1] if byline else None,
        'original_published_at': original,
        'first_publication_status': 'verified_original_page' if original else 'unknown',
        'publication_time_kind': 'original_page_publication' if original else 'platform_page_publication',
        'source_attribution': {'contract': CCTV_CONTRACT, 'status': status,
                               'visible_source_credits': names,
                               'evidence_selector': '#title_area .info',
                               'visible_date_matches_published_at': bound_date is not None},
    }


def media_attribution(source, soup, published_at, body):
    if source.id == 'cctv':
        return _cctv_attribution(soup, published_at, body)
    if source.id == 'yicai':
        return _yicai_attribution(soup, published_at, body)
    if source.id not in MEDIA_IDS:
        return {}
    # Only the article header is authoritative here; body quotes/footer/nav are not.
    credits = []
    for node in soup.select('.header-cont > .source'):
        value = re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip()
        match = re.fullmatch(r'来源\s*[：:]\s*([^：:。；;<>\n]{1,60})', value)
        if match:
            credits.append(match[1].strip())
    names = sorted(set(credits))
    origin = names[0] if len(names) == 1 else None
    status = ('conflicting_source' if len(names) > 1 else 'unknown' if not origin
              else 'self_published' if origin in PLATFORM_NAMES else 'republication')
    # A source credit is not by itself proof that this website first published it.
    original = (published_at if status == 'self_published'
                and re.match(r'^(?:新华社|新华网)[^。]{0,45}日电', body) else None)
    byline = re.search(r'[（(](记者\s+[^()（）]{1,100})[）)]\s*[。.]?$', body)
    reporting_body = (origin if origin and f'《{origin}》记者' in body else None)
    return {
        'publication_platform': '新华网', 'origin_publisher': origin,
        'reporting_body': reporting_body,
        'byline_text': byline[1] if byline else None,
        'original_published_at': original,
        'first_publication_status': 'verified_original_page' if original else 'unknown',
        'publication_time_kind': 'original_page_publication' if original else 'platform_page_publication',
        'source_attribution': {'contract': ATTRIBUTION_CONTRACT, 'status': status,
                               'visible_source_credits': names,
                               'evidence_selector': '.header-cont > .source'},
    }


def first_publication_verified(article):
    if article.get('source_id') == 'yicai':
        attribution = article.get('source_attribution') or {}
        return (attribution.get('contract') == YICAI_CONTRACT
                and attribution.get('status') == 'self_published'
                and attribution.get('visible_source_credits') == ['第一财经']
                and article.get('origin_publisher') == '第一财经'
                and bool(article.get('byline_text'))
                and attribution.get('visible_date_matches_published_at') is True
                and article.get('first_publication_status') == 'verified_original_page'
                and article.get('original_published_at') is not None
                and article['original_published_at'] == article.get('published_at'))
    if article.get('source_id') == 'cctv':
        attribution = article.get('source_attribution') or {}
        return (attribution.get('contract') == CCTV_CONTRACT
                and attribution.get('status') == 'self_published'
                and attribution.get('visible_source_credits') in [[name] for name in CCTV_SELF_NAMES]
                and attribution.get('visible_date_matches_published_at') is True
                and article.get('origin_publisher') in CCTV_SELF_NAMES
                and attribution.get('visible_source_credits') == [article.get('origin_publisher')]
                and article.get('first_publication_status') == 'verified_original_page'
                and article.get('original_published_at') is not None
                and article['original_published_at'] == article.get('published_at'))
    if article.get('source_id') not in MEDIA_IDS:
        # 其他政府/央行原始发布沿用原合同；未完成原报首发绑定的媒体不因平台日期获得资格。
        return article.get('identity') not in ('media_report','professional_report')
    attribution = article.get('source_attribution') or {}
    return (attribution.get('contract') == ATTRIBUTION_CONTRACT
            and attribution.get('status') == 'self_published'
            and article.get('first_publication_status') == 'verified_original_page'
            and article.get('original_published_at') is not None
            and article['original_published_at'] == article.get('published_at'))


def validate_source_attribution(draft, articles):
    """Reject only bounded affirmative claims assigning reporting to the syndicator.

    The independent reviewer still owns broader author/actor/causal entailment.
    """
    articles = list(articles)
    yicai = [a for a in articles if a.get('source_id') == 'yicai']
    if any((a.get('source_attribution') or {}).get('contract') != YICAI_CONTRACT for a in yicai):
        raise ValueError('source_attribution_not_verified')
    yicai_fields = [draft.get(k, '') for k in ('headline', 'summary', 'analysis', 'uncertainty')]
    yicai_fields += [f.get('text', '') for f in draft.get('facts', [])]
    for value in yicai_fields if yicai else []:
        for match in re.finditer(r'第一财经(?:记者)?(调查|走访|采访)', value):
            if re.search(r'(?:不是|并非|非|不属于|不能称为)\s*$', value[max(0, match.start()-8):match.start()]):
                continue
            if not any(a.get('reporting_body') == '第一财经' and re.search(
                    r'(?:第一财经|记者)[^。]{0,30}' + match[1], a.get('evidence_text', '')) for a in yicai):
                raise ValueError('syndicator_reporting_body_mismatch')
    cctv = [a for a in articles if a.get('source_id') == 'cctv']
    if any((a.get('source_attribution') or {}).get('contract') != CCTV_CONTRACT for a in cctv):
        raise ValueError('source_attribution_not_verified')
    fields = [draft.get(k, '') for k in ('headline', 'summary', 'analysis', 'uncertainty')]
    fields += [f.get('text', '') for f in draft.get('facts', [])]
    for value in fields if cctv else []:
        for match in re.finditer(r'(?:央视网(?:财经)?|央视财经)(?:记者)?(调查|走访|采访)', value):
            if re.search(r'(?:不是|并非|非|不属于|不能称为)\s*$', value[max(0, match.start()-8):match.start()]):
                continue
            supported = any(
                a.get('reporting_body') in CCTV_SELF_NAMES
                and re.search(r'(?:总台)?央视(?:财经)?记者[^。]{0,8}'+match[1], a.get('evidence_text', ''))
                for a in cctv)
            if not supported:
                raise ValueError('syndicator_reporting_body_mismatch')
    media = [a for a in articles if a.get('source_id') in MEDIA_IDS]
    if not media:
        return
    if any((a.get('source_attribution') or {}).get('contract') != ATTRIBUTION_CONTRACT
           for a in media):
        raise ValueError('source_attribution_not_verified')
    fields = [draft.get(k, '') for k in ('headline', 'summary', 'analysis', 'uncertainty')]
    fields += [f.get('text', '') for f in draft.get('facts', [])]
    pattern = r'(?:新华网|新华社)(?:记者)?(调查|走访|采访)'
    for value in fields:
        for match in re.finditer(pattern, value):
            if re.search(r'(?:不是|并非|非|不属于|不能称为)\s*$', value[max(0, match.start()-8):match.start()]):
                continue
            supported = any(
                (first_publication_verified(a) or a.get('reporting_body') in PLATFORM_NAMES)
                and re.search(r'(?:新华网|新华社)?记者[^。]{0,8}'+match[1], a.get('evidence_text', ''))
                for a in media)
            if not supported:
                raise ValueError('syndicator_reporting_body_mismatch')
