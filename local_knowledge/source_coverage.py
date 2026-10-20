"""明确国别的公共来源软召回；不决定机构身份、适用性或事实支持。

国别来自固定机构登记及原件官方域名。这里只给同一次准入后的完整原文
窗口增加一个排序视图，不新增、删除或修改引文，也不根据国别猜央行角色。
"""
from __future__ import annotations

import re
from .public_institutions import institution_policy_metadata, _official_origin

POLICY = 'explicit-public-source-country-coverage/1'
COUNTRY_NAMES = (('中国','CN'),('美国','US'),('欧元区','euro_area'))
MAX_SCOPE_WINDOWS = 32


def apply_source_coverage(query, groups, rows):
    clean = [dict(group) for group in groups]
    for group in clean:
        group.pop('source_scope_rank',None)
    diagnostic = {'policy':POLICY,'authority':'candidate_coverage_only','used':False,
                  'country_codes':[],'window_count':0,'reason':'explicit_country_missing'}
    if not isinstance(query,str) or not query.strip():
        return clean,diagnostic
    countries = {code for name,code in COUNTRY_NAMES if re.search(re.escape(name),query)}
    if not countries:
        return clean,diagnostic
    diagnostic['country_codes'] = sorted(countries)
    registry = institution_policy_metadata()['institutions']
    by_id = {row['id']:dict(row) for row in rows}
    sources = {}
    for cid,row in by_id.items():
        if (row.get('visibility')!='public' or row.get('data_origin')!='real'
                or row.get('status')!='verified' or row.get('paused')
                or any(row.get(key) not in (1,True) for key in ('enabled','read_allowed','outbound_allowed'))):
            continue
        publisher = row.get('publisher')
        if not isinstance(publisher,str):
            continue
        matches = [item for item in registry if item['country_code'] in countries
                   and publisher.strip().casefold() in {alias.casefold() for alias in item['publisher_aliases']}
                   and _official_origin(row.get('source_url'),item['allowed_source_domains'])]
        if len(matches)==1:
            sources[cid]=matches[0]['country_code']
    documents = {}
    for group in clean:
        if (group.get('kind')!='canonical_sentence_window' or not group.get('complete')
                or group.get('rejection') or group.get('canonical_anchor_id') not in sources):
            continue
        ids=group['citation_ids']
        if not ids or not all(cid in sources for cid in ids):
            continue
        member_rows=[by_id[cid] for cid in ids]
        if (len({row['version_id'] for row in member_rows})!=1
                or len({sources[cid] for cid in ids})!=1):
            continue
        key=(sources[ids[0]],member_rows[0]['version_id'])
        rank=group.get('canonical_anchor_rank') or group.get('rank_positions',{}).get('canonical')
        if type(rank) is not int or rank<1:
            continue
        documents.setdefault(key,[]).append((rank,group['group_id'],group))
    if not documents:
        diagnostic['reason']='eligible_country_windows_missing'
        return clean,diagnostic
    buckets=[sorted(members,key=lambda item:(item[0],item[1])) for members in documents.values()]
    buckets.sort(key=lambda members:(members[0][0],members[0][1]))
    # 先覆盖不同文档，再取各文档的后续窗口；同文档变体不会复制来源配额。
    count=0
    for offset in range(max(map(len,buckets))):
        for members in buckets:
            if offset<len(members) and count<MAX_SCOPE_WINDOWS:
                count+=1
                members[offset][2]['source_scope_rank']=count
        if count>=MAX_SCOPE_WINDOWS:
            break
    diagnostic.update(used=bool(count),window_count=count,reason=None)
    return clean,diagnostic
