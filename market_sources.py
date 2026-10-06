"""白名单公开发布入口；feedparser/BeautifulSoup 解析，禁止任意 URL 抓取。"""
from __future__ import annotations
import hashlib, re, time, urllib.request, urllib.error
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, urljoin
from zoneinfo import ZoneInfo

UTC=timezone.utc
CN=ZoneInfo('Asia/Shanghai')
MAX_BYTES=10*1024*1024

@dataclass(frozen=True)
class Source:
    id:str
    publisher:str
    url:str
    domain:str
    mode:str='rss'
    timezone:str='UTC'
    enabled:bool=True
    scope:str='official_facts'
    pattern:str=r'.+\.html$'
    identity:str='official_release'
    body_selector:str=''
    title_selector:str='h1,h2'
    list_selector:str='a[href]'

SOURCES=[
 Source('nbs','国家统计局','https://www.stats.gov.cn/sj/zxfb/rss.xml','www.stats.gov.cn',timezone='Asia/Shanghai',pattern=r'/sj/(?:zxfb|sjjd)/\d{6}/t\d{8}_\d+\.html'),
 Source('nbs_analysis','国家统计局','https://www.stats.gov.cn/sj/sjjd/rss.xml','www.stats.gov.cn',timezone='Asia/Shanghai',pattern=r'/sj/(?:zxfb|sjjd)/\d{6}/t\d{8}_\d+\.html'),
 Source('pbc','中国人民银行','https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html','www.pbc.gov.cn','list','Asia/Shanghai',pattern=r'/goutongjiaoliu/113456/113469/\d+/index.html'),
 Source('fed','美联储','https://www.federalreserve.gov/feeds/press_monetary.xml','www.federalreserve.gov',timezone='America/New_York',pattern=r'/newsevents/pressreleases/monetary\d{8}[a-z]\.htm'),
 Source('ecb','欧洲央行','https://www.ecb.europa.eu/rss/press.html','www.ecb.europa.eu',timezone='Europe/Berlin',pattern=r'/press/(?:pr|key|inter|pressconf)/date/\d{4}/html/[^/]+\.html'),
 Source('xinhua','新华网财经','https://www.news.cn/fortune/gundong/ds_8aaa30c0702b4d6997c978bee97139c8.json','www.news.cn','json','Asia/Shanghai',scope='public_media',pattern=r'/fortune/\d{8}/[0-9a-f]{32}/c\.html',identity='media_report',body_selector='#detailContent',title_selector='.title'),
 Source('xinhua_money','新华网金融','https://www.news.cn/money/ds_e99ef52a0795499c9845a80440f6d00b.json','www.news.cn','json','Asia/Shanghai',scope='public_media',pattern=r'/money/\d{8}/[a-f0-9]{32}/c\.html',identity='media_report',body_selector='#detailContent',title_selector='h1'),
 Source('cctv','央视网财经','https://finance.cctv.com/','finance.cctv.com','list','Asia/Shanghai',scope='public_media',pattern=r'/\d{4}/\d{2}/\d{2}/ARTI[A-Za-z0-9]+\.shtml',identity='media_report',body_selector='#content_area',title_selector='#title_area h1'),
 Source('ft','金融时报','https://www.ft.com/markets?format=rss','www.ft.com',enabled=False,scope='metadata_only',pattern=r'/content/[0-9a-f-]{36}'),
 Source('reuters','路透社','https://reutersagency.com/content-delivery-platforms/reuters-connect/','reutersagency.com','licensed',enabled=False,scope='credentials_required'),
 Source('caixin','财新','https://corp.caixin.com/','corp.caixin.com','manual',enabled=False,scope='not_connected'),
 Source('bloomberg','彭博','https://www.bloomberg.com/feeds/markets/news.rss','www.bloomberg.com','licensed',enabled=False,scope='usage_not_confirmed'),
 Source('wsj','华尔街日报','https://www.djreprints.com/menu/licensing-republishing/','www.djreprints.com','licensed',enabled=False,scope='credentials_required')]
REGISTRY={s.id:s for s in SOURCES}

# Backfill-only identity: it is not added to automatic discovery SOURCES.
REGISTRY['cctv_tv'] = Source('cctv_tv','央视网财经','https://tv.cctv.com/','tv.cctv.com',
    mode='backfill_only',timezone='Asia/Shanghai',scope='source_brief_only',
    pattern=r'/2026/(?:0[1-9]|1[0-2])/(?:0[1-9]|[12]\d|3[01])/VIDE[A-Za-z0-9]+\.shtml',identity='media_report')

