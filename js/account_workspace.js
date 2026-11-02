/* 当前账本对象与完整子视图。视图状态只保存引用，不保存金融状态。 */
window.AccountWorkspace=(()=>{
  const titles={home:'我的账户',funds:'我的资金',securities:'我的证券',payments:'转账与AA','payment-detail':'收付款详情','payment-platform':'支付账户',cards:'我的银行卡',subscriptions:'订阅与代扣',holdings:'当前持仓',plans:'生活计划',activity:'全部活动',statements:'流水与报告',archive:'档案目录',calendar:'财务日历','calendar-day':'当日安排','calendar-item':'事项详情','wealth-product':'产品权益与收益','account-detail':'现金账户','card-detail':'银行卡详情','holding-detail':'持仓详情','subscription-detail':'订阅与代扣详情','plan-detail':'生活计划详情','operation-detail':'操作详情','record-detail':'档案详情'};
  const statusLabels={active:'有效',lost:'已挂失',expired:'已过期',draft:'草案',prepared:'待本人确认',unknown:'状态待核实',submitting:'提交中',executed:'已执行',submitted:'已受理',pending_settlement:'待结算',failed:'失败',declined:'被拒绝',cancelled:'已取消',closed:'已关闭',issued:'已发卡',held:'已预留',released:'已解除',completed:'已完成'};
  let state=null,products=[],archive=null,archiveVersion=null,context={view:'home'},generation=0,calendarHost=null;
  const node=(tag,text,cls)=>BankUI.node(tag,text,cls), money=value=>Number.isSafeInteger(value)?BankClient.money(value):'未知';
  function objectVersion(kind,id){if(kind==='operation')return (state?.operations||[]).find(op=>op.id===id)?.version;if(kind==='goal')return (archive?.records?.wealth_goals||[]).find(goal=>goal.id===id)?.object_version;return state?.version;}
  function ref(kind,id,extra={}){return {source_scope:'simulation',object_kind:kind,object_id:String(id),object_version:objectVersion(kind,id),...extra};}
  function link(text,view,metadata={}){const a=node('a',text,'account-view-link');const q=new URLSearchParams({view});for(const [k,v]of Object.entries(metadata))if(v!=null&&['object_kind','object_id','source_scope','portfolio_id','object_version','date'].includes(k))q.set(k,String(v));a.href='account.html?'+q;a.onclick=e=>{if(e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();NavrenViews.open(view,metadata);};return a;}
  function objectRow(item){const a=link('',item.view,{...ref(item.kind,item.id),...item.metadata});a.classList.add('account-object');a.id='account-object-'+encodeURIComponent((context.view||'home')+'-'+item.kind+'-'+item.id+(item.date?'-arrangement':'-fund'));const identity=node('span',undefined,'account-object-identity');identity.append(node('strong',item.name),node('small',item.note||''));const amount=node('span',undefined,'account-object-amount');amount.append(node('strong',item.amount==null?(item.status||'查看详情'):money(item.amount)),node('small',item.status||''));a.append(identity,amount,node('span','›','account-object-arrow'));return a;}
  function allFunds(){if(!state)return [];const rows=[];
    for(const a of state.accounts||[])rows.push({kind:'account',id:a.id,name:a.name,amount:a.balance_minor,note:'已结算现金 · '+(a.currency||'CNY'),status:'演示账户',view:'account-detail'});
    for(const card of state.cards||[])rows.push({kind:'card',id:card.id,name:(card.type==='virtual'?'SIM 虚拟卡':card.type==='credit'?'信用卡':'借记卡')+' · '+card.network+' '+card.last_four,amount:card.type==='credit'?card.outstanding_minor:null,note:card.type==='credit'?'当前未还':'关联现金账户',status:statusLabels[card.status]||card.status,view:'card-detail'});
    for(const h of state.holdings||[])if(h.amount_minor>0)rows.push({kind:'holding',id:h.account_id+'|'+h.product_id,name:products.find(p=>p.id===h.product_id)?.name||'产品资料未取得',amount:h.amount_minor,note:'持有本金 · 演示持仓',view:'holding-detail'});
    for(const [group,kind,label,amount]of [['deposit_contracts','deposit','存款本金','principal_minor'],['loans','loan','未偿本金','outstanding_minor']])for(const r of archive?.records?.[group]||[])rows.push({kind,id:r.id,name:r.name||label,amount:r[amount],note:label+' · 只读合同',status:statusLabels[r.status]||r.status,view:'record-detail',metadata:{filters:{record_group:group}}});
    return rows;
  }
  function arrangements(){if(!state)return [];const rows=[],seen=new Set();
    for(const op of state.operations||[]){if(!['unknown','submitting','prepared'].includes(op.state))continue;rows.push({id:op.id,kind:'operation',name:BankUI.actions[op.action]||op.action,date:state.as_of,amount:op.payload?.amount_minor,status:BankUI.operationStatus(op),priority:['unknown','submitting'].includes(op.state)?0:1,view:'operation-detail'});seen.add(op.id);}
    for(const c of state.commitments||[]){const id=c.evidence_id;if(!id||seen.has(id))continue;seen.add(id);const view=c.kind==='scheduled_transfer'?'payment-detail':c.kind==='subscription'||c.kind==='mandate'?'subscription-detail':'calendar';const kind=c.kind==='subscription'?'subscription':c.kind==='mandate'?'mandate':c.kind==='scheduled_transfer'?'scheduled_payment':c.kind;
      const matches=(state.cashflow_timeline?.timeline||[]).flatMap(day=>day.events||[]).filter(e=>e.evidence_id===id&&typeof c.source_table==='string'&&c.source_table.trim()&&e.source_table===c.source_table);
      const certainties=new Set([c.certainty,...matches.map(e=>e.certainty)].filter(Boolean));
      const certainty=certainties.size===1?[...certainties][0]:null;
      const status=!['candidate','confirmed'].includes(certainty)?'安排确定性待核实':c.due_on<state.as_of?(certainty==='candidate'?'候选日期已过，待核实':'逾期待核查'):certainty==='candidate'?'候选安排':'确认安排';
      rows.push({id,kind,name:c.name,date:c.due_on,amount:c.amount_minor,status,priority:c.due_on<state.as_of?2:3,view});}
    for(const order of state.orders||[])if(order.status==='pending_settlement'&&!seen.has(order.id)){seen.add(order.id);rows.push({id:order.id,kind:'order',name:'赎回到账安排',date:order.settlement_date,amount:order.amount_minor,status:'待结算',priority:3,view:'activity'});}
    for(const record of archive?.records?.deposit_contracts||[])if(record.matures_on&&record.matures_on<=state.commitment_window_end&&record.status==='active')rows.push({id:record.id,kind:'deposit',name:record.name||'存款到期',date:record.matures_on,amount:record.principal_minor,status:'合同到期 · 只读',priority:3,view:'record-detail',metadata:{filters:{record_group:'deposit_contracts'}}});
    for(const goal of archive?.records?.wealth_goals||[])if(goal.due_on&&goal.due_on<=state.commitment_window_end&&goal.status==='active')rows.push({id:goal.id,kind:'goal',name:goal.name+' · 目标截止日',date:goal.due_on,status:'目标安排，不是应付金额',priority:4,view:'record-detail',metadata:{filters:{record_group:'wealth_goals'}}});
    return rows.sort((a,b)=>a.priority-b.priority||String(a.date).localeCompare(String(b.date))||a.id.localeCompare(b.id));
  }
  async function loadArchive(){if(archive&&archiveVersion===state?.version)return archive;const epoch=generation,expected=state?.workspace_id,version=state?.version;const result=await BankClient.get('/financial-records');if(epoch!==generation||expected!==state?.workspace_id||version!==state?.version)return null;if(result.workspace_id&&result.workspace_id!==expected)throw Error('档案属于其他账户，已停止显示。');if(result.version!=null&&String(result.version)!==String(version))throw Error('档案版本已变化，请刷新账户。');archive=result;archiveVersion=version;return result;}
  function connectedHomeReferenceValidator(host,epoch){
    const reference={...context},identity=BankClient.identity(),guard=BankClient.guard();
    return envelope=>{
      if(epoch!==generation)return false;
      const currentIdentity=BankClient.identity();
      if(!identity||!state||state.workspace_id!==identity.workspace_id||!guard.valid(envelope)||envelope.workspace_binding!==identity.workspace_id||envelope.subject_id!==identity.account_id||envelope.current_account_id!==identity.account_id||currentIdentity?.workspace_id!==identity.workspace_id||currentIdentity?.account_id!==identity.account_id||currentIdentity?.privacy_revision!==identity.privacy_revision){unavailable(host,'账户身份或数据尚未核对。');return false;}
      if(!envelope.data.accounts.some(account=>account.account_id===reference.object_id)){unavailable(host,'对象不可用，可能已删除或不属于当前账户。');return false;}
      if(reference.object_version!=null&&String(reference.object_version)!==String(envelope.dataset_version)){unavailable(host,'对象版本已变化，请重新查询当前资料。');return false;}
      return true;
    };
  }
  function home(){
    const heading=document.getElementById('accountAssetsHeading')?.parentElement;
    if(heading&&!heading.querySelector('[data-financial-recovery-entry]')){
      const recovery=BankUI.button('验证并恢复操作权限',()=>BankUI.financialRecoveryForm());
      recovery.dataset.financialRecoveryEntry='';heading.append(recovery);
    }
    const epoch=generation,connectedScope=window.NavrenConnectedAssets?.scope;
    const hasReference=context.object_kind!=null||context.object_id!=null||context.object_version!=null;
    const options={state,products,link,ref,getArchive:()=>archive,isCurrent:()=>epoch===generation&&(context.view||'home')==='home'};
    if(hasReference&&connectedScope&&(context.source_scope==null||context.source_scope===connectedScope))options.validateConnectedEnvelope=connectedHomeReferenceValidator(document.getElementById('accountSubview'),epoch);
    return NavrenAccountHome.render(options);
  }
  async function validateHomeReference(host,epoch){
    const source=context.source_scope,connectedScope=window.NavrenConnectedAssets?.scope;
    const hasReference=context.object_kind!=null||context.object_id!=null||context.object_version!=null;
    if(source==='imported'&&!hasReference)return true;
    if(source!=null&&!['simulation','bank_ledger',connectedScope].includes(source)){
      unavailable(host,'对象来源与当前账本不一致，请重新选择来源。');return false;
    }
    if(!hasReference)return true;
    if(context.object_kind!=='account'||!context.object_id){unavailable(host,'对象类型与详情不一致，请重新查询。');return false;}
    const identity=BankClient.identity();
    if(!identity||!state||state.workspace_id!==identity.workspace_id){unavailable(host,'账户身份或数据尚未核对。');return false;}
    if((source==null||source===connectedScope)&&connectedScope){
      const validateEnvelope=connectedHomeReferenceValidator(host,epoch);document.getElementById('accountHome').hidden=true;host.hidden=false;
      try{
        const envelope=await NavrenConnectedAssets.overview({fresh:true});
        if(epoch!==generation)return false;
        if(!validateEnvelope(envelope))return false;
        host.hidden=true;document.getElementById('accountHome').hidden=false;return true;
      }catch(error){if(epoch===generation)unavailable(host,error.message);return false;}
    }
    if(!(state.accounts||[]).some(account=>account.id===context.object_id)){unavailable(host,'对象不可用，可能已删除或不属于当前账户。');return false;}
    if(context.object_version!=null&&String(context.object_version)!==String(objectVersion('account',context.object_id))){unavailable(host,'对象版本已变化，请重新查询当前资料。');return false;}
    return true;
  }
  function bankSecurityActions(host,view,id){
    if(!state||state.workspace_id!==BankClient.identity()?.workspace_id||!['cards','card-detail','funds','account-detail'].includes(view))return;
    const card=view==='card-detail'?(state.cards||[]).find(row=>row.id===id):null;
    const account=(state.accounts||[]).find(row=>row.id===(card?.account_id||id))||state.accounts?.[0];if(!account)return;
    const row=node('div',undefined,'bank-actions');row.append(BankUI.button('银行访问密码',()=>BankUI.passwordChangeForm(account.id)),BankUI.button('验证并恢复操作权限',()=>BankUI.financialRecoveryForm()));
    const cardAccount=card&&(state.accounts||[]).find(current=>current.id===card.account_id);
    if(view==='card-detail'&&card?.status==='active'&&cardAccount){
      const guard=BankClient.guard?.(),workspace=state.workspace_id;
      row.append(BankUI.button('挂失此卡',()=>{
        if(guard&&!guard.valid()||state?.workspace_id!==workspace||BankClient.identity()?.workspace_id!==workspace)return;
        return BankUI.reportCardLost(card);
      }));
    }else if(view==='card-detail'&&(!card||!cardAccount)){
      row.append(node('p','当前卡片尚未与本人可办理的模拟银行账户核对，暂不能办理挂失。','bank-note'));
    }
    if(card?.status==='lost'){const unblock=BankUI.button('解除模拟挂失',()=>BankUI.unblockCard(card));unblock.disabled=!card.expires_on||card.expires_on<=state.as_of;row.append(unblock,node('p','解挂后交易开关仍关闭，开启交易须另行确认。','bank-note'));}
    host.append(row);
  }
  function renderPreviews(){NavrenAccountHome.refreshAssets();}
  function park(){const store=document.getElementById('accountLegacyViews');for(const id of ['bankBudget','holdingsList','bankTransfers','bankOrchestration','bankCards','bankMandates','bankOrders','bankReports']){const n=document.getElementById(id);if(n){n.querySelectorAll('[data-account-object-kind]').forEach(x=>x.hidden=false);store.append(n);}}}
  function legacy(host,id,kind,objectId){const root=document.getElementById(id);if(!root){host.append(node('p','此视图暂不可用。'));return;}if(kind&&!objectId){unavailable(host);return;}if(kind&&objectId){const matches=[...root.querySelectorAll('[data-account-object-kind]')];if(!matches.some(x=>x.dataset.accountObjectKind===kind&&x.dataset.accountObjectId===objectId)){unavailable(host);return;}for(const n of matches)n.hidden=n.dataset.accountObjectKind!==kind||n.dataset.accountObjectId!==objectId;}host.append(root);}
  function unavailable(host,message='对象不可用，可能已删除或不属于当前账户。'){document.getElementById('accountHome').hidden=true;host.hidden=false;host.replaceChildren(node('p',message,'account-empty'),link('返回我的账户','home'));}
  function referenceButton(host,reference,label='引用此对象提问'){const kind=reference.object_kind,id=reference.object_id,names={account:'现金账户',card:'银行卡',holding:'持仓',plan:'生活计划',operation:'操作',loan:'贷款',deposit:'存款',subscription:'商户订阅',mandate:'银行代扣',goal:'目标',support:'服务记录',permission:'账户授权'};const version=kind==='operation'?(state.operations||[]).find(x=>x.id===id)?.version:kind==='goal'?undefined:reference.object_version;const b=BankUI.button(label,()=>NavrenViews.reference({kind,id,source_scope:reference.source_scope,version,label:(state.accounts||[]).find(x=>x.id===id)?.name||names[kind]||'账户对象'}));host.append(b);}
  function accountDetail(host,id,epoch){const a=(state.accounts||[]).find(x=>x.id===id);if(!a){unavailable(host);return;}host.append(node('h2',a.name),node('p','已结算现金 '+money(a.balance_minor)+' · '+(a.currency||'CNY')),link('从此账户转账','payments',{...ref('account',a.id)}));referenceButton(host,ref('account',a.id));const budget=node('section',undefined,'account-detail-block');budget.append(node('h3','此账户可安排资金'),node('p','正在核对账户规则…'));host.append(budget);const guard=BankClient.guard?.(),current=()=>epoch===generation&&budget.isConnected&&(!guard||guard.valid());BankClient.get('/budget?'+new URLSearchParams({account_id:a.id})).then(r=>{if(!current()||guard&&!guard.valid(r))return;if(r.account_id!=null&&r.account_id!==a.id)throw Error('账户资料与预算不一致，请重新查询。');if((r.currency??'CNY')!==(a.currency||'CNY'))throw Error('预算币种无法与账户资料核对，请重新查询。');if(String(r.version)!==String(state.version))throw Error('账户版本已变化，请重新读取后再核对可安排资金。');budget.replaceChildren(node('h3','此账户可安排资金'),node('strong',money(r.investable_minor)),node('p','现金 '+money(r.cash_minor)+' − 应急保留 '+money(r.reserve_minor)+' − 已知到期 '+money(r.upcoming_due_minor)+' − 已生效预留 '+money(r.held_minor)),node('p','参考日期 '+r.as_of+' · 账本版本 '+r.version));}).catch(e=>{if(current())BankUI.showError(budget,e,()=>render(context));});const cards=(state.cards||[]).filter(x=>x.account_id===id);if(cards.length){host.append(node('h3','关联卡片'));cards.forEach(c=>host.append(objectRow({kind:'card',id:c.id,name:c.network+' '+c.last_four,note:'关联卡片',view:'card-detail',status:statusLabels[c.status]||c.status})));}host.append(link('查看此账户流水','statements',{source_scope:'simulation',object_kind:'account',object_id:id,filters:{account_id:id}}));}
  function archiveList(host){const labels={loans:'贷款',deposit_contracts:'存款',wealth_goals:'目标',support_cases:'服务记录',permission_grants:'账户授权'};for(const [group,title]of Object.entries(labels)){const section=node('section',undefined,'account-detail-block');section.append(node('h2',title));for(const r of archive?.records?.[group]||[])section.append(objectRow({kind:group==='loans'?'loan':group==='deposit_contracts'?'deposit':group==='wealth_goals'?'goal':group==='support_cases'?'support':'permission',id:r.id,name:r.name||r.topic||r.scope||title,amount:r.outstanding_minor??r.principal_minor??r.target_minor,view:group==='wealth_goals'?'record-detail':'record-detail',note:'只读档案',status:statusLabels[r.status]||r.status,metadata:{filters:{record_group:group}}}));if(!(archive?.records?.[group]||[]).length)section.append(node('p','尚未取得此类记录。'));host.append(section);}host.append(node('h2','保障资料'),node('p',archive?.insurance?.notice||'保障数据未接入，不能推断没有保障。'));}
  function recordDetail(host,id,kind){const groups={loan:'loans',deposit:'deposit_contracts',goal:'wealth_goals',support:'support_cases',permission:'permission_grants'},group=context.filters?.record_group||groups[kind];if(!groups[kind]&& !Object.values(groups).includes(group)){unavailable(host);return;}const record=(archive?.records?.[group]||[]).find(r=>r.id===id);if(!record){unavailable(host);return;}host.append(node('h2',record.name||record.topic||record.scope||'档案记录'),node('p','演示账户 · 参考日期 '+archive.as_of+' · 当前接口只读'));const fields={status:'状态',original_principal_minor:'原始本金',outstanding_minor:'未偿本金',annual_rate_bps:'年利率（基点）',matures_on:'合同到期日',principal_minor:'本金',started_on:'开始日期',due_on:'下一安排',interest_bps:'利率（基点）',target_minor:'目标金额',saved_minor:'已保存金额',resolution:'处理结果',expires_on:'到期日',scope:'授权范围',permission:'权限'};const dl=node('dl',undefined,'account-definition-list');for(const [key,label]of Object.entries(fields))if(record[key]!=null)dl.append(node('dt',label),node('dd',key.endsWith('_minor')?money(record[key]):statusLabels[record[key]]||String(record[key])));host.append(dl);if(group==='wealth_goals'){const a=node('a','管理这个目标 ›');a.href='mine.html?'+new URLSearchParams({view:'goal',object_kind:'goal',object_id:id,source_scope:'simulation'});host.append(a);}referenceButton(host,ref(kind,id));}
  function operationDetail(host,id){const op=(state.operations||[]).find(x=>x.id===id);if(!op){unavailable(host);return;}host.append(node('h2',BankUI.actions[op.action]||op.action),node('p',BankUI.operationStatus(op)),node('p','操作编号 '+op.id));if(op.payload?.amount_minor!=null)host.append(node('p','金额 '+money(op.payload.amount_minor)));const b=BankUI.button('核对同一操作与回执',()=>BankUI.openDraft({operation_id:op.id},b,{expected_operation_version:op.version}));b.dataset.operationId=op.id;host.append(b,node('p','关闭确认界面不会取消或重新提交操作；状态待核实时按此编号查询。'));referenceButton(host,ref('operation',id));}
  function paymentDetail(host,id,kind){if(kind==='scheduled_payment'){const payment=(state.scheduled_payments||[]).find(r=>r.id===id);if(!payment){unavailable(host);return;}const beneficiary=(state.beneficiaries||[]).find(r=>r.id===payment.beneficiary_id);host.append(node('h2','定时转账 · '+(beneficiary?.display_name||'收款人资料未取得')),node('p',money(payment.amount_minor)+' · '+(payment.frequency==='monthly'?'每月':'单次')),node('p','下次执行 '+payment.next_due_on+' · '+(statusLabels[payment.status]||payment.status)),node('p','按日期推进执行；受理不代表已扣款，成功和失败回执可在活动中核对。'));if(payment.status==='active')host.append(BankUI.button('取消这项定时转账',()=>BankUI.openDraft({action:'cancel_scheduled_payment',account_id:payment.account_id,scheduled_payment_id:payment.id,idempotency_key:BankClient.key()})));referenceButton(host,ref(kind,id));host.append(link('查看全部活动','activity'));return;}unavailable(host);}
  function activityExtras(host){for(const [key,title,fields]of [['card_applications','卡片申请',['requested_on','status']],['fund_holds','资金预留',['expires_on','status']],['service_reservations','预约与履约',['delivery_on','status']]]){const rows=state[key]||[];if(rows.length){const p=BankUI.panel(title);p.append(BankUI.table(['编号','金额','日期','状态'],rows.map(r=>[r.id,money(r.amount_minor??r.requested_limit_minor),r[fields[0]],statusLabels[r[fields[1]]]||r[fields[1]]])));host.append(p);}}}
  async function render(next){context=next||{view:'home'};let view=context.view||'home';if(context.object_id){const detailViews={activity:'operation-detail',cards:'card-detail',holdings:'holding-detail',plans:'plan-detail',payments:'payment-detail'};const kinds={activity:'operation',cards:'card',holdings:'holding',plans:'plan',payments:'scheduled_payment'};if(kinds[view]&&context.object_kind===kinds[view])view=detailViews[view];}const epoch=++generation,host=document.getElementById('accountSubview'),homeRoot=document.getElementById('accountHome');if(!host)return;if(calendarHost){CalendarUI.destroyAccount?.(calendarHost);calendarHost=null;}park();host.replaceChildren();homeRoot.hidden=view!=='home';host.hidden=view==='home';document.getElementById('accountViewTitle').textContent=titles[view]||'对象不可用';document.querySelector('.header-location').textContent=titles[view]||'我的账户';const back=document.getElementById('accountBack');back.hidden=view==='home';back.parentElement.hidden=back.hidden;back.onclick=e=>{e.preventDefault();NavrenViews.back();};
    const historyViews=['home','funds','account-detail','cards','card-detail','holdings','holding-detail','archive','record-detail','statements','calendar','calendar-day'];
    const currentAssetViews=['home','funds','securities','account-detail','cards','card-detail','holdings','holding-detail','statements','payment-platform'];
    if(BankUI.bankRecoveryWorkspace()){homeRoot.hidden=true;host.hidden=false;host.append(node('p','银行访问会话已失效。','account-empty'),BankUI.button('验证银行访问密码',()=>BankUI.passwordSessionForm()));return;}
    if(view==='home'){const valid=await validateHomeReference(host,epoch);if(!valid||epoch!==generation)return;}
    if(context.source_scope===window.NavrenConnectedAssets?.scope||context.source_scope==null&&currentAssetViews.includes(view)){
      if(view==='home'){await home();return;}
      await NavrenConnectedAssetsUI.render(host,{...context,view},()=>epoch===generation);if(epoch===generation)bankSecurityActions(host,view,context.object_id);return;
    }
    const historical=historyViews.includes(view)&&context.source_scope!=='imported'&&context.source_scope!=='simulation';
    if(historical){
      if(view==='home'){await home();return;}
      const expectedKinds={'account-detail':'account','card-detail':'card','holding-detail':'holding'};
      if(context.object_kind&&expectedKinds[view]&&context.object_kind!==expectedKinds[view]){unavailable(host,'对象类型与详情不一致。');return;}
      await NavrenFinancialUI.render(host,{...context,view},()=>epoch===generation);return;
    }
    if(context.source_scope===NavrenFinancialData.scope){unavailable(host,'独立历史对象只支持查看，不能用于当前账户操作。');return;}
    if(context.object_kind==='account'&&context.object_id&&!(state?.accounts||[]).some(account=>account.id===context.object_id)){unavailable(host,'当前操作账户未取得此编号，不能使用历史账户发起操作。');return;}
    if(!state){homeRoot.hidden=true;host.hidden=false;host.append(node('p','账户身份或数据尚未核对。','account-empty'),BankUI.button('重新读取账户',()=>BankUI.load().catch(()=>{})));return;}
    const detailKinds={'account-detail':['account'],'card-detail':['card'],'holding-detail':['holding'],'plan-detail':['plan','task'],'subscription-detail':['subscription','mandate'],'operation-detail':['operation'],'payment-detail':['scheduled_payment','split_bill']};
    if(context.object_kind&&detailKinds[view]&&!detailKinds[view].includes(context.object_kind)){unavailable(host,'对象类型与详情不一致，请重新查询。');return;}
    const ledgerKind=context.object_kind||detailKinds[view]?.[0];
    if(context.object_id&&['account','card','holding','plan','task','operation','loan','deposit','subscription','mandate','goal','support','permission','scheduled_payment','split_bill'].includes(ledgerKind)){
      const scopes=ledgerKind==='account'?['simulation','bank_ledger']:ledgerKind==='operation'?['simulation','bank_operation']:['simulation'];
      if(context.source_scope!=null&&!scopes.includes(context.source_scope)){unavailable(host,'对象来源与当前账本不一致，请重新选择来源。');return;}
      const actualVersion=objectVersion(ledgerKind,context.object_id);
      if(context.object_version!=null&&(actualVersion==null||String(context.object_version)!==String(actualVersion))){unavailable(host,'对象版本已变化，请重新查询当前资料。');return;}
    }
    if(view==='home'&&context.source_scope==='imported'){homeRoot.hidden=true;host.hidden=false;host.append(node('p','导入账单只提供文件覆盖期间的收支，不能用来推算银行余额。'),link('查看导入账单','statements',{source_scope:'imported'}));return;}
    if(view==='home'){home();loadArchive().then(r=>{if(r&&epoch===generation&&context.view==='home')renderPreviews();}).catch(()=>{});return;}
    if(view==='funds'){host.append(node('div',undefined,'account-object-list'));host.firstChild.append(...allFunds().map(objectRow));host.append(link('银行卡与申请','cards'),link('当前持仓与批次','holdings'),link('存款、贷款及其他档案','archive'),link('订阅与代扣','subscriptions'),link('生活计划','plans'));legacy(host,'bankBudget');loadArchive().then(r=>{if(r&&epoch===generation){host.firstChild.replaceChildren(...allFunds().map(objectRow));}}).catch(e=>{if(epoch===generation)host.append(node('p','贷款与存款资料暂未取得：'+e.message));});return;}
    if(view==='account-detail'){accountDetail(host,context.object_id,epoch);bankSecurityActions(host,view,context.object_id);return;}
    if(view==='calendar'){calendarHost=node('div',undefined,'account-calendar-workspace');host.append(calendarHost);try{await CalendarUI.account(calendarHost,context);}catch(e){if(epoch===generation)BankUI.showError(calendarHost,e,()=>render(context));}return;}
    if(view==='calendar-day'){await CalendarUI.renderDay(host,context);return;}
    if(view==='calendar-item'){await CalendarUI.renderItem(host,context.object_id,context);return;}
    if(view==='wealth-product'){await CalendarUI.renderProduct(host,context.object_id,context.date,context.portfolio_id);return;}
    if(view==='statements'){legacy(host,'bankReports');await renderReports();return;}
    if(view==='archive'||view==='record-detail'){host.append(node('p','正在核对档案…'));try{const r=await loadArchive();if(!r||epoch!==generation)return;host.replaceChildren();if(view==='archive')archiveList(host);else recordDetail(host,context.object_id,context.object_kind);}catch(e){if(epoch===generation)BankUI.showError(host,e,()=>render(context));}return;}
    if(view==='operation-detail'){operationDetail(host,context.object_id);return;}
    if(view==='payment-detail'){paymentDetail(host,context.object_id,context.object_kind);return;}
    const map={payments:'bankTransfers',cards:'bankCards','card-detail':'bankCards',subscriptions:'bankMandates','subscription-detail':'bankMandates',holdings:'holdingsList','holding-detail':'holdingsList',plans:'bankOrchestration','plan-detail':'bankOrchestration',activity:'bankOrders'};if(!map[view]){unavailable(host);return;}
    if(view==='cards'){host.append(BankUI.button('申请卡片',()=>BankUI.cardApplicationForm(state.accounts[0]?.id),true));for(const card of state.cards||[])host.append(objectRow({kind:'card',id:card.id,name:(card.type==='virtual'?'SIM 虚拟卡':card.type==='credit'?'信用卡':'借记卡')+' · '+card.network+' '+card.last_four,note:card.type==='credit'?'当前未还':'关联现金账户',amount:card.type==='credit'?card.outstanding_minor:null,status:statusLabels[card.status]||card.status,view:'card-detail'}));if(!(state.cards||[]).length)host.append(node('p','当前没有卡片，可以提交申请。'));if((state.card_applications||[]).length){const section=BankUI.panel('申请记录');section.append(BankUI.table(['申请编号','类型','状态','日期','说明'],state.card_applications.map(r=>[r.id,r.requested_type==='virtual'?'SIM 虚拟卡':r.requested_type==='credit'?'信用卡':'借记卡',statusLabels[r.status]||r.status,r.requested_on,r.decision_reason])));host.append(section);}return;}
    if(view==='holdings'){const holdings=allFunds().filter(r=>r.kind==='holding');host.append(...holdings.map(objectRow));if(!holdings.length)host.append(node('p','当前没有持仓。'));host.append(link('查看历史组合与真实净值','calendar',{source_scope:'historical',portfolio_id:'historical',filters:{kind:'returns'}}));return;}
    if(view==='plans'){for(const task of state.orchestration_tasks||[])host.append(objectRow({kind:'plan',id:task.id,name:task.title,note:'计划日期 '+task.due_on,amount:task.budget_minor,status:statusLabels[task.status]||task.status,view:'plan-detail'}));if(!(state.orchestration_tasks||[]).length)host.append(node('p','当前没有已建立的生活计划。'));return;}
    let kind=view==='card-detail'?'card':view==='holding-detail'?'holding':view==='plan-detail'?'plan':view==='subscription-detail'?'mandate':null,id=context.object_id;if(view==='subscription-detail'&&context.object_kind==='subscription')id=(state.subscriptions||[]).find(x=>x.id===id)?.mandate_id;
    legacy(host,map[view],kind,id);if(view==='payments'&&context.object_kind==='account'&&context.object_id){host.querySelectorAll('select[aria-label="付款账户"]').forEach(select=>{if([...select.options].some(o=>o.value===context.object_id))select.value=context.object_id;});}if(view==='activity')activityExtras(host);if(kind)referenceButton(host,ref(context.object_kind||kind,context.object_id));
  }
  window.addEventListener('navren:account-rendered',e=>{if(e.detail.state.workspace_id!==BankClient.identity()?.workspace_id)return;state=e.detail.state;products=e.detail.products||[];archive=null;archiveVersion=null;if(!document.querySelector('dialog[open]:not(.bank-dialog[data-operation-state="succeeded"]),.navren-month-popup,.nv-control-popup,.cd-choice-menu:not([hidden]),.ah-payer-menu:not([hidden])')){const next=window.NavrenViews?.current?.()||context;if((next.view||'home')==='home'&&(context.view||'home')==='home'&&!document.getElementById('accountHome').hidden&&[next,context].every(meta=>meta.object_kind==null&&meta.object_id==null&&meta.object_version==null&&meta.source_scope==null)){home().catch(()=>{});return;}render(next).catch(()=>{});}});
  window.addEventListener('bank:unavailable',()=>{state=null;render(window.NavrenViews?.current?.()||context).catch(()=>{});});
  window.addEventListener('navren:bank-session-revoked',()=>{state=null;archive=null;archiveVersion=null;generation++;NavrenAccountHome.clear?.();NavrenConnectedAssets.clear?.();render(window.NavrenViews?.current?.()||context).catch(()=>{});});
  window.addEventListener('navren:privacy-reset',()=>{state=null;archive=null;archiveVersion=null;generation++;for(const id of ['assetOverview','accountFundsPreview','accountUpcomingPreview'])document.getElementById(id)?.replaceChildren();document.getElementById('accountHome').hidden=true;document.getElementById('accountSubview')?.replaceChildren(node('p','私人视图已失效，请重新核对账户。'));});
  document.addEventListener('DOMContentLoaded',()=>{document.querySelectorAll('[data-account-view]').forEach(a=>a.onclick=e=>{e.preventDefault();NavrenViews.open(a.dataset.accountView);});state=BankUI.state();products=BankUI.products()||[];});
  NavrenViews.register('account',render);
  return {render,allFunds,arrangements};
})();
