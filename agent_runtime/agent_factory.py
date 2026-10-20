"""受信启动配置创建统一 Agent；HTTP/模型不能调用该选择接口。"""
from .complex_agent import ComplexConversationAgent

ROUTE_PROVIDERS = ('off', 'native', 'jev')


def create_agent(*, route_provider='off', primary=None, reviewer=None, judge=None,
                 max_rounds=8, max_tools=12, max_tokens=100000,
                 timeout_seconds=105):
    """固定 progressive 和既有渠道校验；客户端注入仅来自受信Python调用者。"""
    if not isinstance(route_provider, str) or route_provider not in ROUTE_PROVIDERS:
        raise ValueError('invalid_route_provider')
    return ComplexConversationAgent(primary=primary, reviewer=reviewer, judge=judge,
        route_provider=route_provider, tool_routing='progressive',
        max_rounds=max_rounds, max_tools=max_tools, max_tokens=max_tokens,
        timeout_seconds=timeout_seconds)