def text_only_article(a):
    """Only an accessible text article can enter the daily selection."""
    if a.get('content_mode')=='source_brief' or a.get('source_id')=='cctv_tv':return False
    if a.get('source_content_format') not in (None,'text_article') or a.get('page_has_video') is True:return False
    if re.search(r'(?:^|/)(?:video|videos|tv|player)(?:/|$)|/VIDE',urlsplit(a.get('url','')).path,re.I):return False
    return readable_article(a)

def source_verified_text_article(a):
    """Bind the stored original text, its title, publication date and literal quotes."""
    try:
        if a.get('content_mode')!='source_verified_text' or not text_only_article(a):return False
        if a.get('read_scope')!='full_text' or a.get('access_status')!='free_full_text' or a.get('evidence_truncated') is not False:return False
        if a.get('source_content_format')!='text_article' or a.get('video_content_read') is not False:return False
        check=a.get('source_check',{});body=a.get('evidence_text');title=a.get('title')
        if check.get('method')!='official_page_title_text_date_binding' or check.get('text_read_scope')!='full_text' or check.get('video_content_read') is not False:return False
        url=safe_url(REGISTRY[a['source_id']],a['url'])
        if url!=a['url'] or check.get('url')!=url or check.get('final_url')!=url:return False
        if not isinstance(title,str) or len(title)<5 or title!=check.get('page_title') or title!=check.get('index_title'):return False
        if not isinstance(body,str) or len(body)<180:return False
        digest=hashlib.sha256(body.encode()).hexdigest()
        if a.get('source_text_sha256')!=digest or any(check.get(k)!=digest for k in ('page_text_sha256','evidence_text_sha256')):return False
        if check.get('source_text_characters')!=len(body) or not re.fullmatch(r'[0-9a-f]{64}',check.get('raw_page_sha256','')) or check['raw_page_sha256']!=a.get('raw_page_sha256'):return False
        stamp=datetime.fromisoformat(check['page_time_iso'])
        if stamp.tzinfo is None or stamp>datetime.now(UTC)+timedelta(minutes=5):return False
        if check.get('page_time_kind')!='published_at' or a.get('reported_time_kind')!='published_at':return False
        if any(datetime.fromisoformat(a[k])!=stamp for k in ('published_at','reported_at')):return False
        day=stamp.astimezone(CN).date().isoformat()
        if day!=check.get('page_date') or day!=check.get('expected_date'):return False
        raw=datetime.strptime(check['page_time_raw'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=CN)
        if raw!=stamp or check.get('checked_at')!=a.get('checked_at') or datetime.fromisoformat(check['checked_at']).tzinfo is None:return False
        quotes=check.get('summary_quotes')
        return isinstance(quotes,list) and bool(quotes) and all(isinstance(q,str) and q.strip() and q in body for q in quotes)
    except (KeyError,TypeError,ValueError,AttributeError):return False

def source_verified_text_event(event,articles):
    if event.get('content_mode')!='source_verified_text' or len(articles)!=1:return False
    a=articles[0];review=event.get('review',{})
    if not source_verified_text_article(a) or review.get('status')!='source_verified_text' or review.get('method')!='official_page_title_text_date_binding':return False
    if review.get('cloud_review')!='not_run' or review.get('independent_review')!='not_run' or event.get('analysis'):return False
    if event.get('source_check')!=a.get('source_check') or event.get('primary_article_id')!=a['id'] or event.get('article_ids')!=[a['id']]:return False
    if event.get('headline')!=a['title'] or event.get('reported_at')!=a.get('reported_at') or event.get('reported_time_kind')!='published_at':return False
    summary=event.get('summary');quotes=a['source_check']['summary_quotes']
    if not isinstance(summary,str) or not summary.strip():return False
    editorial=source_supported_text_summary(event,a)
    if re.sub(r'\s+','',summary)!=re.sub(r'\s+','',''.join(quotes)) and not editorial:return False
    facts=event.get('facts',[])
    if not facts or any(f.get('article_id')!=a['id'] or f.get('evidence_scope')!='full_text' or not isinstance(f.get('quote'),str) or not f['quote'] or f['quote'] not in a['evidence_text'] for f in facts):return False
    if editorial:
        return [f['quote'] for f in facts]==quotes and all(isinstance(f.get('text'),str) for f in facts) and ''.join(f['text'] for f in facts)==summary
    return all(f.get('text')==f['quote'] for f in facts)

TEXT_HISTORY_FINAL_SHA256 = '38759450a48ae35421723231eb6a84b84d15d8cadc615851b2de78741850425d'
# Two bounded editorial summaries checked against their original articles by the content owner.
# This is source support, never model analysis approval or permission for arbitrary paraphrases.
SUPPORTED_TEXT_SUMMARIES = {
    'e_text_hist_70dd4381e925a2358d33253e':('92ce70bde9c3749dae834b0b89a5013c121c02c0f97a2d63b164d36000cbbe5d','ae3548b03413345f957cfe52c7935b5658402502aa8efea22c25d0243aa31750'),
    'e_text_hist_f41457868b61606f78b031f4':('7851c82d406bdb10f3b943b7361811d398789356d98d0389580315154ea9f0c1','1da3bdb4a9e2a37931a219fcc5c1b3cdb3f6c1858387cf6db125a1561501fd65'),
}

def source_supported_text_summary(event,article):
    expected=SUPPORTED_TEXT_SUMMARIES.get(event.get('id'));check=event.get('agent_editorial_check',{})
    if not expected or event.get('source_supported_summary') is not True or not isinstance(event.get('summary'),str):return False
    actual=(article.get('source_text_sha256'),hashlib.sha256(event['summary'].encode()).hexdigest())
    return actual==expected and check=={'status':'source_supported_summary','method':'frozen_original_text_editorial_check',
        'payload_sha256':TEXT_HISTORY_FINAL_SHA256,'source_text_sha256':actual[0],'summary_sha256':actual[1]}

def article_reported_at(article):
    return article.get('reported_at') if article.get('content_mode')=='source_brief' else article.get('published_at')

def source_brief_readable(a):
    """Source verification authorizes only the exact official text brief, not video/full text."""
    try:
        source=REGISTRY.get(a.get('source_id'));check=a.get('source_check',{})
        if a.get('source_id')!='cctv_tv' or not source or a.get('content_mode')!='source_brief':return False
        if a.get('read_scope')!='source_brief' or a.get('access_status')!='free_video_with_brief' or a.get('link_status')!='verified':return False
        if a.get('video_content_read') is not False or a.get('published_at') is not None or a.get('evidence_truncated') is not False:return False
        if check.get('method')!='official_page_title_brief_date_binding' or check.get('text_read_scope')!='source_brief' or check.get('video_content_read') is not False:return False
        url=safe_url(source,a['url'])
        if url!=a['url'] or check.get('url')!=url or check.get('final_url')!=url:return False
        title=a.get('title');brief=a.get('evidence_text',a.get('source_brief'))
        if not isinstance(title,str) or len(title)<5 or not isinstance(brief,str) or not brief.strip():return False
        if title!=check.get('page_title') or title!=check.get('index_title') or brief!=check.get('page_brief') or brief!=check.get('index_brief'):return False
        digest=hashlib.sha256(brief.encode()).hexdigest()
        if any(check.get(k)!=digest for k in ('page_brief_sha256','evidence_excerpt_sha256')) or a.get('source_text_sha256')!=digest:return False
        if a.get('source_text_characters')!=len(brief) or not re.fullmatch(r'[0-9a-f]{64}',check.get('raw_page_sha256','')) or check['raw_page_sha256']!=a.get('raw_page_sha256'):return False
        if a.get('reported_time_kind')!='source_page_updated_at' or check.get('page_time_kind')!='source_page_updated_at':return False
        value=check.get('page_time_iso');stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp>datetime.now(UTC)+timedelta(minutes=5):return False
        day=stamp.astimezone(CN).date().isoformat()
        if day!=check.get('page_date') or day!=check.get('expected_date'):return False
        if any(datetime.fromisoformat(a[k])!=stamp for k in ('reported_at','content_updated_at')):return False
        raw=re.fullmatch(r'更新时间[：:]\s*(\d{4})年(\d{2})月(\d{2})日\s+(\d{2}):(\d{2})',check.get('page_time_raw',''))
        if not raw or datetime(*map(int,raw.groups()),tzinfo=CN)!=stamp:return False
        if datetime.fromisoformat(check['checked_at']).tzinfo is None or check['checked_at']!=a.get('checked_at'):return False
        return True
    except (KeyError,TypeError,ValueError,AttributeError):return False

def evidence_readable(article):
    return source_brief_readable(article) if article.get('content_mode')=='source_brief' else readable_article(article)

def source_verified_brief_event(event,articles):
    if event.get('content_mode')!='source_brief' or len(articles)!=1:return False
    a=articles[0];review=event.get('review',{})
    if not source_brief_readable(a) or review.get('status')!='source_verified' or review.get('method')!='official_page_title_brief_date_binding':return False
    if review.get('cloud_review')!='not_run' or review.get('independent_review')!='not_run' or event.get('analysis'):return False
    if event.get('source_check')!=a.get('source_check') or event.get('primary_article_id')!=a['id'] or event.get('article_ids')!=[a['id']]:return False
    if event.get('headline')!=a['title'] or (not isinstance(event.get('summary'),str) or not event['summary'] or event['summary'] not in a.get('evidence_text',a.get('source_brief',''))) or event.get('reported_at')!=a.get('reported_at') or event.get('reported_time_kind')!='source_page_updated_at':return False
    facts=event.get('facts',[])
    return bool(facts) and all(f.get('article_id')==a['id'] and f.get('evidence_scope')=='source_brief' and isinstance(f.get('quote'),str) and f['quote'] in event['summary'] and f.get('text')==f['quote'] for f in facts)

def now_iso():return datetime.now(UTC).isoformat()

def parse_date(value,zone='UTC'):
    if not isinstance(value,str) or not value.strip():raise ValueError('missing_publication_date')
    value=value.strip()
    if re.match(r'^\d{4}/\d{2}/\d{2}',value):value=value.replace('/','-')
    try:d=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:d=parsedate_to_datetime(value)
    if d.tzinfo is None:d=d.replace(tzinfo=ZoneInfo(zone))
    if d>datetime.now(UTC)+timedelta(minutes=5):raise ValueError('future_publication_date')
    return d.astimezone(UTC).isoformat()

def safe_url(source,url,article=True):
    p=urlsplit(url)
    path=re.sub(r'/+','/',p.path)
    if p.scheme not in ('http','https') or p.hostname!=source.domain or p.username or p.password or p.port not in (None,443):
        raise ValueError('url_domain_denied')
    if article and not re.fullmatch(source.pattern,path):raise ValueError('not_specific_article')
    if not article and url!=source.url:raise ValueError('unregistered_endpoint')
    # 去除跟踪字段，保留已注册订阅地址的查询参数。
    return urlunsplit(('https',source.domain,path,'' if article else p.query,''))

class GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self,source,article):self.source,self.article=source,article
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        normalized=safe_url(self.source,newurl,self.article)
        if urlsplit(newurl).scheme!='https':raise ValueError('insecure_redirect')
        return super().redirect_request(req,fp,code,msg,headers,normalized)

