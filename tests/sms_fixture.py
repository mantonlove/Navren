"""短信强验证的隔离替身：只服务测试，不连接任何真实短信通道。

生产代码要求 red 操作先 start_sms_verification 再 verify 才算授权；旧测试直接拿
demo-code 调 verify 的方式已被取消。本模块把这条两步流程收敛成 authorize()，
并在遇到同一 workspace 的发送限流时按需推进测试时钟，避免每个测试重复写。
"""
from simulated_bank import BankError
from simulated_bank.sms_mfa import SmsVerified

CODE = '975319'  # 隔离夹具验证码，绝非真实短信验证码。


class LocalSms:
    """与 tests/test_sms_permissions.py 同型的测试替身，供两个模块共用。"""

    source = 'test_double'

    def __init__(self, clock):
        self.clock = clock
        self.sends = 0
        self.verifications = 0
        self.after_verify = lambda: None
        self.transform = lambda proof: proof

    def send(self, binding, *, deadline):
        self.sends += 1
        return self.clock() < deadline

    def verify(self, binding, code, *, deadline):
        self.verifications += 1
        if code != CODE:
            return None
        proof = SmsVerified('local-test', 'proof-' + binding.challenge_id,
                            'fixture-registered-phone', binding, self.clock(), binding.expires_at)
        self.after_verify()
        return self.transform(proof)


def request_challenge(bank, workspace, operation, *, advance_clock=None, attempts=4):
    """取短信挑战；发送限流时按测试时钟推进后重试，窗口语义仍由生产代码判定。"""
    for _ in range(attempts):
        try:
            return bank.start_sms_verification(workspace, operation['id'])
        except BankError as error:
            if error.code != 'SMS_SEND_RATE_LIMITED' or advance_clock is None:
                raise
            advance_clock(61)
    return bank.start_sms_verification(workspace, operation['id'])


def authorize(bank, workspace, operation, *, advance_clock=None):
    """完成 start_sms_verification → verify，返回可直接确认的 verification_token。"""
    challenge = request_challenge(bank, workspace, operation, advance_clock=advance_clock)
    grant = bank.verify(workspace, operation['id'],
                        {'verification_code': CODE, 'challenge_id': challenge['challenge_id']})
    return grant['verification_token']
