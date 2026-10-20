#!/usr/bin/env python3
"""显式本地建索引，不下载模型，不读取个人库；正文仍由原文库核验。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from local_knowledge import LocalKnowledgeProvider


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True,help='公共知识库 SQLite 路径')
    parser.add_argument('--release',help='指定封存快照；默认当前快照')
    parser.add_argument('--encoder',choices=('bge','e5','aligned','dual'),help='默认遵循 NAVREN_VECTOR_ENCODER 和公共检索选择器')
    parser.add_argument('--structure',choices=('enabled','disabled'),default='disabled',help='显式构建候选句段父子索引，默认关闭')
    args=parser.parse_args()
    try:
        if args.encoder:os.environ['NAVREN_VECTOR_ENCODER']=args.encoder
        os.environ['NAVREN_STRUCTURE_ENABLED']='1' if args.structure=='enabled' else '0'
        from local_knowledge.retrieval_encoder import get_retrieval_encoder
        # 显式命令自己承担构建，避免同时排入后台产生重复推理。
        provider=LocalKnowledgeProvider(Path(args.database),release_id=args.release,encoder=get_retrieval_encoder())
        result=provider.build_vector_index(args.release)
        result['status']=provider.status()
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.structure=='enabled' and not result.get('structure_index',{}).get('ready'):
            return 1
        return 0
    except Exception as error:
        from local_knowledge.vectors import VectorIndex
        print(json.dumps({'ready':False,'reason':VectorIndex._reason(error)},ensure_ascii=False))
        return 1


if __name__=='__main__':sys.exit(main())
