#!/usr/bin/env python3
"""只读微众现行官网公开目录；解析 JSONP 数据，不执行远程脚本。"""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

SOURCE = 'https://tctp.tctpwebankcdn.net/owb-admin/data/appsystems/owb-admin/productpresentation.json'
PAGE = 'https://www.webank.com/inquiryCenter/query'
COMPONENT = 'https://tctp.tctpwebankcdn.net/owb-res/owb-res/assets/Query.9ddef1f2.js'
MAX_BYTES = 12 * 1024 * 1024
FIELDS = ('code', 'name', 'saleStatus', 'type', 'placement', 'saleType', 'risk',
          'publish', 'investor', 'charge', 'chargeWay', 'customerGroupType')


def read_catalog():
    req = Request(SOURCE, headers={'User-Agent': 'Navren/1.0 public-catalog-verification'})
    with urlopen(req, timeout=15) as response:
        deadline = time.monotonic() + 30
        pieces, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('公开目录读取超时')
            block = response.read1(min(65536, MAX_BYTES + 1 - size))
            if not block:
                break
            size += len(block)
            if size > MAX_BYTES:
                raise ValueError('公开目录超出大小限制')
            pieces.append(block)
        raw = b''.join(pieces)
        http = {'status': response.status, 'final_url': response.url,
                'last_modified': response.headers.get('Last-Modified'),
                'content_type': response.headers.get('Content-Type'),
                'bytes': size, 'sha256': hashlib.sha256(raw).hexdigest()}
    wrapper = re.fullmatch(r'\s*_jsonpproductpresentation\((.*)\)\s*;?\s*', raw.decode('utf-8'), re.S)
    if not wrapper:
        raise ValueError('公开目录 JSONP 包装发生变化')
    payload = json.loads(wrapper[1])
    source_date = date.fromisoformat(payload['extractDate'])
    if source_date > datetime.now(timezone.utc).date():
        raise ValueError('资料日期晚于核验日期')
    rows = payload['productPresentation']
    if not isinstance(rows, list) or not rows:
        raise ValueError('目录为空或结构异常')
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in FIELDS[:-1]):
            raise ValueError('条目字段结构发生变化')
        if row.get('customerGroupType') is not None and not isinstance(row['customerGroupType'], str):
            raise ValueError('适用客群字段结构发生变化')
        if not row['code'] or not row['name']:
            raise ValueError('条目缺少代码或名称')
    codes = Counter(row['code'] for row in rows)
    summary = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'page_url': PAGE, 'data_url': SOURCE, 'page_component_url': COMPONENT,
        'http': http, 'source_date': source_date.isoformat(),
        'source_age_days': (datetime.now(timezone.utc).date() - source_date).days,
        'record_count': len(rows), 'unique_codes': len(codes),
        'duplicate_code_count': sum(n > 1 for n in codes.values()), 'fields': list(FIELDS),
        'sales_status_counts': dict(Counter(row['saleStatus'] for row in rows)),
        'product_type_counts': dict(Counter(row['type'] for row in rows)),
        'sale_type_counts': dict(Counter(row['saleType'] for row in rows)),
        'publisher_count': len({row['publish'] for row in rows}),
        'missing_customer_group_count': sum(row.get('customerGroupType') is None for row in rows),
        'fee_page_placeholder_count': sum(row['charge'] in ('详见产品页面', '详见产品页') for row in rows),
        'fee_text_counts': dict(Counter(row['charge'] for row in rows)),
        'fee_way_text_counts': dict(Counter(row['chargeWay'] for row in rows)),
        'orient_samples': [row for row in rows if row['code'] in ('006210', '001198', '021095')],
        'fee_text_samples': [row for row in rows if row['charge'] not in ('详见产品页面', '详见产品页')][:3],
        'limitations': [
            '销售状态是 extractDate 对应的官方公开快照，不等于核验日实时可申购。',
            'HTTP Last-Modified 与下载时间不能替代资料日期。',
            '目录不含连续净值、实时剩余额度和客户账户持仓。',
            '费用文字可能指向产品页面；账户适用的渠道折扣与申购限制仍需核验。',
            '这是官网使用的公开数据文件，没有核验对外 API 服务承诺或批量再分发许可。',
            '本脚本不修改运行应用的产品接口或推荐与申购状态。',
        ],
    }
    return payload, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='核验摘要 JSON 路径')
    parser.add_argument('--catalog-out', type=Path, help='可选的原始解析目录 JSON 路径')
    args = parser.parse_args()
    catalog, summary = read_catalog()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    if args.catalog_out:
        args.catalog_out.parent.mkdir(parents=True, exist_ok=True)
        args.catalog_out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: summary[k] for k in ('source_date', 'record_count', 'unique_codes',
                                            'sales_status_counts', 'product_type_counts')},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
