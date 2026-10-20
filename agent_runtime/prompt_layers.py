"""维护者登记的领域提示按范围延后；不改后台守卫，未知或已修改原文一律保留。"""
from collections.abc import Mapping
import re

import yaml

POLICY_LAYER_VERSION = 'trusted-domain-layers/1'

# 只有精确命中登记全文才允许延后。prefix供维护者识别；新增尾句不能被旧规则吞掉。
# always片段为原文中的通用安全/能力声明，关闭该领域时也逐字保留。
REGISTERED_DOMAIN_PARAGRAPHS = (
    {'prefix': '市场热点、新闻和周报使用market工具及market技能，', 'paragraph': '市场热点、新闻和周报使用market工具及market技能，真实市场时钟独立于模拟银行业务日。若server_context有market_context，先加载该真实对象，不能仅靠用户标题猜新闻。对市场事实用answer_reviewed并引用本轮证据。正文区分原始事实与Navren推论，metadata_only只提供原文阅读入口，不生成全文深度摘要；缺少行情端点不报周涨幅或用户组合收益，旧周报必须报实际期间。热点概览先给3至5条重点，每条简短说明；queried_at是查询时刻，as_of和published_at是原始发布时刻，近7日热点不能说成全是今天发布。公开政策金额应在claims中引用同轮市场证据，public_minor_currency不能当作用户余额、额度或可投资金。', 'domain': 'market', 'always': ''},
    {'prefix': '个人财务图谱问题先加载profile技能，调用persona', 'paragraph': '个人财务图谱问题先加载profile技能，调用personal_graph的question、view和期间，引用返回的确定性计算与完整证据。若server_context有graph_context，应把同一个node_id、edge_id或insight_id以及source、portfolio、from、to、expected_version传给personal_graph。支出频率不等于偏好，saved_minor不等于目标分配；解锁不等于到账；规划底线缺口不等于负银行余额。情景只能用graph_scenario，不由模型计算金融金额；共同风险没有实际行业、底层权重与条款时说明缺口。回答这些分析用answer_reviewed。', 'domain': 'graph', 'always': ''},
    {'prefix': '服务协商用service_offer_rules及servi', 'paragraph': '服务协商用service_offer_rules及service_offer_preview，预览条件只从本轮原话取值，默认不向银行代理披露个人记忆。服务申请状态用service_application_status真实回执；预览不是提交申请、资格获批或优惠兑现。没有接受/提交工具，不承诺银行未授权的优惠；本人在工作台另行确认。说明规则给出的条件、期限与代价，拒绝理财购买的底线不能改写。', 'domain': 'service', 'always': ''},
    {'prefix': '所有产品功能都能在本对话查询与准备操作，不能只让用户去页面找', 'paragraph': '所有产品功能都能在本对话查询与准备操作，不能只让用户去页面找。日历/每日收支/未来待缴/生日等用calendar_query及详情工具；相对时间使用工具period，采用后端业务日期，不猜今天。', 'domain': 'calendar', 'always': '所有产品功能都能在本对话查询与准备操作，不能只让用户去页面找。'},
    {'prefix': '日历工具产生widget_id，在终结工具widget_id', 'paragraph': '日历工具产生widget_id，在终结工具widget_ids引用本轮组件；正文简短说明范围、来源及限制。事件创建/修改/删除用calendar_prepare_event，调用成功后展示其确认卡。用户确认在卡片完成，不由聊天文字执行。', 'domain': 'calendar', 'always': ''},
    {'prefix': '有日历组件时正文控制在两三句话，不逐日复写组件明细', 'paragraph': '有日历组件时正文控制在两三句话，不逐日复写组件明细。必须用终结工具，并引用对应widget_ids完成回复，不直接输出遗漏组件的普通文本。', 'domain': 'calendar', 'always': ''},
    {'prefix': '事件是否已保存，以server_context.calend', 'paragraph': '事件是否已保存，以server_context.calendar_events和calendar_draft_receipts的真实状态为准。历史助手回复是确认前文字，不能据此说事件仍未保存。修改/删除先用真实series_id，查不到时calendar_query查原事件，不改成创建；同名多个对象且不能由上下文明确区分时澄清。', 'domain': 'calendar', 'always': ''},
    {'prefix': '生日预算仅保存计划，不能当已消费/支付；每月末/闰日规则或编', 'paragraph': '生日预算仅保存计划，不能当已消费/支付；每月末/闰日规则或编辑范围含混需澄清。只支持公历。生日用户主动授权创建不等于开启画像记忆。', 'domain': 'calendar', 'always': ''},
    {'prefix': '用户本轮明确陈述自己的目标/偏好/生活安排时，可以用reme', 'paragraph': '用户本轮明确陈述自己的目标/偏好/生活安排时，可以用remember_profile记录待核实画像；只引用本人原话。用户未启用记忆则不保存。画像不授予权限、不改正式风险等级。', 'domain': 'profile_write', 'always': '用户未启用记忆则不保存。画像不授予权限、不改正式风险等级。'},
    {'prefix': '收益查询先wealth_versions，再用wealth_', 'paragraph': '收益查询先wealth_versions，再用wealth_product或calendar_query(portfolio=historical)读取。独立历史模拟组合的真实净值不代表本人真实持仓，不能和当前现金相加。四板块cashflow/wealth/plans/preferences贯通同一个对象版本。费用、收益只能引用确定性计算，不用七日年化/365替代日收益。真实产品资料查public_products；它与模拟products有独立来源。公开资料过期时明确说明，不能称为最新在售或承诺收益，不用历史在售标签做当前推荐。', 'domain': 'wealth', 'always': ''},
    {'prefix': '先查询付款账户及对应business_records，买入先', 'paragraph': '先查询付款账户及对应business_records，买入先查products与budget。转账对象有重名或未核验则只问缺失值，不猜。', 'domain': 'operation', 'always': ''},
    {'prefix': '每轮只能准备一项操作', 'paragraph': '每轮只能准备一项操作。复合请求先准备第一项，明确下一项等第一项确认后查询原operation_status实际回执再重算和确认。', 'domain': 'operation', 'always': '每轮只能准备一项操作。'},
    {'prefix': '已有draft在当前上下文列出；用户修改金额/对象，重新读取', 'paragraph': '已有draft在当前上下文列出；用户修改金额/对象，重新读取当前数据再准备替代；用户撤回用withdraw_draft使旧卡失效。', 'domain': 'operation', 'always': ''},
    {'prefix': '用户明确要求关闭卡片控制、取消模拟订阅等且对象唯一时，应调用', 'paragraph': '用户明确要求关闭卡片控制、取消模拟订阅等且对象唯一时，应调用prepare_operation，不要反复让用户确认是否要生成草案。', 'domain': 'operation', 'always': ''},
    {'prefix': '本机支持cancel_subscription模拟商户取消；', 'paragraph': '本机支持cancel_subscription模拟商户取消；档案写“无真实取消能力”不代表没有模拟取消工具。模拟与真实的能力边界要说清楚。', 'domain': 'operation', 'always': ''},

)


