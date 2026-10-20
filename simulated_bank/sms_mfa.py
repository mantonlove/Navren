"""短信渠道的受信部署接口；验证码由提供商验证，不落盘、不交给模型。"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SmsBinding:
    challenge_id: str
    subject: str
    operation_id: str
    summary_sha256: str
    business_day: str
    policy_version: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True)
class SmsVerified:
    provider_id: str
    proof_reference: str
    phone_binding_reference: str
    binding: SmsBinding
    verified_at: float
    expires_at: float


class SmsProvider(Protocol):
    """只从服务端注册注入，不能由客户端选择。

    实际 adapter 必须从受信身份库解析绑定手机号，使用供应商的发送及
    验证接口，并执行所给截止时间。接口不得返回 OTP/手机号/消息正文。
    source='test_double' 只允许隔离测试，永不表示真实短信验收。
    目前未配置生产 adapter，也不会自动发短信。
    """
    source: str  # 'provider' 或 'test_double'

    def send(self, binding: SmsBinding, *, deadline: float) -> bool: ...

    def verify(self, binding: SmsBinding, code: str, *, deadline: float) -> SmsVerified | None: ...
