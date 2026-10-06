import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pathlib import Path
from datetime import datetime
from market_content import MarketStore,CN
s=MarketStore();articles={a['id']:a for a in s.articles()};result={'article_count':len(articles),'events':[],'weeklies':[]}
for e in s.versions('market_events'):
 if e['status']!='published':continue
 refs=[articles[i] for i in e['article_ids']]
 assert all(a['status']=='active' for a in refs)
 assert all(f['quote'] in articles[f['article_id']]['evidence_text'] for f in e['facts'])
 assert e['review']['status']=='approved'
 result['events'].append({'id':e['id'],'version':e['version'],'review':e['review'],'articles':[{'id':a['id'],'version':a['version'],'published_at':a['published_at'],'read_scope':a['read_scope'],'url':a['url']} for a in refs],'verified_exact_quotes':len(e['facts'])})
for w in s.versions('market_weeklies'):
 if w['status']!='published':continue
 chars=len(w['summary'])+len(w['transmission'])+sum(len(c['heading'])+len(c['text'])+len(c['conditions']) for c in w['changes'])
 assert chars<=1550
 begin=datetime.fromisoformat(w['period_start']);end=datetime.fromisoformat(w['period_end_exclusive'])
 assert all(begin<=datetime.fromisoformat(articles[i]['published_at']).astimezone(CN)<end for i in w['article_ids'])
 result['weeklies'].append({'period':w['period'],'version':w['version'],'characters':chars,'review':w['review'],'all_articles_in_period':True,'source_count':len(w['article_ids'])})
assert len(result['weeklies'])>=2
assert len({a['id'] for e in result['events'] for a in e['articles']})>=10
Path('outputs/market-acceptance/provenance-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(len(result['events']),result['weeklies'])
