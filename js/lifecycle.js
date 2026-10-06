/* 旧入口适配保留，实际组织由各页面的完整子视图控制器负责。 */
window.NavrenWorkspace={activate:(id)=>window.NavrenViews?.open(id)};
document.addEventListener('DOMContentLoaded',()=>{if(['account','mine','recommend'].includes(document.body.dataset.page))NavrenViews.start();});
