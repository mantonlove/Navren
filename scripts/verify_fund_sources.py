"""核验公告全分页和PDF；不把历史销售关系升格为当前渠道许可。"""
import sys,re,json,subprocess
from pathlib import Path
from urllib.parse import urljoin
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fund_history import fetch,BASE,FUNDS,CACHE,SALE,cached,atomic
from bs4 import BeautifulSoup
risk_index=BASE+'/service/interactive/touzi/fxylb/index.html'
b,probe=fetch(risk_index);s=BeautifulSoup(b,'html.parser');risk_url=urljoin(BASE,next(a['href']for a in s.select('a[href]')if '.pdf' in a['href']))
texts={};proof={}
for label,url in [('risk',risk_url),('sale',SALE)]:
 b,p=fetch(url);f=CACHE/(label+'.pdf');f.write_bytes(b);texts[label]=subprocess.check_output(['pdftotext','-layout',str(f),'-']).decode();proof[label]=p
for code,(name,typ)in FUNDS.items():
 d=cached(code);root=f'{BASE}/products/{typ}/{code}/report/';b,p=fetch(root+'index.html');s=BeautifulSoup(b,'html.parser');pages=max([1]+[int(m.group(1))for a in s.select('a[href]')if(m:=re.fullmatch(r'index(\d+).html',a['href']))]);probes=[p];docs=[]
 for page in range(1,pages+1):
  if page>1:b,p=fetch(root+f'index{page}.html');probes.append(p);s=BeautifulSoup(b,'html.parser')
  for a in s.select('a[href]'):
   if '.pdf' in a['href']:docs.append({'title':a.get_text(strip=True),'url':urljoin(BASE,a['href'])})
 action_docs=[x for x in docs if any(k in x['title']for k in ['分红','拆分','折算'])]
 candidates=d['coverage']['corporate_action_candidates']
 recent=[x for x in action_docs if any(str(y)in x['title']for y in [2025,2026])]
 # 仅在实际公告清单完整分页且期间没有分红/折算公告或累计净值差异变化时，证明此期间无企业行为。
 complete=not candidates and not recent
 d['coverage'].update({'announcement_pages':pages,'announcement_documents':len(docs),'corporate_actions_complete':complete,'action_evidence':action_docs,'verification_basis':'已读管理人完整公告索引，并核验单位/累计净值差额期间恒定；有候选则阻断并待PDF逐笔核实'})
 d['product']['corporate_actions_complete']=complete;d['product']['corporate_actions']=action_docs
 assert code in texts['sale'],'销售公告没有此份额代码'
 d['product']['sales_evidence']['probe']=proof['sale']
 target=code if code!='021095' else '021094'
 line=next((l for l in texts['risk'].splitlines()if target+'.OF' in l),'');match=re.search(r'\bR([1-5])\b',line)
 d['product']['risk_level']=int(match[1])if match else None;d['product']['risk_evidence']={**proof['risk'],'line':line,'scope':'管理人风险名录，非微众当前评级'}
 d['probes']+=probes+[proof['sale'],proof['risk']];atomic(CACHE/(code+'.json'),d);print(code,'公告页',pages,'企业行为完整',complete,'风险',d['product']['risk_level'],flush=True)
atomic('output/equity/history_coverage.json',{'funds':{c:cached(c)['coverage']for c in FUNDS}})
atomic('output/equity/source_probe.json',{'sources':[p for c in FUNDS for p in cached(c)['probes']]})
atomic('output/equity/product_evidence.json',{'products':[{k:v for k,v in cached(c)['product'].items()if k not in ['summary','report']}for c in FUNDS]})
