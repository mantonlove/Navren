#!/usr/bin/env python3
"""离线导入既有官方市场正文；可显式启用固定微众公开端点同步。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from local_knowledge import LocalKnowledgeProvider
from local_knowledge.importers import import_market_store,import_webank_disclosure,_register_once


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True,help='独立公共知识库 SQLite 路径')
    parser.add_argument('--market-database',help='只读已有 MarketStore 数据库，无网络')
    parser.add_argument('--webank',action='store_true',help='显式读取已登记微众现行公开端点')
    parser.add_argument('--webank-code',action='append',default=[])
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--publish',action='store_true',help='准入通过后更新公共发布快照')
    args=parser.parse_args()
    provider=LocalKnowledgeProvider(args.database)
    reports={}
    if args.market_database:
        from types import SimpleNamespace
        reports['market']=import_market_store(provider,SimpleNamespace(path=Path(args.market_database)),publish=args.publish,limit=args.limit)
    if args.webank:
        from webank_catalog import URL,MAX_BYTES
        from local_knowledge.sources import fetch_registered
        _register_once(provider,'webank-direct-disclosure',publisher='微众银行',endpoints=[URL],
            parser_id='webank-record-json/1',
            freshness={'general':7,'sale_status':0,'fees':7,'risk':7,'eligibility':7},
            shape='webank-productpresentation-v1',permission_basis='official_public_dated_channel_disclosure_short_quotes')
        body=fetch_registered(provider,'webank-direct-disclosure',URL,timeout=12,max_bytes=MAX_BYTES)
        reports['webank']=import_webank_disclosure(provider,body,codes=args.webank_code or None,publish=args.publish,limit=args.limit)
    reports['status']=provider.status()
    print(json.dumps(reports,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
