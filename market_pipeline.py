"""增量采集与发布；进程锁、截止时间、失败保留最近合格版本。"""
from __future__ import annotations
import concurrent.futures, fcntl, hashlib, time, uuid
import logging, re, urllib.error
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from market_sources import SOURCES, discover, read_article, score_article, cluster_key, now_iso, readable_article, ArticleUnavailable, text_only_article, source_verified_text_event
from market_content import MarketStore, week_periods, packed, CN, WEEKLY_GENERATION_CONTRACT, event_input_hash, EVENT_EDITOR_POLICY
from market_editor import MarketEditor
from market_model import market_model_session
from market_schedule import plan
from market_daily import DailyMarket
from market_source_attribution import first_publication_verified

EDITOR_VERSION=EVENT_EDITOR_POLICY
DAILY_HOTSPOT_COUNT=6
DAILY_CANDIDATE_LIMIT=18
# 周报最多三次90秒主写及45秒审核，加5秒本地余量；这是准入预留，并非墙钟超时器。
WEEKLY_RESERVE_SECONDS=320
def input_hash(g):return event_input_hash(g)

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

def daily_hotspot_candidates(groups, now, limit=DAILY_CANDIDATE_LIMIT):
    """选稿与日版使用同一首次发布日期，历史高分稿不占当天候选席位。"""
    now=now.astimezone(CN);eligible=[]
    for group in groups:
        try:
            primary=group['articles'][0]
            if not first_publication_verified(primary):continue
            published=datetime.fromisoformat(primary['published_at'])
            if (published.tzinfo is None or published>now
                    or published.astimezone(CN).date()!=now.date()
                    or primary.get('reported_time_kind')=='source_page_updated_at'):continue
        except (KeyError,IndexError,ValueError,TypeError):continue
        if primary.get('identity')=='media_report' and re.search(r'品牌焕新|圆满收官|招商推介|广告|酒业管理层',primary.get('title','')):continue
        eligible.append(group)
    # 同一天内优先银行客户关心的金融、保障和宏观议题，再使用其他经济报道。
    # 这是选稿顺序，不构成事实、适当性或用户账户授权。
    financial=lambda group:bool(re.search(r'银行|存款|存单|理财|债券|基金|证券|保险|金融|利率|汇率|货币|政策|财政|税|经济|物价|就业',group['articles'][0].get('title','')))
    eligible.sort(key=financial,reverse=True)
    return select_candidates(eligible,limit)

def unchanged_historical_contract_upgrade(old,article,now):
    """元数据解析器升级不作原报道修订；旧日证据原封保留，不赋予今日资格。"""
    if not old or old.get('status')!='active' or old.get('source_attribution') or not article.get('source_attribution'):return False
    try:
        published=datetime.fromisoformat(old['published_at'])
        if published.astimezone(CN).date()>=now.astimezone(CN).date() or published!=datetime.fromisoformat(article['published_at']):return False
    except (ValueError,TypeError,KeyError):return False
    compact=lambda text:''.join((text or '').split())
    return (old.get('id')==article.get('id') and old.get('source_id')==article.get('source_id')
        and old.get('title')==article.get('title') and old.get('identity')==article.get('identity')
        and old.get('access_status')==article.get('access_status')
        and not article.get('evidence_truncated') and not old.get('evidence_truncated')
        and compact(old.get('evidence_text'))==compact(article.get('evidence_text')))

def daily_qualified_count(store):
    with store.db() as db:return len(DailyMarket(store).candidates(db,datetime.now(CN)))

