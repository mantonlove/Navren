/* 普通收款联系人真实组件检查；jsdom 与有限假 BankClient，无现役数据库或资金操作。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{JSDOM}=require('jsdom');
const ROOT=path.resolve(__dirname,'../..'),OUT=path.join(ROOT,'outputs/contact-controls-20261010/contacts/validation.json');
const INPUTS=['js/payment_contacts.js','js/form_controls.js'];
const clone=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value));
const tick=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setTimeout(resolve,0));};
const deferred=()=>{let resolve;const promise=new Promise(yes=>{resolve=yes;});return {promise,resolve};};
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(path.join(ROOT,file))).digest('hex');
const sample={recipient_name:'测试收款人',recipient_account:'06222021000000000123',recipient_bank:'测试开户银行',recipient_branch:'',alias:''};
let assertions=0,totalRequests=0;
const equal=(actual,expected,message)=>{assertions++;assert.deepEqual(actual,expected,message);};
const check=(value,message)=>{assertions++;assert.ok(value,message);};

function harness(options={}) {
  const dom=new JSDOM('<!doctype html><main><div id="book"></div><form id="remittance"><div id="picker"></div><input name="recipient_name"><input name="recipient_account"><input name="recipient_bank"><input name="recipient_branch"></form></main>',
    {url:'http://isolated-contacts.invalid/account.html?mode=smart',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window;
  let workspace='workspace-a',privacy=1,generation=0,serial=0,current=true;
  const requests=[],stores=new Map([['workspace-a',[]],['workspace-b',[]]]),gates=new Map(),overrides=new Map(),errors=[];
  const observers=[],NativeMutationObserver=w.MutationObserver;
  w.MutationObserver=class extends NativeMutationObserver {constructor(callback){super(callback);observers.push(this);}};
  const identity=()=>({workspace_id:workspace,account_id:'persona-'+workspace.slice(-1),privacy_revision:privacy,environment:'simulation'});
  const binding=()=>({workspace_id:workspace,privacy_revision:privacy,environment:'simulation',source:'user_entered'});
  function saved(body) {const value={id:String(++serial).padStart(32,'0'),...clone(body),created_at:1791590400,updated_at:1791590400};value.masked_account='*'.repeat(value.recipient_account.length-4)+value.recipient_account.slice(-4);return value;}
  function guard() {const captured=JSON.stringify(identity()),epoch=generation;return {valid(value){return epoch===generation&&captured===JSON.stringify(identity())&&(!value||value.workspace_id===workspace&&value.privacy_revision===privacy);}};}
  async function request(method,url,body) {
    const owner=workspace,envelope=binding(),trace={method,url,body:clone(body),workspace_id:owner};requests.push(trace);totalRequests++;
    check(url==='/remittance-contacts'||/^\/remittance-contacts\/[a-f0-9]{32}\/(update|delete)$/.test(url),'有限假服务拒绝AA及金融API');
    const route=method+' '+url,gate=gates.get(route);gates.delete(route);let value;
    if(overrides.has(route)){const override=overrides.get(route);overrides.delete(route);value=typeof override==='function'?override(body,envelope):override;}
    else if(method==='GET'){equal(url,'/remittance-contacts','列表只读取新API');value={...envelope,contacts:clone(stores.get(owner))};}
    else {
      const list=stores.get(owner),match=/^\/remittance-contacts\/([a-f0-9]{32})\/(update|delete)$/.exec(url);
      if(!match){equal(Object.keys(body).sort(),Object.keys(sample).sort(),'创建只发送手填联系人字段');const item=saved(body);list.push(item);value={...envelope,contact:clone(item)};}
      else {const item=list.find(item=>item.id===match[1]);check(item,'修改限定发请求时的当前工作区');
        if(match[2]==='delete'){equal(clone(body),{},'删除不传付款授权');stores.set(owner,list.filter(row=>row.id!==item.id));value={...envelope,deleted:true,id:item.id};}
        else {equal(Object.keys(body).sort(),Object.keys(sample).sort(),'编辑只发送资料字段');Object.assign(item,clone(body),{updated_at:1791590410,masked_account:'*'.repeat(body.recipient_account.length-4)+body.recipient_account.slice(-4)});value={...envelope,contact:clone(item)};}
      }
    }
    if(gate)await gate.promise;return clone(value);
  }
  w.BankClient={identity,guard,refreshIdentity:async()=>clone(identity()),get:url=>request('GET',url),post:(url,body)=>request('POST',url,body)};
  w.addEventListener('error',event=>{errors.push(event.error?.message||event.message);event.preventDefault();});
  if(options.controls!==false)w.eval(fs.readFileSync(path.join(ROOT,'js/form_controls.js'),'utf8'));
  w.eval(fs.readFileSync(path.join(ROOT,'js/payment_contacts.js'),'utf8'));
  const book=()=>w.document.querySelector('#book'),picker=()=>w.document.querySelector('#picker');
  function button(text,root=book()){const value=[...root.querySelectorAll('button')].find(item=>item.textContent.trim()===text);check(value,'存在按钮 '+text);return value;}
  function type(name,value,root=book()){const input=root.querySelector('[name="'+name+'"]');check(input,'存在手填字段 '+name);input.value=value;input.dispatchEvent(new w.Event('input',{bubbles:true}));return input;}
  async function mountBook(){const controller=w.NavrenPaymentContacts.mount(book(),{isCurrent:()=>current});await controller.ready;await tick();return controller;}
  async function begin(body=sample){button('新增联系人').click();await tick();for(const [name,value]of Object.entries(body))type(name,value);return book().querySelector('form');}
  async function submit(form=book().querySelector('form')){check(form,'存在联系人表单');form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();}
  function seed(body=sample,owner=workspace){const item=saved(body);stores.get(owner).push(item);return clone(item);}
  function switchIdentity(owner='workspace-b',event='navren:privacy-reset'){workspace=owner;privacy++;generation++;w.dispatchEvent(new w.Event(event));}
  const posts=()=>requests.filter(row=>row.method==='POST');
  return {w,requests,stores,errors,book,picker,button,type,mountBook,begin,submit,seed,posts,
    hold(method,url){const gate=deferred();gates.set(method+' '+url,gate);return gate;},
    override(method,url,value){overrides.set(method+' '+url,value);},switchIdentity,
    setCurrent(value){current=value;},async close(){for(const observer of observers)observer.disconnect();w.NavrenFormControls?.close();w.NavrenPaymentContacts.clear();await tick();dom.window.close();}};
}

async function main() {
  const started=new Date().toISOString(),finalized=process.env.CONTACTS_FINAL_FREEZE==='1',before=finalized?Object.fromEntries(INPUTS.map(file=>[file,sha(file)])):null,results=[];
  async function scenario(name,fn){const h=harness();try{await fn(h);check(h.requests.every(row=>row.url==='/remittance-contacts'||/^\/remittance-contacts\/[a-f0-9]{32}\/(update|delete)$/.test(row.url)),'全部请求均为普通联系人API，AA与付款API请求为零');equal(h.errors,[],'无DOM异常');results.push({name,status:'passed'});}catch(error){results.push({name,status:'failed',error:error.stack});}finally{await h.close();}}

  await scenario('新建手填联系人只POST新API，FormData保留完整账号与可空选填项',async h=>{
    await h.mountBook();check(h.book().textContent.includes('暂无联系人'),'空目录正常显示');
    const form=await h.begin();equal(Object.fromEntries(new h.w.FormData(form)),sample,'原生FormData与手填资料一致');
    equal([...form.querySelectorAll('select')].length,0,'新联系人不要求绑定体验用户');await h.submit(form);
    equal(h.posts(),[{method:'POST',url:'/remittance-contacts',body:sample,workspace_id:'workspace-a'}],'保存只POST联系人API');
    equal(h.stores.get('workspace-a').length,1,'假服务保存一条');check(h.book().textContent.includes('联系人已保存'),'保存回执显示');
    check(!h.book().textContent.includes(sample.recipient_account),'列表不显示完整账号');check(h.book().textContent.includes('****************0123'),'列表仅展示脱敏账号');
  });

  await scenario('当前身份刷新和组件重挂载继续读取已保存联系人',async h=>{
    let controller=await h.mountBook();await h.begin({...sample,alias:'家人'});await h.submit();
    const original=h.stores.get('workspace-a')[0].id,postCount=h.posts().length;h.button('刷新').click();await tick();
    check(h.book().textContent.includes('家人'),'刷新后保留别名');controller.destroy();controller=await h.mountBook();
    equal(h.stores.get('workspace-a')[0].id,original,'同一假服务记录保持');check(h.book().textContent.includes('家人'),'重挂后重新读出');equal(h.posts().length,postCount,'刷新不重写联系人');
  });

  await scenario('编辑回填全部资料，删除只调用联系人删除且需确认',async h=>{
    const seeded=h.seed({...sample,recipient_branch:'测试支行',alias:'同学'});await h.mountBook();h.button('编辑').click();await tick();
    const form=h.book().querySelector('form');equal(Object.fromEntries(new h.w.FormData(form)),{...sample,recipient_branch:'测试支行',alias:'同学'},'编辑原生字段完整回填');
    h.type('recipient_name','修改姓名');h.type('recipient_bank','修改银行');h.type('alias','朋友');await h.submit(form);
    equal(h.posts()[0].url,'/remittance-contacts/'+seeded.id+'/update','编辑保存原联系人ID');check(h.book().textContent.includes('朋友'),'新别名显示');
    h.button('删除').click();await tick();equal(h.posts().length,1,'点删除先不发请求');h.button('确认删除').click();await tick();
    equal(h.posts()[1].url,'/remittance-contacts/'+seeded.id+'/delete','仅调用联系人删除');equal(h.posts()[1].body,{},'删除是空资料请求');equal(h.stores.get('workspace-a'),[],'假服务删除后为空');
  });

  await scenario('姓名银行别名和尾号可搜索，隐藏账号中段与完整账号不能搜索',async h=>{
    h.seed({...sample,alias:'家人'});h.seed({...sample,recipient_name:'第二位',recipient_account:'62220210000000008888',recipient_bank:'另一银行'});await h.mountBook();
    const search=h.book().querySelector('input[type=search]'),query=text=>{search.value=text;search.dispatchEvent(new h.w.Event('input',{bubbles:true}));};
    for(const term of ['测试收款人','家人','测试开户银行','0123']){query(term);equal(h.book().querySelectorAll('.pc-person').length,1,'搜索公开资料 '+term);}
    query('000000001');equal(h.book().querySelectorAll('.pc-person').length,0,'隐藏账号中段不作为搜索文本');
    query(sample.recipient_account);equal(h.book().querySelectorAll('.pc-person').length,0,'完整账号不作为搜索文本');
    query('8888');equal(h.book().querySelectorAll('.pc-person').length,1,'其他尾号单独匹配');
  });

  await scenario('无效账号、全零、空姓名或银行阻止POST，修正后可保存',async h=>{
    await h.mountBook();const form=await h.begin();
    for(const invalid of ['123456789','0'.repeat(10),'12345abcde','１２３４５６７８９０']){h.type('recipient_account',invalid);await h.submit(form);equal(h.posts().length,0,'无效账号不发送 '+invalid);}
    h.type('recipient_account',sample.recipient_account);h.type('recipient_name',' ');await h.submit(form);equal(h.posts().length,0,'空姓名不POST');
    h.type('recipient_name',sample.recipient_name);h.type('recipient_bank',' ');await h.submit(form);equal(h.posts().length,0,'空银行不POST');
    h.type('recipient_bank',sample.recipient_bank);await h.submit(form);equal(h.posts().length,1,'修正字段后正常POST');
  });

  await scenario('跨身份迟到GET不能显示旧联系人或继续旧写',async h=>{
    h.seed({...sample,recipient_name:'旧身份姓名'});const gate=h.hold('GET','/remittance-contacts');
    const old=h.w.NavrenPaymentContacts.mount(h.book(),{isCurrent:()=>true});await tick();h.switchIdentity();
    const fresh=await h.mountBook();gate.resolve();await old.ready;await fresh.ready;await tick();
    check(!h.book().textContent.includes('旧身份姓名'),'旧GET数据不出现在新身份');equal(h.posts().length,0,'身份切换未产生写');equal(h.stores.get('workspace-b'),[],'新身份未继承联系人');
  });

  await scenario('跨身份迟到POST回执无旧姓名显示，也不追加旧GET或新身份写',async h=>{
    await h.mountBook();const oldForm=await h.begin({...sample,recipient_name:'迟到保存姓名'}),gate=h.hold('POST','/remittance-contacts');
    await h.submit(oldForm);equal(h.posts().length,1,'切换前已发一个旧身份资料请求');h.switchIdentity();await h.mountBook();
    const count=h.requests.length;gate.resolve();await tick();await h.submit(oldForm);
    equal(h.requests.length,count,'迟到回执及旧DOM事件不再发请求');check(!h.book().textContent.includes('迟到保存姓名'),'新身份无旧资料');
    equal(h.stores.get('workspace-b'),[],'未把旧保存写到新身份');equal(h.posts().map(row=>row.workspace_id),['workspace-a'],'已发请求保留原身份');
  });

  await scenario('失效页面和身份撤销使已留存事件不能保存',async h=>{
    await h.mountBook();const form=await h.begin();h.setCurrent(false);await h.submit(form);equal(h.posts().length,0,'失效页面不能POST');
    h.setCurrent(true);h.switchIdentity('workspace-b','navren:identity-revoked');await h.submit(form);equal(h.posts().length,0,'撤销身份不能POST');equal(h.book().children.length,0,'撤销后清空私有DOM');
  });

  await scenario('来源或响应绑定错误列表失败关闭，不展示未核验资料',async h=>{
    const item=h.seed();h.override('GET','/remittance-contacts',{environment:'simulation',source:'unknown',workspace_id:'workspace-a',privacy_revision:1,contacts:[item]});await h.mountBook();
    equal(h.book().querySelectorAll('.pc-person').length,0,'未知来源不显示联系人');check(h.book().textContent.includes('暂未读取'),'显示读取失败');
    h.override('GET','/remittance-contacts',{environment:'simulation',source:'user_entered',workspace_id:'workspace-other',privacy_revision:1,contacts:[item]});h.button('刷新').click();await tick();
    equal(h.book().querySelectorAll('.pc-person').length,0,'其他工作区响应不显示联系人');
  });

  await scenario('保存回执资料不一致不得显示成功或擅自刷新写入',async h=>{
    await h.mountBook();const form=await h.begin(),item=h.seed({...sample,recipient_name:'不同姓名'});h.override('POST','/remittance-contacts',(_body,envelope)=>({...envelope,contact:item}));await h.submit(form);
    check(h.book().textContent.includes('保存失败'),'差异回执显示失败');check(!h.book().textContent.includes('联系人已保存'),'差异不冒充成功');equal(h.requests.filter(row=>row.method==='GET').length,1,'差异不继续加载成功列表');
  });

  await scenario('真实自定义选择器保留select与FormData，onPick仅填资料不发付款',async h=>{
    const item=h.seed({...sample,alias:'同学'}),picks=[];const controller=h.w.NavrenPaymentContacts.mountPicker(h.picker(),{onPick:value=>{picks.push(clone(value));if(value)for(const name of ['recipient_name','recipient_account','recipient_bank','recipient_branch'])h.w.document.querySelector('#remittance [name="'+name+'"]').value=value[name];}});
    await controller.ready;await tick();const select=h.picker().querySelector('select');check(select,'保留原生选择器');
    h.w.NavrenFormControls.enhance(h.picker());await tick();const trigger=h.picker().querySelector('.nv-control-trigger');check(trigger,'真实自定义选择器挂载');trigger.click();await tick();
    const option=[...h.w.document.querySelectorAll('.nv-control-option')].find(row=>row.textContent.includes('同学'));check(option,'弹层显示已保存联系人');option.click();await tick();
    equal(select.value,item.id,'自定义控件写回原生值');equal(picks.length,1,'只调用一次onPick');equal(picks[0].recipient_account,sample.recipient_account,'回填完整账号');
    equal(Object.fromEntries(new h.w.FormData(h.w.document.querySelector('#remittance'))),{recipient_name:sample.recipient_name,recipient_account:sample.recipient_account,recipient_bank:sample.recipient_bank,recipient_branch:''},'选择保留汇款表单资料');
    equal(h.posts(),[],'选择不提交联系人或付款');equal(h.requests.map(row=>row.url),['/remittance-contacts'],'选择只读取联系人');
    check(h.picker().querySelector('.pc-manage').href.includes('view=payment-contacts'),'管理入口指向通讯录');
  });

  await scenario('canPick为false阻止unknown状态改收款人，reset同步原生和自定义值',async h=>{
    const first=h.seed({...sample,alias:'第一位'}),second=h.seed({...sample,recipient_account:'62220210000000009999',alias:'第二位'}),picks=[];let canPick=true;
    const controller=h.w.NavrenPaymentContacts.mountPicker(h.picker(),{canPick:()=>canPick,onPick:value=>picks.push(clone(value))});await controller.ready;await tick();h.w.NavrenFormControls.enhance(h.picker());
    const select=h.picker().querySelector('select');select.value=first.id;select.dispatchEvent(new h.w.Event('change',{bubbles:true}));equal(picks.length,1,'允许时可以选择');
    canPick=false;select.value=second.id;select.dispatchEvent(new h.w.Event('change',{bubbles:true}));equal(select.value,first.id,'unknown禁止时恢复原选择');equal(picks.length,1,'unknown禁止时不回填另一人');
    controller.reset();await tick();equal(select.value,'','reset清空原生值');check(h.picker().querySelector('.nv-control-value').textContent.includes('手动填写'),'reset同步自定义显示');equal(picks.length,1,'reset不追加回填或付款');equal(h.posts(),[],'禁止和reset均不POST');
  });

  await scenario('picker跨身份旧选择事件和迟到列表不得回填旧收款人',async h=>{
    const oldContact=h.seed({...sample,alias:'旧收款人'}),picks=[];
    const controller=h.w.NavrenPaymentContacts.mountPicker(h.picker(),{onPick:value=>picks.push(clone(value))});await controller.ready;await tick();
    const oldSelect=h.picker().querySelector('select');h.switchIdentity();
    const fresh=h.w.NavrenPaymentContacts.mountPicker(h.picker(),{onPick:value=>picks.push(clone(value))});await fresh.ready;await tick();
    oldSelect.value=oldContact.id;oldSelect.dispatchEvent(new h.w.Event('change',{bubbles:true}));equal(picks,[],'旧选择事件不填入新身份');check(!h.picker().textContent.includes('旧收款人'),'新身份不显示旧收款人');equal(h.posts(),[],'身份切换和旧选择不提交付款');
    h.switchIdentity('workspace-a');const gate=h.hold('GET','/remittance-contacts'),late=h.w.NavrenPaymentContacts.mountPicker(h.picker(),{onPick:value=>picks.push(clone(value))});await tick();
    h.switchIdentity('workspace-b');const newest=h.w.NavrenPaymentContacts.mountPicker(h.picker(),{onPick:value=>picks.push(clone(value))});await newest.ready;gate.resolve();await late.ready;await tick();
    check(!h.picker().textContent.includes('旧收款人'),'迟到picker列表不显示');equal(picks,[],'迟到列表无onPick');equal(h.posts(),[],'picker全部读取路径没有POST');
  });

  const after=finalized?Object.fromEntries(INPUTS.map(file=>[file,sha(file)])):null,changed=finalized?INPUTS.filter(file=>before[file]!==after[file]):[],failed=results.filter(row=>row.status==='failed').length,passed=results.length-failed;
  const report={schema:'navren.payment-contacts-ui-checks.v1',started_at:started,completed_at:new Date().toISOString(),status:failed||changed.length?'failed':finalized?'passed':'functional_passed_pending_source_freeze',finalized,source_freeze:finalized?'root_declared_final':'pending',passed,failed,assertions,tests:results,input_hashes:before,after_hashes:after,changed_inputs:changed,
    execution:{node:process.version,jsdom:require('jsdom/package.json').version,mock_requests:totalRequests,actual_http_requests:0,paid_model_calls:0,real_database_access:0,browser_run:false,real_funds:false},scope:'真实payment_contacts.js和form_controls.js，jsdom交互，有限假BankClient按身份保存资料；刷新持久性仅指同一假服务存储',not_tested:['真实浏览器布局和焦点视觉','真实HTTP与SQLite持久性（由后端独立验证）','现役服务重载或真实联系人写入','原汇款本人确认或真实银行付款']};
  fs.mkdirSync(path.dirname(OUT),{recursive:true});fs.writeFileSync(OUT,JSON.stringify(report,null,2)+'\n');
  process.stdout.write(JSON.stringify({status:report.status,passed,failed,assertions,changed_inputs:changed,report:OUT,failures:results.filter(row=>row.status==='failed').map(row=>({name:row.name,error:row.error}))},null,2)+'\n');
  if(failed||changed.length)process.exitCode=1;
}
main().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