def fetch(source,url,article=False,timeout=12):
    url=safe_url(source,url,article)
    req=urllib.request.Request(url,headers={'User-Agent':'Navren/1.0 public-financial-reader','Accept':'text/html,application/json,application/rss+xml,application/xml'})
    deadline=time.monotonic()+timeout*2
    with urllib.request.build_opener(GuardedRedirect(source,article)).open(req,timeout=timeout) as r:
        final=safe_url(source,r.url,article);chunks=[];size=0
        while True:
            if time.monotonic()>deadline:raise TimeoutError('body_deadline')
            chunk=r.read1(65536)
            if not chunk:break
            chunks.append(chunk);size+=len(chunk)
            if size>MAX_BYTES:raise ValueError('response_too_large')
        return b''.join(chunks),final,r.headers.get('Content-Type','')

def text(node):return re.sub(r'\s+',' ',node.get_text(' ',strip=True)).strip() if node else ''

def discover(source,limit=15,days=35):
    import feedparser
    from bs4 import BeautifulSoup
    raw,_,_=fetch(source,source.url)
    entries=[]
    if source.mode=='rss':
        feed=feedparser.parse(raw)
        if feed.bozo or not feed.entries:raise ValueError('invalid_or_truncated_feed')
        entries=[{'title':x.get('title',''),'url':x.get('link',''),'published':x.get('published') or x.get('updated'),'content_updated':x.get('updated')} for x in feed.entries]
    elif source.mode=='json':
        # 使用已用于文字历史采集的官方滚动目录；列表时间只作发现线索。
        # 发布日期、标题、正文仍须在具体报道页逐项核对。
        import json
        rows=json.loads(raw).get('datasource')
        if not isinstance(rows,list) or not rows:raise ValueError('invalid_article_directory')
        for row in rows:
            if not isinstance(row,dict):continue
            title=str(row.get('showTitle') or row.get('title') or '').strip()
            url=urljoin(source.url,str(row.get('publishUrl') or ''))
            try:safe_url(source,url)
            except ValueError:continue
            if len(title)<5 or re.search(r'视频|图集|海报|图解|一图|读图|品牌|广告|宣传|推介会',title):continue
            hint=re.search(r'/(?:fortune|money)/(\d{8})/',url)
            entries.append({'title':title,'url':url,'published':None,
                            'discovered_date':hint[1] if hint else None})
        entries.sort(key=lambda e:(e.get('discovered_date') or '',
            bool(re.search(r'金融|银行|货币|利率|汇率|债券|国债|证券|理财|保险|财政|房贷|经济|投资|融资|政策|消费|就业',e['title']))),reverse=True)
    elif source.mode=='list':
        soup=BeautifulSoup(raw,'html.parser')
        for a in soup.select(source.list_selector):
            u=urljoin(source.url,a['href'])
            try:safe_url(source,u)
            except ValueError:continue
            # 列表路径日期只用于发现过滤；原始发表时间必须到文章元数据核实。
            m=re.search(r'/(\d{8})\d*/(?:index|c).html$',u) or re.search(r'/fortune/(\d{8})/',u)
            cctv_date=re.search(r'/(\d{4})/(\d{2})/(\d{2})/ARTI',u)
            hint=m[1] if m else ''.join(cctv_date.groups()) if cctv_date else None
            title=text(a)
            if source.scope=='public_media' and (len(title)<5 or re.search(r'品牌焕新|广告|招商|酒业管理层',title)):continue
            entries.append({'title':title,'url':u,'published':None,'discovered_date':hint})
        # 首页专题会长期保留；按路径日期发现近期稿，再到原文核实发表时间。
        if source.scope=='public_media':entries.sort(key=lambda e:(bool(re.search(r'金融|银行|货币|利率|汇率|债券|国债|股|证券|资管|资产管理|保险|税|财政|房贷|消费贷款|数字人民币|经济|GDP|PMI|采购经理|投资|融资|政策|通胀|物价|CPI|PPI|进出口|国际收支|工业企业利润',e['title'],re.I)),e.get('discovered_date') or ''),reverse=True)
    cutoff=datetime.now(UTC)-timedelta(days=days)
    found=[]
    for e in entries:
        try:
            e['url']=safe_url(source,e['url'])
            if e['published']:
                e['published_at']=parse_date(e['published'],source.timezone)
                if datetime.fromisoformat(e['published_at'])<cutoff:continue
            elif e.get('discovered_date') and datetime.strptime(e['discovered_date'],'%Y%m%d').replace(tzinfo=CN)<cutoff:continue
            if e['url'] not in {x['url'] for x in found}:found.append(e)
        except (ValueError,TypeError):continue
        if len(found)>=limit:break
    return found,{'source_id':source.id,'status':'ok','discovered':len(found),'bytes':len(raw),'checked_at':now_iso()}

