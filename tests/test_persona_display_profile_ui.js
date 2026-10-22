// 新模拟资料区的隔离 DOM 合同；不会连接服务、浏览器或现役账户。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'js/personal_profile.js'), 'utf8');
const templateBytes = fs.readFileSync(path.join(root, 'simulated_bank/fixtures/persona_display_profiles.json'));
const templates = JSON.parse(templateBytes).profiles;
const version = crypto.createHash('sha256').update(templateBytes).digest('hex');
const keys = ['nickname','full_name','phone','email','city','occupation','company','birthday','gender','bio','avatar'];
const clone = value => JSON.parse(JSON.stringify(value));
const groups = new Set(process.argv.slice(2));
const run = group => groups.size === 0 || groups.has(group);

class Element {
  constructor(tag, doc) {
    this.tagName=tag.toUpperCase();this.doc=doc;this.children=[];this.dataset={};this.style={};this.attributes={};this.className='';this.value='';this._text='';this.listeners=new Map();this.isConnected=true;
    this.classList={add:(...names)=>{this.className=[...new Set([...this.className.split(' ').filter(Boolean),...names])].join(' ');},
      remove:(...names)=>{this.className=this.className.split(' ').filter(name=>!names.includes(name)).join(' ');},
      toggle:(name,force)=>{const exists=this.className.split(' ').includes(name),keep=force===undefined?!exists:force;if(keep)this.classList.add(name);else this.classList.remove(name);return keep;}};
  }
  set textContent(value) { this._text=String(value);this.children=[]; }
  get textContent() { return this._text+this.children.map(child=>typeof child==='string'?child:child.textContent).join(''); }
  get firstElementChild() { return this.children.find(child=>typeof child!=='string')||null; }
  get parentElement() { return this.parent||null; }
  append(...children) { for(const child of children){this.children.push(child);if(typeof child!=='string')child.parent=this;} }
  replaceChildren(...children) { this._text='';this.children=[];this.append(...children); }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(name,fn) { this.listeners.set(name,fn); }
  focus() { this.doc.activeElement=this; }
  setCustomValidity() {}
  reportValidity() { return true; }
  showModal() { this.open=true; }
  close() { this.open=false; }
  remove() { if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);this.isConnected=false; }
  matches(selector) {
    if(selector.startsWith('.'))return this.className.split(' ').includes(selector.slice(1));
    if(selector.startsWith('[data-'))return Object.hasOwn(this.dataset,selector.slice(6,-1).replace(/-([a-z])/g,(_,s)=>s.toUpperCase()));
    return this.tagName.toLowerCase()===selector;
  }
  querySelectorAll(selector) { const choices=selector.split(',').map(x=>x.trim()),out=[];for(const child of this.children){if(typeof child==='string')continue;if(choices.some(x=>child.matches(x)))out.push(child);out.push(...child.querySelectorAll(selector));}return out; }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
}

function fixture(persona='tycoon', personal={}) {
  const listeners=new Map(), ids=new Map();
  const document={readyState:'loading',activeElement:null,addEventListener(){},querySelectorAll(){return [];},
    createElement(tag){return new Element(tag,document);},
    getElementById(id){return ids.get(id)||document.body.querySelectorAll('input,textarea,select').find(e=>e.id===id)||null;}};
  document.body=new Element('body',document);
  for(const id of ['profileName','profileSubtitle','personalProfileSummary','profileAvatar','editPersonalProfile'])ids.set(id,new Element('div',document));
  const own={...Object.fromEntries(keys.map(key=>[key,''])),nickname:'本人昵称',...personal};
  let identity,state,deferred=false,pending=null;
  const posts=[],gets=[];
  function switchPersona(next){
    const index=Object.keys(templates).indexOf(next)+1;
    identity={registered_user_id:'a'.repeat(32),workspace_id:index.toString(16).padStart(32,'0'),account_id:next,
      privacy_revision:0,auth_subject:'fixture-auth',session_generation:1,csrf:'fixture-csrf',auth_expires_at:9e10,lease_expires_at:9e10};
    state={contract:'navren.account-state/1',registered_user_id:identity.registered_user_id,workspace_binding:identity.workspace_id,
      persona_id:next,revision:7,updated_at:null,initialized_sections:['profile'],sections:{profile:{...own,avatar_preset:'default'}},migration:{legacy_profile:'none',conflicts:[]},
      simulated_persona_profile:{contract:'navren.simulated-persona-profile/1',status:'available',data_origin:'navren_synthetic',is_simulated:true,is_real_identity:false,contact_use:'display_only',editable:false,
        registered_user_id:identity.registered_user_id,workspace_binding:identity.workspace_id,persona_id:next,template_version:version,profile:clone(templates[next])}};
  }
  switchPersona(persona);
  function response(){return {profile:{...Object.fromEntries(keys.map(key=>[key,state.sections.profile[key]])),revision:state.revision,updated_at:state.updated_at},account_revision:state.revision,
    registered_user_id:identity.registered_user_id,workspace_id:identity.workspace_id,workspace_binding:identity.workspace_id,privacy_revision:identity.privacy_revision};}
  const BankClient={identity:()=>clone(identity),ensure:async()=>clone(identity),guard(){const captured=clone(identity);return {valid:()=>captured.workspace_id===identity.workspace_id&&captured.registered_user_id===identity.registered_user_id};},
    async get(url){gets.push(url);const value=response();if(deferred){deferred=false;return new Promise(resolve=>{pending={resolve,value};});}return clone(value);},
    async post(url,payload){posts.push({url,payload:clone(payload)});state.sections.profile={...clone(payload.profile),avatar_preset:'default'};state.revision++;return clone(response());}};
  const Storage={ready:async()=>clone(state),accountReady:()=>true};
  const window={BankClient,NavrenI18n:{language:'zh-CN'},NavrenDefaultAvatars:{render:()=> 'fixture-avatar.png'},
    addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(fn);},
    dispatchEvent(event){for(const fn of listeners.get(event.type)||[])fn(event);}};
  vm.runInNewContext(source,{window,document,BankClient,Storage,Intl,Date,console,queueMicrotask,
    CustomEvent:class{constructor(type,options){this.type=type;this.detail=options?.detail;}},BroadcastChannel:undefined});
  return {api:window.NavrenPersonalProfile,document,window,posts,gets,get state(){return state;},get identity(){return identity;},switchPersona,
    deferGet(){deferred=true;},get pending(){return pending;},summary:()=>ids.get('personalProfileSummary')};
}

