/* 纯保障方向选择器：验证来源绑定与按需求排序，不执行网络、交易或模型请求。 */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const context = vm.createContext({ window: {} });
vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../js/insurance_needs.js'), 'utf8'), context);
const N = context.window.NavrenInsuranceNeeds, passed = [];
const clean = value => JSON.parse(JSON.stringify(value));
function check(label, fn) { fn(); passed.push(label); }
const build = input => clean(N.build(input));
const keys = result => result.items.map(item => item.key);
const clone = input => JSON.parse(JSON.stringify(input));
function fixture(types = [], uid = 'user-a') {
  const identity = { workspace_id: 'workspace-' + uid, account_id: uid, privacy_revision: 3, personal_revision: 1 };
  const overview = { schema_version: 'navren.financial.v1', source: 'navren_synthetic', read_only: true, runtime_ledger_included: false,
    workspace_id: identity.workspace_id, workspace_binding: identity.workspace_id, subject_id: uid, privacy_revision: 3,
    dataset_version: 'insurance-test-v1', as_of: '2026-10-08', data: { policies: [], assets: [], contracts: [], connections: [{ subject_id: uid, connection_id: uid + '-insurer', institution_type: 'insurance' }] } };
  for (const [i, type] of types.entries()) {
    const assetType = { property: 'property', equipment: 'equipment', crop: 'crop_exposure' }[type], assetID = assetType ? uid + '-asset-' + i : null;
    if (assetID) overview.data.assets.push({ subject_id: uid, asset_id: assetID, asset_type: assetType, name: type + ' object ' + i, ownership_scope: type === 'property' ? 'personal' : 'self_employed' });
    overview.data.contracts.push({ subject_id: uid, contract_id: uid + '-contract-' + i, asset_id: assetID, name: type + ' coverage' });
    overview.data.policies.push({ subject_id: uid, policy_id: uid + '-policy-' + i, contract_id: uid + '-contract-' + i,
      insurer_connection_id: uid + '-insurer', policyholder_id: uid, insured_id: uid, covered_asset_id: assetID,
      insurance_type: type, status: 'active', start_date: '2026-01-01', end_date: '2026-12-31', sum_insured_minor: 10000000, premium_minor: 100000, deductible_minor: null, cash_value_minor: null });
  }
  return { identity, overview, priorities: [], products: [] };
}
function memory(input, values, overrides = {}) {
  return { enabled: true, workspace_id: input.identity.workspace_id, subject_id: input.identity.account_id, privacy_revision: input.identity.privacy_revision,
    personal_revision: 1, as_of: '2026-10-08', items: values.map((value, index) => ({ node_id: 'memory-' + index, category: 'product_interest', user_confirmed: true, expired: false, expires_on: null, source_type: 'user_entered', value, ...overrides })) };
}
function product(id, name, group = '符合资质的个人投资者', options = {}) {
  return { id, name, source_scope: 'public_catalog', version: 'catalogue-v1', raw: { type: '保险', customer_group_type: group }, ...options };
}
function unavailable(result, code) {
  assert.equal(result.dataAvailable, false);
  if (code) assert.equal(result.error.code, code);
  for (const item of result.items) { assert.deepEqual(item.policies, []); assert.deepEqual(item.assets, []); assert.deepEqual(item.evidenceSummary.memoryIds, []); }
}
function freeze(value) { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); } return value; }

