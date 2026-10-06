/* 实际 BankUI/i18n 的只读时间轴投影；禁止网络，不调用模型或业务写入。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require('jsdom');
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
(async()=>{
  const dom=new JSDOM('<body><div id="message"><div class="bubble"></div></div></body>',{url:'https://navren.test',runScripts:'outside-only'}),w=dom.window;
  await tick();w.Storage={get:()=> 'en',set(){}};w.STORAGE_KEYS={LANGUAGE:'language'};w.matchMedia=()=>({matches:false,addEventListener(){}});
  w.fetch=()=>{throw Error('forbidden_network');};w.BankClient={get:()=>{throw Error('forbidden_business_read');}};w.NavrenPresentation={name:v=>v,text:v=>v};
  w.eval(fs.readFileSync('js/i18n.js','utf8'));w.eval(fs.readFileSync('js/bank_ui.js','utf8'));
  const message=w.document.getElementById('message'),cases=[
    ['confirmed','已确认事项','Confirmed events'],['candidate','周期候选情景','Recurring-charge candidate scenario'],
    ['unknown','安排确定性待核实','Arrangement certainty is unverified'],[undefined,'安排确定性待核实','Arrangement certainty is unverified'],['bad-state','安排确定性待核实','Arrangement certainty is unverified']
  ];
  w.BankUI.renderResponse(message,{bank:true,cashflow_timeline:{horizon_days:60,as_of:'2026-10-06',horizon_end:'2026-12-05',accounts:[],timeline:[{date:'2026-10-10',events:cases.map(([certainty],i)=>({title:'原始事项'+i,certainty,date_status:'scheduled',cash_delta_minor:0,currency:'CNY',source_table:'subscriptions',evidence_id:'item-'+i}))}]}});
  const rows=[...message.querySelectorAll('.cashflow-event')];assert.equal(rows.length,cases.length);
  function verify(language){
    w.NavrenI18n.setLanguage(language,false);
    rows.forEach((row,i)=>{
      assert.equal(row.querySelector('.cashflow-event-certainty').textContent,cases[i][language==='en'?2:1]);
      assert.equal(row.querySelector('.cashflow-event-date-state').textContent,language==='en'?'Planned date':'计划日期');
      assert.equal(row.querySelector('.cashflow-event-flow').textContent,language==='en'?'No change to cash balance':'不改变现金余额');
      assert.equal(row.querySelector('.cashflow-event-title').textContent,'原始事项'+i);
      assert.ok(row.textContent.includes('¥0.00'),'已提供的零值保持零');
    });
  }
  verify('en');verify('zh-CN');verify('en');dom.window.close();
  console.log('通过：确认/候选/未知/缺失/非法确定性独立展示，中英文往返与原始事项标题、服务端零金额保持；无网络/模型/业务写入。');
})().catch(error=>{console.error(error);process.exitCode=1;});
