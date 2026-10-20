/* 共用概念说明：安全DOM、点击/键盘、避让、关闭与焦点返回。 */
window.NavrenHelp=(()=>{
 const registry={market:['市场热点','左划看下一条，右划看上一条，也可使用箭头或键盘。颜色表示主题；日期是原始发布日期。只展示已核验公开内容，观点保持观点身份。完整事实、条件与出处在解读中；专业媒体原文可能需要订阅。'],surplus:['日常结余','本期已记录收入减净消费。借款、申赎本金、内部转账和还款本金单列；缺少收入证据时保持未知。'],wealth:['财富结构','同币种、同一时点的资产减负债。旧组合只有本金口径时，完整市场净资产保持未知。'],equity:['投资损益','期末权益减期初权益，再减所有者净投入。应收和投资现金的内部转换不重复算收益，净值日与到账日分别记录。'],allocation:['目标规划','本人确认的资金规划不会冻结或扣款。日历与目标关联同一义务时只计一次需求。'],graph:['图例与大小','同类型、同币种、同存量或流量口径分别按金额映射面积；最小节点只是便于点击。蓝资金、棕义务、绿目标、紫条件；实线已记录、虚线确认未来、点线待核实。'],calendar:['财务日历','收益、现金收支和生活事项分开。收益为红正、绿负、灰零；没有当日净值时不填零，隔多日净值变化标为区间。'],preferences:['偏好与认知','只有本人确认的条件才能参与规划。购买或阅读记录不证明偏好、理解或风险适当性；不生成认知评分。'],sources:['产品来源','管理人净值、产品条款与微众销售公告各有来源日期。历史销售关系不代表当前渠道仍可申购。']};
 let open=null,trigger=null;
 const detailsSelector='details.discover-research-help[open],details.insurance-help[open],details.mine-profile-help[open],details[data-help-popover][open]';
 function closeDetails(target,returnFocus=false){
  document.querySelectorAll(detailsSelector).forEach(help=>{
   if(target&&help.contains(target))return;
   const focused=returnFocus&&help.contains(document.activeElement);
   help.open=false;
   if(focused)help.querySelector('summary')?.focus();
  });
 }
 function close(){trigger?.setAttribute('aria-expanded','false');open?.remove();open=null;const prior=trigger;trigger=null;prior?.focus();}
 function button(key){const record=registry[key];if(!record)throw Error('未登记的说明');const b=document.createElement('button');b.type='button';b.className='navren-help';b.textContent='?';b.setAttribute('aria-label',record[0]+'说明');b.setAttribute('aria-expanded','false');b.onclick=()=>{if(trigger===b){close();b.setAttribute('aria-expanded','false');return;}if(trigger)trigger.setAttribute('aria-expanded','false');close();trigger=b;b.setAttribute('aria-expanded','true');const panel=document.createElement('section');panel.className='navren-help-popover';panel.role='dialog';panel.setAttribute('aria-label',record[0]);const title=document.createElement('strong');title.textContent=record[0];const text=document.createElement('p');text.textContent=record[1];const end=document.createElement('button');end.type='button';end.textContent='关闭';end.onclick=()=>{b.setAttribute('aria-expanded','false');close();};panel.append(title,text,end);document.body.append(panel);open=panel;const r=b.getBoundingClientRect(),w=Math.min(310,innerWidth-24);panel.style.width=w+'px';panel.style.left=Math.max(12,Math.min(r.right-w,innerWidth-w-12))+'px';panel.style.top=Math.max(12,Math.min(r.bottom+8,innerHeight-panel.offsetHeight-12))+'px';end.focus();};return b;}
 document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;closeDetails(null,true);if(open)close();});
 document.addEventListener('pointerdown',e=>{closeDetails(e.target);if(open&&!open.contains(e.target)&&!trigger?.contains(e.target))close();},true);
 return {button,registry,close};
})();
