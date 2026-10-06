/* 连续封面流。导航只改变阅读位置，不请求网络、不写用户资料。 */
window.MarketDeck=class MarketDeck {
  static t(text){return window.NavrenI18n?.t(text)||text;}
  static modulo(value,count){return ((value%count)+count)%count;}
  static relative(index,position,count){const delta=index-position;return count>=3?delta-count*Math.floor((delta+count/2)/count):delta;}
  static layers(relative){const t=Math.max(0,Math.min(1,(Math.abs(relative)-.65)/.30)),preview=t*t*(3-2*t);return {full:1-preview,preview};}
  static pose(relative,count,{spacing,compact=false,senior=false,reduced=false}){
    const a=Math.abs(relative),near=Math.min(a,1),fadeEnd=count>=3?Math.min(2,count/2):2;
    return {x:relative*spacing,y:reduced?0:10*near,scale:1-(compact?.14:.18)*near,
      angle:reduced?0:-Math.sign(relative)*(senior?8:compact?12:22)*near,
      z:reduced?0:-(compact?60:100)*Math.min(a,1.5),opacity:a<=1?1:Math.max(0,1-(a-1)/(fadeEnd-1))};
  }
  constructor(root,{events=[],renderCard,theme,historyEntry}={}){
    this.root=root;this.renderCard=renderCard;this.theme=theme;this.events=[];this.cards=new Map();this.index=0;this.virtualPosition=0;this.state='idle';this.generation=0;this.direction=1;this.destroyed=false;
    this.abort=new AbortController();this.motion=matchMedia('(prefers-reduced-motion: reduce)');
    root.classList.add('market-deck');root.setAttribute('role','region');root.setAttribute('aria-roledescription',MarketDeck.t('热点封面流'));root.setAttribute('aria-label',MarketDeck.t('市场热点'));root.tabIndex=0;
    this.stage=document.createElement('div');this.stage.className='market-deck-stage';this.stage.id='market-flow-stage-'+(++MarketDeck.sequence);
    this.controls=document.createElement('div');this.controls.className='market-deck-controls';
    this.previous=this.nav('‹','上一条热点',-1);this.next=this.nav('›','下一条热点',1);
    this.counter=document.createElement('span');this.counter.className='market-deck-counter';this.counter.setAttribute('aria-hidden','true');
    this.announcement=document.createElement('span');this.announcement.className='market-flow-announcement';this.announcement.setAttribute('aria-live','polite');this.announcement.setAttribute('aria-atomic','true');
    this.navigation=document.createElement('div');this.navigation.className='market-deck-navigation';this.navigation.append(this.previous,this.counter,this.next);
    this.controls.append(this.navigation);if(historyEntry)this.controls.append(historyEntry());root.replaceChildren(this.stage,this.controls,this.announcement);
    const on=(node,type,fn,options={})=>node.addEventListener(type,fn,{...options,signal:this.abort.signal});
    on(this.stage,'pointerdown',e=>this.down(e));on(this.stage,'pointermove',e=>this.move(e));on(this.stage,'pointerup',e=>this.up(e));
    on(this.stage,'pointercancel',()=>this.reset());on(this.stage,'lostpointercapture',e=>{if(e.target===this.stage&&this.pointer?.id===e.pointerId)this.reset();});
    on(this.stage,'click',e=>{if(e.detail>0&&(this.state==='settling'||performance.now()<this.suppressUntil)){e.preventDefault();e.stopImmediatePropagation();}},{capture:true});
    on(this.stage,'dragstart',e=>{if(!e.shiftKey)e.preventDefault();});
    on(root,'keydown',e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key)||e.shiftKey||getSelection()?.toString()||this.interactive(e.target)&&!e.target.closest('.market-flow-select,.market-deck-nav'))return;e.preventDefault();this.go(e.key==='ArrowRight'?1:-1);});
    on(window,'pointerdown',e=>{if(e.pointerType==='touch'&&!e.isPrimary&&this.pointer)this.reset();});
    on(window,'pointermove',e=>{this.trackAction(e);if(!this.stage.contains(e.target))this.move(e);});
    on(window,'pointerup',e=>{this.finishAction(e);if(this.pointer?.id===e.pointerId)this.up(e);});
    on(window,'pointercancel',e=>{if(this.pointer?.id===e.pointerId||this.actionPointer?.id===e.pointerId)this.reset();});
    on(window,'blur',()=>this.reset());on(window,'resize',()=>{this.reset();this.measure();});
    on(window,'navren:language',()=>{this.previous.setAttribute('aria-label',MarketDeck.t('上一条热点'));this.next.setAttribute('aria-label',MarketDeck.t('下一条热点'));root.setAttribute('aria-roledescription',MarketDeck.t('热点封面流'));root.setAttribute('aria-label',MarketDeck.t('市场热点'));this.commit();});
    on(window,'pagehide',e=>e.persisted?this.reset():this.destroy());on(this.motion,'change',()=>{this.reset();this.measure();});
    this.observer=new ResizeObserver(entries=>{const width=entries[0]?.contentRect.width;if(this.lastWidth===undefined||Math.abs(width-this.lastWidth)>.5){this.reset();this.measure();}});this.observer.observe(root);
    this.fontObserver=new MutationObserver(()=>{this.reset();this.measure();});this.fontObserver.observe(document.documentElement,{attributes:true,attributeFilter:['style']});this.fontObserver.observe(document.body,{attributes:true,attributeFilter:['class']});
    this.update(events);document.fonts?.ready.then(()=>{if(!this.destroyed)this.measure();});
  }
  nav(text,label,direction){const b=document.createElement('button');b.type='button';b.className='market-deck-nav';b.textContent=text;b.setAttribute('aria-label',MarketDeck.t(label));b.setAttribute('aria-controls',this.stage.id);b.onclick=()=>this.go(direction);return b;}
  interactive(node){return !!node.closest('a,button,input,textarea,select,summary,[contenteditable="true"]');}
  focusToken(){const n=document.activeElement;if(!this.root.contains(n))return null;return {node:n,key:n.dataset.deckFocus,side:!!n.closest('.market-flow-select')};}
  restoreFocus(token){if(!token)return;let target;if(token.key){target=this.front?.querySelector('[data-deck-focus="'+token.key+'"]');}else if(token.side){target=this.front?.querySelector('[data-deck-focus="explanation"]');}else if(token.node?.isConnected&&!token.node.disabled&&!token.node.hidden){target=token.node;} (target||this.root).focus({preventScroll:true});}
  createCard(event){
    const node=document.createElement('div');node.className='market-flow-slot';node.dataset.eventId=event.id;
    const card=this.renderCard(event);node.append(card);
    const select=document.createElement('button');select.type='button';select.className='market-flow-select';select.setAttribute('aria-label','切换到：'+(event.display_title||event.headline));select.onclick=()=>this.goTo(event.id);node.append(select);
    this.stage.append(node);return {node,card,full:card.querySelector('.market-flow-full')||card,select,signature:this.signature(event)};
  }
  signature(event){return JSON.stringify([event.version,event.content_version,event.display_version,event.topic_id,event.theme,event.display_title,event.display_summary,event.display_conditions,event.display_fact,event.display_title_en,event.display_summary_en,event.display_conditions_en,event.headline,event.availability,event.archive,event.articles,window.NavrenI18n?.language]);}
  update(events){
    if(this.destroyed)return;const id=this.events[this.index]?.id,previousIndex=this.index,focus=this.focusToken()||this.settleFocus;this.reset();
    const seen=new Set();this.events=events.filter(e=>e.id&&!seen.has(e.id)&&seen.add(e.id));
    for(const [id,record] of this.cards)if(!seen.has(id)){record.node.remove();this.cards.delete(id);}
    this.stage.querySelector('.market-deck-empty')?.remove();
    for(const event of this.events){let record=this.cards.get(event.id);if(!record){record=this.createCard(event);this.cards.set(event.id,record);}else if(record.signature!==this.signature(event)){const card=this.renderCard(event);record.card.replaceWith(card);record.card=card;record.full=card.querySelector('.market-flow-full')||card;record.signature=this.signature(event);record.select.setAttribute('aria-label','切换到：'+(event.display_title||event.headline));}}
    const found=this.events.findIndex(e=>e.id===id);this.index=found>=0?found:Math.min(previousIndex,Math.max(0,this.events.length-1));this.virtualPosition=this.index;
    if(!this.events.length){const empty=document.createElement('p');empty.className='market-deck-empty';empty.textContent=MarketDeck.t('暂无通过审核的近期热点。');this.stage.append(empty);}
    this.measure();this.commit();this.restoreFocus(focus);
  }
  measure(){
    if(this.destroyed)return;const width=this.root.clientWidth;if(!width)return;this.lastWidth=width;this.compact=width<760;this.senior=document.body.classList.contains('mode-senior');
    this.cardWidth=this.compact?width*.74:Math.min(620,width*.64);this.spacing=this.cardWidth*.60;
    this.root.dataset.compact=this.compact;this.root.dataset.narrow=this.cardWidth<530;
    this.root.style.setProperty('--flow-card-width',this.cardWidth+'px');
    const scale=this.compact?.86:.82,depth=this.compact?60:100,perspective=this.compact?1000:1200,angle=this.senior?8:this.compact?12:22;
    const projection=perspective/(perspective+depth),inset=Math.max(18,this.cardWidth/2-(width/2-18-this.spacing*projection)/(scale*projection*Math.cos(angle*Math.PI/180)))+(this.compact?0:32);
    this.root.style.setProperty('--flow-preview-inset',inset+'px');
    let height=0;for(const c of this.cards.values()){c.node.style.height='auto';c.card.style.height='auto';c.full.style.height='auto';height=Math.max(height,c.full.offsetHeight);}
    for(const c of this.cards.values()){c.node.style.height=height+'px';c.card.style.height='100%';c.full.style.height='100%';}
    this.stage.style.height=(this.events.length?height+42:180)+'px';this.draw(true);
  }
  draw(clearWrap=false){
    if(this.destroyed)return;const count=this.events.length,now=performance.now();let wrapping=false;
    this.root.dataset.state=this.state;this.root.dataset.position=this.virtualPosition.toFixed(4);
    this.root.classList.toggle('is-dragging',this.state==='dragging');this.root.classList.toggle('is-moving',this.state==='dragging'||this.state==='settling');
    this.events.forEach((event,i)=>{
      const c=this.cards.get(event.id),r=MarketDeck.relative(i,this.virtualPosition,count),a=Math.abs(r),active=i===this.index,idle=this.state==='idle';
      if(clearWrap){c.lastRelative=r;c.wrapAt=0;}
      // 离散帧可能跳过取模边界。换边先保持至少两个刷新帧透明，再渐显。
      if(count>=3&&c.lastRelative!==undefined&&Math.abs(r-c.lastRelative)>count/2)c.wrapAt=now;
      c.lastRelative=r;
      const wrapAge=c.wrapAt?now-c.wrapAt:Infinity,wrapOpacity=wrapAge<34?0:Math.min(1,(wrapAge-34)/60);
      const fadeEnd=Math.min(2,count/2),renderedRelative=wrapOpacity<1?r+(Math.sign(r)*fadeEnd-r)*(1-wrapOpacity):r;
      const pose=MarketDeck.pose(renderedRelative,count,{spacing:this.spacing,compact:this.compact,senior:this.senior,reduced:this.motion.matches}),layers=MarketDeck.layers(renderedRelative);
      pose.opacity*=wrapOpacity;if(wrapAge<94)wrapping=true;
      const visible=pose.opacity>.0001,side=!active&&visible&&a<=1.001;
      c.node.style.transform=`translate3d(${pose.x}px,${pose.y}px,${pose.z}px) rotateY(${pose.angle}deg) scale(${pose.scale})`;
      c.node.style.opacity=pose.opacity;c.node.style.zIndex=Math.round((3-a)*100)+(Math.sign(r)===this.direction?1:0);
      c.node.style.setProperty('--flow-full-opacity',layers.full.toFixed(4));
      c.node.style.setProperty('--flow-preview-opacity',layers.preview.toFixed(4));
      c.node.dataset.wrapHidden=wrapOpacity===0;
      c.node.dataset.renderedRelative=renderedRelative.toFixed(4);
      c.node.dataset.relative=r.toFixed(4);c.node.dataset.role=a<.001?'center':r<0?'previous':'next';
      c.node.inert=!visible;c.node.setAttribute('aria-hidden',!visible);c.node.style.pointerEvents=visible?'':'none';
      c.full.inert=!active||!idle;c.full.setAttribute('aria-hidden',!active);c.select.hidden=!side||!idle;c.select.disabled=!side||!idle;
      c.card.classList.toggle('market-deck-front',active);c.node.classList.toggle('is-center',active);
    });
    if(wrapping&&this.state==='dragging')this.queueDraw();
  }
  commit(){
    const count=this.events.length;this.front=count?this.cards.get(this.events[this.index].id).card:null;
    this.previous.hidden=this.next.hidden=count<2;this.previous.disabled=count<2||(count===2&&this.index===0);this.next.disabled=count<2||(count===2&&this.index===1);
    this.counter.textContent=count?(this.index+1)+' / '+count:'0 / 0';this.announcement.textContent=count?MarketDeck.t('第 '+(this.index+1)+' 条，共 '+count+' 条')+'：'+(this.events[this.index].display_title||this.events[this.index].headline):MarketDeck.t('暂无已核验热点');
    this.root.dataset.index=this.index;this.root.dataset.eventId=this.events[this.index]?.id||'';this.draw(true);
    if(this.state==='idle')queueMicrotask(()=>{if(!this.destroyed)window.dispatchEvent(new Event('navren:market-deck-idle'));});
  }
  queueDraw(){if(this.drawFrame)return;this.drawFrame=requestAnimationFrame(()=>{this.drawFrame=0;this.draw();});}
  go(direction){
    direction=Math.sign(direction);if(!direction||this.events.length<2||this.destroyed)return;
    if(this.state==='settling'){this.queuedDirection=direction;return;}
    if(this.state!=='idle')return;this.direction=direction;const target=this.index+direction;
    this.settle(this.events.length===2&&(target<0||target>1)?this.index:this.virtualPosition+direction);
  }
  goTo(id){const i=this.events.findIndex(e=>e.id===id);if(i<0||this.destroyed)return false;if(i===this.index)return true;
    const delta=MarketDeck.relative(i,this.index,this.events.length);
    if(Math.abs(delta)<=1){this.go(Math.sign(delta));return true;}
    const focus=this.focusToken();this.reset();this.index=i;this.virtualPosition=i;this.commit();this.restoreFocus(focus);return true;
  }
  restore(id){const i=this.events.findIndex(e=>e.id===id);if(i<0||this.destroyed)return false;this.reset();this.index=i;this.virtualPosition=i;this.commit();return true;}
  settle(target){
    const focus=this.focusToken(),from=this.virtualPosition,token=++this.generation,commit=Math.abs(target-this.index)>.5;
    cancelAnimationFrame(this.drawFrame);this.drawFrame=0;this.settleFocus=focus;
    this.state='settling';this.direction=Math.sign(target-from)||this.direction;this.draw();
    const duration=this.motion.matches?Math.min(80,commit?80:60):commit?270:190,start=performance.now();
    const tick=now=>{
      if(token!==this.generation||this.destroyed)return;
      const t=Math.min(1,(now-start)/duration),progress=1-Math.pow(1-t,4);
      this.virtualPosition=from+(target-from)*progress;this.draw();
      if(t<1){this.animationFrame=requestAnimationFrame(tick);return;}
      this.animationFrame=0;this.index=MarketDeck.modulo(Math.round(target),this.events.length);this.virtualPosition=this.index;this.state='idle';this.settleFocus=null;this.commit();this.restoreFocus(focus);
      const queued=this.queuedDirection;this.queuedDirection=0;if(queued)this.go(queued);
    };this.animationFrame=requestAnimationFrame(tick);
  }
  down(e){
    if(this.state!=='idle'||e.button!==0||!e.isPrimary)return;this.suppressUntil=0;
    if(this.interactive(e.target)&&!e.target.closest('.market-flow-select')){this.actionPointer={id:e.pointerId,x:e.clientX,y:e.clientY,moved:false};return;}
    if(this.events.length<2||e.shiftKey||e.detail>=2||getSelection()?.toString())return;
    this.state='pending';this.pointer={id:e.pointerId,x:e.clientX,y:e.clientY,position:this.index,samples:[{x:e.clientX,t:e.timeStamp}]};
    const id=e.pointerId;this.selectTimer=setTimeout(()=>{if(this.state==='pending'&&this.pointer?.id===id)this.reset();},450);
  }
  move(e){
    const p=this.pointer;if(!p||p.id!==e.pointerId||!['pending','dragging'].includes(this.state))return;
    if(e.pointerType==='mouse'&&e.buttons===0){this.reset();return;}
    const dx=e.clientX-p.x,dy=e.clientY-p.y;
    if(this.state==='pending'){
      if(Math.abs(dy)>14&&Math.abs(dy)>=Math.abs(dx)/1.35){this.reset();return;}
      if(Math.abs(dx)<14||Math.abs(dx)<Math.abs(dy)*1.35)return;
      clearTimeout(this.selectTimer);this.state='dragging';this.stage.setPointerCapture(e.pointerId);getSelection()?.removeAllRanges();
    }
    e.preventDefault();p.samples.push({x:e.clientX,t:e.timeStamp});p.samples=p.samples.filter(s=>e.timeStamp-s.t<=100);
    const progress=Math.max(-1,Math.min(1,-dx/this.spacing));const edge=this.events.length===2&&(p.position+progress<0||p.position+progress>1);
    this.direction=progress<0?-1:1;this.virtualPosition=p.position+(edge?progress*.18:progress);this.queueDraw();
  }
  trackAction(e){const p=this.actionPointer;if(p?.id===e.pointerId&&(Math.abs(e.clientX-p.x)>12||Math.abs(e.clientY-p.y)>12))p.moved=true;}
  finishAction(e){const p=this.actionPointer;if(p?.id!==e.pointerId)return;this.trackAction(e);this.actionPointer=null;if(p.moved)this.suppressUntil=performance.now()+600;}
  up(e){
    this.finishAction(e);const p=this.pointer;if(!p||p.id!==e.pointerId)return;
    if(this.state!=='dragging'){this.reset();return;}
    this.suppressUntil=performance.now()+600;clearTimeout(this.selectTimer);
    const last={x:e.clientX,t:e.timeStamp};p.samples.push(last);p.samples=p.samples.filter(s=>last.t-s.t<=100);const first=p.samples[0],dt=last.t-first.t,velocity=dt>=30?(last.x-first.x)/dt:0;
    const dx=e.clientX-p.x,commit=Math.abs(dx)>=this.spacing*.28||(Math.abs(dx)>=30&&Math.abs(velocity)>=.45&&Math.sign(velocity)===Math.sign(dx));
    let target=p.position+(commit?(dx<0?1:-1):0);if(this.events.length===2)target=Math.max(0,Math.min(1,target));
    this.pointer=null;this.state='idle';this.release(p.id);this.settle(target);
  }
  release(id){if(this.stage.hasPointerCapture(id))this.stage.releasePointerCapture(id);}
  reset(){
    ++this.generation;clearTimeout(this.selectTimer);cancelAnimationFrame(this.animationFrame);cancelAnimationFrame(this.drawFrame);this.animationFrame=this.drawFrame=0;this.queuedDirection=0;
    const p=this.pointer,focus=this.settleFocus;this.settleFocus=null;this.pointer=this.actionPointer=null;this.state='idle';if(p)this.release(p.id);this.virtualPosition=this.index;this.draw(true);this.restoreFocus(focus);
    queueMicrotask(()=>{if(!this.destroyed)window.dispatchEvent(new Event('navren:market-deck-idle'));});
  }
  destroy(){if(this.destroyed)return;this.reset();this.destroyed=true;this.abort.abort();this.observer.disconnect();this.fontObserver.disconnect();this.previous.onclick=this.next.onclick=null;for(const c of this.cards.values())c.select.onclick=null;}
};
window.MarketDeck.sequence=0;
