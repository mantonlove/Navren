"""按本轮已加载的维护者技能展示工具；业务权限仍由 BankTools 决定。"""
from copy import deepcopy
from collections import OrderedDict
import hashlib
import json
import threading
import time
from .skills import describe, load_advisor


DISCOVERY_TOOLS = frozenset({
    'load_skill', 'account_summary', 'customer_records', 'budget', 'cashflow',
    'money_calculation', 'operation_status', 'answer_facts', 'answer_reviewed',
})
PROGRESSIVE_NOTICE = (
    '本轮按需展示工具。当前已展示的工具可直接调用；需要的领域工具尚未展示时，先用load_skill加载对应技能，下一次请求会展示该技能工具。'
    '可以加载多个技能或子技能。没有展示某工具不表示没有资料，不可据此声称查询完成。'
    '技能加载不授予业务权限，也不执行金融操作。'
)

# 独立历史财务库的工具不在旧技能 metadata 中。只按维护者登记的领域
# 纳入候选，project 再与宿主本轮确实启用的 schema 取交集。
FINANCIAL_SKILLS = frozenset({
    'financial-diagnosis', 'cashflow', 'banking', 'cards', 'profile',
    'asset-allocation', 'portfolio-review', 'wealth',
})
DOMAIN_READ_TOOLS = {
    'cashflow': {'statement_details'},
    'financial-diagnosis': {'statement_details'},
    'product-research': {'research_plans'},
    'goal-planning': {'research_plans'},
    'asset-allocation': {'research_plans'},
}


GOAL_STAGES = ('clarify', 'hypothetical', 'current_funds', 'plan_from_accounts', 'existing_task')
GOAL_RESTRICTED_STAGES = frozenset(('unselected', 'clarify', 'hypothetical'))
GOAL_CLARIFICATION_TOOLS = frozenset(('load_skill', 'answer_reviewed'))
GOAL_STAGE_SCHEMA = {'type': 'string', 'enum': list(GOAL_STAGES),
    'description': '仅goal-planning使用，同一次加载明确方法阶段：clarify为尚未确定口径/币种/期限的开放愿望；'
    'hypothetical为明确非本人事实的假设讨论；current_funds为用户明确查询本人当前资金；'
    'plan_from_accounts为明确结合本人账目/义务的精确规划；existing_task为查询已有生活任务或准备其独立操作。'
    '仅“我想实现一个财富目标”不能认定为current_funds或plan_from_accounts。阶段不授予业务权限。'}

PRODUCT_SOURCE_SCOPES = ('clarify', 'current_simulation', 'real_public')
PRODUCT_SOURCE_SCHEMA = {'type': 'string', 'enum': list(PRODUCT_SOURCE_SCOPES),
    'description': '仅product-research使用。来源未明确时clarify；用户明确选择本机演示规则时current_simulation；'
    '明确真实发行产品时real_public。默认clarify，不根据裸产品名称猜来源。该字段只选择方法，不登记公开身份或授予权限。'}
PRODUCT_SOURCE_TOOLS = {
    'clarify': frozenset(('load_skill', 'answer_reviewed')),
    'current_simulation': frozenset(('load_skill', 'simulation_product_catalog', 'answer_reviewed')),
    'real_public': frozenset(('load_skill', 'knowledge_status', 'knowledge_search', 'public_products', 'answer_reviewed')),
}

def product_source_for_load(name, value=None):
    if name != 'product-research':
        if value is not None:raise ValueError('source_scope_for_other_skill')
        return None
    if value is None:return 'clarify'
    if type(value)is not str or value not in PRODUCT_SOURCE_SCOPES:
        raise ValueError('invalid_product_source_scope')
    return value

def product_method_document(document, source_scope, reference=None):
    if source_scope not in PRODUCT_SOURCE_SCOPES:
        raise ValueError('invalid_product_source_scope')
    if reference is not None:
        if source_scope!='real_public':raise ValueError('product_reference_needs_real_public_scope')
        return document
    markers=('## 来源阶段','## 来源澄清','## 本机演示规则','## 触发与必要读取')
    if any(document.count(marker)!=1 for marker in markers):
        raise ValueError('product_scope_method_marker_missing')
    indexes=[document.index(marker)for marker in markers]
    if indexes!=sorted(indexes):raise ValueError('product_scope_method_marker_order')
    preamble=document[:indexes[0]];shared=document[indexes[0]:indexes[1]]
    branch=(document[indexes[1]:indexes[2]] if source_scope=='clarify' else
            document[indexes[2]:indexes[3]] if source_scope=='current_simulation' else document[indexes[3]:])
    return preamble+shared+branch.rstrip()+'\n\n本轮source_scope='+source_scope+'；仅展示本来源方法，不构成公开身份、本人资格或执行授权。'

