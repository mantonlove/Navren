"""原文支持的统计发布、政策公告和收盘事件身份。

只识别同一底层事件，不作为来源、事实、独立审核或发布通过证明。
本模块不修改事件、原文、数据库或审核记录。
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


def _us_major_close_alias(event, primary, proofs):
    """同一纽约交易日的三大股指收盘；交易日与终点均取自原文。"""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    title = _compact(primary.get('title'))
    headline = _compact(event.get('headline'))
    excluded = r'盘前|早盘|午盘|盘中|预测|预计|预期|展望|回顾|复盘'
    focus = r'纽约股市三大股指|(?:美国股市|美股)[^。；！？]{0,12}?(?:三大股指|收盘|收涨|收跌|连阳|连阴|连跌)'
    if (re.search(excluded, title + headline) or not re.search(focus, title)
            or not re.search(focus, headline)):
        return None
    names = {
        'dow': r'道琼斯工业平均指数|道指',
        'nasdaq': r'纳斯达克综合指数|纳指',
        'sp500': r'标准普尔500种股票指数|标普500(?:种股票)?指数',
    }
    close_point = r'[^。；;！？]{0,80}?(?:收于|收报|报收|报)\d{3,6}(?:\.\d+)?点'
    # 标题和整篇原文可能是综合报道；事件事实必须自己以整体收盘为主对象。
    source = primary.get('evidence_text')
    if not isinstance(source, str) or not source:
        return None
    facts = event.get('facts')
    if not isinstance(facts, list):
        return None
    individual = set()
    for fact in facts:
        if (not isinstance(fact, dict) or fact.get('article_id') != primary.get('id')
                or not isinstance(fact.get('quote'), str) or fact['quote'] not in source
                or not isinstance(fact.get('text'), str)):
            continue
        statement = _compact(fact.get('text'))
        statement = re.sub(r'^据新华社纽约(?:20\d{2}年)?\d{1,2}月\d{1,2}日电[，,]?', '', statement)
        quote = _compact(fact['quote'])
        quoted_indices = {key for key, label in names.items()
                          if re.search(r'(?:' + label + r')' + close_point, quote)}
        broad_statement = re.match(r'^(?:周[一二三四五六日])?(?:纽约股市|美国股市|美股)三大股指.{0,18}?(?:收盘|收涨|收跌|上涨|下跌)', statement)
        broad_quote = re.search(r'(?:纽约股市|美国股市|美股)三大股指.{0,18}?(?:收盘|收涨|收跌|上涨|下跌)', quote)
        if broad_statement and (broad_quote or len(quoted_indices) >= 2):
            break
        for key, label in names.items():
            if (re.match(label, statement)
                    and re.search(r'(?:收于|收报|报收|报)\d{3,6}(?:\.\d+)?点', statement)
                    and key in quoted_indices):
                individual.add(key)
    else:
        if individual != {'dow', 'nasdaq', 'sp500'}:
            return None
    if primary.get('first_publication_status') != 'verified_original_page':
        return None
    try:
        published = datetime.fromisoformat(primary['original_published_at'])
        if (published.tzinfo is None or
                (published.hour, published.minute, published.second, published.microsecond) == (0, 0, 0, 0)):
            return None
        published = published.astimezone(ZoneInfo('America/New_York'))
    except (KeyError, TypeError, ValueError):
        return None
    # 不把无时区、日期占位或开盘前的页面时间当作纽约收盘日。
    if published.hour < 16:
        return None
    labels = re.compile('|'.join('(?:' + value + ')' for value in names.values()))
    weekdays = '一二三四五六日'
    for original in proofs:
        lead = _compact(original)[:800]
        close = re.search(r'截至(?:当天)?收盘|当天收盘|当日收盘|收盘时', lead)
        if not close or close.start() > 400:
            continue
        opening = lead[:close.start()]
        if not re.search(r'纽约股市|美国股市|美股', opening):
            continue
        explicit = set()
        for match in re.finditer(r'(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日', opening):
            try:
                explicit.add(date(int(match[1] or published.year), int(match[2]), int(match[3])))
            except ValueError:
                return None
        mentioned_weekdays = {weekdays.index(match[1]) for match in re.finditer(
            r'(?:纽约股市|美国股市|美股)[^。；！？]{0,20}?周([一二三四五六日])(?:上涨|下跌|收涨|收跌|收盘)', opening)}
        if (len(explicit) > 1 or len(mentioned_weekdays) > 1
                or explicit and explicit != {published.date()}
                or mentioned_weekdays and mentioned_weekdays != {published.weekday()}
                or not explicit and not mentioned_weekdays):
            continue
        closing = lead[close.end():close.end()+350]
        found = list(labels.finditer(closing))
        values = {}
        for index, label in enumerate(found):
            kind = next((key for key, pattern in names.items()
                         if re.fullmatch(pattern, label[0])), None)
            if kind is None:
                continue
            end = found[index+1].start() if index+1 < len(found) else len(closing)
            clause = re.split(r'[。；;！？]', closing[label.end():end], maxsplit=1)[0]
            value = re.search(r'(?:收于|收报|报收|报)(\d{3,6}(?:\.\d+)?)点', clause)
            if value:
                values.setdefault(kind, set()).add(Decimal(value[1]))
        if set(values) == set(names) and all(len(found_values) == 1 for found_values in values.values()):
            return 'market_close:us_major_indices:' + published.date().isoformat()
    return None


def _industrial_profit_release_alias(primary, proofs, day):
    """同一全国规上工业利润统计期间的原发布和解读只占一个事件。"""
    from urllib.parse import urlsplit
    from market_sources import REGISTRY
    title=_compact(primary.get('title'))
    # 只识别标题的主要统计对象；政策、预测、企业/地方/境外新闻中的
    # 背景数据不因此并入全国统计发布。期间和年份必须来自原标题。
    if re.search(r'预测|预计|预期|目标|展望|假设|如果|政策|措施|办法|方案|贷款|融资|上市公司|集团|美国|英国|日本|德国|法国|印度',title):
        return None
    period=r'(20\d{2})年(\d{1,2})[—–－至-](\d{1,2})月(?:份)?'
    matches=list(re.finditer(period+r'(?:全国)?(?:规模以上)?工业企业利润',title))
    identities={(int(m[1]),int(m[2]),int(m[3])) for m in matches}
    if len(identities)!=1:return None
    prefix=title[:matches[0].start()]
    if prefix and not re.fullmatch(r'国家统计局(?:[^。；;！？!?]{0,60}(?:解读|发布|公布)[：:]?|[：:]?)',prefix):
        return None
    year,start,end=next(iter(identities))
    if not (1<=start<=end<=12 and year<=day.year):return None
    # 全国口径在主标题期间后必须直接接统计对象，不能跳过省市名称。
    # 本增量只认已登记的国家统计局原站；媒体原报道者不等于数据发布机关。
    url=urlsplit(primary.get('url',''));host=(url.hostname or '').lower()
    registered=REGISTRY.get(primary.get('source_id'))
    if (registered is None or url.scheme!='https' or host!=registered.domain
            or not re.fullmatch(registered.pattern,url.path)
            or primary.get('publisher')!=registered.publisher):return None
    official=(host in ('stats.gov.cn','www.stats.gov.cn') and
              (primary.get('origin_publisher') or primary.get('publisher'))=='国家统计局')
    if not official:return None
    # 原文必须有全国总体、相同期间的利润增减事实，不能借某行业、
    # 企业、营收或模型summary推定。数值差异不制造第二个事件身份；
    # 数字是否准确仍由原事实校验与完整GLM分别拒绝。
    claim=re.compile(r'(?:(20\d{2})年)?(\d{1,2})[—–－至-](\d{1,2})月(?:份)?[，,:：]?'
                     r'全国规模以上工业企业(?:实现)?利润(?:总额)?'
                     r'(?:(?:为|达|达到)?\d+(?:\.\d+)?(?:万)?亿元[，,])?'
                     r'(?:同比)?(?P<direction>增长|下降)(?P<rate>[+-]?\d+(?:\.\d+)?)%')
    rates=set()
    for original in proofs:
        text=_compact(original)
        explicit_years={int(m[1]) for m in re.finditer(period,text)
                        if (int(m[2]),int(m[3]))==(start,end)}
        if explicit_years and year not in explicit_years:continue
        for m in claim.finditer(text):
            if (int(m[2]),int(m[3]))!=(start,end) or m[1] and int(m[1])!=year:continue
            prefix=re.split(r'[。；;！？!?]',text[:m.start()])[-1][-40:]
            if re.search(r'假设|如果|假如|若|预测|预计|预期|目标',prefix):continue
            if not m[1] and re.search(r'去年|上年|前年',prefix):continue
            rate=Decimal(m['rate'])
            if rate<0:return None
            rates.add(rate if m['direction']=='增长' else -rate)
    # 同篇、同期间的增减值自相矛盾时不据此建立正常身份。不同原文
    # 同一次发布仍只占一格，其矛盾保留在完整材料中交原GLM/版本门。
    if len(rates)==1:
        return 'statistical_release:cn_nbs:industrial_enterprise_profits:'+str(year)+':%02d-%02d'%(start,end)
    return None


def source_release_aliases(event, primary):
    """只为有原文支持的同公告/同交易日同收盘建立身份；共同主题不合并。"""
    day = _publication_day(primary)
    proofs = _source_proofs(event, primary)
    if day is None or not proofs:
        return set()
    return {alias for alias in (_policy_announcement_alias(primary, proofs, day),
                               _market_close_alias(primary, proofs, day),
                               _us_major_close_alias(event, primary, proofs),
                               _industrial_profit_release_alias(primary, proofs, day)) if alias}
