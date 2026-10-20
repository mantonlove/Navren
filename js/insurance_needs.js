/* 保障方向仅取本人选择、已确认需求和当前主体档案；不做核保、缺口或购买资格判断。 */
window.NavrenInsuranceNeeds = (() => {
  'use strict';
  const categories = Object.freeze(['health', 'property', 'agriculture', 'equipment', 'liability', 'pension', 'accident']);
  const labels = Object.freeze({ health: '健康医疗', property: '房产保障', agriculture: '农业保障', equipment: '设备保障', liability: '经营责任', pension: '养老规划', accident: '意外保障' });
  const recordOrder = ['agriculture', 'equipment', 'liability', 'property', 'health', 'accident'];
  const policyKinds = Object.freeze({ health: 'health', medical: 'health', critical_illness: 'health', property: 'property', shop_property: 'property', agriculture: 'agriculture', crop: 'agriculture', equipment: 'equipment', public_liability: 'liability', employer_liability: 'liability', professional_liability: 'liability', accident: 'accident', personal_accident: 'accident' });
  const assetKinds = Object.freeze({ property: 'property', crop: 'agriculture', crop_exposure: 'agriculture', agriculture: 'agriculture', equipment: 'equipment' });
  const needTerms = Object.freeze({
    health: /医疗(?:险|保险|保障)|健康(?:险|保险|保障)|重疾(?:险|保险|保障)|疾病保障|\b(?:medical insurance|health insurance|critical illness cover)\b/i,
    property: /房产保障|住房保障|家财险|房屋(?:保险|保障)|物业(?:保险|保障)|出租房(?:保险|保障)|\b(?:home|property|landlord) insurance\b/i,
    agriculture: /农业(?:保险|保障)|农作物(?:保险|保障)|作物(?:保险|保障)|种植(?:保险|保障)|\b(?:crop|agricultural) insurance\b/i,
    equipment: /农机(?:保险|保障)|设备(?:保险|保障)|机械(?:保险|保障)|\b(?:equipment|machinery) insurance\b/i,
    liability: /公众责任|雇主责任|食品安全责任|经营责任|营业责任|\b(?:public|employer|business|professional) liability\b/i,
    pension: /养老(?:保险|保障|规划|储备)|养老金(?:规划|储备)|商业养老金|退休(?:规划|保障)|年金险|\b(?:pension planning|annuity|retirement planning)\b/i,
    accident: /意外(?:险|保险|保障)|意外伤害|\b(?:accident insurance|accident cover)\b/i
  });
  const string = (value, max = 512) => typeof value === 'string' && value.trim().length > 0 && value.length <= max;
  const day = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value + 'T00:00:00Z')) && new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
  const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  function invalid(code, message) { const error = new Error(message); error.code = code; throw error; }
  function index(rows, idKey, subject) {
    if (!Array.isArray(rows) || rows.length > 200) invalid('INVALID_OVERVIEW', '保障档案未完整取得。');
    const result = new Map();
    for (const row of rows) {
      if (!record(row) || row.subject_id !== subject || !string(row[idKey], 160)) invalid('UNBOUND_RECORD', '保障对象与当前账户无法核对。');
      if (result.has(row[idKey])) invalid('DUPLICATE_RECORD', '保障档案存在重复对象，请重新读取。');
      for (const [key, value] of Object.entries(row)) if (key.endsWith('_minor') && value != null && (!Number.isSafeInteger(value) || value < 0)) invalid('INVALID_AMOUNT', '保障金额无法核对。');
      result.set(row[idKey], row);
    }
    return result;
  }
  function validate(identity, overview) {
    if (!record(identity) || !string(identity.workspace_id, 160) || !string(identity.account_id, 160) || !Number.isSafeInteger(identity.privacy_revision) || identity.privacy_revision < 0) invalid('IDENTITY_UNAVAILABLE', '当前账户身份尚未取得。');
    if (!record(overview)) invalid('DATA_UNAVAILABLE', '保障档案暂未取得。');
    if (overview.schema_version !== 'navren.financial.v1' || overview.source !== 'navren_synthetic' || overview.read_only !== true || overview.runtime_ledger_included !== false || overview.workspace_binding !== identity.workspace_id || overview.subject_id !== identity.account_id || overview.privacy_revision !== identity.privacy_revision || (overview.workspace_id != null && overview.workspace_id !== identity.workspace_id)) invalid('UNBOUND_OVERVIEW', '保障档案与当前账户或授权不一致。');
    if (!string(overview.dataset_version) || !day(overview.as_of) || !record(overview.data)) invalid('INVALID_VERSION', '保障档案的版本或日期缺失，请重新读取。');
    const data = overview.data, subject = identity.account_id;
    const policies = index(data.policies, 'policy_id', subject), assets = index(data.assets, 'asset_id', subject);
    const contracts = index(data.contracts, 'contract_id', subject), connections = index(data.connections, 'connection_id', subject);
    for (const policy of policies.values()) {
      if (!string(policy.insurance_type, 80) || !string(policy.status, 80) || !day(policy.start_date) || !day(policy.end_date) || policy.start_date > policy.end_date) invalid('INVALID_POLICY', '保单类型或保障期间无法核对。');
      const contract = contracts.get(policy.contract_id), connection = connections.get(policy.insurer_connection_id);
      if (!contract || !connection || connection.institution_type !== 'insurance' || (policy.covered_asset_id != null && !assets.has(policy.covered_asset_id)) || (contract.asset_id ?? null) !== (policy.covered_asset_id ?? null)) invalid('BROKEN_ASSOCIATION', '保单、承保机构与保障对象关联缺失。');
      for (const ownerKey of ['policyholder_id', 'insured_id']) if (policy[ownerKey] != null && policy[ownerKey] !== subject) invalid('UNBOUND_RECORD', '保单归属与当前账户无法核对。');
    }
    return { policies, assets, contracts, connections };
  }
  function personalProduct(product) {
    return record(product) && product.source_scope === 'public_catalog' && string(product.id, 160) && string(product.version) && record(product.raw) && product.raw.type === '保险' && ['符合资质的个人投资者', '个人', '个人客户', '个人投资者'].includes(String(product.raw.customer_group_type || '').trim());
  }
  function productCategory(product) {
    if (!personalProduct(product)) return null;
    // 仅给名称明确的个人公开资料归类，名称中的“守护”等不能证明具体保障责任。
    const name = String(product.name || product.raw.name || '');
    const found = [];
    if (/医疗险|医疗保险|中高端医疗|健康险|健康保险|重大疾病保险|重疾险/.test(name)) found.push('health');
    if (/商业养老金|年金险|年金保险|养老保险|养老年金/.test(name)) found.push('pension');
    if (/意外险|意外伤害保险|意外保险/.test(name)) found.push('accident');
    return found.length === 1 ? found[0] : null;
  }
  function confirmedNeeds(identity, overview, memories) {
    const result = new Map();
    if (memories == null) return result;
    if (!record(memories) || memories.workspace_id !== identity.workspace_id || memories.subject_id !== identity.account_id || memories.privacy_revision !== identity.privacy_revision || typeof memories.enabled !== 'boolean') invalid('UNBOUND_MEMORY', '本人需求资料与当前账户无法核对。');
    if (!memories.enabled) return result;
    if (!Array.isArray(memories.items) || memories.items.length > 1000 || !day(memories.as_of) || !Number.isSafeInteger(memories.personal_revision) || memories.personal_revision < 0) invalid('INVALID_MEMORY', '本人需求资料版本或日期缺失。');
    const referenceDay = memories.as_of > overview.as_of ? memories.as_of : overview.as_of, seen = new Set();
    for (const item of memories.items) {
      if (!record(item) || !string(item.node_id, 160) || seen.has(item.node_id)) invalid('INVALID_MEMORY', '本人需求资料存在重复或无效对象。');
      seen.add(item.node_id);
      if ((item.workspace_id != null && item.workspace_id !== identity.workspace_id) || (item.subject_id != null && item.subject_id !== identity.account_id)) invalid('UNBOUND_MEMORY', '本人需求包含其他账户资料。');
      if (item.user_confirmed !== true || item.expired === true || item.deleted === true || ['deleted', 'candidate', 'unverified', 'cancelled', 'rejected', 'expired'].includes(item.status) || !['user_entered', 'conversation_user_statement'].includes(item.source_type) || !['goal', 'product_interest', 'planned_commitment'].includes(item.category) || !string(item.value, 240)) continue;
      if (item.expires_on != null && (!day(item.expires_on) || item.expires_on < referenceDay)) continue;
      const value = item.value;
      // 否定、假设和转述不提升为个人需要；复杂含混陈述交给本人进一步确认。
      if (/不|无需|无意|没有|否认|取消|拒绝|放弃|停止|暂缓|尚未|未计划|假如|如果|朋友|客户|别人|他说|她说|\b(?:not|no|never|don't|doesn't|without|avoid|cancel|if|friend|client)\b/i.test(value)) continue;
      const statements = value.split(/[。；;!?！？\n]/);
      for (const statement of statements) {
        if (!/^(?:我|本人|我们|我的家庭|家庭|家人|父母|孩子|子女)?(?:想|希望|需要|计划|打算|准备|优先|关注|考虑|补充|完善|提升|增加|加强|了解)|^(?:保障需求|保险需求|关注方向|目标)\s*[:：]|^I\s+(?:want|need|plan|would like|am considering)\b/i.test(statement.trim())) continue;
        for (const key of categories) if (needTerms[key].test(statement)) {
          if (!result.has(key)) result.set(key, []);
          result.get(key).push(item.node_id);
        }
      }
    }
    return result;
  }
  function matchedProducts(products) {
    const groups = new Map(), seen = new Set(), duplicates = new Set();
    if (!Array.isArray(products)) return groups;
    for (const product of products) if (personalProduct(product)) {
      if (seen.has(product.id)) duplicates.add(product.id);
      seen.add(product.id);
    }
    for (const product of products) {
      const key = productCategory(product);
      if (!key || duplicates.has(product.id)) continue;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(product.id);
    }
    return groups;
  }
  const setup = () => ({ key: 'setup', label: '补充保障需求', isSetup: true, reasonKind: 'records', policies: [], assets: [], evidenceSummary: { policyIds: [], assetIds: [], memoryIds: [] }, productIds: [] });
  function build(input = {}) {
    const { identity, overview, memories = null, priorities = [], products = [] } = record(input) ? input : {};
    try {
      const data = validate(identity, overview);
      if (!Array.isArray(priorities) || priorities.length > 20 || priorities.some(key => !categories.includes(key))) invalid('INVALID_PRIORITIES', '保障关注方向无法核对。');
      if (!Array.isArray(products)) invalid('INVALID_PRODUCTS', '公开保险目录格式无法核对。');
      const selected = [...new Set(priorities)], memoryNeeds = confirmedNeeds(identity, overview, memories);
      const policyGroups = new Map(), assetGroups = new Map();
      const add = (map, key, value, idKey) => { if (!key) return; if (!map.has(key)) map.set(key, new Map()); map.get(key).set(value[idKey], value); };
      for (const asset of data.assets.values()) add(assetGroups, assetKinds[asset.asset_type], asset, 'asset_id');
      for (const policy of data.policies.values()) {
        if (policy.status !== 'active' || policy.start_date > overview.as_of || policy.end_date < overview.as_of) continue;
        const key = policyKinds[policy.insurance_type];
        add(policyGroups, key, policy, 'policy_id');
        if (key && policy.covered_asset_id) add(assetGroups, key, data.assets.get(policy.covered_asset_id), 'asset_id');
      }
      const order = [...selected, ...memoryNeeds.keys(), ...recordOrder.filter(key => policyGroups.has(key) || assetGroups.has(key))];
      const productGroups = matchedProducts(products);
      const items = [...new Set(order)].slice(0, 3).map(key => {
        const policies = [...(policyGroups.get(key)?.values() || [])].map(item => ({ ...item }));
        const assets = [...(assetGroups.get(key)?.values() || [])].map(item => ({ ...item }));
        return { key, label: labels[key], policies, assets,
          reasonKind: selected.includes(key) ? 'selected' : memoryNeeds.has(key) ? 'memory' : 'records',
          evidenceSummary: { policyIds: policies.map(item => item.policy_id), assetIds: assets.map(item => item.asset_id), memoryIds: [...new Set(memoryNeeds.get(key) || [])] },
          productIds: [...(productGroups.get(key) || [])] };
      });
      if (items.length < 3) items.push(setup());
      return { items, dataAvailable: true, error: null, missingSlots: 3 - items.length, asOf: overview.as_of, datasetVersion: overview.dataset_version };
    } catch (failure) {
      // 明确选项是受限枚举，档案失败时仍可浏览相应公开资料；私有资料全部丢弃。
      const safePriorities = Array.isArray(priorities) ? [...new Set(priorities.filter(key => categories.includes(key)))].slice(0, 3) : [];
      const groups = matchedProducts(products);
      const items = safePriorities.map(key => ({ key, label: labels[key], reasonKind: 'selected', policies: [], assets: [], evidenceSummary: { policyIds: [], assetIds: [], memoryIds: [] }, productIds: [...(groups.get(key) || [])] }));
      if (items.length < 3) items.push(setup());
      return { items, dataAvailable: false, error: { code: failure.code || 'INVALID_INPUT', message: failure.code ? failure.message : '保障资料暂时无法核对，请重新读取。' }, missingSlots: 3 - items.length, asOf: null, datasetVersion: null };
    }
  }
  return Object.freeze({ build, categories, labels, productCategory });
})();
