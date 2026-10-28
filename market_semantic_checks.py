"""Bounded, free checks for explicit FX comparisons and price-only oil briefs.

This is a candidate, not a general entailment verifier. Unknown forms still need
the existing full independent review. It neither rewrites drafts nor approves
them, and never changes stored publications or original articles.
"""
from decimal import Decimal
from datetime import date, datetime
from zoneinfo import ZoneInfo
import re

ALIASES = {'美元':'USD','欧元':'EUR','英镑':'GBP','日元':'JPY',
           '瑞士法郎':'CHF','瑞郎':'CHF','加元':'CAD','瑞典克朗':'SEK'}
CUR = '(?:' + '|'.join(sorted(ALIASES, key=len, reverse=True)) + ')'
GROUP = CUR + '(?:[和与及、]' + CUR + ')*'
NUM = r'\d+(?:\.\d+)?'
COMPARE = re.compile(r'1\s*(?P<base>'+CUR+r')\s*兑换\s*(?P<now>'+NUM+
                     r')\s*(?P<quote>'+CUR+r')\s*[，,]\s*(?P<rel>高于|低于|等于|持平于)'
                     r'前一交易日的\s*(?P<old>'+NUM+r')\s*(?P<old_quote>'+CUR+r')')
DIRECTION = {'上涨':1,'下跌':-1,'上升':1,'下降':-1,'升值':1,'贬值':-1,'走高':1,'走低':-1,'持平':0}
DIR = '(?:'+'|'.join(DIRECTION)+')'
EXPLICIT = re.compile('(?P<base>'+GROUP+')(?:兑|对)(?P<quote>'+CUR+')'
                      '(?:汇率)?(?:均|继续)?(?P<direction>'+DIR+')')
CONTINUATION = re.compile(r'^\s*(?P<base>'+GROUP+')(?:均)?(?P<direction>'+DIR+r')\s*$')
NON_ASSERTION = re.compile(r'如果|假如|若|可能|未必|并非|并不|不意味着|不能|不可|没有|尚未|是否|不确定|未[涨跌升降]')


def quoted_pair_directions(text):
    """Only explicit comparable pairs; inconsistent evidence is not guessed."""
    observed = {}
    for match in COMPARE.finditer(text):
        base, quote, old_quote = (ALIASES[match[k]] for k in ('base','quote','old_quote'))
        if base == quote or quote != old_quote:
            continue
        current, old = Decimal(match['now']), Decimal(match['old'])
        if current <= 0 or old <= 0:
            continue
        sign = int(current > old)-int(current < old)
        if sign != {'高于':1,'低于':-1,'等于':0,'持平于':0}[match['rel']]:
            continue
        observed.setdefault((base,quote), set()).add(sign)
        observed.setdefault((quote,base), set()).add(-sign)
    return {pair:next(iter(signs)) for pair,signs in observed.items() if len(signs)==1}


def fx_direction_issues(text, source_text):
    expected = quoted_pair_directions(source_text)
    issues = []
    # A denominator is inherited only across immediately adjacent comma-list
    # items, never across a sentence, semicolon, or unrelated intervening clause.
    for sentence in re.split(r'[。；;！？!?\n]', text):
        denominator = None
        for clause in re.split(r'[，,]', sentence):
            if NON_ASSERTION.search(clause):
                denominator = None
                continue
            explicit = list(EXPLICIT.finditer(clause))
            claims = [(m['base'],ALIASES[m['quote']],m['direction']) for m in explicit]
            continuation = CONTINUATION.fullmatch(clause) if not claims and denominator else None
            if continuation:
                claims = [(continuation['base'],denominator,continuation['direction'])]
            if not claims:
                denominator = None
                continue
            denominator = claims[-1][1]
            for group, quote, word in claims:
                for label in re.findall(CUR, group):
                    pair = (ALIASES[label],quote)
                    if pair in expected and DIRECTION[word] != expected[pair]:
                        issues.append({'code':'fx_direction_contradicts_explicit_quote',
                                       'pair':'/'.join(pair),'claimed':DIRECTION[word],
                                       'source_supported':expected[pair]})
    return issues


OIL_SOURCE_ANALYSIS = re.compile(r'需求|库存|供[需应给]|产量|消费|进口|出口|炼厂|分析师|经济学家|原因|导致|由于|地缘')
OIL_CAUSAL = re.compile(r'(?:显示|表明|说明|反映|证明|意味着)[^。；;，,]{0,36}(?:需求|库存|供需|供应|供给)')


