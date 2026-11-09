/* Fixed client hints for rejected files; never expose an arbitrary HTTP body. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const {Blob,File}=require('node:buffer');
const source=fs.readFileSync(path.resolve(__dirname,'../js/api.js'),'utf8');
const mimes=['image/png','image/jpeg','image/webp','image/gif','audio/wav','audio/webm','audio/ogg','audio/mpeg','audio/mp4',
  'application/pdf','application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'text/plain','text/markdown','text/csv'];
const identity={registered_user_id:'a'.repeat(32),workspace_id:'b'.repeat(32),account_id:'tycoon',auth_subject:'controlled-auth-subject',
  privacy_revision:4,session_generation:2,csrf:'controlled-csrf',auth_expires_at:Date.now()/1000+3600,lease_expires_at:Date.now()/1000+1800};
const media={contract:'navren.chat-media/1',media_id:'a'.repeat(64),kind:'document',mime_type:'text/plain',bytes:5,sha256:'c'.repeat(64),
  duration_ms:null,storage_status:'bound',content_url:'/api/finance/chat-media/'+'a'.repeat(64),historical_readonly:true,execution_authority:false,
  audio_format:null,file_name:'材料.txt',document_format:'txt',text_chars:5,extraction_status:'text',extraction_warning:null};
const results=[];
let requests=0,bodyReads=0;
function fixture(status){
  const neverRead=()=>{bodyReads++;throw Error('Untrusted server body must not be read');};
  const sandbox={Blob,File,AbortController,crypto:webcrypto,setTimeout,clearTimeout,URL,addEventListener:()=>{},
    BankClient:{identity:()=>({...identity}),guard:()=>({valid:()=>true}),get:async()=>({contract:'navren.chat-media-context/1',
      workspace_binding:identity.workspace_id,registered_user_id:identity.registered_user_id,privacy_revision:4,archive_epoch:0,
      limits:{image_bytes:10485760,audio_bytes:26214400,document_bytes:10485760,document_text_chars:120000,media_per_user_turn:8,mime_types:mimes}})},
    fetch:async()=>{requests++;return {ok:false,status,json:neverRead,text:neverRead,blob:neverRead};}};
  vm.createContext(sandbox);vm.runInContext(source,sandbox,{filename:'js/api.js'});return sandbox.NavrenChatMedia;
}
(async()=>{
  for(const status of [400,422])for(const operation of ['upload','bind','read']){
    const api=fixture(status);
    const task=operation==='upload'?api.upload(new File(['hello'],'材料.txt',{type:'text/plain'})):
      operation==='bind'?api.bind('document-turn',[{...media,storage_status:'stored'}]):api.read(media);
    await assert.rejects(task,error=>{
      assert.equal(error.name,'ChatMediaError');assert.equal(error.code,'MEDIA_INVALID_FILE');assert.equal(error.status,status);
      assert.equal(error.message,'文件内容无法读取，请检查原件或换为支持的格式。');return true;
    });
    results.push({operation,status,passed:true});
  }
  assert.equal(bodyReads,0);assert.equal(requests,6);
  console.log(JSON.stringify({schema:'navren.chat-media-http-validation-errors/1',passed:true,results,controlled_requests:requests,
    untrusted_body_reads:bodyReads,actual_http_requests:0,actual_model_calls:0,real_account_writes:0},null,2));
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
