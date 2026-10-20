"""阿里云官方 SDK 短信通道；凭据、号码和验证码均不写日志。"""
from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import sqlite3


class SmsFailure(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


SCHEMES = {purpose: 'navren-' + purpose for purpose in ('register', 'login', 'bind', 'operation')}
SCHEMES['financial_recovery'] = 'navren-fin-recovery'
DEFAULT_CONFIG = Path.home() / 'Library/Application Support/Navren/sms-config.json'


def _credential(name):
    value = os.environ.get(name, '').strip()
    if value:
        return value
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    if directory:
        filename = {'NAVREN_SMS_ACCESS_KEY_ID': 'navren-sms-access-key-id',
                    'NAVREN_SMS_ACCESS_KEY_SECRET': 'navren-sms-access-key-secret'}[name]
        path = Path(directory) / filename
        if path.is_file():
            return path.read_text().strip()
    if sys.platform == 'darwin':
        try:
            result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a',
                os.environ.get('USER') or getpass.getuser(), '-s', 'CC/' + name, '-w'],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return ''
        if result.returncode == 0:
            return result.stdout.rstrip('\r\n')
    return ''


class AliyunSmsTransport:
    source = 'provider'

    def __init__(self, config, *, client=None, clock=time.time):
        self.config = dict(config)
        self.clock = clock
        self._client = client
        self._lock = threading.Lock()
        self.enabled = self.config.get('enabled') is True
        self.sign_name = self.config.get('sign_name', '')
        self.templates = self.config.get('templates', {})
        self.allowed_numbers = frozenset(self.config.get('allowed_numbers', []))
        self.restricted = self.config.get('mode', 'restricted') != 'production'
        self.daily_limit = self._limit('daily_limit', 10, 10000)
        self.phone_daily_limit = self._limit('phone_daily_limit', 5, 100)
        self.ip_hourly_limit = self._limit('ip_hourly_limit', 5, 1000)
        self._credentials = None
        self.send_budget = self._limit('max_send_attempts', 10, 1000000)
        self.budget_id = self.config.get('budget_id', 'navren-sms-integration-20261015')
        self.budget_database = Path(self.config.get('budget_database') or
                                    DEFAULT_CONFIG.with_name('sms-budget.sqlite3'))

    def _reserve_send_budget(self):
        self.budget_database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.budget_database.is_symlink():
            raise SmsFailure('SMS_CONFIG_INVALID', '短信计数数据库不能使用符号链接。', 503)
        fd = os.open(self.budget_database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.budget_database, 0o600)
        with sqlite3.connect(self.budget_database, timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS send_budgets(id TEXT PRIMARY KEY,attempts INTEGER NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO send_budgets VALUES(?,0)', (self.budget_id,))
            changed = db.execute('UPDATE send_budgets SET attempts=attempts+1 WHERE id=? AND (? IS NULL OR attempts<?)',
                                 (self.budget_id, self.send_budget, self.send_budget)).rowcount
            if changed != 1:
                raise SmsFailure('SMS_TEST_BUDGET_REACHED', '短信联调已达授权发送上限，请联系管理员。', 429)

    def _limit(self, name, default, maximum):
        value = self.config.get(name, default)
        # 只有显式生产配置可取消全站额度；单号码、IP频率与受限联调额度保持。
        if not self.restricted and name in {'daily_limit', 'max_send_attempts'} and value is None:
            return None
        if type(value) is not int or not 1 <= value <= maximum:
            raise SmsFailure('SMS_CONFIG_INVALID', '短信发送限额配置无效。', 503)
        return value

    @property
    def configured(self):
        if (not self.enabled or not isinstance(self.sign_name, str) or not self.sign_name
                or not isinstance(self.templates, dict)
                or any(not isinstance(self.templates.get(k), str) or not self.templates[k]
                       for k in ('register', 'login', 'bind', 'operation'))
                or self.restricted and not self.allowed_numbers):
            return False
        if self._client is not None:
            return True
        with self._lock:
            if self._credentials is None:
                self._credentials = (_credential('NAVREN_SMS_ACCESS_KEY_ID'),
                                     _credential('NAVREN_SMS_ACCESS_KEY_SECRET'))
        return all(self._credentials)

    def supports_purpose(self, purpose):
        return (purpose in SCHEMES and self.configured and
                isinstance(self.templates.get(purpose), str) and bool(self.templates[purpose]))

    def permit_number(self, phone):
        if not isinstance(phone, str) or re.fullmatch(r'1[3-9][0-9]{9}', phone) is None:
            raise SmsFailure('SMS_PHONE_INVALID', '请输入正确的11位手机号。')
        if self.restricted and phone not in self.allowed_numbers:
            raise SmsFailure('SMS_RESTRICTED_NUMBER', '目前仅向已授权的测试手机号发送短信。', 403)

    def _sdk(self):
        if not self.configured:
            raise SmsFailure('SMS_NOT_CONFIGURED', '短信服务尚未配置完成。', 503)
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from alibabacloud_dypnsapi20170525.client import Client
                    from alibabacloud_tea_openapi.models import Config
                    access_id, secret = self._credentials
                    self._client = Client(Config(access_key_id=access_id, access_key_secret=secret,
                                                 endpoint='dypnsapi.aliyuncs.com', protocol='https'))
        return self._client

    def _runtime(self, deadline):
        from alibabacloud_tea_util.models import RuntimeOptions
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise SmsFailure('SMS_TIMEOUT', '短信验证请求已超时，请重新发送。', 504)
        total_ms = max(2, int(min(remaining, 8) * 1000))
        connect_ms = min(2000, max(1, total_ms // 3))
        return RuntimeOptions(autoretry=False, max_attempts=1, ignore_ssl=False,
                              connect_timeout=connect_ms, read_timeout=total_ms-connect_ms)

    @staticmethod
    def _body(response):
        body = response.body.to_map() if response and response.body else {}
        if not isinstance(body, dict):
            return {}
        return body

    def send(self, phone, purpose, reference, *, expires_at, deadline):
        self.permit_number(phone)
        if purpose not in SCHEMES:
            raise SmsFailure('SMS_PURPOSE_INVALID', '短信用途无效。')
        if purpose == 'financial_recovery' and not self.supports_purpose(purpose):
            raise SmsFailure('SMS_NOT_CONFIGURED', '金融锁恢复短信用途尚未配置。', 503)
        from alibabacloud_dypnsapi20170525.models import SendSmsVerifyCodeRequest
        seconds = min(300, int(expires_at - self.clock()))
        if seconds < 1:
            raise SmsFailure('SMS_EXPIRED', '验证已过期，请重新发送。')
        request = SendSmsVerifyCodeRequest(phone_number=phone, country_code='86',
            sign_name=self.sign_name, template_code=self.templates[purpose],
            template_param=json.dumps({'code': '##code##', 'min': str(math.ceil(seconds/60))}),
            scheme_name=SCHEMES[purpose], out_id=reference, code_length=6, code_type=1,
            valid_time=seconds, interval=60, duplicate_policy=1, return_verify_code=False, auto_retry=0)
        try:
            client = self._sdk()
            self._runtime(deadline)
            self._reserve_send_budget()
            runtime = self._runtime(min(deadline, expires_at))
            request.valid_time = min(300, int(expires_at - self.clock()))
            if request.valid_time < 1:
                raise SmsFailure('SMS_EXPIRED', '验证已过期，请重新发送。')
            request.template_param = json.dumps({'code': '##code##', 'min': str(math.ceil(request.valid_time/60))})
            body = self._body(client.send_sms_verify_code_with_options(request, runtime))
        except SmsFailure:
            raise
        except Exception:
            raise SmsFailure('SMS_SEND_UNAVAILABLE', '短信发送暂时不可用，请稍后再试。', 503) from None
        if self.clock() >= deadline or body.get('Success') is not True or body.get('Code') != 'OK':
            raise SmsFailure('SMS_SEND_FAILED', '短信未能成功提交，请稍后再试。', 503)
        # 仅保留不含号码、验证码和响应正文的凭据引用。
        return hashlib.sha256((str(body.get('RequestId', '')) + ':' + reference).encode()).hexdigest()

    def verify(self, phone, purpose, reference, code, *, deadline):
        self.permit_number(phone)
        if purpose not in SCHEMES or not isinstance(code, str) or re.fullmatch(r'[0-9]{6}', code) is None:
            return None
        from alibabacloud_dypnsapi20170525.models import CheckSmsVerifyCodeRequest
        request = CheckSmsVerifyCodeRequest(phone_number=phone, country_code='86',
            scheme_name=SCHEMES[purpose], out_id=reference, verify_code=code, case_auth_policy=2)
        try:
            body = self._body(self._sdk().check_sms_verify_code_with_options(request, self._runtime(deadline)))
        except Exception:
            raise SmsFailure('SMS_VERIFY_UNAVAILABLE', '验证码核验暂时不可用，请重新发送。', 503) from None
        if self.clock() >= deadline:
            raise SmsFailure('SMS_TIMEOUT', '验证码核验已超时，请重新发送。', 504)
        if (body.get('Success') is True and body.get('Code') == 'OK'
                and isinstance(body.get('Model'), dict) and body['Model'].get('VerifyResult') == 'PASS'):
            return hashlib.sha256((str(body.get('RequestId', '')) + ':' + purpose + ':' + reference).encode()).hexdigest()
        return None


def load_sms_transport(config_path=None):
    directory = os.environ.get('CREDENTIALS_DIRECTORY')
    path = Path(config_path or os.environ.get('NAVREN_SMS_CONFIG') or
                (Path(directory)/'navren-sms-config' if directory else DEFAULT_CONFIG))
    if not path.is_file():
        return None
    if path.stat().st_mode & 0o077:
        raise SmsFailure('SMS_CONFIG_PERMISSIONS', '短信配置文件权限应为0600。', 503)
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        raise SmsFailure('SMS_CONFIG_INVALID', '短信配置文件无法读取。', 503) from None
    if not isinstance(config, dict):
        raise SmsFailure('SMS_CONFIG_INVALID', '短信配置文件格式无效。', 503)
    return AliyunSmsTransport(config)