def price_only_oil(article):
    body = article.get('evidence_text','')
    return (bool(re.search(r'油价|原油', article.get('title',''))) and
            bool(re.search(r'期货|交货', body)) and bool(re.search(r'收于|收盘', body)) and
            not OIL_SOURCE_ANALYSIS.search(body))


def oil_causal_issues(text, articles):
    # Mixed or explanatory material goes to normal full review, not this narrow
    # rule. Keywords in a source are not treated as proof of any particular cause.
    if not articles or not all(price_only_oil(a) for a in articles):
        return []
    issues = []
    for clause in re.split(r'[。；;，,\n]', text):
        match = OIL_CAUSAL.search(clause)
        if match and not NON_ASSERTION.search(clause[:match.end()]):
            issues.append({'code':'oil_price_only_cannot_establish_demand_supply_cause',
                           'claim':match[0]})
    return issues


FX_DRIVER = re.compile(r'(?:主要(?:反映|源于|来自|受)|(?:由于|缘于|归因于|带来|推动|提振|支撑))[^。；;]{0,65}(?:欧元|英镑|日元|权重|美元|指数)|(?:欧元|英镑|日元)[^。；;]{0,65}(?:带来|推动|提振|支撑)')
FX_SOURCE_CAUSE = re.compile(r'分析师|经济学家|原因|由于|因为|导致|推动|驱动|受.+影响|归因')
ATTRIBUTED = re.compile(r'(?:^|[。；，：])\s*(?:台商|公司总经理|总经理|董事长|专家|记者)?(?P<speaker>[\u4e00-\u9fff]{2,4})(?:表示|指出|认为|称|说)')
DATED_EVENT = re.compile(r'(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日[^。；]{0,35}(?:举行|召开|举办)')


def evidence_scope_issues(row, articles):
    refs={a['id']:a for a in articles};issues=[]
    for index,fact in enumerate(row.get('facts',[])):
        article=refs.get(fact.get('article_id'))
        if not article:continue
        claim=fact.get('text','');quote=fact.get('quote','');source=article.get('evidence_text','')
        for m in ATTRIBUTED.finditer(claim):
            # 报道称/报道说是报道引导词，不是两字人名；只豁免这两种
            # 完整匹配，句内后续真实姓名仍逐条绑定自己的原文quote。
            if m['speaker']=='报道' and m[0].endswith(('称','说')):continue
            # An attributed fact must carry the named speaker in its exact evidence,
            # rather than borrowing a matching sentence spoken by another person.
            if m['speaker'] not in quote:
                issues.append({'code':'attributed_fact_requires_named_quote','field':'facts','index':index})
        without_dateline=re.sub(r'^(?:新华社|新华网)[^。]{0,70}日电[^\u4e00-\u9fff]*','',source)
        for m in DATED_EVENT.finditer(claim):
            supported=re.search(r'(?:'+m['year']+r'年)?'+str(int(m['month']))+r'月'+str(int(m['day']))+r'日[^。；]{0,50}(?:举行|召开|举办)',without_dateline)
            if not supported:
                issues.append({'code':'publication_date_not_event_date','field':'facts','index':index})
    for article in articles:
        if not re.search(r'美元指数|汇市',article.get('title','')) or FX_SOURCE_CAUSE.search(article.get('evidence_text','')):continue
        for field in ('headline','summary','analysis','uncertainty'):
            for clause in re.split(r'[。；;，,\n]',row.get(field,'')):
                if FX_DRIVER.search(clause) and not NON_ASSERTION.search(clause):
                    issues.append({'code':'fx_price_only_cannot_establish_driver','field':field})
    return issues


EQUITY_CAUSE = re.compile(r'(?:显示|表明|反映|证明|意味着)[^。；;，,]{0,30}(?:风险偏好|信心|资金流入|资金流出|资金回流|情绪)')

def equity_price_only_issues(row, articles):
    if not articles or not all(re.search(r'股市|股指',a.get('title','')) and not re.search(r'分析师|经济学家|风险偏好|市场情绪|资金流入|资金流出|原因|由于',a.get('evidence_text','')) for a in articles):return []
    issues=[]
    for field in ('headline','summary','analysis','uncertainty'):
        for clause in re.split(r'[。；;，,\n]',row.get(field,'')):
            if EQUITY_CAUSE.search(clause) and not NON_ASSERTION.search(clause):
                issues.append({'code':'equity_price_only_cannot_establish_sentiment_or_flow','field':field})
    return issues


