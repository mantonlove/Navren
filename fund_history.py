"""管理人公开网页适配器：分页、修订、企业行为和销售状态分别记录。"""
from pathlib import Path
from datetime import datetime,timezone,date
from decimal import Decimal
import hashlib,json,time,os
from urllib.request import Request,urlopen
from urllib.parse import urlencode,urljoin

BASE='https://www.orient-fund.com'
SALE=BASE+'/upload/pdf/1747116276433-gyzjszqhwzyxgfyxgswgsqxbfjjxsjgdgg.pdf'
FUNDS={'006210':('东方臻宝纯债 A','zhaiquan'),'001198':('东方惠新灵活配置混合 A','hunhe'),'021095':('东方低碳经济混合 C','hunhe')}
CACHE=Path(os.environ.get('NAVREN_FUND_CACHE',str(Path.home()/'Library/Application Support/Navren/funds')))


def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));os.replace(tmp,path)


def fetch(url):
    with urlopen(Request(url,headers={'User-Agent':'Navren/1.0 public-page verification','Accept':'application/json,text/html'}),timeout=15)as r:
        body=r.read(12000000)
        if len(body)>=12000000:raise ValueError('源文件超限')
        return body,{'url':url,'status':r.status,'sha256':hashlib.sha256(body).hexdigest(),'read_at':datetime.now(timezone.utc).isoformat(),'bytes':len(body)}


def collect(code,start,end,cache=CACHE):
    from bs4 import BeautifulSoup
    if code not in FUNDS:raise ValueError('份额代码未登记')
    date.fromisoformat(start);date.fromisoformat(end)
    name,typ=FUNDS[code];rows={};probes=[];duplicate=0;changes=[];total=None
    for page in range(1,101):
        url=BASE+'/chart-web/chart/fundnettable!getFundNetTableJson?'+urlencode({'fundcode':code,'from':start,'to':end,'pages':f'{page}-30'})
        body,probe=fetch(url);d=json.loads(body);probes.append(probe)
        if d.get('currentPage')!=page or type(d.get('totalPage'))is not int or not 1<=d['totalPage']<=100:raise ValueError('分页元数据无效')
        if total is not None and total!=d['totalPage']:raise ValueError('采集中分页总量变化')
        total=d['totalPage'];part=d.get('dataList')
        if not isinstance(part,list)or not part:raise ValueError('分页空记录')
        for raw in part:
            on=raw['date'];date.fromisoformat(on)
            if raw['fundcode']!=code or not start<=on<=end:raise ValueError('代码或日期越界')
            nav=Decimal(str(raw['netvalue']));cum=Decimal(str(raw['totalnetvalue']))
            if not nav.is_finite()or nav<=0 or not cum.is_finite():raise ValueError('净值无效')
            r={'fund_id':'ORIENT:'+code,'share_code':code,'nav_on':on,'unit_nav':str(raw['netvalue']),'cumulative_nav':str(raw['totalnetvalue']),'rate':None if raw.get('rate')==-999999.99 else raw.get('rate'),'raw':raw,'source':probe}
            if on in rows:
                duplicate+=1
                if rows[on]['raw']!=raw:changes.append({'date':on,'old':rows[on],'new':r})
            rows[on]=r
        if page==total:break
        time.sleep(.15)
    oldpath=Path(cache)/(code+'.json');old=json.loads(oldpath.read_text())if oldpath.exists()else{}
    oldmap={r['nav_on']:r for r in old.get('navs',[])}
    for on,r in rows.items():
        if on in oldmap and (oldmap[on]['unit_nav'],oldmap[on]['cumulative_nav'])!=(r['unit_nav'],r['cumulative_nav']):changes.append({'date':on,'old':oldmap[on],'new':r})
    navs=sorted(rows.values(),key=lambda r:r['nav_on']);gaps=[]
    for a,b in zip(navs,navs[1:]):
        diff=(Decimal(b['cumulative_nav'])-Decimal(b['unit_nav']))-(Decimal(a['cumulative_nav'])-Decimal(a['unit_nav']))
        if diff:gaps.append({'from':a['nav_on'],'to':b['nav_on'],'cumulative_unit_gap_change':str(diff),'state':'needs_official_action_evidence'})
    product={'fund_id':'ORIENT:'+code,'share_code':code,'name':name,'manager':'东方基金管理股份有限公司','registrar':'东方基金管理股份有限公司','currency':'CNY','detail_url':f'{BASE}/products/{typ}/{code}/index.html','sales_evidence':{'url':SALE,'effective_on':'2025-05-15','status':'historical_relationship'},'current_channel_status':'unknown','risk_level':None,'executable':False,'fee_rules':{},'corporate_actions':[]}
    for kind in ['summary','feilv','report']:
        body,probe=fetch(f'{BASE}/products/{typ}/{code}/{kind}/index.html');probes.append(probe);s=BeautifulSoup(body,'html.parser')
        for tag in s(['script','style']):tag.decompose()
        text=s.get_text(' ',strip=True)
        product[kind]={'url':probe['url'],'sha256':probe['sha256'],'text':text,'documents':[{'title':a.get_text(strip=True),'url':urljoin(BASE,a['href'])} for a in s.select('a[href]')if '.pdf' in a['href']]}
        if kind=='feilv':product['fee_rules']={'status':'official_page','text':text,'source':probe,'channel_discount':'unknown','notice':'管理/托管/销售服务费已体现在净值中；不再额外扣。申赎费用按具体确认交易，渠道折扣未核验。'}
    data={'schema_version':1,'product':product,'navs':navs,'revision_history':old.get('revision_history',[])+changes,'probes':probes,'coverage':{'requested_from':start,'requested_to':end,'actual_from':navs[0]['nav_on'],'actual_to':navs[-1]['nav_on'],'pages':total,'rows':len(navs),'duplicate_rows':duplicate,'revisions':len(changes),'page_complete':True,'undisclosed_days':'不插值；未列日期不假定零收益','corporate_action_candidates':gaps,'corporate_actions_complete':False,'current_channel_status':'unknown','public_use_scope':'公开网页技术可读；没有验证批量再分发许可，缓存仅用于本地原型，非官方开放API'}}
    atomic(oldpath,data);return data


def cached(code,cache=CACHE):
    p=Path(cache)/(code+'.json')
    try:return json.loads(p.read_text())if p.exists()else None
    except (OSError,ValueError):return None