class ArticleUnavailable(ValueError):
    """已读到页面，但它不能继续作为可直接阅读的完整资料。"""
    def __init__(self,reason):self.code=reason;super().__init__(reason)

def readable_article(a):
    source=REGISTRY.get(a.get('source_id'))
    if not source or not source.enabled or source.scope in ('metadata_only','source_brief_only'):return False
    if a.get('read_scope')!='full_text' or a.get('link_status')!='verified':return False
    try:
        if safe_url(source,a.get('url',''))!=a.get('url'):return False
    except (ValueError,TypeError):return False
    # 旧官方缓存沿用已核验正文；新增媒体必须逐篇通过新访问检查。
    return a.get('access_status')=='free_full_text' or (source.scope=='official_facts' and 'access_status' not in a)

def check_public_access(soup,main):
    import json
    for node in soup.select('script[type="application/ld+json"]'):
        try:data=json.loads(node.string or node.get_text())
        except (ValueError,TypeError):continue
        def paid(value):
            if isinstance(value,dict):
                if value.get('isAccessibleForFree') in (False,'False','false'):return True
                return any(paid(v) for v in value.values())
            return isinstance(value,list) and any(paid(v) for v in value)
        if paid(data):raise ArticleUnavailable('subscription_required')
    gate=r'(?:订阅|购买会员|开通会员|付费|充值)[^。！？\n]{0,18}(?:阅读全文|阅读完整|解锁全文|查看全文)|(?:登录|登陆|注册)后[^。！？\n]{0,12}(?:阅读|查看|继续)|(?:subscribe|sign in|log in)\s+(?:to|for)\s+(?:read|continue|access)|此(?:文|内容)[^。！？\n]{0,10}(?:仅限会员|付费阅读)'
    gated=soup.select('[class*="paywall"], [id*="paywall"], [class*="subscribe-wall"], [class*="login-wall"], [class*="preview-only"]')
    # 门槛提示可能位于正文后方；导航上的普通“登录”不构成门槛。
    visible=soup.__class__(str(soup),'html.parser')
    for node in visible.select('script,style,template,noscript'):node.decompose()
    if any(text(n) for n in gated) or re.search(gate,text(visible),re.I):raise ArticleUnavailable('reading_gate')
    if re.search(r'验证码|访问验证|机器人验证|Page not found|Access Denied|verify you are human',text(soup.select_one('h1,h2,title'))+' '+text(main)[:250],re.I):raise ArticleUnavailable('soft_error_or_empty_body')

