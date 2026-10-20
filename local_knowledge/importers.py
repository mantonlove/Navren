"""登记官方来源的本地适配器；启动导入只读取既有缓存，不访问网络。"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from .store import KnowledgeError, PARSER, WEBANK_PARSER, packed, sha, today


def _register_once(provider,source_id, **configuration):
    try:source=provider.source(source_id)
    except KnowledgeError:
        provider.register_source(source_id,**configuration)
        return
    if source['parser_id']!=configuration.get('parser_id',PARSER) or (source['shape'] and source['shape']!=configuration['shape']):
        provider.set_source_state(source_id,paused=True)
        raise KnowledgeError('source_structure_drift')
    # 既有来源的禁用、撤销和权限属于本地治理；同步不得自动重新授权。


def import_market_store(provider,market_store, *, publish=True,limit=200):
    """Explicit trusted authority injection; no paths are recovered from KG metadata.

    Hold the upstream IMMEDIATE barrier through all KG admission/commit. Existing
    copied quotes additionally recheck the live authority at each public call.
    """
    from market_sources import REGISTRY
    provider.bind_market_authority(market_store)
    imported=[];rejected=[];seen=0;release=None
    with provider.market_read_snapshot(write=True) as authority:
        if authority is None or not authority.ready:
            return {'imported':0,'rejected':0,'ready':provider.status()['ready'],'count':0,
                    'version_ids':[],'coverage_gaps':[{'code':'source_permission_missing'}]}
        for article_id in authority.latest_ids(max(1,min(limit,1000))):
            seen+=1
            try:
                capture=authority.capture(article_id);a=capture.article;source=REGISTRY[a['source_id']]
                published=datetime.fromisoformat(a['published_at'].replace('Z','+00:00')).date().isoformat()
                retrieved=datetime.fromisoformat(a['checked_at'].replace('Z','+00:00')).date().isoformat()
                _register_once(provider,'market-'+source.id,publisher=source.publisher,endpoints=['https://'+source.domain+'/'],
                    parser_id=PARSER,shape='market-official-full-text-v1',freshness={'general':30},
                    permission_basis='exact_market_permission_article_lineage_v2')
                version=provider.stage_document(source_id='market-'+source.id,source_url=a['url'],title=a['title'],text=a['evidence_text'],
                    published_on=published,retrieved_on=retrieved,facets=['general'],parser_id=PARSER,shape='market-official-full-text-v1',
                    document_key='market-'+sha(packed(capture.binding))[:40],_market_capture=capture)
                provider.approve_version(version,verification_basis='exact_market_permission_article_lineage_v2')
                imported.append(version)
            except (KnowledgeError,KeyError,ValueError,TypeError) as error:
                rejected.append({'article_id':article_id,'code':error.code if isinstance(error,KnowledgeError) else 'source_structure_drift'})
        if publish and imported:release=provider.publish(version_ids=imported,preserve_current=True)
    status=provider.status()
    return {'imported':len(imported),'rejected':len(rejected),'ready':status['ready'],
            'count':status['published_chunk_count'],'version_ids':imported,'rejections':rejected,
            'release_id':release or status['public_release_id'],'scanned':seen}


def import_webank_disclosure(provider,body, *, codes=None,publish=False,limit=100):
    """只采用现行微众直接 JSONP 披露；不推断底层基金、份额或即时可买。"""
    from webank_catalog import decode_catalog,URL,FIELD_MAP
    try:
        source_date,decoded=decode_catalog(body)
    except (ValueError,KeyError,TypeError,UnicodeError):
        try:provider.set_source_state('webank-direct-disclosure',paused=True)
        except KnowledgeError:pass
        raise KnowledgeError('source_structure_drift') from None
    wrapper=body.decode('utf-8').strip()
    raw=json.loads(wrapper[wrapper.index('(')+1:wrapper.rindex(')')])
    if set(raw)-{'extractDate','productPresentation'} or any(set(row)-set(FIELD_MAP) for row in raw['productPresentation']):
        try:provider.set_source_state('webank-direct-disclosure',paused=True)
        except KnowledgeError:pass
        raise KnowledgeError('source_structure_drift')
    source_id='webank-direct-disclosure'
    _register_once(provider,source_id,publisher='微众银行',endpoints=[URL],parser_id=WEBANK_PARSER,shape='webank-productpresentation-v1',
        permission_basis='official_public_dated_channel_disclosure_short_quotes',
        freshness={'sale_status':0,'fees':7,'risk':7,'eligibility':7,'general':7})
    provider.register_channel('WEBANK',name='微众银行')
    selected=set(codes) if codes else None
    versions=[]
    # 原字段结构与 decode_catalog 一致；局部 JSON 是明确版本化的解析表示。
    for row in raw['productPresentation']:
        if selected is not None and row['code'] not in selected:continue
        if len(versions)>=max(1,min(limit,500)):break
        pid='WEBANK:'+row['code']
        try:
            provider.register_product(pid,issuer='WEBANK_DISCLOSURE',scheme='webank_channel_product_code',
                                      code=row['code'],name=row['name'],verified=True)
            text=packed(row)
            fields={'charge':'fees','chargeWay':'fees','risk':'risk','investor':'eligibility','customerGroupType':'eligibility','saleStatus':'sale_status'}
            spans=[]
            for key,value in sorted(row.items()):
                if value is None or (isinstance(value,str) and not value.strip()):continue
                fragment=packed(key)+':'+packed(value)
                start=text.find(fragment)
                if start<0:raise KnowledgeError('source_structure_drift')
                for offset in range(0,len(fragment),1000):
                    spans.append({'start_char':start+offset,'end_char':start+min(len(fragment),offset+1000),
                                  'paragraph':'productPresentation:'+row['code']+'/'+key,
                                  'facets':[fields.get(key,'general')]})
            version=provider.stage_document(source_id=source_id,source_url=URL,title=row['name'],text=text,raw_bytes=body,
                product_id=pid,channel_id='WEBANK',document_key=pid,published_on=source_date.isoformat(),retrieved_on=today(),
                data_on=source_date.isoformat(),facets=['general'],
                parser_id=WEBANK_PARSER,shape='webank-productpresentation-v1',
                spans=spans)
            provider.approve_version(version,verification_basis='strict_webank_jsonp_field_identity_schema_v1')
            versions.append(version)
        except KnowledgeError:
            raise
    if not versions:raise KnowledgeError('identity_unverified')
    release=provider.publish(version_ids=versions,preserve_current=True) if publish else None
    return {'imported':len(versions),'rejected':0,'version_ids':versions,'release_id':release,
            'ready':provider.status()['ready'],'count':provider.status()['published_chunk_count'],
            'scope':'dated_channel_disclosure','share_mapping_verified':False,'transaction_authorized':False}
