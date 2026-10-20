"""只读账单问法收窄工具目录；准入估算和真实发送使用相同投影。"""
import re
from copy import deepcopy

from .evidence import EvidenceGuardedClient


STATEMENT_TOOLS = frozenset({
    'statement_source', 'monthly_statement', 'annual_statement', 'statement_details', 'load_skill', 'money_calculation',
    'answer_facts', 'answer_reviewed',
})


DETAIL_QUERY = re.compile(r'几笔|哪几笔|逐笔|明细|每笔|分别花|具体花|transaction details|how many transactions', re.I)
STATEMENT_QUERY = re.compile(r'账单|流水|收支|消费|支出|收入|钱花|花费|spend|statement', re.I)


def statement_month_context(text, history, as_of):
    """仅从本人当前/近期账单问句继承月份，不把旧回答当本轮事实。"""
    messages = [text]
    if DETAIL_QUERY.search(text or ''):
        for row in reversed((history or [])[-12:]):
            if row.get('role') != 'user':
                continue
            value = row.get('content', '')
            if not isinstance(value, str) or not (STATEMENT_QUERY.search(value) or DETAIL_QUERY.search(value)):
                break
            messages.append(value)
    for value in messages:
        explicit = set(re.findall(r'(?<!\d)(\d{4}-(?:0[1-9]|1[0-2]))(?!\d)', value))
        months = set(re.findall(r'(?<!\d)(1[0-2]|0?[1-9])月', value))
        years = set(re.findall(r'(?<!\d)(\d{4})年', value))
        if len(explicit) > 1 or len(months) > 1 or len(years) > 1:
            return None
        if explicit:
            return explicit.pop()
        if months:
            year = years.pop() if years else str(as_of)[:4]
            if re.fullmatch(r'\d{4}', year):
                return f'{year}-{int(months.pop()):02d}'
    return None


def statement_tool_scope(text, *, has_attachment=False, has_context=False, history=()):
    """仅收窄明确的历史账单读取；带附件、跨页面或操作请求保留完整目录。"""
    if not isinstance(text, str) or has_attachment or has_context:
        return None
    value = text.strip().lower()
    excluded = ('转账', '汇款', '申购', '买入', '赎回', '挂失', '取消', '预测',
                '未来', '投资', '理财', '预算', '可用资金', '异常', '重复扣费',
                'transfer', 'purchase', 'redeem', 'invest', 'forecast', 'budget',
                'can i', 'afford', 'anomal', 'duplicate')
    if any(word in value for word in excluded):
        return None
    phrases = ('钱花在哪', '钱花到哪', '支出分类', '消费分类', '月度账单', '年度账单',
               '月账单', '年账单', '收支情况', 'spending breakdown', 'monthly statement',
               'annual statement')
    matched = (any(word in value for word in phrases)
               or re.search(r'\d{1,2}月.*(?:支出|花费|账单|消费|收支)', value)
               or re.search(r'where\b.*\b(?:spend|spent)', value)
               or (DETAIL_QUERY.search(value) and (STATEMENT_QUERY.search(value)
                   or any(row.get('role')=='user' and STATEMENT_QUERY.search(row.get('content',''))
                          for row in (history or [])[-6:]))))
    return STATEMENT_TOOLS if matched else None


class ScopedEvidenceClient(EvidenceGuardedClient):
    """收窄的是可见工具定义；原证据门、权限和预算接口保持一致。"""
    def __init__(self, client, observer, preparation=None, *, allowed_tools=None):
        super().__init__(client, observer, preparation)
        self.allowed_tools = frozenset(allowed_tools) if allowed_tools else None
        if self.allowed_tools == STATEMENT_TOOLS:
            self.append_notice('\n账单汇总或明细读取成功后用answer_facts选择相关fact_ids。汇总最多6项；明细须选择完整笔数、合计及当前页每笔金额，最多14项。金额直接引用已核验事实，不重写整份金额签名。追问几笔/明细时必须调用statement_details，不能只重复月度分类汇总或要求用户换问法。')

    def _request_args(self, args):
        guarded = super()._request_args(args)
        if self.allowed_tools is None or len(guarded) < 2 or not isinstance(guarded[1], list):
            return guarded
        schemas = [schema for schema in guarded[1]
                   if schema.get('function', {}).get('name') in self.allowed_tools]
        if self.allowed_tools == STATEMENT_TOOLS:
            facts = next((schema for schema in schemas
                          if schema['function']['name']=='answer_facts'), None)
            ids = (facts or {}).get('function', {}).get('parameters', {}).get(
                'properties', {}).get('fact_ids', {}).get('items', {}).get('enum', [])
            # 此范围仅是事实汇总。读取前仍可澄清；有正式事实后用原确定性渲染和发布校验。
            if ids:
                schemas = [schema for schema in schemas
                           if schema['function']['name'] != 'answer_reviewed']
                schemas = deepcopy(schemas)
                for schema in schemas:
                    if schema['function']['name']=='answer_facts':
                        schema['function']['parameters']['properties']['fact_ids']['maxItems']=14
        return (guarded[0], schemas, *guarded[2:])