def read_article(source,entry):
    from bs4 import BeautifulSoup
    url=safe_url(source,entry['url']); title=entry['title']; published=entry.get('published_at')
    link_status='verified'; body_text='';updated=None;origin=source.publisher;author='';precision=entry.get('published_precision') or ('second' if published else 'date')
    try:
        raw,final,kind=fetch(source,url,True)
        if 'html' not in kind:raise ArticleUnavailable('not_html_article')
        soup=BeautifulSoup(raw,'html.parser')
        # 媒体源只读取订阅元数据与访问状态，不获取/保存付费全文。
        if source.scope=='metadata_only':
            link_status='subscription_required'
        else:
            metas={str(m.get('name') or m.get('property','')).lower():m.get('content','') for m in soup.select('meta[content]')}
            title=metas.get('articletitle') or metas.get('og:title') or text(soup.select_one(source.title_selector)) or title
            if source.id in ('xinhua','xinhua_money'):title=re.sub(r'[-_]新华网$','',title).strip()
            origin=metas.get('source') or source.publisher
            author=metas.get('author') or ''
            original=metas.get('pubdate') or metas.get('article:published_time') or metas.get('firstpublishedtime') or metas.get('date') or metas.get('publishdate')
            if source.id in ('xinhua','xinhua_money'):
                clock=re.search(r'\d{2}:\d{2}:\d{2}',text(soup.select_one('.header-time .time,.time')))
                if original and re.fullmatch(r'\d{4}[-/]\d{2}[-/]\d{2}',original) and clock:original=original+' '+clock[0]
            if source.id=='cctv':
                # 脚本内的发布元数据是站点时间字段，不执行脚本。
                clock=re.search(r'\bvar\s+publishDate\s*=\s*[\'"](\d{14})[\'"]',str(soup))
                info=re.search(r'\d{4}年\d{2}月\d{2}日\s+\d{2}:\d{2}',text(soup.select_one('#title_area .info')))
                if clock:original=datetime.strptime(clock[1],'%Y%m%d%H%M%S').isoformat()
                elif info:original=datetime.strptime(info[0],'%Y年%m月%d日 %H:%M').isoformat(timespec='minutes')
            if original:
                parsed=parse_date(original,source.timezone)
                date_only=bool(re.fullmatch(r'\d{4}[-/]\d{2}[-/]\d{2}',original))
                if not (date_only and published and datetime.fromisoformat(parsed).astimezone(ZoneInfo(source.timezone)).date()==datetime.fromisoformat(published).astimezone(ZoneInfo(source.timezone)).date()):
                    published=parsed;precision='date' if date_only else 'second' if re.search(r'\d{2}:\d{2}:\d{2}',original) else 'minute'
            revision=metas.get('article:modified_time') or metas.get('lastmodifiedtime')
            if revision:updated=parse_date(revision,source.timezone)
            main=soup.select_one(source.body_selector or '#zoom, .TRS_Editor, .trs_editor_view, .ecb-pressContent, #content .section, #article, article, main')
            if not main and source.id=='fed':main=soup.select_one('.col-xs-12.col-sm-8.col-md-8')
            if not main:raise ArticleUnavailable('article_body_missing')
            check_public_access(soup,main)
            if main.select('video,audio,iframe[src*="video"],iframe[src*="player"],.video-player,#player,.player-box,.video_box'):
                raise ArticleUnavailable('video_page_not_text_article')
            for n in main.select('script,style,nav,footer,form'):n.decompose()
            body_text=text(main)
            if len(body_text)<120:raise ArticleUnavailable('soft_error_or_empty_body')
            if len(title)<5:raise ValueError('article_title_missing')
    except urllib.error.HTTPError as e:
        if source.scope=='metadata_only' and e.code in (401,403):link_status='restricted'
        else:raise
    if not published:raise ValueError('publication_date_not_verified')
    # 原文只作后台审核材料；公共接口保留直链和短证据，不转载全文。
    access='free_full_text' if source.scope!='metadata_only' else link_status
    identity=source.identity if source.scope=='public_media' else 'professional_report' if source.scope=='metadata_only' else 'official_interpretation' if source.id=='nbs_analysis' else 'official_speech' if '/press/key/' in url else 'official_interview' if '/press/inter/' in url else source.identity
    fingerprint=hashlib.sha256(('source-parser-v3\n'+title+'\n'+published+'\n'+precision+'\n'+body_text+'\n'+access+'\n'+identity+'\n'+origin).encode()).hexdigest()
    return {'id':'a_'+hashlib.sha256(url.encode()).hexdigest()[:24],'source_id':source.id,'publisher':source.publisher,
        'title':title,'title_cn':title if re.search(r'[\u4e00-\u9fff]',title) else '',
        'url':url,'published_at':published,'published_precision':precision,'original_timezone':source.timezone,'content_updated_at':updated,
        'fetched_at':now_iso(),'checked_at':now_iso(),'read_scope':'metadata_only' if source.scope=='metadata_only' else 'full_text',
        'link_status':link_status,'access_status':access,'origin_publisher':origin,'page_author':author,
        'fingerprint':fingerprint,'evidence_text':body_text[:20000],
        'evidence_truncated':len(body_text)>20000,'identity':identity,
        'source_content_format':'text_article' if source.scope!='metadata_only' else 'metadata_only','page_has_video':False}

