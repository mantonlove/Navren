"""免费原文、稿件身份、转载合并及访问变化的发布边界。全程离线。"""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import urllib.request

from market_sources import REGISTRY, ArticleUnavailable, GuardedRedirect, cluster_key, discover, read_article, readable_article, safe_url
from market_content import MarketStore
from market_pipeline import refresh, select_candidates
from market_editor import MarketEditor

XURL='https://www.news.cn/fortune/20260930/'+'b'*32+'/c.html'
CURL='https://finance.cctv.com/2026/09/30/ARTItest260930.shtml'
BODY='报道介绍金融政策变化，并说明居民资金安排仍需结合实际条件。'*12

def page(kind='xinhua',extra='',body=BODY):
    if kind=='xinhua':
        return '<meta name="source" content="新华网"><meta name="publishdate" content="2026-09-30"><div class="title">金融政策与居民资金安排</div><div class="header-time"><span class="time">11:18:09</span></div><div id="detailContent">'+body+extra+'</div>'
    return '<meta name="source" content="新华网"><meta name="author" content="责任编辑"><meta property="og:title" content="金融政策与居民资金安排"><script>var publishDate ="20260930111846";</script><div id="title_area"><h1>金融政策与居民资金安排</h1><div class="info">2026年09月30日 11:18</div></div><div id="content_area">'+body+extra+'</div>'

def article(kind='xinhua',html=None):
    url=XURL if kind=='xinhua' else CURL
    with patch('market_sources.fetch',return_value=((html or page(kind)).encode(),url,'text/html')):
        return {**read_article(REGISTRY[kind],{'title':'列表标题','url':url}),'version':1,'status':'active'}

class Client:
    model_id='test-public-reader'
    def __init__(self,result):self.result=result;self.calls=[]
    def complete_json(self,**kwargs):self.calls.append(kwargs);return copy.deepcopy(self.result)

