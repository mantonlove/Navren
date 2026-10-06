"""目标本人确认到既有 held 原件的审计关联；不创建预留或变动银行资金。"""
import json
import sqlite3
from datetime import date

from simulated_bank import BankError
from financial_methods.contracts import iso_date


SCHEMA = ('''CREATE TABLE IF NOT EXISTS navren_goal_occupancy_links(
 workspace_id TEXT NOT NULL,goal_id TEXT NOT NULL,hold_id TEXT NOT NULL,
 draft_id TEXT NOT NULL,goal_version TEXT NOT NULL,receipt_hash TEXT NOT NULL,
 binding_hash TEXT NOT NULL,hold_snapshot TEXT NOT NULL,
 PRIMARY KEY(workspace_id,hold_id),
 FOREIGN KEY(workspace_id,goal_id) REFERENCES wealth_goals(workspace_id,id) ON DELETE CASCADE,
 FOREIGN KEY(draft_id) REFERENCES navren_goal_drafts(id))''',)


def _hash(value):
    from goal_service import version
    return version(value)


def _invalid(reason, status=409):
    raise BankError('GOAL_OCCUPANCY_INVALID',reason,status)


def _current_hold(db, workspace, hold_id, account_id):
    hold=db.execute('SELECT * FROM fund_holds WHERE workspace_id=? AND id=?',(workspace,hold_id)).fetchone()
    account=db.execute('SELECT id,workspace_id,currency FROM accounts WHERE workspace_id=? AND id=?',(workspace,account_id)).fetchone()
    ws=db.execute('SELECT as_of FROM workspaces WHERE id=?',(workspace,)).fetchone()
    if not hold or not account or not ws or hold['account_id']!=account_id or account['currency']!='CNY':
        _invalid('目标占用必须来自同一工作区、明确账户与人民币原件。')
    task=db.execute('SELECT * FROM orchestration_tasks WHERE workspace_id=? AND id=?',(workspace,hold['task_id'])).fetchone()
    if not task or task['status']!='in_progress' or hold['status']!='held' or type(hold['amount_minor']) is not int or hold['amount_minor']<=0:
        _invalid('既有预留或任务已不处于有效占用状态。')
    try:
        created,expires,now=map(iso_date,(hold['created_on'],hold['expires_on'],ws['as_of']))
        if created>now or expires<now or created>expires:raise ValueError()
    except (ValueError,TypeError):_invalid('既有预留日期无效、尚未生效或已经过期。')
    return {'hold':dict(hold),'account':dict(account),'task':dict(task)}


def prepare_occupancy_binding(db, workspace, payload, goal_id):
    present='occupancy_hold_ids' in payload or 'occupancy_account_id' in payload
    if not present:return None
    ids=payload.get('occupancy_hold_ids');account=payload.get('occupancy_account_id')
    if (payload['action']=='delete' or not isinstance(ids,list) or not 1<=len(ids)<=32
            or any(not isinstance(v,str) or not v for v in ids) or len(set(ids))!=len(ids)
            or not isinstance(account,str) or not account
            or type(payload.get('priority')) is not int or payload['priority']<0
            or payload.get('funding_kind')!='cash' or payload.get('status','active')!='active'):
        _invalid('关联预留须明确不重复的真实预留ID、账户、本人优先级和现金用途。',422)
    snapshots=[_current_hold(db,workspace,hid,account) for hid in sorted(ids)]
    if sum(s['hold']['amount_minor'] for s in snapshots)>payload['target_minor']:
        _invalid('关联既有占用不得超过本人确认的总用途金额。',422)
    for hid in ids:
        linked=db.execute('SELECT goal_id FROM navren_goal_occupancy_links WHERE workspace_id=? AND hold_id=?',(workspace,hid)).fetchone()
        if linked and linked[0]!=goal_id:
            raise BankError('GOAL_OCCUPANCY_REUSED','同一既有预留已关联另一目标，不能再次计为覆盖。',409)
    return {'schema_version':1,'workspace_id':workspace,'goal_id':goal_id,
            'account_id':account,'hold_ids':sorted(ids),'snapshots':snapshots}