def score_article(a):
    t=a['title'].lower()
    importance=1 if re.search(r'政策|利率|贴息|采购经理|金融统计|货币|monetary|inflation|interest rate',t) else .55
    novelty=.8 if re.search(r'调整|通知|指数|增长|决定|statement|decision',t) else .5
    quality=(.85 if a.get('identity')=='media_report' else 1) if readable_article(a) else .35
    age=max(0,(datetime.now(UTC)-datetime.fromisoformat(a['published_at'])).total_seconds()/86400)
    recency=max(0,1-age/7)
    return round(.35*importance+.25*novelty+.2*quality+.1*recency,4),{'importance':importance,'new_fact':novelty,'support':quality,'recency':recency,'authorized_relevance':0,'weights_are_design_parameters':True}

def cluster_key(a):
    # 相同原始指标/政策的官方解读按同期间合并；转载不增加支持质量。
    t=a['title'];d=datetime.fromisoformat(article_reported_at(a)).astimezone(CN).strftime('%Y-%m')
    if a['source_id'].startswith('nbs') and re.search(r'采购经理|PMI',t,re.I):return 'pmi_'+d
    if a['source_id']=='pbc' and '购房贷款贴息' in t:return 'housing_subsidy_'+d
    if a.get('identity')=='media_report':
        origin=re.sub(r'新华网|新华社','新华社',a.get('origin_publisher') or a['publisher'])
        title=re.sub(r'新华社消息[丨|：:]|[\s，,。！!？?·丨|—－-]','',t)
        # 只合并同原始来源、同标题、同月份；不把两家转载算独立证实。
        return 'media_'+hashlib.sha256((origin+'\n'+title+'\n'+d).encode()).hexdigest()[:24]
    return a['id']
