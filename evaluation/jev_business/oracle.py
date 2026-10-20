"""隔离种子/原件预检和必要条件；自动结果不是完整语义准确率。"""
from __future__ import annotations
from contextlib import contextmanager, closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import threading
from unittest.mock import patch

from .catalog import ROOT, load_dataset, sha

_LOCK = threading.RLock()
_MISSING = object()

def packed(v):
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

@contextmanager
def isolated_environment(root):
    from cryptography.fernet import Fernet
    values = {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
              'NAVREN_KNOWLEDGE_DB': str(root/'knowledge.sqlite3'),
              'NAVREN_MARKET_DB': str(root/'market.sqlite3'),
              'NAVREN_FUND_CACHE': str(root/'funds'), 'NAVREN_FINANCIAL_DB': ''}
    prior = {k: os.environ.get(k, _MISSING) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in prior.items():
            if v is _MISSING: os.environ.pop(k, None)
            else: os.environ[k] = v

@contextmanager
def no_network():
    with patch('socket.create_connection', side_effect=AssertionError('fixture_network_forbidden')), \
         patch('socket.socket.connect', side_effect=AssertionError('fixture_network_forbidden')):
        yield

def clone_database(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file() or source == target or target.exists():
        raise ValueError('clone_requires_existing_source_and_new_target')
    read = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
    write = sqlite3.connect(target)
    try: read.backup(write)
    finally: read.close(); write.close()
    target.chmod(0o600)

def reviewed_suite():
    return json.loads((ROOT/'local_knowledge/acceptance/reviewed_cases.json').read_text())

def public_review(provider, case):
    from local_knowledge.acceptance.run_review import verify_original, evaluate
    suite = reviewed_suite()
    original = verify_original(provider, suite)
    if not original or not all(c['passed'] for c in original):
        raise ValueError('public_original_verification_failed')
    selected = next(c for c in suite['cases'] if c['id'] == case['origin']['id'])
    return {'original_checks': original, 'provider_check': evaluate(provider, suite, selected)}

def independent_accounts():
    seed = json.loads((ROOT/'simulated_bank/fixtures/demo.json').read_text())
    balances = {a['key']: a['opening_balance_minor'] for a in seed['accounts']}
    for row in seed['transactions']:
        delta = row['amount_minor'] * (1 if row['kind'] in ('income','refund') else -1)
        balances[row['account_key']] += delta
    return [{'key': a['key'], 'name': a['name'], 'currency': seed['currency'],
             'balance_minor': balances[a['key']]} for a in seed['accounts']]

class NoFetchCatalog:
    def read(self, query='', limit=12, offset=0):
        return {'status': 'unavailable', 'products': [], 'verified_nav_products': [],
                'source': 'isolated_fixture_no_cold_fetch', 'source_date': None,
                'stale': True, 'current_sale_status': None, 'executable': False,
                'notice': '本轮只能使用冻结公共原件，无冷抓取。'}

def build_fixture(case, temporary, *, public_database=None, rules_path=None):
    """只接受系统临时空目录；绝不使用环境中的个人现役数据库。"""
    data = load_dataset()
    if case not in data['cases']: raise ValueError('case_not_frozen_original')
    root = Path(temporary).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if not root.is_relative_to(temp) or root == temp or not root.is_dir() or any(root.iterdir()):
        raise ValueError('fixture_requires_system_temporary_empty_directory')
    with _LOCK, isolated_environment(root), no_network():
        from finance_gateway import FinanceGateway
        from market_content import MarketStore
        from local_knowledge import LocalKnowledgeProvider
        if public_database: clone_database(public_database, root/'knowledge.sqlite3')
        gateway = FinanceGateway(database=str(root/'bank.sqlite3'))
        gateway.market = MarketStore(path=root/'market.sqlite3')
        gateway.public_products = NoFetchCatalog()
        (root/'funds').mkdir()
        # 避免延续实例默认 fund_history 缓存，按已有方法重绑每实例缓存。
        from types import FunctionType, MethodType
        import fund_history
        from wealth_portfolio import WealthPortfolio
        for name in ('provision','products','migrate'):
            method = getattr(WealthPortfolio, name)
            namespace = dict(method.__globals__)
            namespace['cached'] = lambda code: fund_history.cached(code, cache=root/'funds')
            bound = FunctionType(method.__code__, namespace, method.__name__, method.__defaults__, method.__closure__)
            bound.__kwdefaults__ = method.__kwdefaults__
            setattr(gateway.wealth, name, MethodType(bound, gateway.wealth))
        workspace, token = gateway.create_session()
        shutil.copyfile(rules_path or ROOT/'simulated_bank/service_benefit_rules.json', root/'service-rules.json')
        gateway.service_negotiations.rules_path = root/'service-rules.json'
        original_report = None
        if case['group'] != 'excluded' and case.get('public_original'):
            if public_database is None: raise ValueError('public_snapshot_required')
            original_report = public_review(gateway.knowledge_provider, case)
        gateway._jev_business_fixture = {'case_id': case['id'], 'business_date': '2026-10-03',
            'accounts': independent_accounts(), 'public_review': original_report,
            'history': [], 'private_active_database_used': False,
            'public_snapshot_sha256': sha(public_database) if public_database else None,
            'rules_sha256': sha(gateway.service_negotiations.rules_path)}
        return gateway, workspace, token

def check(label, actual, expected):
    return {'label': label, 'actual': actual, 'expected': expected,
            'passed': type(actual) is type(expected) and actual == expected}

def preflight(g, w, case):
    with no_network():
        checks = []
        archive = g.bank.customer_archive(w)
        expected = independent_accounts()
        actual = [{'key': next(a['key'] for a in expected if row['id'].endswith('-'+a['key'])),
                   'name': row['name'], 'currency': row['currency'], 'balance_minor': row['balance_minor']}
                  for row in archive['accounts']]
        checks.append(check('种子独立整数复算账户', sorted(actual,key=lambda a:a['key']), sorted(expected,key=lambda a:a['key'])))
        checks.append(check('账户业务日', archive['as_of'], '2026-10-03'))
        checks.append(check('没有添加对话前文', g.personal_data.read_history(w), []))
        report = deepcopy(g._jev_business_fixture)
        from agent_runtime.data_boundary import CloudDataBoundary
        from local_knowledge.runtime import RoundProvider
        release=reviewed_suite()['reviewed_release_id'] if case.get('public_original') else g.knowledge_provider.status().get('public_release_id')
        runtime_provider=RoundProvider(g.knowledge_provider.for_release(release) if release else g.knowledge_provider,records=(),release_id=release)
        catalog=runtime_provider.public_catalog()
        boundary=CloudDataBoundary();boundary.register_public_terms(catalog['terms'],catalog['identifiers'])
        report['public_query_hints']=boundary.public_query_hints()
        report['public_catalog_count']={k:len(catalog[k]) for k in ('terms','identifiers')}
        report['public_product_records_count']=0
        report['public_product_records_policy']='no_cold_fetch_records_empty; catalog_and_canonical_search_preserved; not_full_product_catalog_coverage'
        if case['id'] == 'P02':
            view = g.calendar.query(w, {'period':'next_month','view':'month'})
            checks.append(check('原日历下月范围', view['range'], {'from':'2026-11-01','to':'2026-11-30'}))
            report['calendar'] = {k:view[k] for k in ('range','coverage','limitations','summary_by_currency')}
        if case['id'] == 'P08':
            p = g.service_negotiations.preview(w, {'text':case['question'],'share_memory':False})
            checks.extend([check('原服务两候选', sorted(o['offer_id'] for o in p['offers']),
                                 ['SYNTH-E-STATEMENT-FEE-90D','SYNTH-PAYROLL-FEE-180D']),
                check('保留工资意愿',p['conditions']['salary_deposit_intent'],True),
                check('保留拒买理财',p['conditions']['investment_purchase_refused'],True),
                check('规则为演示合成',g.service_negotiations.rules()['environment'],'synthetic')])
            rules = json.loads(g.service_negotiations.rules_path.read_text())
            independently_selected = [o for o in rules['offers'] if o['offer_id'] in
                ('SYNTH-E-STATEMENT-FEE-90D','SYNTH-PAYROLL-FEE-180D')]
            checks.append(check('预览所有原规则字段逐项一致',
                all(any(all(offer.get(k)==v for k,v in rule.items()) for offer in p['offers'])
                    for rule in independently_selected), True))
            report['service_oracle'] = {'offers':independently_selected,'conditions':p['conditions'],'notice':rules['notice']}
            # 预检草案不能被模型沿用；恢复无草案的独立起点。
            with g.bank.transaction() as db:
                db.execute('DELETE FROM base_service_negotiation_terms WHERE draft_id IN (SELECT id FROM service_negotiation_drafts WHERE workspace_id=?)',(w,))
                db.execute('DELETE FROM base_service_condition_policies WHERE draft_id IN (SELECT id FROM service_negotiation_drafts WHERE workspace_id=?)',(w,))
                db.execute('DELETE FROM service_negotiation_drafts WHERE workspace_id=?',(w,))
        if case.get('public_original'):
            public = report['public_review']
            checks.append(check('官方保存原件逐字段核对', all(x['passed'] for x in public['original_checks']), True))
            checks.append(check('固定发布提供器原题核对',public['provider_check']['passed'],True))
        pending = case.get('fixture_status') == 'historical_context_unavailable'
        return {'status': 'not_ready' if pending else 'passed' if all(c['passed'] for c in checks) else 'failed',
                'reason': 'P05_trusted_historical_as_of_context_not_supported' if pending else None,
                'checks': checks, 'oracle': report, 'model_calls':0, 'semantic_pass':None}

def snapshot(g, w):
    """金融/申请/记忆原始状态；允许只读缓存、预览草案、历史答复变动。"""
    with closing(g.bank.connect()) as db:
        tables = ('accounts','orders','holdings','operations','profiles','service_negotiation_applications',
                  'calendar_series','fund_holds')
        state = {}
        for table in tables:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                state[table] = None; continue
            cols = [r[1] for r in db.execute('PRAGMA table_info('+table+')')]
            if 'workspace_id' not in cols: raise ValueError('snapshot_table_without_workspace:'+table)
            rows = [dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE workspace_id=?',(w,))]
            state[table] = sorted(rows,key=packed)
    memories = g.memories.state(w)
    state['memories'] = {'enabled':memories['enabled'],'items':memories['items']}
    with g.personal_data.connection(w) as db:
        for table in ('profile_observations','memory_candidates'):
            rows=[dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE workspace_id=?',(w,))]
            for row in rows: row.pop('confirmation_token',None)
            state[table]=sorted(rows,key=packed)
    return state

def observed_outputs(record, name):
    rows=record.get('authoritative_tool_outputs',record.get('tool_outputs',[]))
    return [o['output'] for o in rows if o.get('name') == name
            and isinstance(o.get('output'),dict) and not o['output'].get('error')]

def classify(record, case):
    """拒绝HTTP200/approved/词命中自动充当 gold。"""
    result = record.get('result') or {}
    checks = [check('题目身份与原问句', (record.get('id'),record.get('question')), (case['id'],case['question'])),
              check('隔离预检通过',record.get('preflight',{}).get('status'),'passed'),
              check('金融和申请记忆状态不变', bool(record.get('before')) and record.get('before')==record.get('after'),True),
              check('无操作确认卡',bool(result.get('bank_actions')),False)]
    contract = result.get('answer_contract')
    terminal = (isinstance(contract,dict) and contract.get('kind') in ('verified_facts','reviewed_text')
                and isinstance(result.get('reply'),str) and bool(result['reply'].strip())
                and bool(result.get('bank_response_id')))
    terminal_technical = (record.get('http_status')==200 and not result.get('isFallback',False)
                 and not result.get('agent_runtime',{}).get('failure') and terminal)
    checks.append(check('已发布原生终结合同',terminal_technical,True))
    outputs = lambda name: observed_outputs(record,name)
    if case['id'] == 'P01':
        accounts = [a for o in outputs('account_summary') for a in o.get('data',{}).get('accounts',[])]
        expected = record.get('preflight',{}).get('oracle',{}).get('accounts',[])
        correct = bool(expected) and len(accounts)==len(expected) and all(any(
            a.get('id','').endswith('-'+e['key']) and a.get('currency')==e['currency'] and
            a.get('balance_minor')==e['balance_minor'] for a in accounts) for e in expected)
        # 模型投影可能金额改成带签名 metrics/facts；缺字段待审，不从预检补成已读。
        checks.append({'label':'余额工具实际同轮读取','passed':bool(outputs('account_summary'))})
        if accounts and all('balance_minor' in a for a in accounts):
            checks.append(check('模型实际读取余额与种子一致',correct,True))
        if contract and contract.get('kind')=='verified_facts':
            checks.append(check('选定事实与组件同一集合',sorted(set(contract.get('fact_ids',[]))),
                                sorted(set((result.get('reply_presentation') or {}).get('fact_ids',[])))))
    elif case['id'] == 'P02':
        views = [o.get('data',{}) for o in outputs('calendar_query')]
        checks.append(check('实际读取正确下月范围', any(v.get('range')=={'from':'2026-11-01','to':'2026-11-30'} for v in views),True))
        checks.append(check('已发布同轮日历组件',any(v.get('type')=='financial_calendar' and v.get('range')=={'from':'2026-11-01','to':'2026-11-30'} for v in result.get('widgets',[])),True))
    elif case['id'] in ('P04','P05','R01','R02'):
        searches = [o.get('data',{}) for o in outputs('knowledge_search')]
        if case['id'] in ('P04','P05'):
            hits = [hit for s in searches for hit in s.get('hits',[])]
            checks.append(check('实际同轮历史引用匹配版本/日期/产品',any(
                h.get('product_id')=='WEBANK:000015' and h.get('release_id')==reviewed_suite()['reviewed_release_id']
                and h.get('document_version_id')==reviewed_suite()['products'][1]['version_id']
                and h.get('published_on')=='2026-09-24' and h.get('share_class_id') is None for h in hits),True))
        else:
            checks.append(check('规则已过滤不可用引用',bool(searches) and all(not s.get('hits') for s in searches),True))
            gap = case['expect']['gap']
            checks.append(check('对应覆盖缺口保留',any(g.get('code')==gap for s in searches for g in s.get('coverage_gaps',[])),True))
    elif case['id'] == 'P08':
        previews = [o.get('data',{}) for o in outputs('service_offer_preview')]
        checks.append(check('实际同轮读取受限预览',bool(previews),True))
        checks.append(check('两原候选与拒买底线',any(sorted(x.get('offer_id') for x in p.get('offers',[]))==['SYNTH-E-STATEMENT-FEE-90D','SYNTH-PAYROLL-FEE-180D']
            and p.get('conditions',{}).get('investment_purchase_refused') is True and
            all(x.get('requires_investment_purchase') is False for x in p.get('offers',[])) for p in previews),True))
    # 传输与终结格式完成不等于业务完成。必读工具/来源/组件检查仍
    # 留在 failures；缺少它们不能被误记为模型传输或原生格式失败。
    technical = terminal_technical
    failures = [c['label'] for c in checks if c.get('passed') is False]
    if record.get('runner_failure') or record.get('preflight',{}).get('status')!='passed': status='fixture_or_runner_error'
    elif not technical: status='technical_failure'
    elif failures: status='necessary_condition_failure'
    else: status='needs_independent_semantic_review'
    return {'status':status,'technical_completed':technical,'checks':checks,'failed_checks':failures,
            'semantic_pass': None, 'reviewer_approved_used_as_gold':False,
            'necessary_conditions_only':True,'human_review':{'verdict':None,'reviewer':None,'evidence':None,
                'must_convey':case['must_convey'],'must_not_convey':case['must_not_convey']}}
