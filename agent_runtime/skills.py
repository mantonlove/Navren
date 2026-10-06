"""仓库内技能的按需加载；技能描述不授予任何工具或业务权限。"""
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parent.parent / 'skills'
NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
REFERENCE = re.compile(r'[a-z0-9][a-z0-9._-]*\.md')
MAX_DOCUMENT_BYTES = 64 * 1024


def _read(path):
    # 不接受指向仓库外或其他技能的软链接，模型也不能指定任意文件路径。
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError('invalid_skill_file')
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('invalid_skill_file')
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError('skill_document_too_large')
    return path.read_text(encoding='utf-8')


def _definition(path):
    text = _read(path)
    if not text.startswith('---\n'):
        raise ValueError('invalid_skill_metadata')
    parts = text.split('---', 2)
    if len(parts) != 3:
        raise ValueError('invalid_skill_metadata')
    try:
        front = yaml.safe_load(parts[1])
    except yaml.YAMLError as error:
        raise ValueError('invalid_skill_metadata') from error
    if not isinstance(front, dict):
        raise ValueError('invalid_skill_metadata')
    name, description = front.get('name'), front.get('description')
    if (not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > 64
            or name != path.parent.name or not isinstance(description, str)
            or not 1 <= len(description.strip()) <= 1024):
        raise ValueError('invalid_skill_metadata')
    metadata = front.get('metadata', {})
    if not isinstance(metadata, dict):
        raise ValueError('invalid_skill_metadata')
    role = metadata.get('role', 'reviewer' if name == 'reviewer' else 'advisor')
    if role not in ('advisor', 'reviewer') or (name == 'reviewer' and role != 'reviewer'):
        raise ValueError('invalid_skill_role')
    tools = metadata.get('tools', [])
    if (not isinstance(tools, list) or len(tools) != len(set(map(str, tools)))
            or any(not isinstance(tool, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', tool)
                   for tool in tools)):
        raise ValueError('invalid_skill_tools')
    references = []
    directory = path.parent / 'references'
    if directory.exists():
        if directory.is_symlink():
            raise ValueError('invalid_skill_file')
        for reference in sorted(directory.glob('*.md')):
            if not REFERENCE.fullmatch(reference.name):
                raise ValueError('invalid_skill_reference')
            _read(reference)
            references.append(reference.name)
    return {'name': name, 'description': description.strip(), 'role': role,
            'tools': list(tools), 'references': references}


def definitions():
    """没有缓存：维护者新增技能后目录和模型工具枚举使用同一份注册。"""
    return [_definition(path) for path in sorted(ROOT.glob('*/SKILL.md'))]


def catalog():
    return [{key: item[key] for key in ('name', 'description', 'tools')}
            for item in definitions() if item['role'] == 'advisor']


def describe(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('unknown_skill')
    for item in definitions():
        if item['name'] == name:
            return item
    raise ValueError('unknown_skill')


def load(name, reference=None):
    """内部审核链可读 reviewer；产品主 Agent 必须使用 load_advisor。"""
    item = describe(name)
    if reference is None:
        return _read(ROOT / name / 'SKILL.md')
    if not isinstance(reference, str) or reference not in item['references']:
        raise ValueError('unknown_skill_reference')
    return _read(ROOT / name / 'references' / reference)


def load_advisor(name, reference=None):
    item = describe(name)
    if item['role'] != 'advisor':
        raise ValueError('reviewer_skill_forbidden')
    return {'skill': name, 'reference': reference, 'instructions': load(name, reference),
            'tools': item['tools'], 'references': item['references'],
            'notice': '技能仅提供方法；可调用范围、对象权限、精确计算和用户确认由业务代码决定。'}
