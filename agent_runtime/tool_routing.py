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


class TurnToolScope:
    def __init__(self, *, allowed_tools=None, financial_tools=()):
        self.allowed_tools = None if allowed_tools is None else frozenset(allowed_tools)
        self.financial_tools = frozenset(financial_tools)
        self.names = set(DISCOVERY_TOOLS if self.allowed_tools is None else self.allowed_tools)
        self.active_skills = []
        self.documents = {}

    def add_skill(self, name, reference=None):
        item = describe(name)
        if item['role'] != 'advisor':
            raise ValueError('reviewer_skill_forbidden')
        self.names.update(item['tools'])
        self.names.update(DOMAIN_READ_TOOLS.get(name, ()))
        if name in FINANCIAL_SKILLS:
            self.names.update(self.financial_tools)
        if name not in self.active_skills:
            self.active_skills.append(name)
        key = (name,reference)
        if key not in self.documents:
            self.documents[key] = load_advisor(name,reference)['instructions']

    def project(self, schemas):
        return [schema for schema in schemas
                if schema['function']['name'] in self.names
                and (self.allowed_tools is None or schema['function']['name'] in self.allowed_tools)]

    def summary(self, schemas):
        return {'active_skills': list(self.active_skills),
                'available_tools': [schema['function']['name'] for schema in self.project(schemas)]}

    def primary_messages(self, messages, *, policy=None, catalog=None, context=None):
        """方法正文只放一个受信槽位；不删除工具业务证据或对话历史。"""
        result=list(messages)
        first=deepcopy(result[0])
        if policy is not None:
            from .prompt_layers import layer_policy, method_body
            if not first['content'].startswith(policy):
                raise ValueError('trusted_policy_prefix_mismatch')
            policy_tools=self.names if self.allowed_tools is None else self.names & self.allowed_tools
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
                for (name,reference),document in self.documents.items())
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
        # Verify the entire version set before exposing any saved method.
        try:
            for name, reference, digest in row['methods']:
                text = load_advisor(name, reference)['instructions']
                if hashlib.sha256(text.encode()).hexdigest() != digest:
                    return []
        except (ValueError, OSError):
            return []
        for name, reference, _ in row['methods']:
            scope.add_skill(name, reference)
        return [{'skill': name, 'reference': reference, 'instructions_sha256': digest}
                for name, reference, digest in row['methods']]

    def remember(self, scope, workspace, history, privacy_revision):
        methods = [(name, reference, hashlib.sha256(text.encode()).hexdigest())
                   for (name, reference), text in scope.documents.items()]
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
