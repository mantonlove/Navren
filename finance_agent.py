"""银行场景的确定性工具编排。只读账本、解释与生成草案，不持有执行授权。

自然语言永远不触发确认；用户通过独立同源表单核对并授权。
"""
import re
import uuid
from decimal import Decimal, InvalidOperation


BANK_PATTERN = r'账单|流水|消费|支出|收入|余额|持仓|资产|理财|配置|可投资|应急金|保留资金|风险等级|风险画像|投资期限|申购|购买|买入|赎回|代扣|订阅|会员|周期扣费|转账|缴费|挂失|额度|模拟.*产品|SIM-'


def supports(text):
    return isinstance(text,str) and bool(re.search(BANK_PATTERN,text,re.I))


def money(value):
    return f'{Decimal(value)/100:,.2f}元'


def chinese_number(raw):
    if re.fullmatch(r'\d+(?:\.\d+)?',raw): return Decimal(raw)
    digits = dict(zip('零一二三四五六七八九',range(10)))
    digits['两'] = 2
    number,section,total = 0,0,0
    for char in raw:
        if char in digits: number = digits[char]
        elif char in '十百千':
            section += (number or 1)*{'十':10,'百':100,'千':1000}[char]; number=0
        elif char=='万': total += (section+number or 1)*10000; section=number=0
        else: raise ValueError('金额格式')
    return Decimal(total+section+number)


def parse_amount(text):
    # 必须有货币单位，或紧随操作动词，避免把产品期限/风险数字当成金额。
    match = re.search(r'(\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万]+)\s*(万|千)?\s*(?:元|块)',text)
    if not match:
        match = re.search(r'(?:申购|购买|买入|赎回)\s*([\d零一二两三四五六七八九十百千万]+(?:\.\d+)?)(万|千)?',text)
    if not match: return None
    try:
        value = chinese_number(match[1])*{'万':10000,'千':1000,None:1}[match[2]]*100
        if value!=value.to_integral() or not 1<=value<=100000000: return None
        return int(value)
    except (ValueError,InvalidOperation): return None


