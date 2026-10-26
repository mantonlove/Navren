"""Only identify the same annual China Innovation Index release.

The alias is supported by the bound primary article/quotes, not by a shared
topic, arbitrary number, summary, source name or collection timestamp.
This module never mutates an event, article, database or review.
"""
from decimal import Decimal
import re


def _compact(value):
    return re.sub(r'\s+', '', value or '').replace('％', '%')


def innovation_release_alias(event, primary):
    title = _compact(primary.get('title'))
    headline = _compact(event.get('headline'))
    excluded = (r'预测|预计|预期|目标|展望|将达|将增|将于|贷款|信贷|授信|'
                r'补贴|财政|办法|计划|产品|签署|宣布|决定|上调|下调|新政|政策|'
                r'美国|日本|英国|德国|法国|印度|欧元区|欧盟|全球创新指数')
    if re.search(excluded, title + ' ' + headline):
        return None
    title_years = set(re.findall(r'(20\d{2})年中国创新指数', title))
    if len(title_years) != 1:
        return None
    year = next(iter(title_years))
    headline_years = set(re.findall(r'(20\d{2})年中国创新指数', headline))
    if headline_years and headline_years != {year}:
        return None
    # Both a number and its annual comparison must be in original evidence.
    # Summaries and generated fact text are deliberately not evidence here.
    evidence = _compact(primary.get('evidence_text'))
    excerpt = _compact(primary.get('evidence_excerpt'))
    if evidence and excerpt and excerpt not in evidence:
        return None
    proofs = [evidence] if evidence else [excerpt]
    if not evidence and re.search(r'假设|如果|假如|若|预测|预计|预期|目标', excerpt):
        return None
    for fact in event.get('facts', []):
        if fact.get('article_id') != primary.get('id'):
            continue
        quote = _compact(fact.get('quote'))
        # Full original text wins over a quotation that omitted its context.
        # Public snapshots carry only a short excerpt, so retain the already
        # publication-bound primary quote when the full text is unavailable.
        if quote and not evidence:
            proofs.append(quote)
    pattern = re.compile(
        r'(20\d{2})年中国创新指数(?:达到|为|达)(\d+(?:\.\d+)?)'
        r'(?:[（(][^（）()]{0,35}[）)])?[，,]?'
        r'(?:同比|比(?:(20\d{2})年|上年))增长(\d+(?:\.\d+)?)%')
    identities = set()
    for proof in proofs:
        for match in pattern.finditer(proof):
            if match[1] != year or match[3] and int(match[3]) != int(year) - 1:
                continue
            context = re.split(r'[。；;！？!?]', proof[:match.start()])[-1][-40:]
            if re.search(r'假设|如果|假如|若|预测|预计|预期|目标', context):
                continue
            index, growth = Decimal(match[2]), Decimal(match[4])
            if index <= 0:
                continue
            identities.add((year, format(index.normalize(), 'f'),
                            format(growth.normalize(), 'f')))
    # Contradictory source/quote values are not guessed or merged.
    if len(identities) != 1:
        return None
    return 'cn_innovation_index_release:' + ':'.join(next(iter(identities)))


def _source_proofs(event, primary):
    full = primary.get('evidence_text')
    excerpt = primary.get('evidence_excerpt')
    if full:
        if excerpt and _compact(excerpt) not in _compact(full):
            return []
        return [full]
    proofs = [excerpt] if excerpt else []
    proofs.extend(f['quote'] for f in event.get('facts', [])
                  if f.get('article_id') == primary.get('id')
                  and isinstance(f.get('quote'), str))
    return proofs


def _publication_day(primary):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    value = primary.get('original_published_at') or primary.get('published_at')
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(ZoneInfo('Asia/Shanghai')).date()
    except (TypeError, ValueError):
        return None


def _policy_announcement_alias(primary, proofs, day):
    from html import unescape
    # 身份由原始标题、发布主体和正文中的同一动作/对象同时支持。
    title = _compact(re.sub(r'<[^>]*>', ' ', unescape(primary.get('title', ''))))
    title = re.sub(r'[（(][^（）()]*(?:公告|通知|决定)[^（）()]*[）)]$', '', title)
    match = re.fullmatch(r'(中国人民银行|央行|国家金融监督管理总局|中国证监会|财政部|国家发展改革委)'
                         r'(决定)?(设立|创设|启动|推出|印发|发布)([\u4e00-\u9fffA-Za-z0-9·—－]{8,80})', title)
    if not match:
        return None
    authority = '中国人民银行' if match[1] == '央行' else match[1]
    action, subject = match[3], match[4]
    if re.search(r'拟|计划|预期|假设|如果|传闻|辟谣|否认|未决定|尚未|征求意见', subject):
        return None
    for original in proofs:
        text = _compact(original)
        # 不能只从标题或生成的summary补“谁宣布了什么”；截去上下文的引文不作原全文替代。
        for found in re.finditer(re.escape(action + subject), text):
            sentence = re.split(r'[。；;！？!?]', text[:found.start()])[-1]
            if authority not in sentence:
                continue
            if re.search(r'拟|计划|预期|假设|如果|传闻|辟谣|否认|未决定|尚未', sentence[-30:]):
                continue
            return 'policy_announcement:' + day.isoformat() + ':' + authority + ':' + action + subject
    return None


def _market_close_alias(primary, proofs, day):
    from datetime import date
    title = _compact(primary.get('title'))
    if re.search(r'盘前|早盘|午评|午盘|开盘|预测|展望|预期|盘中', title):
        return None
    if not (re.search(r'收盘', title) or re.match(r'今日股市\d{4}[丨|:：]', title)):
        return None
    identities = set()
    for original in proofs:
        text = _compact(original)
        for match in re.finditer(r'(?:沪指|上证(?:综指|指数)|深成指|创业板指|科创综指)[^。；！？]{0,90}?(?:收盘|收报|报收|收于|收红|收涨|收跌|收绿|收平|尾盘收)', text):
            clause = re.split(r'[。；！？]', text[:match.start()])[-1]
            prefix = text[:match.start()]
            # 具体交易日优先使用同句/导语日期；不得将采集日当交易日。
            dates = list(re.finditer(r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日', clause or prefix[:80]))
            if dates:
                m = dates[-1]
                try:traded = date(int(m[1] or day.year), int(m[2]), int(m[3]))
                except ValueError:continue
                if traded > day:
                    continue
            else:
                heading_date = re.match(r'今日股市(\d{2})(\d{2})[丨|:：]', title)
                if not heading_date or (int(heading_date[1]),int(heading_date[2])) != (day.month,day.day):
                    continue
                traded = day
            # 同一A股交易日的收盘综述是同一事件。报价矛盾仍须内容审核拒绝，
            # 不能让不同数字把同一收盘再变成一个独立名额。
            identities.add(traded.isoformat())
    if len(identities) != 1:
        return None
    return 'market_close:cn_ashare:' + next(iter(identities))


def source_release_aliases(event, primary):
    """只为有原文支持的同公告/同交易日同收盘建立身份；共同主题不合并。"""
    day = _publication_day(primary)
    proofs = _source_proofs(event, primary)
    if day is None or not proofs:
        return set()
    return {alias for alias in (_policy_announcement_alias(primary, proofs, day),
                               _market_close_alias(primary, proofs, day)) if alias}