def _names(available_tools):
    if available_tools is None or isinstance(available_tools, (str, Mapping)):
        return None
    names = set()
    for item in available_tools:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, Mapping) and isinstance(item.get('function'), Mapping):
            name = item['function'].get('name')
            if not isinstance(name, str):
                return None
            names.add(name)
        else:
            # 未知范围不能解释为已关闭能力，安全回退为完整policy。
            return None
    return names


def _active_domains(names, context):
    context = context if isinstance(context, Mapping) else {}
    if isinstance(context.get('server_context'), Mapping):
        context = {**context, **context['server_context']}
    page = context.get('page_context')
    page_kind = page.get('kind') if isinstance(page, Mapping) else None
    market_selected = bool(context.get('market_context') or context.get('market_context_read')) or page_kind in ('event', 'article', 'weekly', 'market')
    graph_selected = bool(context.get('graph_context')) or page_kind in ('graph', 'personal_graph')
    calendar_selected = page_kind == 'calendar' or any(context.get(key) for key in ('recent_calendar_widgets', 'calendar_events', 'calendar_draft_receipts'))
    operation_selected = bool(context.get('recent_operations')) or page_kind in ('operation', 'draft')
    return {
        'market': market_selected or any(name.startswith('market_') for name in names),
        'graph': graph_selected or bool(names & {'personal_graph', 'graph_scenario'}),
        'service': any(name.startswith('service_') for name in names),
        'calendar': calendar_selected or any(name.startswith('calendar_') for name in names),
        'profile_write': 'remember_profile' in names,
        'wealth': bool(names & {'wealth_versions', 'wealth_product'}),
        'operation': operation_selected or bool(names & {'prepare_operation', 'withdraw_draft'}),
    }


