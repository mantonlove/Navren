#!/usr/bin/env python3
"""从 JS 产品文件中提取 JSON 数组，合并为 products.json"""
import json, os, re, sys

JS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'js')
OUTPUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'products.json')

def extract_json_array(content):
    """从 JS 文件中提取 JSON 数组（处理嵌套括号）"""
    idx = content.find('= [')
    if idx < 0:
        # 尝试 const IDX=[...] 格式（索引文件）
        idx = content.find('=[')
        if idx < 0:
            return None
        start = content.index('[', idx)
    else:
        start = content.index('[', idx)

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(content)):
        c = content[i]
        if escape:
            escape = False
            continue
        if c == '\\':
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                json_str = content[start:i+1]
                # 移除尾部逗号（处理 ][ 拼接的情况）
                json_str = json_str.rstrip()
                if json_str.endswith(','):
                    json_str = json_str[:-1]
                return json_str
    return None

def main():
    all_products = []
    seen_ids = set()
    js_files = sorted(f for f in os.listdir(JS_DIR) if f.startswith('products_') and f.endswith('.js'))

    for fname in js_files:
        fpath = os.path.join(JS_DIR, fname)
        with open(fpath, 'r', encoding='utf-8') as f:
            content = f.read()

        json_str = extract_json_array(content)
        if not json_str:
            print(f'  SKIP {fname}: could not extract JSON array')
            continue

        try:
            products = json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f'  ERROR {fname}: {e}')
            continue

        added = 0
        for p in products:
            pid = p.get('product_id', '')
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_products.append(p)
                added += 1

        print(f'  {fname}: {len(products)} products, {added} new (total: {len(all_products)})')

    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(all_products, f, ensure_ascii=False)

    print(f'\nDone. {len(all_products)} products written to {OUTPUT}')

if __name__ == '__main__':
    main()