async function main(){
  if(run('display')){
  for(const persona of Object.keys(templates)){
    const app=fixture(persona,{full_name:'本人自填姓名',phone:'',email:'',birthday:''});
    await app.api.refresh();const text=app.summary().textContent;
    for(const key of ['full_name','email','simulation_contact_phone','city','occupation','birthday'])assert.ok(text.includes(templates[persona][key]),`${persona}:${key}`);
    assert.ok(text.includes('虚构'));assert.ok(text.includes('仅用于展示'));assert.ok(text.includes('本人自填姓名'));
    assert.equal(app.api.current().full_name,'本人自填姓名');assert.equal(app.api.current().phone,'');assert.equal(app.api.current().email,'');
    assert.equal(app.summary().querySelectorAll('a').length,0);assert.deepEqual(app.posts,[]);assert.deepEqual(app.gets,['/personal-profile']);
  }
  console.log('four complete simulation panels; personal values and deliberate blanks retained; contacts are text only');
  }

  if(run('reject')){
  for(const invalid of [value=>value.workspace_binding='f'.repeat(32),value=>value.registered_user_id='b'.repeat(32),value=>value.contact_use='sms',value=>value.profile.email='person@example.com']){
    const app=fixture();invalid(app.state.simulated_persona_profile);await app.api.refresh();
    assert.ok(!app.summary().textContent.includes('SIM-PHONE'));assert.equal(app.api.current().nickname,'本人昵称');
  }
  console.log('foreign bindings and operational or real contact metadata rejected without replacing personal profile');
  }

  if(run('edit')){
  const editor=fixture('grain_farmer',{full_name:'',phone:'',email:'',birthday:''});await editor.api.refresh();await editor.api.open();
  for(const key of ['full_name','phone','email','birthday'])assert.equal(editor.document.getElementById('personalProfile-'+key).value,'');
  editor.document.getElementById('personalProfile-nickname').value='本人修改昵称';
  await editor.document.body.querySelector('form').onsubmit({preventDefault(){}});
  assert.equal(editor.posts.length,1);assert.equal(editor.posts[0].payload.profile.nickname,'本人修改昵称');
  for(const key of ['full_name','phone','email','birthday'])assert.equal(editor.posts[0].payload.profile[key],'');
  assert.ok(!JSON.stringify(editor.posts).includes('SIM-PHONE'));assert.ok(!JSON.stringify(editor.posts).includes('.invalid'));
  console.log('original editor and save payload retain canonical fields; no simulated data copied into personal save');
  }

  if(run('late')){
  const app=fixture();await app.api.refresh();app.deferGet();const late=app.api.refresh();
  for(let n=0;n<10&&!app.pending;n++)await new Promise(resolve=>setImmediate(resolve));assert.ok(app.pending);
  app.switchPersona('working_landlady');app.window.dispatchEvent({type:'navren:account-switched'});await app.api.refresh();
  app.pending.resolve(app.pending.value);await late;
  assert.ok(app.summary().textContent.includes(templates.working_landlady.full_name));assert.ok(!app.summary().textContent.includes(templates.tycoon.full_name));
  console.log('late prior-persona GET cannot restore old persona information after switch');
  }
}

main().catch(error=>{console.error(error);process.exitCode=1;});
