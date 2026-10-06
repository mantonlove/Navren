"""银行场景的确定性工具编排。只读账本、解释与生成草案，不持有执行授权。

自然语言永远不触发确认；用户通过独立同源表单核对并授权。
"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from agent_runtime.cashflow_finance import run_cashflow_workflow
from finance_capabilities import is_account_balance_query, select_workflow_capabilities


def supports(text):
    return bool(select_workflow_capabilities(text))


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


def _recipient(state,text):
    matches=[]
    for item in state.get('beneficiaries',[]):
        display=item['display_name'].replace('（虚构）','')
        if (item.get('alias') and item['alias'] in text) or display in text:
            matches.append(item)
    return matches


def _card(state,text,credit_only=False):
    cards=[c for c in state.get('cards',[]) if c['status']=='active' and (not credit_only or c['type']=='credit')]
    last_four=re.search(r'(?:尾号|后四位|末四位)\s*(\d{4})',text)
    if last_four:
        return [c for c in cards if c['last_four']==last_four[1]]
    if '信用卡' in text:return [c for c in cards if c['type']=='credit']
    if '借记卡' in text:return [c for c in cards if c['type']=='debit']
    return cards


def _action(label,action,account_id,**values):
    return {'label':label,'draft':{'action':action,'account_id':account_id,
                                  **values,'idempotency_key':uuid.uuid4().hex}}


def process(gateway,workspace,text,*,model_agents=None):
    selected_capabilities = select_workflow_capabilities(text)
    capabilities_used=[]

    def read_capability(capability_id, parameters=None):
        result=gateway.query_read_capability(workspace,capability_id,parameters or {})
        capabilities_used.append(capability_id)
        return result

    state = read_capability('simulated_bank.account_snapshot')
    data_source = read_capability('simulated_bank.statement_source')
    imported_source = data_source['active_source']=='imported'
    main = next(a for a in state['accounts'] if a['id'].endswith('-main'))
    wealth_query = is_account_balance_query(text) or bool(re.search(r'理财|配置|申购|购买|买入|赎回|持仓|余额|资产|风险等级|风险画像|投资期限|应急金|保留资金|可投资|SIM-|模拟.*产品|现金流|资金时间轴',text,re.I))
    needs_budget = is_account_balance_query(text) or (wealth_query and bool(re.search(
        r'理财|配置|申购|购买|买入|赎回|持仓|余额|资产|风险等级|风险画像|投资期限|应急金|保留资金|可投资|SIM-|模拟.*产品|现金流|资金时间轴|未来\s*\d{1,3}\s*(?:天|日)',
        text,re.I)))
    product_query = bool(re.search(r'理财|产品|配置|收益|风险等级|风险画像|投资期限|申购|购买|买入|赎回|比较|对比|推荐|稳健方案|SIM-|(?:怎么|如何|什么|哪些).{0,4}(?:投资|理财|产品)|(?:投资|理财).{0,4}(?:建议|方案|推荐|产品)',text,re.I))
    budget = (read_capability('budgets.investable', {'account_id': main['id']})
              if needs_budget else None)
    output = {'bank':True,'environment':'simulation','workspace_id':workspace,'version':state['version'],
              'bank_response_id':uuid.uuid4().hex,
              'intent':'bank_analysis','isFallback':False,'bank_actions':[], 'products':[],
              'budget':budget if (not imported_source or wealth_query) else None,
              'follow_up_questions':[], 'compliance_pass':True,'statement_source':data_source,
              'agent_chain':[],'capabilities_used':capabilities_used,
              'business_capabilities':list(selected_capabilities)}
    paragraphs = ([f'当前账单来源：本地导入数据集（{data_source["dataset"]["transaction_count"]}笔，覆盖{data_source["dataset"]["first_date"]}至{data_source["dataset"]["last_date"]}）。仅统计已导入记录；导入流水不含当前可用余额或可投资金额，资产、理财和银行代扣均来自独立模拟账户。']
                  if imported_source else [f'当前账单来源：内置模拟银行账本（业务日期 {state["as_of"]}），按本金计算。'])
    performed = False
    if 'statements' in selected_capabilities:
        year = re.search(r'((?:19|20)\d{2}|2100)\s*年(?:度|报|账单|消费|支出)?',text)
        if re.search(r'年报|年度|全年',text):
            default_year = int(data_source['dataset']['last_date'][:4]) if imported_source and data_source['dataset']['last_date'] else 2025
            report = read_capability('statements.annual_analysis',
                                     {'year':int(year[1]) if year else default_year})
            period = f'{report["year"]}年（覆盖{len(report["covered_months"])}个月；'+('全年完整' if report['complete_calendar_year'] else '非完整年度')+'）'
            output['agent_chain'].append({'tool':'annual_analysis','source':report.get('source','simulation'),
                                          'year':report['year'],'covered_months':report['covered_months']})
        else:
            explicit = re.search(r'(20\d{2})[-年](0?[1-9]|1[0-2])(?:月)?',text)
            single = re.search(r'(?<!\d)(0?[1-9]|1[0-2])月',text)
            if '上月' in text or '上个月' in text:
                basis = date.fromisoformat(state['as_of']) if not imported_source else datetime.now(timezone(timedelta(hours=8))).date()
                month = (basis.replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
                if imported_source: paragraphs.append(f'“上月”以北京时间 {basis.isoformat()} 为基准。')
            elif explicit:
                month = f'{explicit[1]}-{int(explicit[2]):02}'
            elif single:
                report_year = data_source['dataset']['last_date'][:4] if imported_source and data_source['dataset']['last_date'] else state['as_of'][:4]
                month = f'{report_year}-{int(single[1]):02}'
                if imported_source:
                    paragraphs.append('没有指定年份，已按本地账单最新有数据年份查询。')
            elif imported_source:
                month = data_source['dataset']['last_date'][:7] if data_source['dataset']['last_date'] else datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m')
                paragraphs.append('未指定月份，已按本地文件最新有数据月份查询。')
            else:
                basis = date.fromisoformat(state['as_of'])
                month = (basis.replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
            report = read_capability('statements.monthly_analysis',{'month':month})
            period = month+f'（{report["transaction_count"]}笔流水）'
            output['agent_chain'].append({'tool':'bill_analysis','source':report.get('source','simulation'),
                                          'month':month,'transaction_count':report['transaction_count'],
                                          'transaction_ids':report.get('transaction_ids',[])})
            if not report['transaction_count']: paragraphs.append('这个月没有已记录流水，不能据此认定没有消费。')
        totals = report['totals_minor']
        paragraphs.append(f'{period}：收入 {money(totals.get("income",0))}，消费 {money(totals.get("expense",0))}，退款 {money(totals.get("refund",0))}。转账和投资流转单列，不计入消费。')
        categories = sorted(report['category_expense_minor'].items(),key=lambda item:-item[1])
        if categories: paragraphs.append('消费分类：'+'；'.join(f'{k} {money(v)}' for k,v in categories)+'。')
        anomalies = read_capability('statements.anomalies')
        output['agent_chain'].append({'tool':'anomaly_rules','source':anomalies.get('source','simulation'),
                                      'duplicate_groups':len(anomalies['duplicates']),
                                      'high_amounts':len(anomalies['high_amounts'])})
        paragraphs.append(f'现有流水规则标记 {len(anomalies["duplicates"])} 组疑似重复扣款、{len(anomalies["high_amounts"])} 笔异常大额消费，均需核实，不代表已经确认欺诈。')
        paragraphs.append(report['notice'])
        output['report'],output['anomalies'] = report,anomalies
        performed = True
    if 'subscriptions' in selected_capabilities:
        output['periodic_charges'] = read_capability('subscriptions.periodic_charges')
        output['agent_chain'].append({'tool':'periodic_charge_detection',
                                      'source':output['periodic_charges'].get('source','simulation'),
                                      'candidate_ids':[c['candidate_id'] for c in output['periodic_charges']['candidates']]})
        active = [m for m in state['mandates'] if m['status']=='active'] if not imported_source else []
        if imported_source:
            paragraphs.append('周期扣费判断仅基于本地导入流水；导入数据不包含银行代扣授权状态。')
        else:
            paragraphs.append('当前有效银行代扣：'+'；'.join(f'{m["merchant"]} {money(m["amount_minor"])}，下次 {m["next_charge_date"]}' for m in active)+'。' if active else '当前没有有效银行代扣授权。')
        if re.search(r'取消|退订|关闭|停止|解除',text):
            if imported_source:
                paragraphs.append('当前展示的是本地导入流水，无法据此识别或取消真实订阅。请在账户页切回模拟档案后，再选择模拟商户订阅或银行代扣。')
                performed = True
                output['reply']='\n\n'.join(paragraphs)
                return output
            if re.search(r'银行代扣|代扣授权|银行授权',text):
                selected = [m for m in active if m['merchant'] in text or m['merchant'].removeprefix('模拟') in text or
                            any(alias in text and target in m['merchant'] for alias,target in [('视频','视频'),('云存储','云存储'),('课程','课程')])]
                if len(selected)==1:
                    m = selected[0]
                    output['bank_actions'].append(_action('核对并关闭 '+m['merchant']+' 银行代扣','close_mandate',
                        m['account_id'],mandate_id=m['id']))
                    paragraphs.append('已生成待核对草案，尚未关闭。关闭银行代扣不等于取消商户订阅合同，也不免除应付款。')
                else: paragraphs.append('请明确要关闭哪一个商户的银行代扣，或到「账户」逐项选择。')
            else:
                subscriptions=[s for s in state.get('subscriptions',[]) if s['merchant_status']=='active']
                selected=[s for s in subscriptions if s['merchant_id'] in text or s['plan_name'] in text or
                          any(alias in text and token in (s['merchant_id']+' '+s['plan_name']) for alias,token in
                              [('视频','视频'),('云盘','云盘'),('云存储','云盘'),('健身','健身'),('课程','课程')])]
                if len(selected)==1:
                    item=selected[0]
                    output['bank_actions'].append(_action('核对并取消模拟订阅 '+item['plan_name'],
                        'cancel_subscription',item['account_id'],subscription_id=item['id']))
                    paragraphs.append('已生成模拟商户取消草案，尚未取消。确认后只更新本机模拟商户合同；银行代扣授权保持原状。')
                else: paragraphs.append('请明确要取消哪一个模拟商户订阅；如只想停银行扣款，请说“关闭某商户的银行代扣”。')
        performed = True
    if 'wealth' in selected_capabilities:
        output['intent']='bank_wealth'
        reserve = re.search(r'(?:保留|留出|留下|应急金(?:为|设为)?)[^，。；]{0,6}?([\d零一二两三四五六七八九十百千万]+(?:\.\d+)?\s*(?:万|千)?\s*(?:元|块))',text)
        floor = parse_amount(reserve[1]) if reserve else 0
        if floor:
            budget = read_capability('budgets.investable',
                                     {'account_id':main['id'],'reserve_floor_minor':floor})
        output['budget']=budget
        account_balances = '；'.join(f'{account["name"]} {money(account["balance_minor"])}'
                                     for account in state['accounts'])
        paragraphs.append(f'模拟账户现金合计 {money(state["total_cash_minor"])}（{account_balances}）；理财持仓本金 {money(state["holding_principal_minor"])}；待结算赎回 {money(state["pending_redemption_minor"])}。')
        paragraphs.append(f'按日常账户计算，可投资 {money(budget["investable_minor"])}：余额 {money(budget["cash_minor"])} − 应急保留 {money(budget["reserve_minor"])} − 30天内已知到期款 {money(budget["upcoming_due_minor"])} − 已预留资金 {money(budget["held_minor"])}。备用账户未自动并入可投资金额；未预测其他生活支出。')
        selected=[]
        if product_query:
            products = read_capability('products.catalog')
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
        if product_query:
            paragraphs.append('所有产品均为虚构演示；没有预测或承诺收益，未模拟市场估值与费用。')
        performed = True
    if 'wealth' in selected_capabilities and re.search(
            r'资金时间轴|现金流|未来\s*\d{1,3}\s*(?:天|日).{0,10}(?:资金|余额)|(?:\d{1,3}\s*(?:天|日)(?:后|内)).{0,10}(?:资金|余额)',
            text):
        requested = re.search(r'(?<!\d)(\d{1,3})\s*(?:天|日)',text)
        horizon_days = int(requested[1]) if requested else 90
        if not 1 <= horizon_days <= 365:
            output['reply']='模拟资金时间轴的范围为 1 至 365 天；请给出这个范围内的天数。'
            return output
        timeline = read_capability('cashflow.timeline', {'days':horizon_days})
        output['agent_chain'].append({'tool':'cashflow_timeline','source':timeline['source'],
                                      'horizon_days':horizon_days,'as_of':timeline['as_of'],
                                      'evidence_count':sum(len(day['events']) for day in timeline['timeline'])})
        try:
            memory_context = gateway.memories.read_relevant(workspace,text,workflow='cashflow')
            memory_nodes = memory_context.nodes
        except Exception:
            # 个性化记忆是可选上下文；存储不可用时照常提供不含记忆的资金推演。
            memory_nodes = ()
        try:
            model_options = {}
            if model_agents is not None:
                model_options = {"supervisor": model_agents[0], "reviewer": model_agents[1]}
            workflow = run_cashflow_workflow(text, state, timeline,
                                             memory_nodes=tuple(memory_nodes), **model_options)
        except Exception:
            workflow = {
                'status':'fallback',
                'answer':'这项资金时间轴暂时无法完成来源核验。为避免给您造成误导，我先不提供具体金额，请稍后重试。',
                'task_graph':[],
                'trace':[{'stage':'cashflow.prepare','outcome':'failed_closed','model_id':None,'duration_ms':None}],
                'findings':[{'code':'CASHFLOW_PREPARATION_FAILED','severity':'critical',
                             'message':'资金时间轴证据准备未通过校验。','evidence_ids':[]}],
                'evidence':[], 'evidence_sources':{}, 'attempts':0, 'bank_actions':[],
            }
        output['agent_workflow'] = workflow
        output['collaboration_status'] = workflow['status']
        output['isFallback'] = workflow['status'] != 'approved'
        output['fallback_reason'] = workflow.get('fallback_reason')
        output['user_memory_context'] = workflow.get('memory_context', [])
        if workflow['status']=='approved':
            output['cashflow_timeline'] = timeline
        else:
            output['cashflow_timeline'] = {'status':'withheld','source':timeline['source'],
                                           'horizon_days':horizon_days}
        output['agent_chain'].append({'tool':'multi_agent_cashflow','status':workflow['status'],
                                      'task_count':len(workflow['task_graph']),
                                      'evidence_count':len(workflow['evidence']),
                                      'reviewed':any(item['stage']=='reviewer.review' and item['outcome']=='approve'
                                                     for item in workflow['trace'])})
        if workflow['status'] == 'approved':
            paragraphs.append(workflow['answer'])
        else:
            # 现金流主模型或独立审核失败时，不展示同一回复中的其他未审核金额和建议。
            output['budget'] = None
            output['products'] = []
            output['bank_actions'] = []
            output.pop('report', None)
            output.pop('anomalies', None)
            output['cashflow_timeline'] = {'status': 'withheld', 'source': timeline['source'],
                                           'horizon_days': horizon_days}
            output['reply'] = workflow['answer']
            return output
        performed = True
    if 'transfer' in selected_capabilities and re.search(r'转账|转给|汇给|给.{0,8}转|定时转账|每月.*转|按月.*转',text):
        matches=_recipient(state,text)
        amount=parse_amount(text)
        if len(matches)!=1:
            paragraphs.append('请从账户页选择一个明确的模拟收款人；同名联系人需要按别名区分，不能仅凭含糊姓名生成转账。')
        elif not amount:
            paragraphs.append('已识别模拟收款人 '+matches[0]['display_name']+'，请补充明确金额后再生成待确认草案。')
        elif matches[0]['status']!='active' or not matches[0]['verified']:
            paragraphs.append('该模拟收款人未通过核验或已被拦截，不能生成转账草案。')
        else:
            recurring=bool(re.search(r'每月|每个月|每月定期|按月',text))
            action='schedule_transfer' if recurring else 'transfer'
            if recurring:
                next_due=(date.fromisoformat(state['as_of'])+timedelta(days=1)).isoformat()
                output['bank_actions'].append(_action('核对并启用每月模拟转账 · '+matches[0]['display_name']+' '+money(amount),
                    action,main['id'],beneficiary_id=matches[0]['id'],amount_minor=amount,
                    frequency='monthly',next_due_on=next_due))
                paragraphs.append('已生成每月转账草案，首次模拟执行日为 '+next_due+'；只有你在确认卡逐项核对并确认后才会启用，之后仍需手动推进模拟日期。')
            else:
                output['bank_actions'].append(_action('核对并转账 · '+matches[0]['display_name']+' '+money(amount),
                    action,main['id'],beneficiary_id=matches[0]['id'],amount_minor=amount,reference='对话生成的模拟转账'))
                paragraphs.append('已生成待核对转账草案，尚未执行。确认卡会重新检查收款人、额度、近期到期款和预留资金；确认后只记入本机模拟账本。')
        performed=True
    if 'transfer' in selected_capabilities and re.search(r'AA收款|发起AA|登记.*收款',text):
        splits=state.get('split_bills',[])
        if re.search(r'登记.*收款',text):
            people=[p for p in state.get('split_participants',[]) if p['status']=='requested' and
                    (p['display_name'] in text or p['display_name'].replace('（虚构）','') in text)]
            if len(people)==1:
                person=people[0]; split=next((s for s in splits if s['id']==person['split_id']),None)
                if split:
                    output['bank_actions'].append(_action('核对并登记AA模拟收款 '+person['display_name'],
                        'collect_split_payment',split['account_id'],split_id=split['id'],participant_id=person['id']))
                    paragraphs.append('已生成待核对收款草案；确认后只记一笔模拟入账，不代表真实资金到账。')
                else: paragraphs.append('没有找到该参与人对应的模拟AA账单。')
            else: paragraphs.append('请在账户页选择具体AA账单和参与人后登记模拟收款。')
        else:
            drafts=[s for s in splits if s['status']=='draft']
            if len(drafts)==1:
                split=drafts[0]
                output['bank_actions'].append(_action('核对并发起AA收款 · '+split['title'],
                    'request_split',split['account_id'],split_id=split['id']))
                paragraphs.append('已生成整张AA账单的待确认草案；确认后仅记录本机模拟收款请求，不会联系参与人。')
            else: paragraphs.append('当前没有唯一可发起的模拟AA账单，请到账户页选择。')
        performed=True
    if 'cards' in selected_capabilities and re.search(r'挂失|提额|提高.*额度|调整.*额度|开启.*交易|关闭.*交易|申请.*(?:信用卡|借记卡|银行卡)|申请补卡|办一张.*卡',text):
        action=None; values={}; label=''
        if '挂失' in text:
            action='report_card_lost'; cards=_card(state,text)
            label='核对并模拟挂失'
        elif re.search(r'提额|提高.*额度',text):
            action='request_card_limit'; cards=_card(state,text,credit_only=True); amount=parse_amount(text)
            label='核对并提交模拟提额申请'
            if amount: values['requested_limit_minor']=amount
        elif re.search(r'开启.*交易|关闭.*交易',text):
            cards=_card(state,text)
            kind=next((name for words,name in [('线上','online'),('境外','overseas'),('非接触','contactless')] if words in text),None)
            if kind:
                action='card_control'; values.update(control_type=kind,enabled='开启' in text); label='核对并调整模拟卡片交易控制'
            else: paragraphs.append('请说明要开启或关闭线上、境外或非接触交易中的哪一项。'); cards=[]
        elif '申请补卡' in text:
            action='apply_card'; cards=[c for c in state.get('cards',[]) if c['status']=='lost' and
                    (c['last_four'] in text or (c['type']=='credit' and '信用卡' in text) or (c['type']=='debit' and '借记卡' in text))]
            label='核对并提交模拟补卡申请'
            if len(cards)==1: values.update(requested_type=cards[0]['type'],requested_limit_minor=cards[0]['credit_limit_minor'] if cards[0]['type']=='credit' else 0,card_id=cards[0]['id'])
        else:
            action='apply_card'; requested_type='credit' if '信用卡' in text else 'debit'
            cards=[None]; label='核对并提交模拟办卡申请'
            values['requested_type']=requested_type
            amount=parse_amount(text) if requested_type=='credit' else 0
            if amount is not None: values['requested_limit_minor']=amount
        if action:
            if len(cards)!=1:
                paragraphs.append('请明确卡片类型或尾号；有多张卡时不能替你猜选。')
            elif action=='request_card_limit' and 'requested_limit_minor' not in values:
                paragraphs.append('请补充拟申请的新额度，例如“信用卡提额到5000元”。')
            elif action=='apply_card' and 'requested_limit_minor' not in values:
                paragraphs.append('请补充信用卡申请额度后再提交模拟草案。')
            else:
                if cards[0] is not None: values['card_id']=cards[0]['id']
                card_account=cards[0]['account_id'] if cards[0] is not None else main['id']
                output['bank_actions'].append(_action(label,action,card_account,**values))
                paragraphs.append('已生成待核对模拟卡片操作，尚未执行。办卡或提额结果只会在手动推进模拟日期后按演示规则审核，不代表真实发卡或授信。')
        performed=True
    if 'cross_scene' in selected_capabilities and re.search(r'生日|礼物|鲜花|蛋糕|预约服务|预留.*(?:任务|预算|资金)',text):
        tasks=state.get('orchestration_tasks',[])
        task=next((t for t in tasks if t['status'] in ('draft','in_progress') and
                   any(k in text for k in ('生日','礼物','鲜花','蛋糕','预约','预留'))),None)
        if not task:
            paragraphs.append('当前没有可继续的模拟预约任务。')
        else:
            held=next((h for h in state.get('fund_holds',[]) if h['task_id']==task['id'] and h['status']=='held'),None)
            if held:
                quotes=[q for q in state.get('service_quotes',[]) if q['status']=='illustrative']
                if re.search(r'预约|完成|下单',text) and quotes:
                    output['bank_actions'].append(_action('核对并完成生日礼物模拟预约','book_services',held['account_id'],
                        task_id=task['id'],hold_id=held['id'],quote_ids=[q['id'] for q in quotes]))
                    paragraphs.append('已生成预约确认草案。确认后才会扣除模拟金额并建立本机预约记录，不会联系真实服务商。')
                else:
                    paragraphs.append('该任务已有生效资金预留 '+money(held['amount_minor'])+'；如需完成预约，请明确说“预约生日礼物”。')
            elif task['status']=='draft':
                quotes=[q for q in state.get('service_quotes',[]) if q['status']=='illustrative' and q['valid_until']>=state['as_of']]
                amount=sum(q['price_minor'] for q in quotes)
                if amount:
                    expires=(date.fromisoformat(task['due_on'])-timedelta(days=1)).isoformat()
                    output['bank_actions'].append(_action('核对并预留生日礼物预算 '+money(amount),'reserve_funds',main['id'],
                        task_id=task['id'],amount_minor=amount,expires_on=expires))
                    paragraphs.append('已生成资金预留草案 '+money(amount)+'；预留只占用模拟可用额度，不扣款。确认预留后再生成预约草案。')
                else: paragraphs.append('生日任务当前没有有效模拟报价，无法生成预约草案。')
            else: paragraphs.append('任务当前状态不允许继续预约，请到账户页查看记录。')
        performed=True
    if 'service_negotiation' in selected_capabilities:
        output['service_negotiation_request'] = {'text': text}
        paragraphs.append('我会先整理你的条件，再从预先批准的模拟服务优惠中核验可行方案。方案只使用演示规则，不涉及贷款、外部商家或真实合同；你可以选择是否向模拟银行代理分享已确认的有限财务偏好。')
        performed=True
    if not performed: paragraphs.append('可以查询账单、比较模拟理财产品，或生成转账、卡片、订阅、AA与生日预约草案；所有草案都需在确认卡中逐项核对。')
    output['reply']='\n\n'.join(paragraphs)
    return output