class PublicSourceTests(unittest.TestCase):
    def test_media_fields_and_exact_publication_clock(self):
        a=article();b=article('cctv')
        self.assertEqual(a['published_at'],'2026-09-30T03:18:09+00:00')
        self.assertEqual(b['published_at'],'2026-09-30T03:18:46+00:00')
        self.assertEqual(a['published_precision'],'second')
        for item in (a,b):
            self.assertTrue(readable_article(item));self.assertEqual(item['identity'],'media_report')
            public=MarketStore.public_article(item)
            self.assertEqual(public['identity'],'media_report');self.assertEqual(public['origin_publisher'],'新华网')
            self.assertNotIn('evidence_text',public);self.assertLessEqual(len(public['evidence_excerpt']),120)
        self.assertEqual(b['page_author'],'责任编辑')

    def test_long_preview_paywall_login_and_challenge_rejected(self):
        markers=['请订阅以阅读完整文章','登录后查看全文','<div class="paywall">开通会员</div>','<script type="application/ld+json">{"isAccessibleForFree":false}</script>','验证码 访问验证']
        for marker in markers:
            with self.subTest(marker=marker),self.assertRaises(ArticleUnavailable):article(html=page(extra=marker) if marker!='验证码 访问验证' else page(body=marker+' '+BODY))

    def test_missing_media_access_flag_cannot_claim_free_full_text(self):
        a=article();a.pop('access_status');self.assertFalse(readable_article(a))

    def test_gate_outside_body_and_plain_menu_login(self):
        for notice in ('登录后查看全文','请订阅以阅读完整文章'):
            with self.subTest(notice=notice),self.assertRaises(ArticleUnavailable):article(html=page()+'<div class="member-notice">'+notice+'</div>')
        self.assertTrue(readable_article(article(html=page()+'<nav>登录 会员中心</nav>')))

    def test_china_and_us_pmi_reports_do_not_merge(self):
        a={**article(),'title':'中国制造业采购经理指数PMI为50.1%'}
        b={**article('cctv'),'title':'美国制造业PMI下降至48.7%'}
        self.assertNotEqual(cluster_key(a),cluster_key(b))

    def test_selected_coverage_retains_primary_sources_and_two_media_channels(self):
        candidates=[{'id':'official'+str(i),'articles':[{'identity':'official_release','source_id':'nbs'}]} for i in range(6)]
        candidates += [{'id':'x'+str(i),'articles':[{'identity':'media_report','source_id':'xinhua'}]} for i in range(3)]
        candidates += [{'id':'c','articles':[{'identity':'media_report','source_id':'cctv'}]}]
        selected=select_candidates(candidates,6)
        self.assertEqual(len(selected),6);self.assertEqual([x['id'] for x in selected][-2:],['x0','c'])
        weekly=select_candidates(candidates,3)
        self.assertEqual([x['id'] for x in weekly],['official0','official1','x0'])
        self.assertEqual(select_candidates(candidates[:2],6),candidates[:2])
        self.assertEqual(len(select_candidates(candidates[6:],6)),2,'原始发布不足时不靠堆媒体凑条数')
        self.assertEqual(len(select_candidates(candidates[6:],3)),1,'周报候选最多一条精选媒体报道')

    def test_media_republication_is_one_cluster(self):
        a=article();b=article('cctv')
        self.assertNotEqual(a['id'],b['id']);self.assertEqual(cluster_key(a),cluster_key(b))
        other={**b,'origin_publisher':'另一家原始媒体'}
        self.assertNotEqual(cluster_key(a),cluster_key(other))

    def test_article_url_and_redirect_boundaries(self):
        self.assertEqual(safe_url(REGISTRY['xinhua'],XURL+'?tracking=1#read'),XURL)
        for u in ['https://www.news.cn/fortune/index.htm',XURL.replace('www.news.cn','evil.test'),XURL.replace('www.news.cn','www.news.cn:80')]:
            with self.assertRaises(ValueError):safe_url(REGISTRY['xinhua'],u)
        with self.assertRaises(ValueError):GuardedRedirect(REGISTRY['xinhua'],True).redirect_request(None,None,302,'',{},XURL.replace('https:','http:'))
        req=urllib.request.Request(XURL)
        followed=GuardedRedirect(REGISTRY['xinhua'],True).redirect_request(req,None,302,'',{},XURL+'?tracking=1')
        self.assertEqual(followed.full_url,XURL)

    def test_discovery_ignores_old_topics_and_outside_domains(self):
        recent=datetime.now(timezone.utc).strftime('%Y%m%d')
        good=XURL.replace('20260930',recent)
        raw=('<div id="txtMarquee-left"><div class="infoList"><a href="'+XURL.replace('20260930','20200101')+'">旧专题长期保留</a><a href="'+good+'">金融政策最新发布</a><a href="https://evil.test/article.html">外部栏目</a></div></div>').encode()
        with patch('market_sources.fetch',return_value=(raw,REGISTRY['xinhua'].url,'text/html')):
            entries,state=discover(REGISTRY['xinhua'],limit=5)
        self.assertEqual([e['url'] for e in entries],[good]);self.assertEqual(state['discovered'],1)
        self.assertNotIn('published_at',entries[0],'路径日期不能冒充原文发表时间')

    def test_disabled_ft_cache_never_in_recommended_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=MarketStore(Path(tmp)/'market.sqlite3');a=article()
            store.save_article({**a,'id':'a_ft','source_id':'ft','publisher':'金融时报','url':'https://www.ft.com/content/'+'a'*8+'-'+ 'b'*27,'published_at':datetime.now(timezone.utc).isoformat(),'read_scope':'metadata_only','link_status':'subscription_required'})
            self.assertEqual(store.hotspots()['reading'],[])
            self.assertEqual(store.article('a_ft')['status'],'reading_unavailable')
            self.assertEqual(next(s for s in store.source_states() if s['id']=='ft')['status'],'disabled')

    def test_access_failure_preserves_archive_identity_but_withholds_unverified_reading(self):
        import urllib.error
        failures=[(ArticleUnavailable('subscription_required'),'temporarily_unavailable'),
            (urllib.error.HTTPError(XURL,401,'Sign in required',None,None),'temporarily_unavailable'),
            (urllib.error.HTTPError(XURL,403,'Access denied',None,None),'temporarily_unavailable'),
            (TimeoutError(),'temporarily_unavailable'),
            (urllib.error.HTTPError(XURL,404,'Not found',None,None),'withdrawn')]
        for error,expected_status in failures:
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as tmp:
                store=MarketStore(Path(tmp)/'market.sqlite3');a=article();a['published_at']=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat();store.save_article(a)
                event={'id':'e_public','headline':'金融政策报道','published_at':a['published_at'],
                    'article_ids':[a['id']],'article_versions':{a['id']:1},'primary_article_id':a['id'],
                    'score':1,'review':{'status':'approved'},'category':'macro_policy',
                    'summary':'报道介绍金融政策变化。','analysis':'具体安排仍取决于实施条件。','uncertainty':'需要继续核验。',
                    'facts':[{'text':'报道介绍金融政策变化','quote':'报道介绍金融政策变化','article_id':a['id']}]}
                store.publish('market_events',event['id'],event,'event-v1')
                store.publish('market_weeklies','2026-09-28',{'title':'周报','period':'2026-09-28','article_ids':[a['id']]},'weekly-v1')
                store.update_daily(run_id='verified-before-access-failure',valid_check=True)
                before=store.hotspots();self.assertEqual(len(before['events']),1)
                self.assertTrue(store.weekly()['report'])
                def discovery(source,*args):return [],{'source_id':source.id,'status':'ok','checked_at':datetime.now(timezone.utc).isoformat()}
                with patch('market_pipeline.discover',side_effect=discovery),patch('market_pipeline.read_article',side_effect=error):refresh(store,ingest_only=True,force_check=True,total_seconds=5)
                cached=store.articles()[0]
                self.assertEqual(cached['status'],expected_status)
                self.assertEqual(cached['version'],1);self.assertEqual(cached['evidence_text'],a['evidence_text'])
                after=store.hotspots();self.assertEqual(after['original_count'],1);self.assertEqual(after['readable_count'],0)
                self.assertEqual(after['revision'],before['revision']);self.assertEqual(len(after['events']),1)
                row=after['events'][0];self.assertEqual(row['id'],event['id']);self.assertEqual(row['availability'],expected_status)
                self.assertEqual(row['summary'],'');self.assertNotIn('facts',row)
                detail=store.history_event(after['edition_date'],event['id'],after['revision'],expected_version=1)
                self.assertEqual(detail['facts'],[]);self.assertEqual(detail['analysis'],'')
                self.assertIsNone(store.weekly()['report'])

    def test_media_editor_retains_origin_and_does_not_label_official(self):
        a=article();draft={'events':[{'id':'e_media','headline':a['title'],'summary':'这是一篇金融政策报道。','analysis':'传导仍取决于具体政策条件。','uncertainty':'需要继续观察。','facts':[{'text':'报道介绍金融政策变化。','article_id':a['id'],'quote':'报道介绍金融政策变化'}]}]}
        primary=Client(draft);reviewer=Client({'verdict':'approve','reason':'对应原文事实。','issues':[]})
        result=MarketEditor(primary,reviewer).events([{'id':'e_media','articles':[a]}],'test-public')
        self.assertEqual(result[0]['identity'],'media_report_and_navren_analysis')
        self.assertIn('origin_publisher',primary.calls[0]['user']);self.assertIn('media_report',reviewer.calls[0]['user'])

if __name__=='__main__':unittest.main()
