/* 聊天附件草稿：剪贴板与文件选择共用队列，发送前只在本页预览。 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NavrenChatAttachments = api;
})(typeof window === 'object' ? window : globalThis, function (root) {
  'use strict';
  const IMAGE_TYPES = new Set(['image/png','image/jpeg','image/webp','image/gif']);
  const DOCUMENT_TYPES = Object.freeze({pdf:'application/pdf',docx:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    xlsx:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',pptx:'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    txt:'text/plain',md:'text/markdown',csv:'text/csv'});
  const MAX_FILE = 10485760, MAX_TOTAL = 26214400, MAX_FILES = 5;
  const english = () => root.NavrenI18n?.language === 'en';
  const words = (zh,en) => english() ? en : zh;
  const ui = (tag, className, text) => {const node=root.document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;};
  function normalizedFile(file) {
    if (!(file instanceof root.Blob) || !Number.isSafeInteger(file.size) || file.size < 1 || file.size > MAX_FILE) {
      throw Error(words('每个附件须为非空文件，且不超过 10 MB。','Each file must be non-empty and no larger than 10 MB.'));
    }
    const name=typeof file.name==='string'?file.name:'image.png';
    const type=(file.type||'').split(';')[0].toLowerCase();
    if (IMAGE_TYPES.has(type)) return file;
    const extension=name.split('.').pop().toLowerCase(), expected=DOCUMENT_TYPES[extension];
    if (!expected || (type && !['application/octet-stream',expected].includes(type) && !(extension==='md'&&type==='text/plain'))) {
      throw Error(words('支持 PNG、JPEG、WebP、GIF、PDF、DOCX、XLSX、PPTX、TXT、MD 和 CSV。','Supported: PNG, JPEG, WebP, GIF, PDF, DOCX, XLSX, PPTX, TXT, MD and CSV.'));
    }
    return type===expected?file:new root.File([file],name,{type:expected,lastModified:file.lastModified||Date.now()});
  }
  function clipboardFiles(data) {
    if (!data) return [];
    const files=Array.from(data.files||[]);
    if (files.length) return files;
    return Array.from(data.items||[]).filter(item=>item.kind==='file').map(item=>item.getAsFile()).filter(Boolean);
  }
  function sizeLabel(bytes) {return bytes<1048576?Math.max(1,Math.ceil(bytes/1024))+' KB':(bytes/1048576).toFixed(1)+' MB';}
  let staged=[], host=null, onChange=()=>{}, notify=()=>{}, revision=0;
  const draftUrls=new Map(), liveUrls=new Set();
  function urlFor(file) {let url=draftUrls.get(file);if(!url){url=root.URL.createObjectURL(file);draftUrls.set(file,url);}return url;}
  function disposeDraft(file) {const url=draftUrls.get(file);if(url){root.URL.revokeObjectURL(url);draftUrls.delete(file);}}
  function changed() {revision++;render();onChange();}
  function fileIdentity(file) {return IMAGE_TYPES.has(file.type)?words('图片','Image'):String(file.name||'').split('.').pop().toUpperCase();}
  function fileInfo(file, warning) {
    const content=ui('span','chat-file-info');
    const name=ui('strong','chat-file-name',file.name||words('剪贴板图片','Clipboard image'));name.title=name.textContent;
    content.append(name,ui('span','chat-file-size',fileIdentity(file)+' · '+sizeLabel(file.size)));
    if(warning)content.append(ui('span','chat-file-note',warning));
    return content;
  }
  function render() {
    if(!host)return;
    host.replaceChildren();host.hidden=staged.length===0;
    for(const file of staged){
      const chip=ui('div','chat-file-chip');
      if(IMAGE_TYPES.has(file.type)){const image=ui('img','chat-file-thumb');image.src=urlFor(file);image.alt='';chip.append(image);}
      else chip.append(ui('span','chat-file-badge',fileIdentity(file)));
      chip.append(fileInfo(file));
      const remove=ui('button','chat-file-remove','×');remove.type='button';remove.setAttribute('aria-label',words('移除附件：','Remove attachment: ')+(file.name||'image'));
      remove.onclick=()=>{staged=staged.filter(item=>item!==file);disposeDraft(file);changed();};chip.append(remove);host.append(chip);
    }
  }
  function addFiles(raw) {
    let guard;
    try{guard=root.BankClient?.guard?.();if(guard&&!guard.valid())return false;}catch(_){return false;}
    let incoming;
    try{incoming=Array.from(raw).map(normalizedFile);
      if(staged.length+incoming.length>MAX_FILES)throw Error(words('每条消息最多添加 5 个附件。','Attach up to 5 files per message.'));
      if([...staged,...incoming].reduce((n,file)=>n+file.size,0)>MAX_TOTAL)throw Error(words('每条消息的附件合计不能超过 25 MB。','Attachments must total no more than 25 MB per message.'));
    }catch(error){notify(error.message);return false;}
    if(!incoming.length)return false;
    staged.push(...incoming);changed();return true;
  }
  function clear() {for(const file of staged)disposeDraft(file);staged=[];changed();}
  function consume(files) {const sent=new Set(files);for(const file of staged)if(sent.has(file))disposeDraft(file);staged=staged.filter(file=>!sent.has(file));changed();}
  function dispose() {clear();for(const url of liveUrls)root.URL.revokeObjectURL(url);liveUrls.clear();}
  function onPaste(event) {
    const files=clipboardFiles(event.clipboardData);
    if(files.length){event.preventDefault();addFiles(files);return;}
    // 部分操作系统只向网页给出文件路径，不能据路径读取用户磁盘。
    const uri=event.clipboardData?.getData('text/uri-list')||'';
    if(/^file:\/\//m.test(uri)){event.preventDefault();notify(words('浏览器未取得文件原件，请使用＋选择附件。','The browser did not provide the file. Use + to choose it.'));}
  }
  function init(settings={}) {
    host=root.document.getElementById('chatAttachmentPreview');onChange=settings.onChange||(()=>{});notify=settings.notify||(()=>{});
    const area=root.document.querySelector('.chat-input-area');
    if(area&&!area.dataset.attachmentsReady){area.dataset.attachmentsReady='true';area.addEventListener('paste',onPaste);}
    render();
  }
  function mountFiles(message, files, media=[]) {
    const body=message?.querySelector('.message-body')||message?.querySelector('.bubble');if(!body||!files.length)return;
    const row=ui('div','chat-sent-files');
    for(const [index,file] of files.entries()){
      if(IMAGE_TYPES.has(file.type)){
        const image=ui('img','chat-sent-image');const url=root.URL.createObjectURL(file);liveUrls.add(url);
        image.onload=image.onerror=()=>{if(liveUrls.delete(url))root.URL.revokeObjectURL(url);};
        image.src=url;image.alt=file.name||words('聊天图片','Chat image');row.append(image);
      }else{const item=ui('div','chat-sent-document');item.append(ui('span','chat-file-badge',fileIdentity(file)),fileInfo(file,media[index]?.extraction_status==='no_text'?media[index].extraction_warning:null));row.append(item);}
    }
    body.prepend(row);
  }
  function mountStoredDocument(body, media, isCurrent) {
    const button=ui('button','chat-sent-document');button.type='button';button.title=words('下载原附件','Download original attachment');
    const file={name:media.file_name,type:media.mime_type,size:media.bytes};
    button.append(ui('span','chat-file-badge',media.document_format.toUpperCase()),fileInfo(file,media.extraction_status==='no_text'?media.extraction_warning:null),ui('span','chat-file-download','↓'));
    if(media.extraction_status==='no_text')button.title+=' · '+media.extraction_warning;
    button.onclick=async()=>{if(!isCurrent())return;button.disabled=true;let handle;
      try{handle=await root.NavrenChatMedia.open(media);if(!isCurrent()){handle.dispose();return;}
        const link=ui('a','');link.href=handle.url;link.download=media.file_name;link.hidden=true;root.document.body.append(link);link.click();link.remove();root.setTimeout(()=>handle.dispose(),1000);
      }catch(error){if(isCurrent())notify(error.message||words('附件暂时无法读取。','The attachment is unavailable.'));}
      finally{if(isCurrent())button.disabled=false;}
    };
    body.prepend(button);
  }
  if(root.addEventListener)for(const name of ['navren:privacy-reset','navren:identity-revoked','navren:account-change','navren:account-switched','pagehide'])root.addEventListener(name,dispose);
  return Object.freeze({init,addFiles,clear,consume,files:()=>staged.slice(),matches:files=>files.length===staged.length&&files.every((file,i)=>file===staged[i]),
    mountFiles,mountStoredDocument,render,clipboardFiles,normalizedFile,limits:Object.freeze({maxFiles:MAX_FILES,maxFile:MAX_FILE,maxTotal:MAX_TOTAL})});
});
