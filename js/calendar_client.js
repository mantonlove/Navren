/* 日历与银行客户端共用同源会话、CSRF 和错误合同。 */
window.CalendarClient={
 financial(parameters){return NavrenFinancialData.read('presentation',{view:'calendar',...parameters});},
  query(p){const q=new URLSearchParams();for(const [k,v] of Object.entries(p))if(v!==undefined)q.set(k,Array.isArray(v)?v.join(','):String(v));return BankClient.get('/calendar?'+q);},
  day(id,date,offset=0){return BankClient.get('/calendar/views/'+encodeURIComponent(id)+'/days/'+date+'?offset='+offset+'&limit=100');},
  item(id){return BankClient.get('/calendar/items/'+encodeURIComponent(id));},
  prepare(p){return BankClient.post('/calendar/events/prepare',p);},
  confirm(id){return BankClient.post('/calendar/events/'+id+'/confirm',{confirmed:true});},
  draft(id){return BankClient.get('/calendar/event-drafts/'+id);},
  preview(text){return BankClient.postText('/calendar/import/preview',text);},
  commit(id){return BankClient.post('/calendar/import/commit',{batch_id:id,confirmed:true});},
  reminders(){return BankClient.get('/calendar/reminders');},
  read(id){return BankClient.post('/calendar/reminders/'+id+'/read',{});}
};
