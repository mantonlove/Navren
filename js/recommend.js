/** 历史推荐入口适配：只浏览历史研究，不按产品名称连接银行交易。 */
let currentMode = 'classic';
document.addEventListener('DOMContentLoaded', () => {
  currentMode = getInterfaceMode();
  document.body.className = 'mode-' + currentMode;
  applyFontSize(getModeFont(currentMode));
});
function goTo(url) { NavrenApp.navigate(url, currentMode); }
function renderRecommendations() { window.DiscoverWorkspace?.refresh(); }
window.generateReport = () => window.DiscoverWorkspace?.openCurrentPlan(true);
window.showProductDetail = () => window.NavrenViews?.open('plans');
window.closeProductModal = () => window.NavrenViews?.back();
function sendToChat() { window.NavrenViews?.reference(null); document.querySelector('.navren-page-ask input,.navren-page-ask textarea')?.focus(); }
function showToast(message) {
  document.querySelector('.toast')?.remove();
  const toast = document.createElement('div'); toast.className = 'toast'; toast.role = 'status'; toast.textContent = message;
  document.body.append(toast); setTimeout(() => toast.remove(), 3000);
}