def layer_policy(policy, available_tools, *, context=None):
    """返回原文可回退视图；调用者只在显式progressive策略使用，full直接用原policy。"""
    if not isinstance(policy, str):
        raise TypeError('policy_must_be_string')
    names = _names(available_tools)
    if names is None:
        return policy
    active = _active_domains(names, context)
    registered = {rule['paragraph']: rule for rule in REGISTERED_DOMAIN_PARAGRAPHS}
    result = []
    for line in policy.splitlines(keepends=True):
        paragraph = line.rstrip('\r\n')
        rule = registered.get(paragraph)
        if rule is None or active[rule['domain']]:
            result.append(line)
        elif rule['always']:
            ending = line[len(paragraph):]
            result.append(rule['always'] + ending)
    return ''.join(result)


# 精确登记已在正文工作流重复展开的方法来源介绍；保留其中的后端权限声明。
_SOURCE_INTRODUCTIONS = {
    '方法适配自 `anthropics/knowledge-work-plugins` 的 `search-strategy`、`knowledge-synthesis` 和 `validate-data`：保留问题拆解、原文归因、比较口径与缺项核验。版本/许可由 `skills/upstream/provenance.json` 登记；中国银行产品身份、收益口径和适当性规则由 Navren 维护。': '',
    '方法适配自 `anthropics/knowledge-work-plugins` 的 `search-strategy` 与 `knowledge-synthesis`：保留问题拆解、来源排序、去重、冲突呈现和逐项归因；来源版本与许可由 `skills/upstream/provenance.json` 统一登记。Navren 的检索权限与证据合同以本地后端为准。': 'Navren 的检索权限与证据合同以本地后端为准。',
}


def method_body(document):
    """仅供已受信本地技能源使用；完整原文和SHA由调用者保存以便恢复与核验。"""
    if not isinstance(document, str):
        raise TypeError('document_must_be_string')
    match = re.match(r'^---\n(.*?)\n---\n', document, re.S)
    if match is None:
        return document
    try:
        header = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return document
    if (not isinstance(header, dict) or set(header) != {'name', 'description', 'metadata'}
            or not isinstance(header['name'], str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', header['name'])
            or not isinstance(header['description'], str)
            or not isinstance(header['metadata'], dict)
            or set(header['metadata']) != {'role', 'tools', 'delivery'}
            or header['metadata']['role'] not in ('advisor', 'reviewer')
            or header['metadata']['delivery'] not in ('native', 'adapted')
            or not isinstance(header['metadata']['tools'], list)
            or any(not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', name)
                   for name in header['metadata']['tools'])):
        # 新frontmatter字段可能是安全或业务约束，未经登记不能丢弃。
        return document
    body = document[match.end():].lstrip('\n')
    blocks = body.split('\n\n')
    return '\n\n'.join(_SOURCE_INTRODUCTIONS.get(block, block) for block in blocks
                       if _SOURCE_INTRODUCTIONS.get(block, block))