def process(gateway,workspace,text):
    state = gateway.overview(workspace)
    products = gateway.bank.products(workspace)
    main = next(a for a in state['accounts'] if a['id'].endswith('-main'))
    budget = gateway.budget(workspace,main['id'])
    output = {'bank':True,'environment':'simulation','workspace_id':workspace,'version':state['version'],
              'bank_response_id':uuid.uuid4().hex,
              'intent':'bank_analysis','isFallback':False,'bank_actions':[], 'products':[],
              'budget':budget,'follow_up_questions':[], 'compliance_pass':True}
    paragraphs = [f'以下来自当前模拟银行账本（业务日期 {state["as_of"]}），按本金计算。']
    performed = False
    if re.search(r'转账|缴费|挂失|调整额度|申请.*卡',text):
        paragraphs.append('这项业务尚未接入Navren页面，当前无法执行。已接入的是账单分析、理财申购与赎回、关闭银行代扣授权。')
        performed = True
    if re.search(r'账单|流水|消费|支出|收入|月报|年报|年度|异常',text):
        year = re.search(r'(20\d{2})\s*年(?:度|报|账单|消费|支出)?',text)
        if re.search(r'年报|年度|全年',text):
            report = gateway.bank.annual_analysis(workspace,int(year[1]) if year else 2025)
            period = f'{report["year"]}年（覆盖{len(report["covered_months"])}个月；'+('全年完整' if report['complete_calendar_year'] else '非完整年度')+'）'
        else:
            explicit = re.search(r'(20\d{2})[-年](0?[1-9]|1[0-2])(?:月)?',text)
            single = re.search(r'(?<!\d)(0?[1-9]|1[0-2])月',text)
            month = f'{explicit[1]}-{int(explicit[2]):02}' if explicit else f'{state["as_of"][:4]}-{int(single[1]):02}' if single else '2026-09'
            report = gateway.bank.analysis(workspace,month)
            period = month+f'（{report["transaction_count"]}笔流水）'
            if not report['transaction_count']: paragraphs.append('这个月没有已记录流水，不能据此认定没有消费。')
        totals = report['totals_minor']
        paragraphs.append(f'{period}：收入 {money(totals.get("income",0))}，消费 {money(totals.get("expense",0))}，退款 {money(totals.get("refund",0))}。转账和投资流转单列，不计入消费。')
        categories = sorted(report['category_expense_minor'].items(),key=lambda item:-item[1])
        if categories: paragraphs.append('消费分类：'+'；'.join(f'{k} {money(v)}' for k,v in categories)+'。')
        anomalies = gateway.bank.anomalies(workspace)
        paragraphs.append(f'现有流水规则标记 {len(anomalies["duplicates"])} 组疑似重复扣款、{len(anomalies["high_amounts"])} 笔异常大额消费，均需核实，不代表已经确认欺诈。')
        paragraphs.append(report['notice'])
        output['report'],output['anomalies'] = report,anomalies
        performed = True
    if re.search(r'代扣|订阅|会员|周期扣费',text):
        output['periodic_charges'] = gateway.bank.periodic_charges(workspace)
        active = [m for m in state['mandates'] if m['status']=='active']
        paragraphs.append('当前有效银行代扣：'+'；'.join(f'{m["merchant"]} {money(m["amount_minor"])}，下次 {m["next_charge_date"]}' for m in active)+'。' if active else '当前没有有效银行代扣授权。')
        if re.search(r'取消|关闭|停止|解除',text):
            selected = [m for m in active if m['merchant'] in text or m['merchant'].removeprefix('模拟') in text or
                        any(alias in text and target in m['merchant'] for alias,target in [('视频','视频'),('云存储','云存储'),('课程','课程')])]
            if len(selected)==1:
                m = selected[0]
                output['bank_actions'].append({'label':'核对并关闭 '+m['merchant'], 'draft':{
                    'action':'close_mandate','account_id':m['account_id'],'mandate_id':m['id'],'idempotency_key':uuid.uuid4().hex}})
                paragraphs.append('已生成待核对草案，尚未关闭。关闭银行代扣不等于取消商户订阅合同，也不免除应付款。')
            else: paragraphs.append('请明确要关闭哪一个商户的银行代扣，或到「账户」逐项选择。')
        performed = True
    if re.search(r'理财|配置|申购|购买|买入|赎回|持仓|余额|资产|风险等级|风险画像|投资期限|应急金|保留资金|可投资|SIM-|模拟.*产品',text,re.I):
        output['intent']='bank_wealth'
        reserve = re.search(r'(?:保留|留出|留下|应急金(?:为|设为)?)[^，。；]{0,6}?([\d零一二两三四五六七八九十百千万]+(?:\.\d+)?\s*(?:万|千)?\s*(?:元|块))',text)
        floor = parse_amount(reserve[1]) if reserve else 0
        budget = gateway.budget(workspace,main['id'],floor or 0)
        output['budget']=budget
        paragraphs.append(f'现金合计 {money(state["total_cash_minor"])}；理财本金 {money(state["holding_principal_minor"])}；待结算赎回 {money(state["pending_redemption_minor"])}。')
        paragraphs.append(f'以日常账户支付：余额 {money(budget["cash_minor"])} − 应急保留 {money(budget["reserve_minor"])} − 30天已知到期款 {money(budget["upcoming_due_minor"])} = 可投资 {money(budget["investable_minor"])}。备用账户资金未自动转入；未预测其他生活支出。')
        paragraphs.append(f'模拟风险画像 R{state["profile"]["risk_level"]}，最长投资期限 {state["profile"]["horizon_days"]} 天。')
        aliases = {'SIM-CASH-01':['活钱'],'SIM-BOND-30':['30天','三十天'], 'SIM-BOND-90':['90天','九十天'],
                   'SIM-FLEX-03':['灵活配置'],'SIM-EQUITY-04':['权益'],'SIM-LONG-05':['180天','高风险180']}
        selected = [p for p in products if p['id'].lower() in text.lower() or p['name'] in text or any(a in text for a in aliases[p['id']])]
        output['products']=selected if selected else products
        for p in output['products']:
            reason = '风险与期限匹配' if p['profile_eligible'] else '不匹配：'+' '.join(p['exclusion_reasons'])
            paragraphs.append(f'{p["name"]}（{p["id"]}）：R{p["risk_level"]}，锁定{p["term_days"]}天，起购{money(p["min_purchase_minor"])}，赎回后{p["redemption_delay_days"]}个模拟自然日到账；{reason}。')
        action = 'redeem' if re.search(r'赎回|卖出',text) else 'purchase' if re.search(r'申购|购买|买入|买',text) else None
        if re.search(r'(?:不要|不|取消)\s*(?:申购|购买|买入|赎回|卖出)|只是.*(?:比较|了解)',text):
            action=None
            paragraphs.append('本次仅提供查询与比较，没有生成交易草案。已准备操作的取消请在确认卡或账户记录中进行。')
        if action:
            action_text = re.split(r'(?:申购|购买|买入|赎回|卖出)',text,maxsplit=1)[-1]
            amount = parse_amount(action_text)
            if len(selected)==1 and amount:
                p=selected[0]
                if action=='purchase' and not p['profile_eligible']:
                    paragraphs.append('当前产品与画像不匹配，不能生成申购草案。')
                elif action=='purchase' and amount>budget['investable_minor']:
                    paragraphs.append('金额超过当前可投资额度，不能生成申购草案。')
                else:
                    output['bank_actions'].append({'label':('核对申购 ' if action=='purchase' else '核对赎回 ')+p['name']+' '+money(amount), 'draft':{
                        'action':action,'account_id':main['id'],'product_id':p['id'],'amount_minor':amount,
                        'liquidity_floor_minor':floor or 0,'idempotency_key':uuid.uuid4().hex}})
                    paragraphs.append('已生成待核对草案，尚未执行。打开确认卡后会再次检查金额、画像与持仓，申购和赎回须额外验证。')
            else: paragraphs.append('请明确一个模拟产品和金额，例如「购买模拟活钱产品3000元」。也可在推荐页逐项比较后选择。')
        output['highlight_names']=[p['name'] for p in output['products']]
        paragraphs.append('所有产品均为虚构演示；没有预测或承诺收益，未模拟市场估值与费用。')
        performed = True
    if not performed: paragraphs.append('请指定账单月份、模拟产品与金额，或银行代扣商户。')
    output['reply']='\n\n'.join(paragraphs)
    return output
