"""增量采集与发布；进程锁、截止时间、失败保留最近合格版本。"""
from __future__ import annotations
import concurrent.futures, fcntl, hashlib, time, uuid
import logging, urllib.error
from datetime import datetime, timedelta, timezone
from market_sources import SOURCES, discover, read_article, score_article, cluster_key, now_iso, readable_article, ArticleUnavailable, text_only_article, source_verified_text_event
from market_content import MarketStore, week_periods, packed, CN
from market_editor import MarketEditor
from market_schedule import plan

EDITOR_VERSION='market-editor-2-public-media'
def input_hash(g):return hashlib.sha256(packed({'editor':EDITOR_VERSION,'articles':[(a['id'],a['fingerprint']) for a in g['articles']]}).encode()).hexdigest()

def failure(error):
    """保存可诊断的错误类型，不存可能含凭据的完整异常字符串。"""
    reason=getattr(error,'reason',error)
    return {'reason':type(error).__name__, 'error_type':type(reason).__name__,
        'error_code':getattr(error,'code',None) or getattr(reason,'errno',None)}

def select_candidates(candidates,limit):
    """评分排序不变，少量媒体席位优先覆盖不同采集渠道。"""
    media=[g for g in candidates if g.get('articles') and all(a.get('identity')=='media_report' for a in g['articles'])]
    official=[g for g in candidates if g not in media]
    slots=max(1,limit//3);selected=[];sources=set()
    for g in media:
        ids={a['source_id'] for a in g['articles']}
        if ids-sources:selected.append(g);sources.update(ids)
        if len(selected)>=slots:break
    for g in media:
        if len(selected)>=slots:break
        if g not in selected:selected.append(g)
    picks=official[:limit-len(selected)]+selected
    # 原始发布不足时，用同样已通过正文检查的权威媒体补齐。
    # 不因媒体席位上限让充足的文字候选最终只展示两张。
    for g in candidates:
        if len(picks)>=limit:break
        if g not in picks:picks.append(g)
    return sorted(picks,key=lambda g:candidates.index(g))

def refresh(store=None,editor=None,*,source_limit=15,backfill_weeks=2,ingest_only=False,total_seconds=720,force_check=False,scheduled=False,sources=None,hotspots_only=False):
    store=store or MarketStore();id=uuid.uuid4().hex;started=now_iso();deadline=time.monotonic()+total_seconds
    lock=open(str(store.path)+'.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:lock.close();return {'status':'already_running'}
    schedule=plan(store)
    jobs=schedule['jobs'] if scheduled else ['hotspots','weekly']
    if hotspots_only:jobs=[j for j in jobs if j=='hotspots']
    if not jobs:
        fcntl.flock(lock,fcntl.LOCK_UN);lock.close();return {'status':'not_due'}
    if ingest_only:jobs=['ingest']
    periods=week_periods(backfill_weeks)
    if scheduled and 'weekly' in jobs:
        start=datetime.fromisoformat(schedule['targets']['weekly']).replace(tzinfo=CN)
        periods=[(start,start+timedelta(days=7))]
    if not scheduled:schedule['targets']['weekly']=periods[0][0].date().isoformat()
    report={'id':id,'started_at':started,'status':'running','targets':schedule['targets'],
        'streams':{k:{'status':'running'} for k in jobs},'sources':[],'new_or_revised':0,'published_events':0,'published_weeklies':0,'rejected':[]}
    store.record_run(id,report)
    try:
        previous={a['url']:a for a in store.articles()}
        dependencies={i for table in ('market_events','market_weeklies') for body in store.versions(table)
                      if body['status']=='published' for i in body.get('article_ids',[])}
        def ingest(source):
            try:entries,state=discover(source,source_limit,35)
            except Exception as error:
                entries=[];state={'source_id':source.id,'status':'failed','checked_at':now_iso(),**failure(error)}
            accepted=[];rejected=[];withdrawn=[];unavailable=[]
            urls={e['url'] for e in entries}
            known=sorted((a for a in previous.values() if a['source_id']==source.id and a['status']=='active' and (a['id'] in dependencies or (source.scope=='public_media' and datetime.fromisoformat(a['published_at'])>=datetime.now(timezone.utc)-timedelta(days=35))) and a['url'] not in urls),key=lambda a:a['checked_at'])
            entries += known[:max(15,source_limit)]
            for entry in entries:
                if time.monotonic()>deadline:rejected.append({'reason':'run_deadline'});break
                old=previous.get(entry['url'])
                if not force_check and old and old['status']=='active' and (datetime.now(timezone.utc)-datetime.fromisoformat(old['checked_at'])).total_seconds()<10800:continue
                try:
                    article=read_article(source,entry)
                    if old and old.get('content_mode')=='source_verified_text' and old['status']=='active':
                        # 历史正文含段落换行，日常解析器折叠空白；解析格式变化不构成原文修订。
                        # 相同文字、标题和发表时刻保留旧证据绑定，不重写30日历史快照。
                        compact=lambda value:''.join(value.split())
                        if (article['title']==old['title']
                                and datetime.fromisoformat(article['published_at'])==datetime.fromisoformat(old['published_at'])
                                and not article.get('evidence_truncated')
                                and compact(article['evidence_text'])==compact(old.get('evidence_text',''))):
                            continue
                    accepted.append(article)
                except Exception as error:
                    code=getattr(error,'code',None)
                    rejected.append({'url':entry['url'],**failure(error)})
                    if state['status']=='ok' and isinstance(error,(urllib.error.URLError,TimeoutError,OSError)) and code not in (404,410):
                        state.update(status='partial',**failure(error))
                    if old:
                        if code in (404,410):withdrawn.append(old['id'])
                        elif isinstance(error,ArticleUnavailable) or code in (401,403):unavailable.append(old['id'])
            if rejected and state['status']=='ok':state['status']='partial'
            state.update(accepted=len(accepted),rejected=len(rejected));return source.id,state,accepted,rejected,withdrawn,unavailable
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures={pool.submit(ingest,s):s for s in (sources if sources is not None else SOURCES) if s.enabled}
            for f in concurrent.futures.as_completed(futures):
                s=futures[f]
                try:
                    sid,state,articles,rejected,withdrawn,unavailable=f.result()
                    for a in articles:report['new_or_revised']+=int(store.save_article(a))
                    for a in withdrawn:store.withdraw_article(a)
                    for a in unavailable:store.unavailable_article(a)
                    report['rejected'].extend(rejected)
                except Exception as error:state={'source_id':s.id,'status':'failed','checked_at':now_iso(),**failure(error)}
                store.source_status(s.id,state);report['sources'].append(state)
        if not ingest_only and time.monotonic()<deadline:
            editor=editor or MarketEditor();clusters={}
            current_articles={a['id']:a for a in store.articles()}
            for a in current_articles.values():
                if a['status']=='active' and text_only_article(a):clusters.setdefault(cluster_key(a),[]).append(a)
            groups=[]
            for key,items in clusters.items():
                items.sort(key=lambda a:(a.get('identity')=='media_report',a['source_id']=='nbs_analysis',a['published_at']))
                groups.append({'id':'e_'+hashlib.sha256(key.encode()).hexdigest()[:24],'articles':items})
            groups.sort(key=lambda g:score_article(g['articles'][0])[0],reverse=True)
            selected=[];seen=set();now=datetime.now(timezone.utc)
            scopes=([(now.astimezone(CN).replace(hour=0,minute=0,second=0,microsecond=0),now,6)] if 'hotspots' in jobs else [])+([(a,b,3) for a,b in periods] if 'weekly' in jobs else [])
            for start,end,limit in scopes:
                candidates=[g for g in groups if start<=min(datetime.fromisoformat(a['published_at']) for a in g['articles'])<end]
                # 在评分候选中留少量媒体报道，兼顾原始公告与市场信息；转载组仍只占一条。
                for g in select_candidates(candidates,limit):
                    if g['id'] not in seen:selected.append(g);seen.add(g['id'])
            existing={e['id']:e for e in store.versions('market_events')}
            verified_text_bindings={tuple(sorted(e['article_versions'].items())) for e in existing.values()
                if e['status']=='published' and e.get('content_mode')=='source_verified_text'
                and source_verified_text_event(e,[current_articles[i] for i in e['article_ids'] if i in current_articles])}
            pending=[g for g in selected if not (
                (g['id'] in existing and existing[g['id']]['status']=='published' and existing[g['id']].get('input_hash')==input_hash(g))
                or tuple(sorted((a['id'],a['version']) for a in g['articles'])) in verified_text_bindings)]
            for offset in range(len(pending)):
                if time.monotonic()+95>deadline:report['rejected'].append({'stage':'editor','reason':'run_deadline'});break
                # 单事件独立审核，避免一条引文失败阻断其他合格报道。
                batch=pending[offset:offset+1]
                try:
                    for e in editor.events(batch,id):
                        h=input_hash(next(g for g in batch if g['id']==e['id']));e['input_hash']=h
                        report['published_events']+=int(store.publish('market_events',e['id'],e,h))
                except Exception as error:report['rejected'].append({'stage':'event_review','ids':[g['id'] for g in batch],'reason':getattr(error,'code',str(error))[:120]})
            published=[store.event(e['id']) for e in store.versions('market_events') if e['status']=='published']
            published=[e for e in published if e['status']=='published' and e.get('articles')
                       and all(text_only_article(a) for a in e['articles'])]
            existing_weeklies={w['period']:w for w in store.versions('market_weeklies')}
            for start,end in periods if 'weekly' in jobs else []:
                if scheduled and start.date().isoformat()!=schedule['targets']['weekly']:continue
                if time.monotonic()+95>deadline:
                    report['rejected'].append({'stage':'weekly_review','reason':'run_deadline'});break
                candidates=sorted((e for e in published if start<=datetime.fromisoformat(e['published_at']).astimezone(CN)<end),
                    key=lambda e:e.get('score') if isinstance(e.get('score'),(int,float)) else score_article(e['articles'][0])[0],reverse=True)
                events=select_candidates(candidates,3)
                if not events:
                    report['rejected'].append({'stage':'weekly_review','period':start.date().isoformat(),'reason':'no_verified_events'});continue
                h=hashlib.sha256(packed({'editor':'weekly-v2','events':[(e['id'],e['version']) for e in events]}).encode()).hexdigest();old=existing_weeklies.get(start.date().isoformat())
                if old and old['status']=='published' and old.get('input_hash')==h:continue
                try:
                    weekly=editor.weekly(events,start,end,id);weekly['input_hash']=h;weekly['reading']=[store.article(i) for i in weekly['article_ids'][:5]]
                    report['published_weeklies']+=int(store.publish('market_weeklies',weekly['period'],weekly,h))
                except Exception as error:report['rejected'].append({'stage':'weekly_review','period':start.date().isoformat(),'reason':getattr(error,'code',str(error))[:120]})
        good=any(s['status']=='ok' and s['source_id']!='ft' for s in report['sources'])
        hot_errors=any(x.get('stage') in ('editor','event_review') for x in report['rejected'])
        limited=any(s['status']!='ok' for s in report['sources']) or hot_errors
        if 'hotspots' in jobs:report['streams']['hotspots']={'status':'ok' if good and not limited else 'partial' if good else 'failed'}
        if 'hotspots' in jobs:
            # Publication fingerprints and the calendar-day edition are independent.
            # An identical approved event set still creates today's first edition.
            report['daily_edition']=store.update_daily(run_id=id,valid_check=good,
                coverage={'limited':limited,'sources':report['sources'],'scope':'registered_public_sources'})
        if 'ingest' in jobs:report['streams']['ingest']={'status':'ok' if good and not limited else 'partial' if good else 'failed'}
        if 'weekly' in jobs:
            target=report['targets']['weekly'];ready=next((w for w in store.versions('market_weeklies') if w['period']==target and w['status']=='published'),None)
            report['streams']['weekly']={'status':'ok' if ready else 'failed','period':target,'version':ready['version'] if ready else None}
        report['status']='failed' if all(s['status']=='failed' for s in report['streams'].values()) else 'complete_with_limits' if report['rejected'] or limited or any(s['status']!='ok' for s in report['streams'].values()) else 'complete'
    except Exception as error:
        report['status']='failed';report['rejected'].append({'stage':'pipeline',**failure(error)})
        report['streams']={k:{'status':'failed'} for k in jobs}
    finally:
        # Recovery seals only actually generated past editions, never invents a missing day.
        store.seal_daily()
        if 'hotspots' in jobs and 'daily_edition' not in report:
            store.update_daily(run_id=id,valid_check=False,coverage={'limited':True,'check_state':'failed'})
        report['ended_at']=now_iso();report['elapsed_seconds']=round(time.monotonic()-(deadline-total_seconds),2)
        store.record_run(id,report);fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
    return report

def due(store,now=None):
    return bool(plan(store,now)['jobs'])

def start_background(store):
    import threading
    stop=threading.Event()
    def worker():
        while not stop.is_set():
            try:
                if due(store):refresh(store,backfill_weeks=1,scheduled=True)
            except Exception:logging.exception('市场定时任务异常')
            stop.wait(60)
    threading.Thread(target=worker,name='navren-market-refresh',daemon=True).start();return stop
