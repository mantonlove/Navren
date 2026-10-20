/* 普通收款人通讯录。保存资料只填入汇款表单，不创建或确认付款。 */
window.NavrenPaymentContacts = (() => {
  const records = new Map();
  const fields = ['recipient_name','recipient_account','recipient_bank','recipient_branch','alias'];
  const node = (tag,text,cls) => {const el=document.createElement(tag);if(text!=null)el.textContent=String(text);if(cls)el.className=cls;return el;};
  const key = value => value&&typeof value.workspace_id==='string'&&Number.isInteger(value.privacy_revision)?JSON.stringify([value.workspace_id,value.account_id,value.privacy_revision]):null;
  function live(record) {return !record.disposed&&records.get(record.host)===record&&record.host.isConnected&&!record.host.closest('[hidden]')&&record.options.isCurrent?.()!==false&&record.key===key(BankClient.identity())&&record.guard?.valid();}
  function contact(value) {
    if(!value||!/^[a-f0-9]{32}$/.test(value.id)||fields.some(name=>typeof value[name]!=='string'||/[\x00-\x1f\x7f]/.test(value[name]))||!value.recipient_name.trim()||!value.recipient_bank.trim()||!/^\d{10,32}$/.test(value.recipient_account)||/^0+$/.test(value.recipient_account)||typeof value.masked_account!=='string')throw Error('联系人资料无法核对。');
    return {...value};
  }
  async function request(record,path,body) {
    if(!live(record))throw Error('当前账户或页面已变化。');
    const guard=BankClient.guard(),epoch=record.epoch;
    const value=await (body===undefined?BankClient.get(path):BankClient.post(path,body));
    if(epoch!==record.epoch||!live(record)||!guard.valid(value)||value.environment!=='simulation'||value.source!=='user_entered')throw Error('联系人响应已失效，请重新读取。');
    return value;
  }
  function button(record,text,action,primary=false) {const el=node('button',text,'pc-button'+(primary?' is-primary':''));el.type='button';el.onclick=()=>{if(live(record)&&!record.busy)action();};return el;}
  function notice(record,text,error=false) {record.notice=text;record.error=error;if(record.status){record.status.textContent=text;record.status.className=error?'pc-error':'pc-status';record.status.setAttribute('role',error?'alert':'status');}}
  async function load(record) {
    const version=++record.load;
    try {const data=await request(record,'/remittance-contacts');if(!live(record)||version!==record.load)return;
      if(!Array.isArray(data.contacts))throw Error('联系人列表尚未取得。');
      const seen=new Set();record.contacts=data.contacts.map(value=>{const item=contact(value);if(seen.has(item.id))throw Error('联系人资料重复。');seen.add(item.id);return item;});
      record.loaded=true;notice(record,'');draw(record);
    }catch(error){if(live(record)&&version===record.load){notice(record,'联系人暂未读取，请重试。',true);draw(record);}}
  }
  function form(record,parent) {
    const draft=record.draft,el=node('form',null,'pc-form'),title=node('h3',draft.id?'编辑联系人':'新增联系人');el.append(title);
    const grid=node('div',null,'pc-form-grid'),inputs={};
    for(const [name,label,required,max]of [['recipient_name','姓名',true,64],['recipient_account','银行卡 / 收款账号',true,32],['recipient_bank','开户银行',true,100],['recipient_branch','开户支行（选填）',false,100],['alias','别名（选填）',false,60]]){
      const wrap=node('label',null,'pc-field'),input=node('input');input.name=name;input.value=draft[name]||'';input.required=required;input.maxLength=max;input.autocomplete='off';input.setAttribute('aria-label',label);if(name==='recipient_account'){input.inputMode='numeric';input.pattern='[0-9]{10,32}';}
      wrap.append(node('span',label),input);grid.append(wrap);inputs[name]=input;input.oninput=()=>{if(!live(record))return;draft[name]=input.value;input.setCustomValidity('');};
    }
    const error=node('p','','pc-error');error.setAttribute('role','alert');const actions=node('div',null,'pc-actions'),save=node('button','保存联系人','pc-button is-primary');save.type='submit';actions.append(button(record,'取消',()=>{record.draft=null;draw(record);}),save);el.append(grid,error,actions);parent.append(el);
    el.onsubmit=async event=>{
      event.preventDefault();if(!live(record)||record.busy)return;
      const body=Object.fromEntries(fields.map(name=>[name,inputs[name].value.trim()]));
      if(!/^\d{10,32}$/.test(body.recipient_account)||/^0+$/.test(body.recipient_account)){inputs.recipient_account.setCustomValidity('请填写10至32位有效数字账号。');}
      for(const name of ['recipient_name','recipient_bank'])if(!body[name])inputs[name].setCustomValidity('请填写'+(name==='recipient_name'?'姓名':'开户银行')+'。');
      if(!el.reportValidity())return;record.busy=true;save.disabled=true;error.textContent='';
      try{const result=await request(record,draft.id?'/remittance-contacts/'+draft.id+'/update':'/remittance-contacts',body),saved=contact(result.contact);
        if(draft.id&&saved.id!==draft.id||fields.some(name=>saved[name]!==body[name]))throw Error('保存结果与填写的联系人不一致。');
        record.draft=null;await load(record);if(live(record))notice(record,'联系人已保存。');
      }catch(failure){if(live(record))error.textContent='保存失败：'+failure.message;}
      finally{record.busy=false;if(live(record)&&save.isConnected)save.disabled=false;}
    };
    requestAnimationFrame(()=>{if(live(record)&&el.isConnected)inputs.recipient_name.focus({preventScroll:true});});
  }
  function drawBook(record) {
    const toolbar=node('div',null,'pc-toolbar'),search=node('input');search.type='search';search.placeholder='搜索姓名、银行或账号尾号';search.setAttribute('aria-label','搜索联系人');search.value=record.search;const add=button(record,'新增联系人',()=>{record.draft=Object.fromEntries(fields.map(name=>[name,'']));record.deleting=null;draw(record);},true);add.disabled=!record.loaded;toolbar.append(search,add,button(record,'刷新',()=>load(record)));record.host.append(toolbar);
    if(record.draft)form(record,record.host);
    const card=node('section',null,'pc-book'),heading=node('div',null,'pc-book-heading'),count=node('span',(record.loaded?record.contacts.length:0)+' 位联系人');heading.append(node('h3','联系人'),count);card.append(heading);record.host.append(card);
    const list=node('div',null,'pc-list');card.append(list);
    function paintList() {
      list.replaceChildren();const query=record.search.trim().toLocaleLowerCase(),items=record.contacts.filter(item=>[item.recipient_name,item.alias,item.recipient_bank,item.recipient_account.slice(-4)].join(' ').toLocaleLowerCase().includes(query));
      if(!items.length){list.append(node('p',record.loaded?(query?'没有匹配的联系人':'暂无联系人，点击“新增联系人”添加。'):'正在读取联系人…','pc-empty'));return;}
      for(const item of items){const row=node('article',null,'pc-person'),avatar=node('span',item.recipient_name.slice(0,1),'pc-avatar'),identity=node('div',null,'pc-person-copy'),actions=node('div',null,'pc-row-actions');identity.append(node('strong',item.alias||item.recipient_name),node('span',(item.alias?item.recipient_name+' · ':'')+item.recipient_bank),node('small',item.masked_account));actions.append(button(record,'编辑',()=>{record.draft={...item};record.deleting=null;draw(record);}),button(record,'删除',()=>{record.deleting=item.id;draw(record);}));row.append(avatar,identity,actions);
        if(record.deleting===item.id){const confirm=node('div',null,'pc-delete-confirm');confirm.append(node('span','删除这个联系人？'),button(record,'取消',()=>{record.deleting=null;draw(record);}),button(record,'确认删除',async()=>{record.busy=true;try{const result=await request(record,'/remittance-contacts/'+item.id+'/delete',{});if(result.deleted!==true||result.id!==item.id)throw Error('删除结果无法核对。');record.deleting=null;await load(record);if(live(record))notice(record,'联系人已删除。');}catch(error){if(live(record))notice(record,error.message,true);}finally{record.busy=false;} }));row.append(confirm);}
        list.append(row);
      }
    }
    search.oninput=()=>{if(live(record)){record.search=search.value;paintList();}};paintList();
  }
  function drawPicker(record) {
    const field=node('div',null,'pc-picker'),label=node('label','已保存的收款人'),select=node('select');select.id='remittance-contact';select.setAttribute('aria-label','已保存的收款人');label.htmlFor=select.id;
    select.append(new Option('手动填写收款信息',''));for(const item of record.contacts)select.append(new Option((item.alias||item.recipient_name)+' · '+item.recipient_bank+' · 尾号 '+item.recipient_account.slice(-4),item.id));select.value=record.selected||'';select.disabled=!record.loaded||!record.contacts.length;
    select.onchange=()=>{if(!live(record))return;if(record.options.canPick?.()===false){select.value=record.selected||'';window.NavrenFormControls?.refresh?.(select);return;}record.selected=select.value;record.options.onPick?.(record.contacts.find(item=>item.id===select.value)||null);};
    const link=node('a','管理联系人','pc-manage');const url=new URL('mine.html',location.href);url.search='';url.searchParams.set('view','payment-contacts');const mode=new URL(location.href).searchParams.get('mode');if(mode)url.searchParams.set('mode',mode);url.hash='payment-contacts';link.href=url.pathname+url.search+url.hash;
    field.append(label,link,select);record.host.append(field);if(record.error)record.host.append(button(record,'重试读取联系人',()=>load(record)));
  }
  function draw(record) {if(!live(record))return;record.host.classList.add('pc-root',record.type==='book'?'pc-workspace':'pc-remittance');record.host.replaceChildren();record.type==='book'?drawBook(record):drawPicker(record);record.status=node('p',record.notice||'',record.error?'pc-error':'pc-status');record.status.setAttribute('role',record.error?'alert':'status');record.host.append(record.status);}
  function dispose(record) {if(record.disposed)return;record.disposed=true;record.epoch++;record.contacts=[];record.draft=null;record.guard=null;if(records.get(record.host)===record){record.host.replaceChildren();records.delete(record.host);}}
  function mount(host,options={},type='book') {
    if(!host)return null;const previous=records.get(host);if(previous)dispose(previous);
    const record={host,options,type,disposed:false,key:null,guard:null,epoch:0,load:0,loaded:false,contacts:[],draft:null,search:'',selected:'',deleting:null,busy:false,notice:'',error:false};records.set(host,record);
    const ready=(async()=>{try{await BankClient.refreshIdentity();if(record.disposed||records.get(host)!==record||options.isCurrent?.()===false)return;record.key=key(BankClient.identity());record.guard=BankClient.guard();if(!record.key||!live(record))throw Error('账户身份尚未取得。');draw(record);await load(record);}catch(error){if(!record.disposed&&records.get(host)===record){host.replaceChildren(node('p','联系人暂不可用，请重新进入页面。','pc-error'));}}})();
    return {ready,destroy:()=>dispose(record),reset(){if(!live(record))return;record.selected='';const control=host.querySelector('select');if(control){control.value='';window.NavrenFormControls?.refresh?.(control);}}};
  }
  function clear() {for(const record of [...records.values()])dispose(record);}
  window.addEventListener('navren:privacy-reset',clear);window.addEventListener('navren:identity-revoked',clear);window.addEventListener('pagehide',clear);
  return Object.freeze({mount:(host,options)=>mount(host,options,'book'),mountPicker:(host,options)=>mount(host,options,'picker'),clear});
})();