def validate_occupancy_binding(db, workspace, payload, draft):
    expected=prepare_occupancy_binding(db,workspace,payload,draft['goal_id'])
    try:actual=json.loads(draft['occupancy_binding']) if draft.get('occupancy_binding') is not None else None
    except (ValueError,TypeError):_invalid('预留关联草案原件损坏。')
    if expected!=actual:_invalid('预留、任务或账户已变化，请重新核对关联草案。')
    return actual


def commit_occupancy_binding(db, workspace, draft, payload, goal, result, binding):
    # 原GoalService确认事务内只写关联；旧关系不会随新目标版本继续取得信用。
    db.execute('DELETE FROM navren_goal_occupancy_links WHERE workspace_id=? AND goal_id=?',(workspace,draft['goal_id']))
    if binding is None:return
    from goal_service import GoalService
    prospective={**draft,'status':'committed','result':json.dumps(result,ensure_ascii=False)}
    proof=GoalService._confirmation(db,workspace,goal,prospective)
    if not proof['confirmed'] or proof['funding_kind']!='cash':_invalid('预留关联没有一致的本人目标确认回执。')
    for snapshot in binding['snapshots']:
        try:
            db.execute('INSERT INTO navren_goal_occupancy_links VALUES(?,?,?,?,?,?,?,?)',
                       (workspace,goal['id'],snapshot['hold']['id'],draft['id'],goal['object_version'],
                        _hash({'digest':draft['digest'],'result':result}),_hash(binding),json.dumps(snapshot,ensure_ascii=False,sort_keys=True)))
        except sqlite3.IntegrityError:
            raise BankError('GOAL_OCCUPANCY_REUSED','同一预留不可为多个目标取得覆盖信用。',409) from None


def goal_occupancy_facts(db, workspace, goal_id):
    """同 db 只读重验已确认关系；事实占用还不意味着预算已扣过。"""
    from goal_service import GoalService
    zero={'already_occupied_minor':0,'occupancy_refs':[],'occupancy_verified_in_baseline':False,
          'input_refs':[],'source_versions':{},'snapshots':[]}
    links=[dict(r) for r in db.execute('SELECT * FROM navren_goal_occupancy_links WHERE workspace_id=? AND goal_id=? ORDER BY hold_id',(workspace,goal_id))]
    if not links:return zero,[]
    refs=['fund_holds/'+r['hold_id'] for r in links]
    bad={**zero,'already_occupied_minor':None,'occupancy_refs':refs,'input_refs':refs}
    try:
        current=db.execute('SELECT * FROM wealth_goals WHERE workspace_id=? AND id=?',(workspace,goal_id)).fetchone()
        if not current:_invalid('目标已不存在。')
        goal=GoalService._item(dict(current));proof=GoalService._confirmation(db,workspace,goal)
        if not proof['confirmed'] or proof['funding_kind']!='cash':_invalid('目标确认已经失效。')
        draft=db.execute("SELECT * FROM navren_goal_drafts WHERE workspace_id=? AND goal_id=? AND status='committed' ORDER BY rowid DESC LIMIT 1",(workspace,goal_id)).fetchone()
        draft=dict(draft);payload,result=GoalService._committed_result(draft,workspace)
        binding=validate_occupancy_binding(db,workspace,payload,draft)
        if not binding or set(binding['hold_ids'])!={r['hold_id'] for r in links}:_invalid('关联集合与本人原确认不同。')
        ws=db.execute('SELECT version FROM workspaces WHERE id=?',(workspace,)).fetchone()
        versions={r:str(ws[0]) for r in refs};extra=[]
        snapshots={s['hold']['id']:s for s in binding['snapshots']}
        for link in links:
            snapshot=snapshots[link['hold_id']]
            if (link['draft_id']!=draft['id'] or link['goal_version']!=goal['object_version']
                    or link['receipt_hash']!=_hash({'digest':draft['digest'],'result':result})
                    or link['binding_hash']!=_hash(binding) or json.loads(link['hold_snapshot'])!=snapshot):
                _invalid('目标预留关联原件已被修改。')
            ref='goal_occupancy/'+workspace+'/'+goal_id+'/'+link['hold_id']
            extra.append(ref);versions[ref]=_hash(link)
        return {**zero,'already_occupied_minor':sum(s['hold']['amount_minor'] for s in snapshots.values()),
                'occupancy_refs':refs,'input_refs':[*refs,*extra], 'source_versions':versions,
                'snapshots':list(snapshots.values())},[]
    except (BankError,ValueError,TypeError,KeyError):
        ws=db.execute('SELECT version FROM workspaces WHERE id=?',(workspace,)).fetchone()
        bad['source_versions']={r:str(ws[0]) for r in refs} if ws else {}
        return bad,['goal_occupancy_link_invalid_or_stale']