def goal_stage_for_load(name, value=None):
    if name != 'goal-planning':
        if value is not None:
            raise ValueError('goal_stage_for_other_skill')
        return None
    if value is None:
        return 'unselected'
    if type(value) is not str or value not in GOAL_STAGES:
        raise ValueError('invalid_goal_stage')
    return value

def goal_method_document(document, stage, reference=None):
    if stage not in GOAL_RESTRICTED_STAGES:
        return document
    if reference is not None:
        raise ValueError('goal_reference_needs_personal_stage')
    marker='## 触发与必要读取'
    if document.count(marker) != 1:
        raise ValueError('goal_stage_method_marker_missing')
    short=document.split(marker,1)[0].rstrip()
    notice=('本轮goal阶段尚未选择。可在load_skill同一次选择goal_stage后继续；'
        '也可先用answer_reviewed给普通澄清。尚未展示的个人工具不代表没有数据。'
        if stage=='unselected' else '本轮为'+stage+'阶段，可直接完成初步开放分析或明确的假设讨论；只有必要未知会改变下一步时才自然澄清，不以问卷代替分析。')
    return short+'\n\n## 本轮结构化方法阶段\n'+notice+'\n不得把目标愿望解释成当前账户事实；个人精确规划方法尚未加载。'

class TurnToolScope:
    def __init__(self, *, allowed_tools=None, financial_tools=()):
        self.allowed_tools = None if allowed_tools is None else frozenset(allowed_tools)
        self.financial_tools = frozenset(financial_tools)
        self.names = set(DISCOVERY_TOOLS if self.allowed_tools is None else self.allowed_tools)
        self.active_skills = []
        self.documents = {}
        self.goal_stage = None
        self._goal_tools = set()
        self._other_skill_tools = set()
        self.product_source_scope = None
        self._skill_tools = {}

    def add_skill(self, name, reference=None, *, goal_stage=None, source_scope=None):
        item = describe(name)
        if item['role'] != 'advisor':
            raise ValueError('reviewer_skill_forbidden')
        if name=='goal-planning':
            stage=('unselected' if goal_stage is None else goal_stage)
            if stage!='unselected' and stage not in GOAL_STAGES:
                raise ValueError('invalid_goal_stage')
            if stage in GOAL_RESTRICTED_STAGES and reference is not None:
                raise ValueError('goal_reference_needs_personal_stage')
            self.goal_stage=stage
        elif goal_stage is not None:
            raise ValueError('goal_stage_for_other_skill')
        product_scope=product_source_for_load(name,source_scope)
        if product_scope is not None:
            if reference is not None and product_scope!='real_public':
                raise ValueError('product_reference_needs_real_public_scope')
            self.product_source_scope=product_scope
        added=set(item['tools']) | set(DOMAIN_READ_TOOLS.get(name, ()))
        if name in FINANCIAL_SKILLS:
            added.update(self.financial_tools)
        self.names.update(added)
        (self._goal_tools if name=='goal-planning' else self._other_skill_tools).update(added)
        self._skill_tools.setdefault(name,set()).update(added)
        if name not in self.active_skills:
            self.active_skills.append(name)
        key = (name,reference)
        if key not in self.documents:
            self.documents[key] = load_advisor(name,reference)['instructions']

    def projected_names(self):
        names=set(self.names)
        if self.goal_stage in GOAL_RESTRICTED_STAGES:
            allowed=set(GOAL_CLARIFICATION_TOOLS)
            if self.goal_stage=='hypothetical':allowed.add('money_calculation')
            # Method projection only: retain independently loaded skills and
            # always intersect the original host allowlist in project().
            withheld=(set(DISCOVERY_TOOLS)|self._goal_tools)-allowed-self._other_skill_tools
            names.difference_update(withheld)
        if self.product_source_scope is not None:
            # Intersect discovery and this product contribution with its source.
            # Explicit independently loaded methods keep only their own current
            # contribution; a restricted goal must not re-open private tools.
            allowed=set(PRODUCT_SOURCE_TOOLS[self.product_source_scope])
            for skill,contribution in self._skill_tools.items():
                if skill=='product-research':continue
                if skill=='goal-planning'and self.goal_stage in GOAL_RESTRICTED_STAGES:
                    allowed.update(GOAL_CLARIFICATION_TOOLS)
                    if self.goal_stage=='hypothetical':allowed.add('money_calculation')
                else:allowed.update(contribution)
            names.intersection_update(allowed)
        return names

    def project(self, schemas):
        names=self.projected_names()
        return [schema for schema in schemas
                if schema['function']['name'] in names
                and (self.allowed_tools is None or schema['function']['name'] in self.allowed_tools)]

    def summary(self, schemas):
        return {'active_skills': list(self.active_skills), 'goal_stage':self.goal_stage,
                'product_source_scope':self.product_source_scope,
                'available_tools': [schema['function']['name'] for schema in self.project(schemas)]}

    def primary_messages(self, messages, *, policy=None, catalog=None, context=None):
        """方法正文只放一个受信槽位；不删除工具业务证据或对话历史。"""
        result=list(messages)
        first=deepcopy(result[0])
        if policy is not None:
            from .prompt_layers import layer_policy, method_body
            if not first['content'].startswith(policy):
                raise ValueError('trusted_policy_prefix_mismatch')
            projected_names=self.projected_names()
            policy_tools=projected_names if self.allowed_tools is None else projected_names & self.allowed_tools
            first['content']=layer_policy(policy,policy_tools,context=context)+first['content'][len(policy):]
        if catalog is not None:
            import json
            encode=lambda value:json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            full=encode(catalog)
            lean=encode([{k:item[k] for k in ('name','description')} for item in catalog])
            first['content']=first['content'].replace('专业技能目录：'+full,'专业技能目录：'+lean,1)
        if self.documents:
            from .prompt_layers import method_body
            first['content'] += ('\n本轮已加载以下专业方法，对应已展示工具可直接读取资料；'
                '同一正文不要重复load_skill。需要其他技能或参考文件再加载。方法不构成业务事实或操作授权。\n')
            first['content'] += '\n'.join('技能 '+name+(' / '+reference if reference else '')+'：\n'+method_body(document)
                for (name,reference),raw_document in self.documents.items()
                for document in [goal_method_document(raw_document,self.goal_stage,reference)
                    if name=='goal-planning' else product_method_document(raw_document,self.product_source_scope,reference)
                    if name=='product-research' else raw_document])
        result[0]=first
        return result