@market_model_session
def refresh(store=None,editor=None,*,source_limit=30,backfill_weeks=2,ingest_only=False,total_seconds=720,force_check=False,scheduled=False,sources=None,hotspots_only=False):
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
                    if unchanged_historical_contract_upgrade(old,article,datetime.now(timezone.utc)):continue
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
                try:
                    stamp=datetime.fromisoformat(a['published_at'])
                    if stamp.tzinfo is None:continue
                except (ValueError,KeyError,TypeError):continue
                if a['status']=='active' and text_only_article(a):clusters.setdefault(cluster_key(a),[]).append(a)
            groups=[]
            for key,items in clusters.items():
                items.sort(key=lambda a:(a.get('identity')=='media_report',a['source_id']=='nbs_analysis',a['published_at']))
                groups.append({'id':'e_'+hashlib.sha256(key.encode()).hexdigest()[:24],'articles':items})
            groups.sort(key=lambda g:score_article(g['articles'][0])[0],reverse=True)
            selected=[];seen=set();weekly_ids=set();now=datetime.now(timezone.utc)
            existing={e['id']:e for e in store.versions('market_events')}
            scopes=([('weekly',a,b,3) for a,b in periods] if 'weekly' in jobs else [])+([('hotspots',now,now,DAILY_CANDIDATE_LIMIT)] if 'hotspots' in jobs else [])
            for kind,start,end,limit in scopes:
                if kind=='hotspots':
                    # 多留经过原文核对的备选；一条审核失败后继续下一条，
                    # 达到六个独立、合格事件后停止生成，不为备选无限调用模型。
                    chosen=daily_hotspot_candidates(groups,now,limit)
                else:
                    candidates=[g for g in groups if start<=min(datetime.fromisoformat(a['published_at']) for a in g['articles'])<end]
                    chosen=select_candidates(candidates,limit)
                if kind=='weekly':weekly_ids.update(g['id'] for g in chosen)
                # 在评分候选中留少量媒体报道，兼顾原始公告与市场信息；转载组仍只占一条。
                for g in chosen:
                    if g['id'] not in seen:selected.append(g);seen.add(g['id'])
            verified_text_bindings={tuple(sorted(e['article_versions'].items())) for e in existing.values()
                if e['status']=='published' and e.get('content_mode')=='source_verified_text'
                and source_verified_text_event(e,[current_articles[i] for i in e['article_ids'] if i in current_articles]) and store._event_is_reusable(e)}
            pending=[g for g in selected if not (
                (g['id'] in existing and existing[g['id']].get('input_hash')==input_hash(g) and store._event_is_reusable(existing[g['id']],input_hash(g)))
                or tuple(sorted((a['id'],a['version']) for a in g['articles'])) in verified_text_bindings)]
            def publish_events(pending,reserve=0,until_daily_target=False):
                if until_daily_target:
                    with store.db() as db:incumbents=DailyMarket(store).candidates(db,datetime.now(CN))
                    if len(incumbents)==DAILY_HOTSPOT_COUNT:
                        boundary=max(datetime.fromisoformat(e['reported_at']) for e in incumbents)
                        pending=[g for g in pending if datetime.fromisoformat(g['articles'][0]['published_at'])>boundary]
                        pending=sorted(pending,key=lambda g:datetime.fromisoformat(g['articles'][0]['published_at']),reverse=True)[:DAILY_HOTSPOT_COUNT]
                        report['hotspot_replacement']={'required':bool(pending),'candidates':[g['id'] for g in pending],'published':[]}
                        until_daily_target=False
                for offset in range(len(pending)):
                    if until_daily_target and daily_qualified_count(store)>=DAILY_HOTSPOT_COUNT:break
                    if time.monotonic()+95+reserve>deadline:report['rejected'].append({'stage':'editor','reason':'run_deadline'});break
                    # 单事件独立审核，避免一条引文失败阻断其他合格报道。
                    batch=pending[offset:offset+1]
                    try:
                        snapshot=store.capture_event_generation(batch)
                        for e in editor.events(snapshot['groups'],id,generations=snapshot['generations']):
                            h=input_hash(next(g for g in snapshot['groups'] if g['id']==e['id']));e['input_hash']=h
                            store._record_event_review(e)
                            report['published_events']+=int(store.publish('market_events',e['id'],e,h))
                            if e['id'] in report.get('hotspot_replacement',{}).get('candidates',[]):report['hotspot_replacement']['published'].append(e['id'])
                    except Exception as error:report['rejected'].append({'stage':'event_review','ids':[g['id'] for g in batch],'reason':getattr(error,'code',str(error))[:120]})
            # 先完成到期周报的材料和周报，再使用剩余预算处理热点。
            publish_events([g for g in pending if g['id'] in weekly_ids],WEEKLY_RESERVE_SECONDS)
            published=[store.event(e['id']) for e in store.versions('market_events') if e['status']=='published']
            published=[e for e in published if e['status']=='published' and e.get('articles')
                       and all(text_only_article(a) for a in e['articles'])]
            existing_weeklies={w['period']:w for w in store.versions('market_weeklies')}
            for start,end in periods if 'weekly' in jobs else []:
                if scheduled and start.date().isoformat()!=schedule['targets']['weekly']:continue
                if time.monotonic()+WEEKLY_RESERVE_SECONDS>deadline:
                    report['rejected'].append({'stage':'weekly_review','reason':'run_deadline'});break
                candidates=sorted((e for e in published if start<=datetime.fromisoformat(e['published_at']).astimezone(CN)<end),
                    key=lambda e:e.get('score') if isinstance(e.get('score'),(int,float)) else score_article(e['articles'][0])[0],reverse=True)
                events=select_candidates(candidates,3)
                if not events:
                    report['rejected'].append({'stage':'weekly_review','period':start.date().isoformat(),'reason':'no_verified_events'});continue
                try:
                    snapshot=store.capture_weekly_generation(events,start,end,id)
                    generation=snapshot['generation'];h=generation['input_hash'];old=existing_weeklies.get(start.date().isoformat())
                    if old and old['status']=='published' and old.get('generation',{}).get('contract')==WEEKLY_GENERATION_CONTRACT and old.get('input_hash')==h:
                        with store.db() as db:
                            db.execute('BEGIN')
                            store._validate_weekly_publication(db,old['period'],old,old['input_hash'],require_publication_stamp=True)
                        store._discard_unreviewed_weekly_generation(generation['generation_id'])
                        continue
                    weekly=editor.weekly(snapshot['events'],start,end,id,generation=generation);weekly['input_hash']=h;weekly['reading']=deepcopy(snapshot['reading'])
                    store._record_weekly_review(weekly)
                    report['published_weeklies']+=int(store.publish('market_weeklies',weekly['period'],weekly,h))
                except Exception as error:report['rejected'].append({'stage':'weekly_review','period':start.date().isoformat(),'reason':getattr(error,'code',str(error))[:120]})
            publish_events([g for g in pending if g['id'] not in weekly_ids],until_daily_target=True)
        good=any(s['status']=='ok' and s['source_id']!='ft' for s in report['sources'])
        hot_errors=any(x.get('stage') in ('editor','event_review') for x in report['rejected'])
        limited=any(s['status']!='ok' for s in report['sources']) or hot_errors
        if 'hotspots' in jobs:
            # Publication fingerprints and the calendar-day edition are independent.
            # An identical approved event set still creates today's first edition.
            report['daily_edition']=store.update_daily(run_id=id,valid_check=good,
                coverage={'limited':limited,'sources':report['sources'],'scope':'registered_public_sources','required_count':DAILY_HOTSPOT_COUNT})
            count=report['daily_edition'].get('count')
            replacement=report.get('hotspot_replacement',{})
            replacement_failed=replacement.get('required') is True and not replacement.get('published')
            complete=good and count==DAILY_HOTSPOT_COUNT and not replacement_failed
            report['streams']['hotspots']={'status':'ok' if complete else 'partial' if good else 'failed',
                'required_count':DAILY_HOTSPOT_COUNT,'qualified_count':count,
                'publication_complete':complete,'fresh_replacement_failed':replacement_failed}
        if 'ingest' in jobs:report['streams']['ingest']={'status':'ok' if good and not limited else 'partial' if good else 'failed'}
        if 'weekly' in jobs:
            target=report['targets']['weekly']
            current=next((w for w in store.weekly_publications() if w['period']==target),None)
            ready=current if current and current['status']=='published' else None
            report['streams']['weekly']={'status':'ok' if ready else 'failed','period':target,
                'version':ready['version'] if ready else None,
                'publication_status':current['status'] if current else 'missing'}
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