check('固定分类键与受控标签', () => {
  assert.deepEqual(clean(N.categories), ['health', 'property', 'agriculture', 'equipment', 'liability', 'pension', 'accident']);
  assert.equal(N.labels.health, '健康医疗'); assert.ok(Object.isFrozen(N.categories));
});
check('四账户档案产生不同方向，并分别带真实对象依据', () => {
  const definitions = [
    [['health', 'property', 'property'], ['property', 'health', 'setup']],
    [['crop', 'equipment', 'equipment', 'health', 'property'], ['agriculture', 'equipment', 'property']],
    [['health', 'property', 'property', 'property', 'property'], ['property', 'health', 'setup']],
    [['health', 'property', 'shop_property', 'public_liability'], ['liability', 'property', 'health']]
  ];
  for (const [i, [types, expected]] of definitions.entries()) {
    const result = build(fixture(types, 'profile-' + i)); assert.deepEqual(keys(result), expected); assert.equal(result.dataAvailable, true);
    for (const item of result.items.filter(item => !item.isSetup)) {
      assert.equal(item.reasonKind, 'records'); assert.ok(item.evidenceSummary.policyIds.length || item.evidenceSummary.assetIds.length);
      assert.equal(item.policies.length, item.evidenceSummary.policyIds.length);
    }
  }
});
check('用户ID和姓名不会决定方向', () => {
  const a = fixture(['health', 'property'], 'retired_entrepreneur'), b = fixture(['health', 'property'], 'grain_farmer');
  a.identity.display_name = '退休长者'; a.overview.data.summary = { total_assets_minor: 999999999999 }; a.identity.risk_level = 5;
  b.identity.display_name = '大学生'; b.identity.risk_level = 1;
  assert.deepEqual(keys(build(a)), keys(build(b))); assert.ok(!keys(build(a)).includes('pension'));
});
check('本人当前选择优先，去重且最多三方向', () => {
  const input = fixture(['crop', 'equipment', 'property']); input.priorities = ['pension', 'health', 'pension', 'accident', 'property'];
  assert.deepEqual(keys(build(input)), ['pension', 'health', 'accident']); assert.ok(build(input).items.every(item => item.reasonKind === 'selected'));
});
check('确认记忆优先于档案；手动选择优先于记忆', () => {
  const input = fixture(['crop', 'equipment', 'property']); input.memories = memory(input, ['我需要医疗保障。', '我的家庭希望完善养老规划。']);
  // 保守语法：明确主语与需求动词，不从家庭支出或退休标签推断养老。
  input.memories.items[1].value = '家庭希望完善养老规划。';
  assert.deepEqual(keys(build(input)), ['health', 'pension', 'agriculture']);
  input.priorities = ['accident']; const result = build(input); assert.deepEqual(keys(result), ['accident', 'health', 'pension']);
  assert.equal(result.items[1].reasonKind, 'memory'); assert.deepEqual(result.items[1].evidenceSummary.memoryIds, ['memory-0']);
});
check('关闭记忆、删除记忆、撤销确认不会继续推荐', () => {
  const input = fixture(['health']); input.memories = memory(input, ['我想规划养老保障']);
  assert.ok(keys(build(input)).includes('pension'));
  input.memories.enabled = false; assert.deepEqual(keys(build(input)), ['health', 'setup']);
  input.memories.enabled = true; input.memories.items = []; assert.deepEqual(keys(build(input)), ['health', 'setup']);
  input.memories = memory(input, ['我想规划养老保障'], { user_confirmed: false }); assert.ok(!keys(build(input)).includes('pension'));
});
check('过期、删除和待核实记录不产生需求', () => {
  for (const flag of [{ expired: true }, { expires_on: '2026-10-07' }, { expires_on: '2026-02-30' }, { deleted: true }, { status: 'deleted' }, { status: 'candidate' }, { status: 'expired' }]) {
    const input = fixture(['health']); input.memories = memory(input, ['我需要养老保障'], flag); assert.ok(!keys(build(input)).includes('pension'));
  }
});
check('未经允许的来源与普通陈述、职业、持有或阅读不当成需求', () => {
  const plain = ['退休金每月到账', '我有健康保险', '我阅读了养老资料', '医疗险市场规模很大', '他需要医疗保险', '客户想配置养老保险', '朋友想配置养老保险', '我想了解医疗行业股票', '养老年金险'];
  const input = fixture(['property']); input.memories = memory(input, plain); assert.deepEqual(keys(build(input)), ['property', 'setup']);
  input.memories = memory(input, ['我需要养老保障'], { source_type: 'model_inferred' }); assert.ok(!keys(build(input)).includes('pension'));
  input.memories = memory(input, ['我需要养老保障'], { category: 'knowledge_preference' }); assert.ok(!keys(build(input)).includes('pension'));
});
check('否定、取消、假设和混合含混需求不匹配', () => {
  const input = fixture(['property']); input.memories = memory(input, ['我不需要养老保障', '我无需医疗保障', '我想取消养老保险', '如果我想买医疗险', '我没有医疗险但想买年金险', 'I do not need retirement planning']);
  assert.deepEqual(keys(build(input)), ['property', 'setup']);
});
check('具体正向需求、结构字段及英文陈述可用', () => {
  const input = fixture(); input.memories = memory(input, ['保障需求：公众责任保障', 'I need medical insurance', '我希望补充意外保障']);
  assert.deepEqual(keys(build(input)), ['liability', 'health', 'accident']); assert.ok(build(input).items.every(item => item.reasonKind === 'memory'));
});
check('持有年金或life保单也不能自动推断养老需求', () => {
  for (const kind of ['pension', 'annuity', 'life']) { const result = build(fixture([kind])); assert.deepEqual(keys(result), ['setup']); }
});
check('每个档案领域用类型与关联资产判定', () => {
  for (const [kind, key] of [['health', 'health'], ['property', 'property'], ['crop', 'agriculture'], ['equipment', 'equipment'], ['public_liability', 'liability'], ['accident', 'accident']]) {
    const result = build(fixture([kind])); assert.equal(result.items[0].key, key); assert.equal(result.items[0].policies.length, 1);
  }
});
check('资产方向不声称已有保障；未知资产不制造方向', () => {
  const input = fixture(); input.overview.data.assets.push({ subject_id: input.identity.account_id, asset_id: 'uncovered-equipment', asset_type: 'equipment', name: '设备' });
  const result = build(input); assert.equal(result.items[0].key, 'equipment'); assert.deepEqual(result.items[0].policies, []); assert.equal(result.items[0].assets.length, 1);
  input.overview.data.assets[0].asset_type = 'private_equity'; assert.deepEqual(keys(build(input)), ['setup']);
});
check('仅截至档案日期有效的保单形成方向', () => {
  for (const mutation of [p => p.status = 'expired', p => p.status = 'pending', p => p.end_date = '2026-09-30', p => p.start_date = '2026-11-01']) {
    const input = fixture(['health']); mutation(input.overview.data.policies[0]); assert.deepEqual(keys(build(input)), ['setup']);
  }
});
check('无档案保留本人明确方向；未知保单数量不伪装零', () => {
  const input = fixture(); input.overview = null; input.priorities = ['health', 'pension']; input.products = [product('medical', '医疗险'), product('pension', '养老年金险')];
  const result = build(input); unavailable(result, 'DATA_UNAVAILABLE'); assert.deepEqual(keys(result), ['health', 'pension', 'setup']); assert.deepEqual(result.items[0].productIds, ['medical']);
  input.priorities = []; assert.deepEqual(keys(build(input)), ['setup']); assert.equal(build(input).missingSlots, 2);
  unavailable(build(null), 'IDENTITY_UNAVAILABLE');
});
check('概要身份与版本问题拒绝私有对象及记忆', () => {
  const mutations = [
    [i => i.identity.account_id = 'other', 'UNBOUND_OVERVIEW'], [i => i.overview.workspace_binding = 'other', 'UNBOUND_OVERVIEW'],
    [i => i.overview.workspace_id = 'other', 'UNBOUND_OVERVIEW'], [i => i.overview.subject_id = 'other', 'UNBOUND_OVERVIEW'],
    [i => i.overview.privacy_revision = 2, 'UNBOUND_OVERVIEW'], [i => i.overview.schema_version = 'other', 'UNBOUND_OVERVIEW'],
    [i => i.overview.source = 'imported', 'UNBOUND_OVERVIEW'], [i => i.overview.read_only = false, 'UNBOUND_OVERVIEW'],
    [i => i.overview.runtime_ledger_included = true, 'UNBOUND_OVERVIEW'], [i => delete i.overview.dataset_version, 'INVALID_VERSION'],
    [i => i.overview.as_of = '2026-02-30', 'INVALID_VERSION'], [i => delete i.overview.privacy_revision, 'UNBOUND_OVERVIEW']
  ];
  for (const [change, code] of mutations) {
    const input = fixture(['property']); input.memories = memory(input, ['我需要养老保障']); input.priorities = ['health']; change(input);
    const result = build(input); unavailable(result, code); assert.deepEqual(keys(result), ['health', 'setup']);
  }
});
check('其他主体或重复私有ID拒绝整组，不保留部分内容', () => {
  for (const [collection, idKey] of [['policies', 'policy_id'], ['assets', 'asset_id'], ['contracts', 'contract_id'], ['connections', 'connection_id']]) {
    const input = fixture(['property']); input.overview.data[collection][0].subject_id = 'other'; unavailable(build(input), 'UNBOUND_RECORD');
    const duplicate = fixture(['property']); duplicate.overview.data[collection].push({ ...duplicate.overview.data[collection][0] }); unavailable(build(duplicate), 'DUPLICATE_RECORD');
    const absent = fixture(['property']); delete absent.overview.data[collection][0][idKey]; unavailable(build(absent), 'UNBOUND_RECORD');
  }
});
check('保障对象、合同、承保机构归属与期间均需关联', () => {
  for (const mutate of [i => i.overview.data.policies[0].covered_asset_id = 'missing', i => i.overview.data.policies[0].contract_id = 'missing', i => i.overview.data.policies[0].insurer_connection_id = 'missing', i => i.overview.data.contracts[0].asset_id = null, i => i.overview.data.connections[0].institution_type = 'broker']) {
    const input = fixture(['property']); mutate(input); unavailable(build(input), 'BROKEN_ASSOCIATION');
  }
  for (const key of ['policyholder_id', 'insured_id']) { const input = fixture(['health']); input.overview.data.policies[0][key] = 'other'; unavailable(build(input), 'UNBOUND_RECORD'); }
  const invalid = fixture(['health']); invalid.overview.data.policies[0].end_date = '2026-02-30'; unavailable(build(invalid), 'INVALID_POLICY');
});
check('未知保额、免赔与现金价值保持null；金额非法拒绝', () => {
  const input = fixture(['health']); input.overview.data.policies[0].sum_insured_minor = null;
  const result = build(input); assert.equal(result.items[0].policies[0].sum_insured_minor, null); assert.equal(result.items[0].policies[0].deductible_minor, null);
  for (const amount of [-1, 1.2, Number.MAX_SAFE_INTEGER + 1, '100']) { const copy = clone(input); copy.overview.data.policies[0].premium_minor = amount; unavailable(build(copy), 'INVALID_AMOUNT'); }
});
check('记忆账户、版本、日期及重复ID未核对则拒绝私有内容', () => {
  for (const [mutate, code] of [[i => i.memories.workspace_id = 'other', 'UNBOUND_MEMORY'], [i => i.memories.subject_id = 'other', 'UNBOUND_MEMORY'], [i => i.memories.privacy_revision = 0, 'UNBOUND_MEMORY'], [i => delete i.memories.personal_revision, 'INVALID_MEMORY'], [i => i.memories.as_of = 'unknown', 'INVALID_MEMORY'], [i => i.memories.items.push({ ...i.memories.items[0] }), 'INVALID_MEMORY'], [i => i.memories.items[0].subject_id = 'other', 'UNBOUND_MEMORY']]) {
    const input = fixture(['health']); input.memories = memory(input, ['我需要养老保障']); mutate(input); unavailable(build(input), code);
  }
});
check('个人产品按明确名称归类，机构和含糊守护不能越界', () => {
  const rows = [product('health', '众民保中高端医疗'), product('pension', '商业养老金'), product('accident', '个人意外险'), product('institution', '公众责任险', '符合资质的机构投资者'), product('guard', '企业家臻爱守护'), product('unknown', '个人医疗险', '未明确')];
  assert.deepEqual(rows.map(p => N.productCategory(p)), ['health', 'pension', 'accident', null, null, null]);
  const input = fixture(); input.priorities = ['health', 'pension', 'accident']; input.products = rows;
  assert.deepEqual(build(input).items.map(item => item.productIds), [['health'], ['pension'], ['accident']]);
});
check('目录版本、来源不明及重复产品不产生匹配', () => {
  const input = fixture(); input.priorities = ['health'];
  input.products = [product('missing', '医疗险', undefined, { version: null }), product('sim', '医疗险', undefined, { source_scope: 'bank_product' }), product('dup', '医疗险'), product('dup', '医疗险'), product('one', '个人医疗险')];
  assert.deepEqual(build(input).items[0].productIds, ['one']);
});
check('没有明确匹配不凑机构、年金或随机产品', () => {
  const input = fixture(['crop', 'equipment']); input.products = [product('pension', '养老年金险'), product('org', '农机财产险', '符合资质的机构投资者')];
  const result = build(input); assert.deepEqual(keys(result), ['agriculture', 'equipment', 'setup']); assert.ok(result.items.every(item => !item.productIds.length));
});
check('异常枚举不成为标题或HTML；原文安全保留为数据', () => {
  const input = fixture(['property']); input.overview.data.assets[0].name = '<img src=x onerror=attack()>';
  input.overview.data.contracts[0].name = '<script>attack()</script>';
  input.memories = memory(input, ['我需要医疗保障<script>attack()</script>']);
  const result = build(input); assert.equal(result.items[0].label, '健康医疗'); assert.equal(result.items[1].label, '房产保障');
  assert.equal(result.items[1].assets[0].name, '<img src=x onerror=attack()>'); assert.ok(result.items.every(item => !item.label.includes('<')));
  input.priorities = ['<script>attack()</script>']; unavailable(build(input), 'INVALID_PRIORITIES'); assert.deepEqual(keys(build(input)), ['setup']);
});
check('输入不变异，冻结数据可反复调用，去除记忆后即时变化', () => {
  const input = fixture(['property']); input.priorities = ['health']; input.products = [product('one', '医疗险')]; input.memories = memory(input, ['我需要养老保障']);
  const before = clone(input); freeze(input); const first = build(input), second = build(input); assert.deepEqual(first, second); assert.deepEqual(input, before);
  const next = clone(input); next.memories.items = []; assert.ok(!keys(build(next)).includes('pension'));
});
console.log(JSON.stringify({ passed: passed.length, checks: passed, network: 'none', models: 'none', financial_writes: 'none' }, null, 2));