_ANNOUNCEMENT_ROUNDUP = re.compile(r'(?:早间|午间|晚间|盘后)公告|公告(?:汇总|一览|合集)|这些公告')
_ISSUER_HEADER = re.compile(r'(?:^|[\s。；;】])(?P<issuer>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9*·-]{1,23})[：:]')

# A page first published today can analyse an announcement already public days
# earlier. This narrow rule checks the *selected action*, not the date of a
# market close, a future delivery date, or the source page's own dateline.
_SECONDARY_REPORT = re.compile(r'新闻分析|(?:深度|专题)分析|综述|回顾|盘点|汇编|合集|一览')
_PUBLIC_ACTION = re.compile(r'宣布|签署|公布|披露|发布|决定|批准|获批|达成|启动|上线')
_DATED_ACTION = re.compile(
    r'(?P<date>(?:(?P<year>20\d{2})年)?(?:(?P<month>\d{1,2})月)?(?P<day>\d{1,2})日)'
    r'(?P<gap>[^。；;！？!?]{0,42}?)(?:' + _PUBLIC_ACTION.pattern + r')')
# Signing, approval and launch dates are event dates, not proof that the action
# was already public. Only an explicit disclosure verb starts the old clock.
_DATED_PUBLIC_ACTION = re.compile(
    r'(?P<date>(?:(?P<year>20\d{2})年)?(?:(?P<month>\d{1,2})月)?(?P<day>\d{1,2})日)'
    r'(?P<gap>[^。；;！？!?]{0,16}?)(?:宣布|公开披露|披露|公布|发布|公告)')


def _action_day(match, published):
    """Infer only an unambiguous same-month day; unknown cross-month stays unknown."""
    year = int(match['year']) if match['year'] else published.year
    month = int(match['month']) if match['month'] else published.month
    if not match['year'] and match['month'] and month > published.month:
        return None
    if not match['month'] and int(match['day']) > published.day:
        return None
    try:
        return date(year, month, int(match['day']))
    except ValueError:
        return None


def old_announcement_headline_issues(row, articles):
    """Reject a secondary report that selects an explicitly old public action.

    Ambiguous dates/actions fail open to the independent reviewer. This is not
    a general first-publication classifier and never treats a prior trading
    day or historical background alone as an old announcement.
    """
    headline = row.get('headline', '')
    summary = row.get('summary', '')
    facts = row.get('facts', [])
    if (not isinstance(headline, str) or not isinstance(summary, str)
            or not isinstance(facts, list)):
        return []
    # The first selected fact and the first summary sentence must frame the
    # old action as the card's subject. A dated background fact later in a
    # current-day story is insufficient to block that story.
    first_summary = re.split(r'[。；;！？!?]', summary, maxsplit=1)[0]
    focus = set(_PUBLIC_ACTION.findall(headline)) & set(_PUBLIC_ACTION.findall(first_summary))
    if not focus:
        return []
    refs = {a.get('id'): a for a in articles if isinstance(a, dict)}
    for fact in facts[:1]:
        if not isinstance(fact, dict):
            continue
        article = refs.get(fact.get('article_id'))
        quote = fact.get('quote')
        title = article.get('title') if article else None
        source = article.get('evidence_text') if article else None
        if (not article or not isinstance(quote, str) or not isinstance(title, str)
                or not isinstance(source, str) or not _SECONDARY_REPORT.search(title)
                or article.get('first_publication_status') != 'verified_original_page'
                or quote not in source):
            continue
        try:
            stamp = datetime.fromisoformat(article['published_at'])
            if stamp.tzinfo is None:
                continue
            published = stamp.astimezone(ZoneInfo('Asia/Shanghai')).date()
        except (KeyError, TypeError, ValueError):
            continue
        for match in _DATED_PUBLIC_ACTION.finditer(quote):
            if (match['gap'].lstrip().startswith(('电', '讯'))
                    or re.search(r'\d{1,2}日', match['gap'])):
                continue  # A wire dateline or later date is not the old action's public date.
            old = _action_day(match, published)
            if old is None or (published - old).days < 2 or match['date'] not in first_summary:
                continue
            if not (focus & set(_PUBLIC_ACTION.findall(quote))):
                continue
            # An explicitly dated new action in selected evidence can justify
            # another angle; absent such evidence the headline remains old.
            if any(isinstance(other, dict) and isinstance(other.get('quote'), str)
                   and any(not new['gap'].lstrip().startswith(('电', '讯'))
                           and _action_day(new, published) == published for new in
                           _DATED_ACTION.finditer(other['quote'])) for other in facts):
                continue
            return [{'code': 'selected_old_announcement_without_new_fact',
                     'article_id': article['id']}]
    return []


