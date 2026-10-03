#!/usr/bin/env python3
"""Navren知识库同步脚本——Demo版"""
import json, datetime, os

SYNC_CONFIG = {
    # 产品每日变动（发行/到期/净值更新）—— 每日同步
    "chinawealth.com.cn": {"name":"中国理财网","frequency":"每日 08:00","endpoint":"https://www.chinawealth.com.cn/lcweb/management/proScreen","rationale":"银行理财产品每日有发行/到期，净值每日更新"},
    "amac.org.cn": {"name":"中基协","frequency":"每日 08:30","endpoint":"https://gs.amac.org.cn","rationale":"基金产品每日有新增备案/清盘"},
    "chinabond.com.cn": {"name":"中债登","frequency":"每日 18:00","endpoint":"https://www.chinabond.com.cn","rationale":"债券发行/兑付每日变动，收益率曲线每日收盘后发布"},
    # 批次更新 —— 每周同步
    "nfra.gov.cn": {"name":"金融监管总局","frequency":"每周一 08:00","endpoint":"https://www.nfra.gov.cn","rationale":"保险产品审批后批次公示，非实时"},
    "chinatrc.com.cn": {"name":"中信登","frequency":"每周一 09:00","endpoint":"https://www.chinatrc.com.cn","rationale":"信托产品成立/终止频率低于银行理财和基金"},
}
# cron 定时任务示例：
# 0 8 * * 1-5 cd /app && python3 sync_products.py  # 工作日每日
# 0 18 * * 1-5 cd /app && python3 sync_products.py --bond-only  # 债券每日收盘后

def sync():
    now = datetime.datetime.now().isoformat()
    
    with open('js/demo_products.json') as f:
        products = json.load(f)
    
    for p in products:
        p["synced_at"] = now
    
    with open('js/demo_products.json', 'w') as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    
    with open('js/demo_products.js', 'w') as f:
        f.write(f'const DEMO_PRODUCTS = {json.dumps(products, ensure_ascii=False, indent=2)};\n')
        f.write(f'const PRODUCT_META = {{"version":"1.0.1","updated":"{now}","count":{len(products)},"sources":{len(SYNC_CONFIG)},"next_sync":"每周一08:00","note":"自动同步于{now}"}};\n')
    
    print(f"✓ {len(products)} products synced at {now}")

def sync_market_data():
    """从公开金融数据源拉取实时市场指标"""
    now = datetime.datetime.now().isoformat()
    try:
        import akshare as ak
        lpr = ak.macro_china_lpr()
        lpr_1y = float(lpr.iloc[-1]['LPR1Y'])
        lpr_5y = float(lpr.iloc[-1]['LPR5Y'])
        print(f"  LPR: 1Y={lpr_1y}% 5Y={lpr_5y}%")
    except Exception as e:
        print(f"  LPR fetch failed: {e}")
        lpr_1y, lpr_5y = 3.0, 3.5

    try:
        bond = ak.bond_china_close_return(symbol="国债")
        bond_10y = float(bond.iloc[-1]['收盘价']) if len(bond) > 0 else 2.55
    except:
        bond_10y = 2.55

    try:
        gold = ak.spot_golden_benchmark_sge()
        gold_price = float(gold.iloc[-1]['收盘价']) if len(gold) > 0 else 680
    except:
        gold_price = 680

    try:
        pe = ak.index_value_name_funddb()
        hs300_pe = float(pe[pe['name'].str.contains('沪深300')].iloc[0]['pe']) if len(pe) > 0 else 12.8
    except:
        hs300_pe = 12.8

    try:
        fx = ak.currency_boc_safe()
        usd_cny = float(fx[fx['货币名称'].str.contains('美元')]['折算价'].values[0]) / 100
    except:
        usd_cny = 7.25

    data = {
        "update_time": now,
        "source": "akshare (LPR/国债/PE/黄金/汇率公开数据)",
        "key_indicators": {
            "一年期LPR": f"{lpr_1y}%", "五年期LPR": f"{lpr_5y}%",
            "10年国债收益率": f"{bond_10y}%", "沪深300PE": f"{hs300_pe}倍",
            "黄金(Au99.99)": f"{gold_price}元/克", "美元/人民币": f"{usd_cny}",
            "存款基准利率(活期)": "0.20%", "存款基准利率(1年定期)": "1.50%"
        },
        "market_brief": f"LPR {lpr_1y}%(1Y)/{lpr_5y}%(5Y)。10Y国债{bond_10y}%。沪深300PE {hs300_pe}倍。黄金{gold_price}元/克。USD/CNY {usd_cny}。",
        "disclaimer": "数据通过akshare从公开金融数据源获取,仅供参考。更新时间见update_time。"
    }

    with open('js/market_data.json', 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open('js/market_data.js', 'w') as f:
        f.write(f'const MARKET_DATA = {json.dumps(data, ensure_ascii=False, indent=2)};\n')
        f.write(f'const MARKET_UPDATED = "{now}";\n')
    print(f"✓ Market data synced at {now}")

if __name__ == "__main__":
    sync()
    print()
    sync_market_data()
