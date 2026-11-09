"""宿主对本轮明示禁止及有限公共产品问答的确定性限制；模型不能关闭此策略。

本模块只识别限制并读取授权/修订元数据，不读取账户、画像或历史正文。
限定白名单表示可查询公共资料或计算用户给出的假设，不授予业务执行权。
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import re


PUBLIC_HYPOTHETICAL_TOOLS = frozenset({
    'load_skill', 'knowledge_status', 'knowledge_search', 'public_products',
    'money_calculation', 'answer_facts', 'answer_reviewed',
    'market_hotspots', 'market_status', 'market_event', 'market_article', 'market_weekly',
})

_NEGATION = r'(?:不要|不需要|不必|无需|禁止|请勿|勿|别|不再|不)'
_READ_USE = r'(?:读取|读入|查询|查看|访问|获取|调取|使用|利用|调用|参考|用|查|读)'
_PERSON = r'(?:本人的?|我的?|自己的?|个人|私人)'
_PRIVATE = r'(?:账户|帐户|账号|银行卡|财务(?:资料|数据|信息)?|(?:风险|财务)?画像|个人资料|资料|数据|信息|资金|余额|账单|帐单|流水|持仓|资产|记忆|历史对话)'
_RESTRICTION = re.compile(
    _NEGATION + r'\s*' + _READ_USE + r'[^，,。；;！？!?\n]{0,18}?' + _PERSON + r'\s*' + _PRIVATE
    + r'|' + _PERSON + r'\s*' + _PRIVATE + r'[^，,。；;！？!?\n]{0,8}?' + _NEGATION + r'\s*' + _READ_USE
    + r'|' + _NEGATION + r'\s*' + _READ_USE + r'\s*(?:任何)?(?:个人资料|私人资料|账户|帐户|财务|画像|账单|帐单|流水|持仓|记忆)'
    + r'|(?:do\s+not|don[’\x27]t|without|never)\s+(?:read(?:ing)?|use|using|access(?:ing)?)\s+'
      r'(?:any\s+)?(?:my\s+(?:personal\s+)?|personal\s+|private\s+)(?:account(?:s)?|financ(?:ial|e)|profile|data|history)',
    re.I,
)


# This is a finite, host-owned turn scope, not a model-derived permission.
# A public product question does not imply consent to inspect personal holdings.
_PUBLIC_PRODUCT = re.compile(r'产品|理财|基金|股票|债券|七日年化|业绩比较基准|'
    r'\b(?:fund|bond|stock|investment\s+product)\b', re.I)
_PRIVATE_REQUEST = re.compile(
    r'(?:本人|我的|我(?:当前|目前|现在|名下)(?:的)?|自己的?|个人|私人)[^，,。；;！？!?\n]{0,24}'
    r'(?:账户|帐户|余额|财务|资金|财务日历|日历|银行卡|账单|帐单|流水|持仓|资产|负债|画像|风险承受|风险偏好|风险等级|风险测评|正式测评|适当性|记忆|历史对话|收入|支出|现金流|已(?:有|买|购)|投资组合)'
    r'|(?:账户|帐户|账单|帐单|流水|持仓|画像|资产配置|投资组合)[^，,。；;！？!?\n]{0,12}(?:我的?|本人|个人)'
    r'|(?:查|看|读|分析|核对|诊断|整理)[^，,。；;！？!?\n]{0,8}(?:账户余额|可用余额|银行卡|账单|帐单|流水|本人持仓|个人画像)'
    r'|(?:持仓里|持仓中的|账户里|账户中的|已持有|我已买|我买的|我已有|我现有|我(?:之前|以前|曾经|已经|刚刚?|上次)?(?:买的|买过|购入|持有|申购))'
    r'|(?:我的|我(?:已经?|正在)?持有(?:的)?|我名下|本人持有)[^，,。；;！？!?\n]{0,16}(?:基金|理财|产品|收益|股票|债券)'
    r'|(?:我(?:明确)?愿意|我想|帮我|替我)[^，,。；;！？!?\n]{0,12}(?:迁入|开通|协商)'
    r'|(?:帮我|替我|我要|我想|给我)[^，,。；;！？!?\n]{0,12}(?:转账|还款|扣款|挂失|申购|赎回|买入|卖出|认购|修改密码|投诉|申请服务|准备申请)'
    r'|\bmy\s+(?:account|balance|holdings|portfolio|profile|transactions|statement|risk)\b', re.I)


def _scope_authorization_text(text):
    # Quoted/reported instructions and explicit hypotheses are data, not the
    # user's affirmative request to read their financial state. Keep independent
    # clauses so a real compound request still has its original private path.
    for pattern in (r'“[^”]*”',r'‘[^’]*’',r'「[^」]*」',r'『[^』]*』',
                    r'"[^"\n]*"',r"'[^'\n]*'"):
        text=re.sub(pattern,' ',text)
    text=re.sub(r'(?:文档|网页|资料|条款|材料|提示词|产品说明|模型|Jev)'
        r'[^，,。；;！？!?\n]{0,16}?(?:说|写|要求|提示|建议|内容为|内容是)'
        r'[^，,。；;！？!?\n]*',' ',text,flags=re.I)
    return re.sub(r'(?:假设|假如|如果|设想|举例|例如|譬如)[^，,。；;！？!?\n]*',' ',text)


def _disclaimed_hypothesis_scope(text, authorization_text):
    """A narrow data ceiling, after actual private requests kept their path."""
    if (any(text.count(left) != text.count(right) for left, right in
            (('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』')))
            or text.count('"') % 2 or text.count("'") % 2):
        return False
    active = text
    for pattern in (r'“[^”]*”', r'‘[^’]*’', r'「[^」]*」', r'『[^』]*』',
                    r'"[^"\n]*"', r"'[^'\n]*'"):
        active = re.sub(pattern, ' ', active)
    # Reported material cannot establish the user's own scope declaration.
    if re.search(r'(?:文档|网页|资料|条款|材料|提示词|产品说明|模型|Jev|朋友|他人|第三方|对方)'
                 r'[^，,。；;！？!?\n]{0,16}?(?:说|写|要求|提示|建议|内容为|内容是)', active, re.I):
        return False
    # Preserve independent real read/action requests that the earlier finite
    # private branch does not recognize; never turn them into a hypothesis.
    if re.search(r'查询|核对|查看|读取|调取|访问|告诉我|给我看看|帮我查询|'
                 r'转账|还款|扣款|挂失|申购|赎回|买入|卖出|认购|保存|记住', authorization_text):
        return False
    return bool(re.search(r'(?:只|仅)(?:做|作|进行|按)?(?:条件(?:化)?|假设(?:条件)?)(?:讨论|分析|推演)', active)
        and re.search(r'假设|假如|如果|设想', active)
        and re.search(r'(?:不是|并非|不代表)(?:我(?:的)?|本人(?:的)?)(?:真实|实际)(?:情况|事实|资料)', active)
        and re.search(r'(?:不是|并非|不代表)(?:(?:我|本人)(?:的)?)?已(?:经)?确认(?:的)?目标', active))


def explicit_simulated_wealth_read(text):
    """Select maintainer methods for an explicit own simulated-holdings read.

    This selects no product/account and makes no reads or business decisions.
    Quotes, reported instructions, hypotheses and explicit privacy denials
    cannot select this path. The normal tool and operation guards still apply.
    """
    if not isinstance(text, str) or _RESTRICTION.search(text):
        return False
    active = _scope_authorization_text(text)
    return bool(_PRIVATE_REQUEST.search(active)
        and re.search(r'(?:查|看|读|核对)[^。；;！？!?\n]{0,64}模拟[^。；;！？!?\n]{0,48}(?:持仓|产品)', active))


# Additional finite imperative forms, not a substring-based action classifier.
# These only reopen the existing private read/prepare path; confirmation and
# execution remain owned by the unchanged bank/operation authorization gates.
_ACTIVE_BALANCE = re.compile(
    r'(?:请)?(?:只|仅|直接)?(?:告诉我|给我看看|帮我查询)'
    r'(?:(?:我的|本人的?|账户|帐户|银行卡|现金))?'
    r'(?:可用余额|账户余额|帐户余额|余额|可安排资金)(?:是多少|有多少)?')
_ACTIVE_ACTION = r'(?:修改密码|申请服务|准备申请|转账|还款|扣款|挂失|申购|赎回|买入|卖出|认购|买理财)'
_ACTIVE_OPERATION = re.compile(r'(?:请)?(?:现在)?(?:先|直接)?'+_ACTIVE_ACTION
    +r'(?:\s*[，,]?\s*(?:再|然后|接着)'+_ACTIVE_ACTION+r'){0,2}')
_TRADE_AMOUNT = r'\s*(?:0|[1-9]\d{0,8})(?:\.\d{1,2})?\s*元'
_TRADE_PRODUCT = r'[A-Za-z0-9\u4e00-\u9fff_-]{0,30}(?:产品|基金|理财)'
_ACTIVE_ACCOUNT_TRADE = re.compile(
    r'(?:请)?(?:现在)?(?:先)?(?:用|使用|从)(?:同一)?'
    r'(?P<account>[A-Za-z0-9\u4e00-\u9fff_-]{1,20})(?:账户|帐户)(?:的)?'
    r'(?:(?:申购|赎回|买入|卖出|认购)'+_TRADE_AMOUNT+r'(?P<after>'+_TRADE_PRODUCT+r')'
    r'|(?P<before>'+_TRADE_PRODUCT+r')(?:申购|赎回|买入|卖出|认购)'+_TRADE_AMOUNT+r')')


# Finite complete cashflow -> draft command. A product noun terminates the
# draft target; no free action/question/statement tail can be consumed here.
# Quoted/reported/hypothetical text may not be deleted to create a command.
_COMPOUND_CASHFLOW_PREPARE = re.compile(
    r'(?:请)?(?:先)?(?:查看|查询|核对|分析)'
    r'(?:我的?|本人的?|自己的?)?(?:未来|接下来)'
    r'(?:\s*[1-9]\d{0,3}\s*(?:天|日))?现金流\s*'
    r'(?:之后|后|然后|[，,]\s*(?:再|然后|接着))\s*'
    r'(?:帮我|为我)?准备'
    r'(?:(?:申购|赎回|买入|卖出|认购)'
    r'(?:\s*(?:0|[1-9]\d{0,8})(?:\.\d{1,2})?\s*(?:元|万元))?\s*'
    r'(?!\d+(?:\.\d+)?\s*(?:元|万元))'
    r'(?P<label>[A-Za-z0-9\u4e00-\u9fff_-]{0,24})'
    r'(?:产品|基金|理财|股票|债券)'
    r'|(?:转账|还款))'
    r'(?:[，,]\s*(?:等待|待|等)(?:我本人|本人|我)(?:核对后)?确认)?')
_COMPOUND_TARGET_AMBIGUOUS = re.compile(
    r'哪些|哪(?:个|种|款|只)|有何|好不好|值不值得|合不合适|可不可以|'
    r'能不能|应不应该|要不要|会不会|可否|注意事项|原理|步骤|讲解|'
    r'演示|案例|一般|通用|讨论|说明|分析|主题|内容|风险|费用|可行性|'
    r'不要|不必|不需要|无需|请勿|禁止|不能|不可|不该|不打算|不想|'
    r'不买|不卖|不申购|不赎回|不转账|不还款|不是|并非|非请求|'
    r'执行|免确认|确认|请求|指令|假设|假如|如果|设想|举例|例如|譬如|'
    r'文档|网页|资料|条款|材料|提示词|产品说明|模型|Jev|的|是|为|'
    r'(?i:what|why|how|whether|safe|fees|risk)')


def _active_private_request(original_text, authorization_text):
    # A reported block is not promoted to a command just because a later line
    # or comma happens to contain an imperative. Refusals apply only to these
    # newly recognized forms; the original private-request patterns stay whole.
    if explicit_operation_check(original_text) == 'purchase':
        return True
    if '\n' in original_text or '\r' in original_text:
        return False
    for left,right in (('“','”'),('‘','’'),('「','」'),('『','』')):
        if original_text.count(left)!=original_text.count(right):return False
    if original_text.count('"')%2 or original_text.count("'")%2:return False
    if re.search(r'(?:^|[，,。；;！？!?])\s*(?:文档|网页|资料|条款|材料|提示词|产品说明|模型|Jev)'
            r'[^，,。；;！？!?]{0,16}?(?:说|写|要求|提示|建议|(?:原文|内容)(?:如下|为|是))',original_text,re.I):
        return False
    if re.search(r'(?:^|[，,。；;！？!?])\s*(?:假设|假如|如果|设想|举例|例如|譬如)',original_text):
        return False
    if re.search(r'教学|示例|解释|介绍|流程|如何|怎么|是否|能否|吗|么|[?？]',authorization_text):
        return False
    clauses=[part.strip()for part in re.split(r'[，,。；;！!]',authorization_text)if part.strip()]
    if any(_ACTIVE_BALANCE.fullmatch(clause)for clause in clauses):return True
    for clause in clauses:
        trade = _ACTIVE_ACCOUNT_TRADE.fullmatch(clause)
        if trade and not re.search(
                r'他人|别人|朋友|对方|第三人|公司|企业|假设|假如|如果|示例|解释|如何|怎样|不要|不能|禁止',
                trade.group('account')+(trade.group('before') or trade.group('after') or '')):
            return True
    command=original_text.strip().rstrip('。！!').strip()
    authorization_command=authorization_text.strip().rstrip('。！!').strip()
    if command == authorization_command:
        compound=_COMPOUND_CASHFLOW_PREPARE.fullmatch(command)
        if compound and not _COMPOUND_TARGET_AMBIGUOUS.search(compound.group('label') or ''):
            return True
    return bool(_ACTIVE_OPERATION.fullmatch(authorization_text.strip().rstrip('。！!').strip()))


def explicit_simulated_wealth_operation(text):
    """Identify a single affirmative draft workflow, never its arguments."""
    if (not isinstance(text, str) or _RESTRICTION.search(text)
            or '模拟' not in text or '草案' not in text
            or re.search(r'不要.{0,8}(?:准备|草案)|不准备|不用.{0,8}草案|同时|然后|以及', text)):
        return None
    active = _scope_authorization_text(text)
    if active != text or not _active_private_request(text, active):
        return None
    commands = [match for clause in re.split(r'[，,。；;！!]', active)
                if (match := _ACTIVE_ACCOUNT_TRADE.fullmatch(clause.strip()))]
    if len(commands) != 1:
        return None
    actions = re.findall(r'申购|赎回|买入|卖出|认购', commands[0].group())
    if len(actions) != 1:
        return None
    return {'申购':'purchase', '买入':'purchase', '认购':'purchase',
            '赎回':'redeem', '卖出':'redeem'}[actions[0]]


def explicit_operation_check(text):
    """Finite current-operation checks; no object, amount or write authority.

    A replacement imperative names its own simulated product and amount. It
    cannot borrow an assistant's previous authorization or quoted commands.
    """
    if (not isinstance(text,str) or len(text)>300 or _RESTRICTION.search(text)
            or re.search(r'\n|\r|假设|假如|如果|示例|解释|如何|怎样|别人|他人|朋友|公司|企业|忽略|绕过|权限|同时|然后|以及|不要|不准备|禁止|[<>`]',text)):
        return None
    value=text.strip().rstrip('。！!')
    trade=_ACTIVE_ACCOUNT_TRADE.fullmatch(re.split(r'[，,]',value)[0])
    if (trade and '模拟' in trade.group() and re.search(r'申购|买入|认购',trade.group())
            and re.fullmatch(r'[，,](?:先)?核对后准备',value[len(trade.group()):])):
        return 'purchase'
    if re.fullmatch(r'(?:那)?改为模拟[A-Za-z0-9\u4e00-\u9fff_-]{0,24}(?:产品|基金|理财)[，,](?:申购|买入|认购)'+_TRADE_AMOUNT,value):
        return 'purchase'
    account=r'[A-Za-z0-9\u4e00-\u9fff_-]{1,20}账户'
    payee=r'[A-Za-z0-9\u4e00-\u9fff_-]{1,20}'
    if (re.fullmatch(r'(?:改成)?从'+account+r'给'+payee+r'转'+_TRADE_AMOUNT+r'[，,]先查这张账户能否安排',value)
            or re.fullmatch(r'(?:还是)?用'+account+r'给别名[“"]'+payee+r'[”"]的收款人转'+_TRADE_AMOUNT,value)):
        return 'transfer'
    return None


@dataclass(frozen=True)
class RequestPrivacyPolicy:
    private_context_disabled: bool = False
    version: str = 'explicit-private-data-restriction/1'
    scope_reason: str = 'normal'

    @classmethod
    def from_text(cls, text):
        if not isinstance(text, str):
            raise ValueError('privacy_policy_requires_user_text')
        # Conservative: a mixed request cannot override its own explicit denial.
        # Unknown semantic forms keep the normal policy; this is a finite gate,
        # not a claimed complete natural-language privacy classifier.
        return cls(bool(_RESTRICTION.search(text)))

    @staticmethod
    def is_public_reference(reference):
        return (isinstance(reference, dict) and
            (reference.get('kind') in ('event','article','weekly') or
             (reference.get('kind') == 'product' and
              reference.get('source_scope') in ('public_catalog','public_nav'))))

    @classmethod
    def for_turn(cls, text, *, page_context=None, graph_context=None):
        """Use only original user text and validated, server-selected context.

        A product binding is useful evidence, never a grant of private access.
        No private archive/history is read to decide this ceiling. The callers
        validate ticket/graph context; model routing outputs are not inputs.
        """
        explicit=cls.from_text(text)
        if explicit.private_context_disabled:
            return cls(True, version='host-turn-data-scope/1', scope_reason='explicit_restriction')
        private_selected=(graph_context is not None or
            (page_context is not None and not cls.is_public_reference(page_context)))
        authorization_text=_scope_authorization_text(text)
        if (private_selected or _PRIVATE_REQUEST.search(authorization_text)
                or _active_private_request(text,authorization_text)):
            return cls(False, version='host-turn-data-scope/1')
        if _disclaimed_hypothesis_scope(text, authorization_text):
            return cls(True, version='host-turn-data-scope/1', scope_reason='disclaimed_hypothesis')
        public_product=(bool(_PUBLIC_PRODUCT.search(text)) or
            (cls.is_public_reference(page_context) and page_context.get('kind') == 'product'))
        return cls(bool(public_product), version='host-turn-data-scope/1', scope_reason='public_product' if public_product else 'normal')

    @property
    def allowed_tools(self):
        return PUBLIC_HYPOTHETICAL_TOOLS if self.private_context_disabled else None

    def permits(self, tool_name):
        return not self.private_context_disabled or tool_name in PUBLIC_HYPOTHETICAL_TOOLS

    def project_schemas(self, schemas):
        return [schema for schema in schemas
                if self.permits(schema.get('function', {}).get('name'))]

    def require_tool(self, tool_name):
        if not self.permits(tool_name):
            from simulated_bank import BankError
            raise BankError('REQUEST_PRIVATE_DATA_FORBIDDEN',
                            '本轮已禁止读取或使用个人资料，只能查询公共资料或计算明确的假设。', 403)

    def metadata_snapshot(self, gateway, workspace, *, db=None):
        """只保留本地绑定所需版本/业务日，绝不哈希个人资料正文。"""
        if db is None:
            with closing(gateway.bank.connect()) as connection:
                return self.metadata_snapshot(gateway, workspace, db=connection)
        from simulated_bank import BankError
        from user_memory import privacy_revision
        row = db.execute('SELECT version,as_of FROM workspaces WHERE id=?', (workspace,)).fetchone()
        if row is None:
            raise BankError('NOT_FOUND', '当前工作区不存在。', 404)
        return {'policy_version': self.version, 'workspace_version': row['version'],
                'as_of': row['as_of'], 'privacy_revision': privacy_revision(db, workspace),
                'personal_revision': gateway.personal_data.revision(workspace)}

    def metadata_context(self, gateway, workspace):
        snapshot = self.metadata_snapshot(gateway, workspace)
        wire = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(wire.encode()).hexdigest()
