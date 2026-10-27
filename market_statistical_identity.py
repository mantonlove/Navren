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
                               _industrial_profit_release_alias(primary, proofs, day)) if alias}
