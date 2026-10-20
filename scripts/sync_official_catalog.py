#!/usr/bin/env python3
"""隔离公共缓存的一次官网核验；不打开银行库、不加载模型、不创建定时任务。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from webank_catalog import PublicProductCatalog


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True,type=Path,help='公共缓存目录')
    parser.add_argument('--receipt',type=Path,help='可选只读核验回执路径')
    args=parser.parse_args()
    catalog=PublicProductCatalog(data_dir=args.data_dir)
    catalog.refresh(force=True)
    result=catalog.read(limit=1)
    receipt={key:result.get(key) for key in ('provider','source_url','data_url','source_date',
        'fetched_at','total','sha256','catalog_version','catalog_sync')}
    receipt['insurance_count']=sum(p['type']=='保险' for p in (catalog.cached or {}).get('products',[]))
    receipt['official_response_headers']=dict(catalog._validators)
    receipt['data_dir']=str(catalog.data_dir)
    receipt['raw_bytes']=len(catalog._raw or b'')
    receipt['fetch_error']=catalog._meta.get('fetch_error')
    catalog.stop()
    encoded=json.dumps(receipt,ensure_ascii=False,indent=2)+'\n'
    if args.receipt:
        args.receipt.parent.mkdir(parents=True,exist_ok=True)
        args.receipt.write_text(encoded,encoding='utf-8')
    print(encoded,end='')
    return 0 if receipt['catalog_sync']['fetch_status'] in ('ok','unchanged') else 1


if __name__=='__main__':raise SystemExit(main())
