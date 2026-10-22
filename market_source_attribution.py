"""Bounded source-credit checks; no fetching, clock inference or publication writes."""
import re

ATTRIBUTION_CONTRACT = 'xinhua-visible-source-v1'
MEDIA_IDS = {'xinhua', 'xinhua_money'}
PLATFORM_NAMES = ('新华网', '新华社')


def media_attribution(source, soup, published_at, body):
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