def compact_method_record(record):
    """仅替换维护者方法正文；收据保留来源与版本，业务证据原样保留。"""
    result=deepcopy(record)
    data=result.get('data')
    if isinstance(data,dict) and isinstance(data.get('instructions'),str):
        document=data.pop('instructions')
        data['instructions_sha256']=hashlib.sha256(document.encode()).hexdigest()
        data['instructions_utf8_bytes']=len(document.encode())
        data['method_only_notice']='专业方法已加载；此收据不是账户、产品或金额事实，也不授予操作权限。'
        data['notice']=data.get('notice','')+' '+data['method_only_notice']
    return result


class PublishedMethodContinuation:
    """Retain maintainer methods only after publication in this exact dialogue.

    No account facts, object IDs, model text or authority enter this cache.
    Restoring a method still intersects the current host tool allowlist.
    """
    def __init__(self, *, limit=256, ttl=1800, clock=time.monotonic):
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self._items = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def history_digest(history):
        return hashlib.sha256(json.dumps(history, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    def restore(self, scope, workspace, history, privacy_revision):
        with self._lock:
            row = self._items.pop(workspace, None)
        if (row is None or self.clock() - row['saved_at'] > self.ttl
                or row['history_sha256'] != self.history_digest(history)
                or row['privacy_revision'] != privacy_revision):
            return []
        # Goal stage is a per-turn semantic decision, never inherited authority.
        # Legacy cache rows may contain goal methods; restore all other methods
        # under their original version/history/privacy checks.
        methods=[method for method in row['methods'] if method[0]not in ('goal-planning','product-research')]
        # Verify the entire remaining version set before exposing any saved method.
        try:
            for name, reference, digest in methods:
                text = load_advisor(name, reference)['instructions']
                if hashlib.sha256(text.encode()).hexdigest() != digest:
                    return []
        except (ValueError, OSError):
            return []
        for name, reference, _ in methods:
            scope.add_skill(name, reference)
        return [{'skill': name, 'reference': reference, 'instructions_sha256': digest}
                for name, reference, digest in methods]

    def remember(self, scope, workspace, history, privacy_revision):
        methods = [(name, reference, hashlib.sha256(text.encode()).hexdigest())
                   for (name, reference), text in scope.documents.items() if name not in ('goal-planning','product-research')]
        # A cache miss is harmless; unusually broad sessions load again normally.
        if not methods or len(methods) > 16:
            return
        row = {'history_sha256': self.history_digest(history),
               'privacy_revision': privacy_revision, 'saved_at': self.clock(),
               'methods': methods}
        with self._lock:
            self._items.pop(workspace, None)
            self._items[workspace] = row
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