def verify_goal_occupancy(db, workspace, funds, row):
    """返回 (AllocationGoal占用字段, gaps)；funds必须先由根发行器解析和核baseline。"""
    facts,gaps=goal_occupancy_facts(db,workspace,row.get('goal_id'))
    fields={k:facts[k] for k in ('already_occupied_minor','occupancy_refs','occupancy_verified_in_baseline')}
    claimed=row.get('occupancy_refs',())
    if not facts['occupancy_refs']:
        if row.get('already_occupied_minor') not in (None,0) or claimed:
            return {**fields,'already_occupied_minor':None,'occupancy_refs':list(claimed) if isinstance(claimed,(list,tuple)) else []},['goal_occupancy_unregistered']
        return fields,[]
    if gaps:return fields,gaps
    from goal_service import GoalService
    current=db.execute('SELECT * FROM wealth_goals WHERE workspace_id=? AND id=?',(workspace,row.get('goal_id'))).fetchone()
    goal=GoalService._item(dict(current));proof=GoalService._confirmation(db,workspace,goal)
    try:
        sources=dict(row.get('source_versions',()))
        refs=row.get('input_refs',())
        if not isinstance(refs,(tuple,list)):raise ValueError()
    except (ValueError,TypeError):
        return {**fields,'already_occupied_minor':None},['goal_occupancy_confirmation_evidence_invalid']
    if (row.get('confirmed') is not True or row.get('currency')!=funds.currency or funds.currency!='CNY'
            or row.get('subject_id')!=funds.subject_id or row.get('data_scope')!=funds.data_scope
            or type(row.get('amount_minor')) is not int or row.get('amount_minor')!=goal['target_minor']
            or type(row.get('priority')) is not int or row.get('priority')!=goal['priority']
            or row.get('needed_on')!=goal['due_on'] or row.get('product_key','absent') is not None
            or any(ref not in refs for ref in proof['input_refs'])
            or any(sources.get(ref)!=v for ref,v in proof['source_versions'].items())):
        return {**fields,'already_occupied_minor':None},['goal_occupancy_subject_or_confirmation_mismatch']
    try:
        begin=iso_date(funds.as_of or funds.period_start)
        ws=db.execute('SELECT version,as_of FROM workspaces WHERE id=?',(workspace,)).fetchone()
        versions=dict(funds.source_versions)
        if funds.basis!='floor_surplus' or funds.formula_id!='funds.current_budget/1' or versions.get('bank')!=str(ws['version']):
            raise ValueError('goal_occupancy_budget_version_or_basis_mismatch')
        for snapshot in facts['snapshots']:
            hold,account=snapshot['hold'],snapshot['account'];ref='fund_holds/'+hold['id']
            if (funds.subject_id not in (workspace,account['id']) or 'accounts/'+account['id'] not in funds.input_refs
                    or ref not in funds.input_refs or versions.get(ref)!=str(ws['version'])
                    or not iso_date(hold['created_on'])<=begin<=iso_date(hold['expires_on'])):
                raise ValueError('goal_occupancy_not_deducted_in_budget_baseline')
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        return {**fields,'already_occupied_minor':None},[str(error)]
    return {**fields,'occupancy_verified_in_baseline':True},[]