def _announcement_sections(article):
    """Only explicitly labelled corporate roundups with repeated issuer heads.

    The issuer must recur as the announcer/discloser after its heading; a colon,
    ticker list, or multiple parties to a normal report is not sufficient.
    Positions are in the complete source text, never generated prose.
    """
    if not _ANNOUNCEMENT_ROUNDUP.search(article.get('title', '')):
        return []
    text = article.get('evidence_text', '')
    headers = list(_ISSUER_HEADER.finditer(text))
    sections = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        issuer = header['issuer']
        lead = text[header.end():min(end, header.end() + 256)]
        announcement = re.escape(issuer) + r'(?:\([^)]{0,30}\)|（[^）]{0,30}）)?(?:公告|披露)'
        if re.search(announcement, lead):
            sections.append((header.start('issuer'), end, issuer))
    return sections if len({s[2] for s in sections}) > 1 else []


def compound_announcement_issues(row, articles):
    """A card may select one issuer's concrete release from a real roundup.

    This is a bounded rejection gate, not a general event classifier. Unknown
    quote positions in a confirmed roundup fail closed; the full independent
    review still judges whether that issuer's facts concern one actual event.
    """
    issues = []
    for article in articles:
        sections = _announcement_sections(article)
        if not sections:
            continue
        facts = [f for f in row.get('facts', []) if f.get('article_id') == article['id']]
        if not facts:
            continue
        selected = set()
        text = article.get('evidence_text', '')
        for fact in facts:
            quote = fact.get('quote', '')
            matches = list(re.finditer(re.escape(quote), text)) if quote else []
            owners = set()
            for match in matches:
                owner = {issuer for start, end, issuer in sections
                         if start <= match.start() and match.end() <= end}
                if len(owner) != 1:
                    owners.add(None)
                else:
                    owners.update(owner)
            if len(owners) != 1 or None in owners:
                issues.append({'code': 'announcement_roundup_quote_scope_unproven', 'article_id': article['id']})
                break
            selected.update(owners)
        else:
            if len(selected) != 1:
                issues.append({'code': 'compound_company_announcements_in_one_card', 'article_id': article['id']})
            elif (next(iter(selected)) not in row.get('headline', '') or
                  _ANNOUNCEMENT_ROUNDUP.search(row.get('headline', ''))):
                issues.append({'code': 'announcement_roundup_requires_specific_event_headline', 'article_id': article['id']})
    return issues


def semantic_issues(row, articles):
    """Facts are checked against their own source, never another event's body."""
    refs = {a['id']:a for a in articles}
    issues = (old_announcement_headline_issues(row, articles)
              +evidence_scope_issues(row, articles)+equity_price_only_issues(row, articles)
              +compound_announcement_issues(row, articles))
    for index, fact in enumerate(row.get('facts',[])):
        article = refs.get(fact.get('article_id'))
        if article:
            issues += [{**i,'field':'facts','index':index} for i in
                       fx_direction_issues(fact.get('text',''),article.get('evidence_text',''))]
            issues += [{**i,'field':'facts','index':index} for i in
                       oil_causal_issues(fact.get('text',''),[article])]
    combined = '\n'.join(a.get('evidence_text','') for a in articles)
    for field in ('headline','summary','analysis','uncertainty'):
        issues += [{**i,'field':field} for i in fx_direction_issues(row.get(field,''),combined)]
    for field in ('headline','summary','analysis','uncertainty'):
        issues += [{**i,'field':field} for i in oil_causal_issues(row.get(field,''),articles)]
    return issues


def validate_market_semantics(row, articles):
    issues = semantic_issues(row, articles)
    if issues:
        raise ValueError('market_semantic_check_failed:'+issues[0]['code'])
    return row
