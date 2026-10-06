/** 消费同源服务的已核验任务投影；不从自由回复、HTML或本地历史生成金融指标。 */
window.NavrenTaskChat = (() => {
  function message(host,text){host.replaceChildren();const p=document.createElement('p');window.NavrenI18n?NavrenI18n.setText(p,text):p.textContent=text;host.append(p);}
  function reference(ref){return {kind:ref.kind,id:ref.id,source_scope:ref.source_scope,version:ref.version,
    ...(ref.parameters?{parameters:structuredClone(ref.parameters)}:{})};}
  function destination(ref){
    const paths={account:['account','account-detail'],product:['recommend','product'],research_plan:['recommend','plan'],
      event:['recommend','reading'],weekly:['recommend','weekly'],goal:['mine','goal'],calendar_event:['account','calendar-item']};
    const target=paths[ref.kind];if(!target)return null;
    const parameters=ref.parameters||{},archiveKeys=['edition_date','revision','event_version'];
    if(ref.kind==='weekly'&&parameters.period!=null&&parameters.period!==ref.id)return null;
    const dateValid=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&
      Number.isFinite(Date.parse(value+'T12:00:00Z'))&&new Date(value+'T12:00:00Z').toISOString().slice(0,10)===value;
    if(archiveKeys.some(key=>parameters[key]!=null)&&
      (ref.kind!=='event'||!dateValid(parameters.edition_date)||
       !Number.isSafeInteger(parameters.revision)||parameters.revision<1||
       !Number.isSafeInteger(parameters.event_version)||String(parameters.event_version)!==String(ref.version)))return null;
    const url=new URL(target[0]+'.html',location.href);
    for(const [key,value]of Object.entries({view:target[1],object_kind:ref.kind,object_id:ref.id,
      object_version:ref.version,source_scope:ref.source_scope})){
      if(['view','object_kind','object_id','object_version','source_scope','portfolio_id','edition_date','revision','date','period'].includes(key)&&value!=null)url.searchParams.set(key,String(value));
    }
    for(const key of ['portfolio_id','edition_date','revision','date','period'])if(parameters[key]!=null)url.searchParams.set(key,String(parameters[key]));
    return url.pathname+url.search;
  }
  function begin(){
    if(!window.NavrenTaskComponents||!BankClient.identity())return null;
    const host=document.createElement('div');host.className='task-result-host';let boundMessage=null;
    const component=NavrenTaskComponents.create(host,{
      onInvalidate:()=>{
        if(!boundMessage)return;
        delete boundMessage.dataset.speechReceipt;delete boundMessage.dataset.bankResponseId;
        for(const button of boundMessage.querySelectorAll('.message-audio-control')){
          if(button.classList.contains('speaking')&&window.NavrenSpeech?.activeKind==='tts')NavrenSpeech.stop();
          button.remove();
        }
        boundMessage.querySelector('.bubble')?.classList.remove('has-audio-control');
      },
      onAction:async(action,context)=>{
        if(!context.isCurrent())return;
        const ref=action.reference;
        if(action.type==='review_operation'){
          if(ref.source_scope!=='bank_operation')throw Error('untrusted_operation_source');
          const trigger=[...host.querySelectorAll('[data-task-action-id]')].find(b=>b.dataset.taskActionId===action.action_id);
          return BankUI.openDraft({operation_id:ref.id},trigger,{...context,expected_operation_version:ref.version});
        }
        if(action.type==='open_detail'){
          const url=destination(ref);if(!url)throw Error('detail_adapter_unavailable');
          return NavrenApp.navigate(url);
        }
        if(['ask','revise_conditions'].includes(action.type))return NavrenViews.reference(reference(ref));
        throw Error('unsupported_task_action');
      },
      onReceipt:(action,context)=>{
        if(action.reference.source_scope!=='bank_operation')throw Error('untrusted_operation_source');
        return BankUI.openReceipt(action.reference.id,null,{...context,expected_operation_version:action.reference.version});
      }
    });
    return {host,component,ticket:component.begin(),bindMessage:message=>{boundMessage=message;message.__navrenTaskCurrent=()=>component.isCurrent();}};
  }
  function render(messageElement,result,request){
    if(result.task_result==null){request?.component.destroy();return false;}
    const bubble=messageElement.querySelector('.bubble');
    const reject=()=>{
      delete messageElement.dataset.speechReceipt;
      delete messageElement.dataset.bankResponseId;
      Object.assign(result,{reply:'结构化任务资料尚未核实，请重新查询。',bank:true,isFallback:true,task_result_rejected:true,
        speech_receipt:'',bank_response_id:'',bank_actions:[],widgets:[],market_sources:[],user_memory_context:null,
        memory_proposals:[],service_negotiation_request:null,follow_up_questions:[],ui_actions:[],toast:null,
        allocation:null,image_analysis:null,agent_chain:null,followup_memory:null});
    };
    if(!request){reject();message(bubble,result.reply);return true;}
    bubble.replaceChildren(request.host);
    const outcome=request.component.render(result.task_result,request.ticket);
    if(!outcome.ok){request.component.destroy();reject();message(request.host,result.reply);}
    else request.bindMessage(messageElement);
    return true;
  }
  return {begin,render,destination};
})();
