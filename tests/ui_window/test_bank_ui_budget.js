/* 验证可安排金额直接读取预算服务，未知与异步身份不被前端改为零。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require('jsdom');
const source=fs.readFileSync('js/bank_ui.js','utf8');const tick=()=>new Promise(r=>setTimeout(r,5));
async function fixture(currency='CNY'){
  const dom=new JSDOM('<!doctype html><div id="root"></div>',{url:'https://navren.test',runScripts:'outside-only'}),w=dom.window;
  await tick();let valid=true,resolveBudget;
  const state={version:9,accounts:[{id:'test-main',balance_minor:90000,currency}],profile:{reserve_minor:10000},upcoming_due_minor:30000,commitments:[],notice:'固定合同夹具'};
  const budget={version:9,as_of:'2026-10-03',cash_minor:90000,reserve_minor:10000,upcoming_due_minor:30000,held_minor:0,investable_minor:12345,commitments:[]};
  w.NavrenPresentation={name:s=>s,text:s=>s};w.BankClient={guard:()=>({valid:()=>valid}),get:p=>{assert.ok(p.startsWith('/budget?account_id=test-main'));return new Promise(r=>resolveBudget=r);},money:v=>String(v/100)};
  w.eval(source);const panel=w.BankUI.budgetPanel(state);w.document.getElementById('root').append(panel);
  return {dom,w,panel,budget,release:v=>resolveBudget(v),invalidate:()=>valid=false};
}
(async()=>{
  const a=await fixture();assert.ok(a.panel.textContent.includes('正在核对'));a.release(a.budget);await tick();assert.ok(a.panel.querySelector('.bank-usable').textContent.includes('123.45'));assert.ok(!a.panel.querySelector('.bank-usable').textContent.includes('500.00'),'不按前端余额减留款重算');assert.ok(a.panel.querySelector('details'));a.dom.window.close();
  const b=await fixture();b.invalidate();b.release(b.budget);await tick();assert.equal(b.panel.querySelector('.bank-usable'),null,'旧身份响应不显示金额');b.dom.window.close();
  const c=await fixture();c.release({...c.budget,version:10});await tick();assert.ok(c.panel.textContent.includes('账户版本已变化'));assert.equal(c.panel.querySelector('.bank-usable'),null);c.dom.window.close();
  const d=await fixture('USD');d.release(d.budget);await tick();assert.ok(d.panel.textContent.includes('资金币种口径尚未核对'));assert.equal(d.panel.querySelector('.bank-usable'),null);d.dom.window.close();
  console.log('通过：预算金额直接读服务、身份迟到拒绝、账本版本冲突与未绑定币种阻断；固定合同夹具，无网络。');
})().catch(e=>{console.error(e);process.exitCode=1;});
