"""按可信体验角色投影虚构展示资料，不写本人资料或银行身份。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


CONTRACT = 'navren.simulated-persona-profile/1'
TEMPLATE = Path(__file__).resolve().parents[1] / 'simulated_bank/fixtures/persona_display_profiles.json'
TEMPLATE_SHA256 = '83f5678099f0aec1e5a2d7c79ad35f1dbfebb748fe55e0c8cb46eee8d31c8be2'
PERSONAS = frozenset(('tycoon', 'grain_farmer', 'working_landlady', 'retired_entrepreneur'))
VISIBLE_FIELDS = ('full_name', 'email', 'simulation_contact_phone', 'city', 'occupation', 'birthday')


def _read_templates():
    raw = TEMPLATE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TEMPLATE_SHA256:
        raise ValueError('SIMULATED_PERSONA_TEMPLATE_CHANGED')
    value = json.loads(raw)
    if (value.get('schema') != 'navren.simulated-persona-profile-templates/1'
            or value.get('data_origin') != 'navren_synthetic'
            or value.get('is_real_identity') is not False
            or value.get('contact_use') != 'display_only'
            or set(value.get('profiles', {})) != PERSONAS):
        raise ValueError('SIMULATED_PERSONA_TEMPLATE_INVALID')
    for persona, profile in value['profiles'].items():
        if (profile.get('persona_id') != persona or profile.get('data_origin') != 'navren_synthetic'
                or profile.get('is_simulated') is not True
                or any(type(profile.get(key)) is not str or not profile[key].strip()
                       for key in VISIBLE_FIELDS)
                or not profile['email'].endswith('@navren.example.invalid')
                or re.fullmatch(r'SIM-PHONE-\*{4}-100[1-4]', profile['simulation_contact_phone']) is None):
            raise ValueError('SIMULATED_PERSONA_TEMPLATE_INVALID')
    return value['profiles']


def project_simulated_persona_profile(binding):
    """binding 仅由 AccountState 的现有可信会话校验提供，无客户端角色参数。"""
    unavailable = {'contract': CONTRACT, 'status': 'unavailable',
                   'data_origin': 'navren_synthetic', 'is_simulated': True,
                   'is_real_identity': False, 'contact_use': 'display_only', 'editable': False}
    if (type(binding) is not dict or binding.get('persona_id') not in PERSONAS
            or any(type(binding.get(key)) is not str or re.fullmatch(r'[a-f0-9]{32}', binding[key]) is None
                   for key in ('registered_user_id', 'workspace_id'))):
        return {**unavailable, 'reason': 'SIMULATED_PERSONA_BINDING_UNAVAILABLE'}
    try:
        profile = _read_templates()[binding['persona_id']]
    except (OSError, ValueError, TypeError, KeyError):
        # 附加的展示资料缺失时，本人资料与原有保存接口继续可用。
        return {**unavailable, 'reason': 'SIMULATED_PERSONA_TEMPLATE_UNAVAILABLE'}
    return {**unavailable, 'status': 'available',
            'registered_user_id': binding['registered_user_id'],
            'workspace_binding': binding['workspace_id'], 'persona_id': binding['persona_id'],
            'template_version': TEMPLATE_SHA256, 'profile': deepcopy(profile)}
