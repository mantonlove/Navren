/* Controlled browser contract checks: no real account, HTTP, model or DOM writes. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto,createHash}=require('node:crypto');
const {Blob,File}=require('node:buffer');
const repo=path.resolve(__dirname,'..');
const clone=value=>JSON.parse(JSON.stringify(value));
const documentTypes={
  'application/pdf':'pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document':'docx',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':'xlsx',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation':'pptx',
  'text/plain':'txt','text/markdown':'md','text/csv':'csv'};
const uploadMimes=['image/png','image/jpeg','image/webp','image/gif','audio/wav','audio/webm','audio/ogg','audio/mpeg','audio/mp4',...Object.keys(documentTypes)];
const owner='a'.repeat(12)+'4aaa8'+'a'.repeat(15);
const source=fs.readFileSync(path.join(repo,'js/api.js'),'utf8');
const archiveSource=fs.readFileSync(path.join(repo,'js/chat_archive.js'),'utf8');
const hash=value=>createHash('sha256').update(value).digest('hex');
const baseBytes=Buffer.from('文档原件：保留字节与权限。');
const results=[];
let interceptedRequests=0;
function descriptor(mime='text/plain',status='bound',bytes=baseBytes){
  const kind=Object.hasOwn(documentTypes,mime)?'document':mime.split('/')[0];
  return {contract:'navren.chat-media/1',media_id:'a'.repeat(64),kind,mime_type:mime,bytes:bytes.length,sha256:hash(bytes),
    duration_ms:null,storage_status:status,content_url:'/api/finance/chat-media/'+'a'.repeat(64),
    historical_readonly:true,execution_authority:false,audio_format:null,
    ...(kind==='document'?{file_name:'材料.'+documentTypes[mime],document_format:documentTypes[mime],text_chars:17,
      extraction_status:'text',extraction_warning:null}:{})};
}
function fixture(){
  const identity={registered_user_id:owner,workspace_id:'b'.repeat(32),account_id:'tycoon',auth_subject:'controlled-auth-subject',
    privacy_revision:4,session_generation:2,csrf:'controlled-csrf',auth_expires_at:Date.now()/1000+3600,lease_expires_at:Date.now()/1000+1800};
  const f={identity,valid:true,requests:[],contextReads:0,contextEpoch:0,fetch:null};
  f.context=()=>({contract:'navren.chat-media-context/1',workspace_binding:identity.workspace_id,registered_user_id:identity.registered_user_id,
    privacy_revision:identity.privacy_revision,archive_epoch:f.contextEpoch,limits:{image_bytes:10485760,audio_bytes:26214400,
      document_bytes:10485760,document_text_chars:120000,media_per_user_turn:8,mime_types:uploadMimes}});
  const sandbox={Blob,File,AbortController,crypto:webcrypto,setTimeout,clearTimeout,console,
    URL:{createObjectURL:()=> 'blob:controlled',revokeObjectURL:()=>{}},addEventListener:()=>{},
    BankClient:{identity:()=>clone(identity),guard:()=>({valid:()=>f.valid}),get:async route=>{
      assert.equal(route,'/chat-media/context');f.contextReads++;return clone(f.context());}},
    fetch:async(...args)=>{interceptedRequests++;f.requests.push(args);assert.equal(typeof f.fetch,'function');return f.fetch(...args);}};
  vm.createContext(sandbox);vm.runInContext(source,sandbox,{filename:'js/api.js'});
  f.api=sandbox.NavrenChatMedia;return f;
}
function archivePage(media){
  return {contract:'navren.registered-dialogue-archive/2',registered_user_id:owner,workspace_id:'b'.repeat(32),persona_id:'tycoon',
    privacy_revision:4,personal_revision:0,archive_epoch:0,snapshot_id:'c'.repeat(64),next_cursor:null,has_more:false,
    model_context_limit:16,execution_authority:false,archive_coverage:{source:'registered_user_archive',legacy_public_history_imported:false},
    items:[{request_id:'document-turn',sequence:1,received_at:1700000000,
      user:{message_id:'document-turn:user',role:'user',status:'archived',content:'',references:[],attachments:Array.isArray(media)?media:[media]},
      assistant:{message_id:'document-turn',role:'assistant',status:'not_published',content:null,
        historical_readonly:true,current_fact_eligible:false,execution_authority:false}}]};
}
async function archiveRead(media,mutate){
  const sandbox={module:{exports:{}},AbortController};vm.createContext(sandbox);vm.runInContext(archiveSource,sandbox,{filename:'js/chat_archive.js'});
  const page=archivePage(media);if(mutate)mutate(page);
  const client=sandbox.module.exports.create({identityProvider:()=>({registered_user_id:owner,workspace_id:'b'.repeat(32),account_id:'tycoon',privacy_revision:4,auth_subject:'controlled-auth-subject'}),readPage:async()=>page});
  client.attach();try{return await client.loadNext();}finally{client.unload();}
}
function rejectsValidation(media){
  assert.throws(()=>fixture().api.validate(media),error=>error.code==='INVALID_MEDIA_DESCRIPTOR');
  return assert.rejects(archiveRead(media),error=>error.code==='INVALID_ARCHIVE_DTO');
}
function disposition(name){return "attachment; filename*=UTF-8''"+encodeURIComponent(name).replace(/[!'()*]/g,c=>'%'+c.charCodeAt(0).toString(16).toUpperCase());}
function readResponse(media,{bytes=baseBytes,type=media.mime_type,headers={},onBlob}={}){
  const expected={'Content-Type':media.mime_type,'Content-Length':String(media.bytes),'X-Content-Type-Options':'nosniff',
    'Cache-Control':'no-store','Content-Disposition':media.kind==='document'?disposition(media.file_name):'inline','Accept-Ranges':'none',...headers};
  return {status:200,headers:{get:name=>expected[name]??null},blob:async()=>{if(onBlob)onBlob();return new Blob([bytes],{type});}};
}
async function check(name,run){await run();results.push({name,passed:true});}
(async()=>{
  await check('seven-document-mimes-preserve-descriptors-and-inert-history',async()=>{
    for(const mime of Object.keys(documentTypes)){
      const media=descriptor(mime),validated=fixture().api.validate(media,{boundOnly:true});
      assert.deepEqual(clone(validated),media);assert.ok(Object.isFrozen(validated));
      const state=await archiveRead(media);assert.equal(state.messages.length,1);
      const message=state.messages[0];assert.deepEqual(clone(message.attachments[0]),media);
      for(const item of [message,message.attachments[0]]){assert.equal(item.historical_readonly,true);assert.equal(item.execution_authority,false);assert.ok(Object.isFrozen(item));}
      assert.equal(message.current_fact_eligible,false);assert.equal(message.read_only,true);
    }
    const noText={...descriptor('application/pdf'),text_chars:0,extraction_status:'no_text',extraction_warning:'扫描件尚未识别。'};
    fixture().api.validate(noText);assert.equal((await archiveRead(noText)).messages[0].attachments[0].extraction_warning,noText.extraction_warning);
  });
  await check('strict-document-fields-status-mime-name-and-bounds',async()=>{
    const original=descriptor();
    for(const field of ['file_name','document_format','text_chars','extraction_status','extraction_warning']){
      const missing=clone(original);delete missing[field];await rejectsValidation(missing);
    }
    for(const change of [{extra:true},{extracted_text:'must stay server-side'},{document_format:'pdf'},{kind:'image'},
      {mime_type:'application/octet-stream'},{file_name:'.txt'},{file_name:'../材料.txt'},{file_name:' 材料.txt'},
      {file_name:'材料.txt\n'},{file_name:'材料\u200b.txt'},{file_name:'材料\uE000.txt'},{file_name:'a'.repeat(177)+'.txt'},
      {text_chars:-1},{text_chars:120001},{text_chars:1.5},{text_chars:true},{text_chars:0},
      {extraction_status:'no_text'},{extraction_warning:''},{extraction_warning:'不应同时成功与警告'},
      {text_chars:0,extraction_status:'no_text',extraction_warning:null},
      {text_chars:0,extraction_status:'no_text',extraction_warning:'警'.repeat(201)},
      {duration_ms:1},{audio_format:{encoding:'pcm_s16le',sample_rate:16000,channels:1}},
      {bytes:10485761},{bytes:0},{bytes:1.5},{historical_readonly:false},{execution_authority:true},
      {content_url:'/arbitrary/path'},{sha256:'x'.repeat(64)}])await rejectsValidation({...original,...change});
    const maximum={...original,file_name:'😀'.repeat(176)+'.TXT',text_chars:120000,bytes:10485760};
    fixture().api.validate(maximum);await archiveRead(maximum);
    const maxWarning={...original,text_chars:0,extraction_status:'no_text',extraction_warning:'😀'.repeat(200)};
    fixture().api.validate(maxWarning);await archiveRead(maxWarning);
    assert.throws(()=>fixture().api.validate({...original,storage_status:'stored'},{boundOnly:true}),error=>error.code==='INVALID_MEDIA_DESCRIPTOR');
    await assert.rejects(archiveRead({...original,storage_status:'stored'}),error=>error.code==='INVALID_ARCHIVE_DTO');
  });
  await check('image-audio-and-pcm-existing-validation-remains-strict',async()=>{
    for(const mime of uploadMimes.filter(type=>!Object.hasOwn(documentTypes,type))){const media=descriptor(mime);fixture().api.validate(media);await archiveRead(media);}
    const pcm={...descriptor('audio/pcm'),bytes:32,audio_format:{encoding:'pcm_s16le',sample_rate:16000,channels:1}};
    fixture().api.validate(pcm);await archiveRead(pcm);
    for(const media of [{...descriptor('image/png'),kind:'audio'},{...descriptor('audio/wav'),kind:'image'},
      {...descriptor('image/png'),file_name:'unexpected.png'},{...descriptor('audio/wav'),bytes:26214401},
      {...descriptor('image/png'),bytes:10485761},{...descriptor('audio/wav'),duration_ms:600001},
      {...pcm,audio_format:null},{...pcm,audio_format:{...pcm.audio_format,sample_rate:12345}},
      {...descriptor('audio/wav'),audio_format:pcm.audio_format}])await rejectsValidation(media);
    assert.throws(()=>fixture().api.validate({...descriptor('image/png'),duration_ms:1}),error=>error.code==='INVALID_MEDIA_DESCRIPTOR');
    assert.throws(()=>fixture().api.validate({...pcm,bytes:33}),error=>error.code==='INVALID_MEDIA_DESCRIPTOR');
  });
  await check('upload-file-name-header-raw-body-and-verified-document-reply',async()=>{
    for(const [mime,extension]of Object.entries(documentTypes)){
      const f=fixture(),file=new File([baseBytes],"材料 !'()*."+extension,{type:mime});
      const media={...descriptor(mime,'stored'),file_name:file.name};
      f.fetch=async(url,request)=>{
        assert.equal(url,'/api/finance/chat-media');assert.equal(request.method,'POST');assert.strictEqual(request.body,file);
        assert.equal(request.headers['X-Navren-File-Name'],encodeURIComponent(file.name));
        assert.equal(request.headers['Content-Type'],mime);assert.equal(request.headers['X-Navren-Expected-Workspace'],f.identity.workspace_id);
        assert.equal(Object.keys(request.headers).some(key=>key.toLowerCase()==='content-length'),false);
        return {ok:true,status:200,json:async()=>({contract:'navren.chat-media-upload/1',media,workspace_binding:f.identity.workspace_id,privacy_revision:4,archive_epoch:0})};
      };
      assert.deepEqual(clone(await f.api.upload(file,{uploadKey:'d'.repeat(32)})),media);assert.equal(f.contextReads,2);
    }
    for(const mime of ['image/png','audio/wav']){
      const f=fixture(),file=new File([baseBytes],'原件.'+(mime==='image/png'?'png':'wav'),{type:mime});
      f.fetch=async(url,request)=>{assert.equal(request.headers['X-Navren-File-Name'],encodeURIComponent(file.name));return {ok:true,json:async()=>({contract:'navren.chat-media-upload/1',media:descriptor(mime,'stored'),workspace_binding:f.identity.workspace_id,privacy_revision:4,archive_epoch:0})};};
      await f.api.upload(file);
    }
  });
  await check('document-upload-rejects-invalid-local-files-before-network',async()=>{
    for(const blob of [new Blob([baseBytes],{type:'text/plain'}),new File([baseBytes],'wrong.pdf',{type:'text/plain'}),
      new File([baseBytes],'.txt',{type:'text/plain'}),new File([baseBytes],'bad\u200b.txt',{type:'text/plain'}),
      new File([new Uint8Array(10485761)],'large.txt',{type:'text/plain'}),new File([baseBytes],'a.txt',{type:'application/octet-stream'})]){
      const f=fixture();await assert.rejects(f.api.upload(blob),error=>['INVALID_MEDIA_DESCRIPTOR','MEDIA_TOO_LARGE','MEDIA_UNSUPPORTED'].includes(error.code));assert.equal(f.requests.length,0);assert.equal(f.contextReads,0);
    }
    const f=fixture(),blob=new Blob([baseBytes],{type:'image/png'});f.fetch=async(url,request)=>{
      assert.equal(Object.hasOwn(request.headers,'X-Navren-File-Name'),false);
      return {ok:true,json:async()=>({contract:'navren.chat-media-upload/1',media:descriptor('image/png','stored'),workspace_binding:f.identity.workspace_id,privacy_revision:4,archive_epoch:0})};};
    await f.api.upload(blob);
  });
  await check('upload-returned-file-name-hash-and-context-cannot-change',async()=>{
    for(const mutate of [media=>media.file_name='替换.txt',media=>media.sha256='f'.repeat(64)]){
      const f=fixture(),file=new File([baseBytes],'材料.txt',{type:'text/plain'}),media=descriptor('text/plain','stored');mutate(media);
      f.fetch=async()=>({ok:true,json:async()=>({contract:'navren.chat-media-upload/1',media,workspace_binding:f.identity.workspace_id,privacy_revision:4,archive_epoch:0})});
      await assert.rejects(f.api.upload(file),error=>error.code==='INVALID_MEDIA_DESCRIPTOR');
    }
    const f=fixture(),originalContext=f.context;f.context=()=>{const ctx=originalContext();ctx.limits.document_bytes++;return ctx;};
    await assert.rejects(f.api.upload(new File([baseBytes],'材料.txt',{type:'text/plain'})),error=>error.code==='MEDIA_STALE');assert.equal(f.requests.length,0);
  });
  await check('document-read-requires-exact-disposition-blob-type-size-and-sha',async()=>{
    const media={...descriptor(),file_name:"材料 !'()*.txt"},f=fixture();
    f.fetch=async(url,request)=>{assert.equal(url,media.content_url);assert.equal(request.headers.Accept,media.mime_type);return readResponse(media);};
    const blob=await f.api.read(media);assert.equal(blob.type,media.mime_type);assert.equal(hash(Buffer.from(await blob.arrayBuffer())),media.sha256);assert.equal(f.contextReads,2);
    for(const options of [{headers:{'Content-Disposition':'inline'}},{headers:{'Content-Disposition':disposition('另一个.txt')}},
      {headers:{'Content-Disposition':"attachment; filename*=UTF-8''"+encodeURIComponent(media.file_name)}},
      {headers:{'Content-Type':'text/csv'}},{headers:{'Content-Length':String(media.bytes+1)}},
      {headers:{'X-Content-Type-Options':''}},{headers:{'Cache-Control':'public'}},{headers:{'Accept-Ranges':'bytes'}},
      {bytes:Buffer.alloc(media.bytes,65)},{bytes:Buffer.from('short')},{type:'text/csv'}]){
      const bad=fixture();bad.fetch=async()=>readResponse(media,options);
      await assert.rejects(bad.api.read(media),error=>error.code==='MEDIA_INTEGRITY');
    }
    for(const mime of ['image/png','audio/wav']){const f=fixture(),media=descriptor(mime);f.fetch=async()=>readResponse(media);await f.api.read(media);}
  });
  await check('document-read-identity-epoch-abort-and-clear-guards-remain-active',async()=>{
    for(const change of [f=>f.identity.session_generation++,f=>f.valid=false,f=>f.api.clear(),f=>f.contextEpoch++]){
      const f=fixture(),media=descriptor();f.fetch=async()=>readResponse(media,{onBlob:()=>change(f)});
      await assert.rejects(f.api.read(media),error=>error.code==='MEDIA_STALE');
    }
    const f=fixture(),controller=new AbortController();controller.abort();
    await assert.rejects(f.api.read(descriptor(),{signal:controller.signal}),error=>error.code==='MEDIA_ABORTED');assert.equal(f.requests.length,0);
  });
  await check('bind-keeps-document-descriptor-exact-and-bound-only',async()=>{
    const media=descriptor('text/plain','stored'),f=fixture();
    f.fetch=async(url,request)=>{
      assert.equal(url,'/api/finance/chat-media/bind');assert.deepEqual(JSON.parse(request.body),{request_id:'document-turn',media_ids:[media.media_id]});
      return {status:200,json:async()=>({contract:'navren.chat-media-bind/1',request_id:'document-turn',attachments:[{...media,storage_status:'bound'}],workspace_binding:f.identity.workspace_id,archive_epoch:0})};};
    const bound=await f.api.bind('document-turn',[media]);assert.equal(bound.attachments[0].file_name,media.file_name);assert.ok(Object.isFrozen(bound.attachments[0]));
    for(const change of [bound=>bound.file_name='另一个.txt',bound=>bound.text_chars++]){
      const f=fixture(),tampered={...media,storage_status:'bound'};change(tampered);
      f.fetch=async()=>({status:200,json:async()=>({contract:'navren.chat-media-bind/1',request_id:'document-turn',attachments:[tampered],workspace_binding:f.identity.workspace_id,archive_epoch:0})});
      await assert.rejects(f.api.bind('document-turn',[media]),error=>error.code==='INVALID_MEDIA_DESCRIPTOR');
    }
  });
  await check('archive-rejects-duplicate-media-extra-authority-and-unbound-history',async()=>{
    const media=descriptor();await assert.rejects(archiveRead([media,clone(media)]),error=>error.code==='INVALID_ARCHIVE_DTO');
    await assert.rejects(archiveRead(media,page=>page.execution_authority=true),error=>error.code==='INVALID_ARCHIVE_DTO');
    await assert.rejects(archiveRead(media,page=>page.items[0].user.attachments[0].current_fact_eligible=true),error=>error.code==='INVALID_ARCHIVE_DTO');
    await assert.rejects(archiveRead(media,page=>page.items[0].assistant.attachments=[media]),error=>error.code==='INVALID_ARCHIVE_DTO');
    await assert.rejects(archiveRead(media,page=>page.workspace_id='c'.repeat(32)),error=>error.code==='PAGE_BINDING_CHANGED');
  });
  console.log(JSON.stringify({schema:'navren.chat-document-browser-contract/1',passed:true,results,controlled_requests:interceptedRequests,
    actual_http_requests:0,actual_model_calls:0,real_account_writes:0,scope:'Node vm with controlled authenticated reader and fetch. Browser interaction and deployed service are not exercised.'},null,2));
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
