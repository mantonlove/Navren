"""只验证新增模拟资料投影及其与本人资料的兼容，不运行原验收组。"""
from copy import deepcopy
from datetime import date
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from base_runtime.account_state import AccountState, DEFAULTS, SCHEMA
from base_runtime.persona_display_profile import (
    PERSONAS, TEMPLATE, TEMPLATE_SHA256, VISIBLE_FIELDS, project_simulated_persona_profile,
)
from simulated_bank import BankError


class PersonaDisplayProfileTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.owner = 'a' * 32
        self.binding = {'registered_user_id': self.owner, 'workspace_id': '1' * 32,
                        'persona_id': 'tycoon'}
        self.state = AccountState.__new__(AccountState)
        self.state.store = SimpleNamespace(clock=lambda: 1792627200, protect=lambda value: value)
        self.state.binding = self.resolve_binding
        self.initial = {'sections': {'profile': deepcopy(DEFAULTS['profile']),
                                     'settings': deepcopy(DEFAULTS['settings'])},
                        'migration': {'legacy_profile': 'none', 'conflicts': []}}
        self.initial['sections']['profile']['nickname'] = '本人昵称'
        self.seed(self.initial)

    def tearDown(self):
        self.db.close()

    def resolve_binding(self, workspace, _):
        if workspace != self.binding['workspace_id']:
            raise BankError('ACCOUNT_STATE_BINDING_CHANGED', '工作区不属于当前会话。', 403)
        return dict(self.binding)

    def seed(self, value):
        self.db.execute('INSERT OR REPLACE INTO registered_account_state VALUES(?,?,?,?)',
                        (self.owner, 7, json.dumps(value, ensure_ascii=False), 1792627200))

    def read(self):
        return self.state._load(self.db, None, self.binding, migrate=False)[0]

    def test_four_complete_distinct_profiles_are_stable_without_sql_writes(self):
        original = '\n'.join(self.db.iterdump())
        changes = self.db.total_changes
        names, phones, birthdays = set(), set(), set()
        for number, persona in enumerate(sorted(PERSONAS), 1):
            self.binding.update(persona_id=persona, workspace_id=f'{number:032x}')
            state = self.read()
            view = state['simulated_persona_profile']
            self.assertEqual(view['status'], 'available')
            self.assertEqual(view['workspace_binding'], self.binding['workspace_id'])
            self.assertEqual(view['registered_user_id'], self.owner)
            self.assertEqual(view['persona_id'], persona)
            self.assertFalse(view['is_real_identity'])
            self.assertEqual(view['contact_use'], 'display_only')
            self.assertFalse(view['editable'])
            self.assertEqual(view['template_version'], TEMPLATE_SHA256)
            profile = view['profile']
            self.assertTrue(all(profile[field].strip() for field in VISIBLE_FIELDS))
            self.assertIn('模拟', profile['full_name'])
            self.assertTrue(profile['email'].endswith('@navren.example.invalid'))
            self.assertRegex(profile['simulation_contact_phone'], r'^SIM-PHONE-\*{4}-100[1-4]$')
            self.assertLess(date.fromisoformat(profile['birthday']), date(2026, 10, 22))
            self.assertEqual(view, project_simulated_persona_profile(dict(self.binding)))
            names.add(profile['full_name']); phones.add(profile['simulation_contact_phone'])
            birthdays.add(profile['birthday'])
        self.assertEqual((len(names), len(phones), len(birthdays)), (4, 4, 4))
        self.assertEqual(self.db.total_changes, changes)
        self.assertEqual('\n'.join(self.db.iterdump()), original)

    def test_custom_and_deliberately_cleared_personal_fields_stay_identical(self):
        value = deepcopy(self.initial)
        value['sections']['profile'].update(full_name='本人自填姓名', email='', phone='',
                                             city='本人城市', birthday='', occupation='')
        self.seed(value)
        original = '\n'.join(self.db.iterdump())
        for persona in PERSONAS:
            self.binding['persona_id'] = persona
            state = self.read()
            self.assertEqual(state['sections']['profile'], value['sections']['profile'])
            self.assertEqual(state['sections']['settings'], value['sections']['settings'])
            self.assertEqual(state['initialized_sections'], ['profile', 'settings'])
            self.assertEqual(state['revision'], 7)
        self.assertEqual('\n'.join(self.db.iterdump()), original)

    def test_uninitialized_and_conflicting_personal_profiles_are_not_seeded_or_merged(self):
        for migration in ({'legacy_profile': 'none', 'conflicts': []},
                          {'legacy_profile': 'conflict', 'conflicts': [{'store': 'personal_profiles'}]}):
            self.seed({'sections': {}, 'migration': migration})
            original = '\n'.join(self.db.iterdump())
            state = self.read()
            self.assertEqual(state['sections']['profile'], DEFAULTS['profile'])
            self.assertEqual(state['initialized_sections'], [])
            self.assertEqual(state['migration'], migration)
            self.assertEqual(state['simulated_persona_profile']['status'], 'available')
            self.assertEqual('\n'.join(self.db.iterdump()), original)

    def test_original_profile_save_never_contains_simulated_contacts(self):
        view = self.read()['simulated_persona_profile']
        personal = deepcopy(self.initial['sections']['profile'])
        personal.update(nickname='本人修改后的昵称', full_name='', email='', phone='')
        result = self.state._save(self.binding['workspace_id'],
                                 {'expected_revision': 7, 'section': 'profile', 'value': personal},
                                 None, self.db)
        stored = json.loads(self.db.execute('SELECT value FROM registered_account_state').fetchone()[0])
        self.assertEqual(stored['sections']['profile'], personal)
        self.assertNotIn('SIM-PHONE', json.dumps(stored))
        self.assertNotIn('.invalid', json.dumps(stored))
        self.assertEqual(result['simulated_persona_profile'], view)
        self.assertEqual(result['revision'], 8)

    def test_projection_is_not_a_writable_account_state_section(self):
        original = '\n'.join(self.db.iterdump())
        with self.assertRaises(BankError):
            self.state._save(self.binding['workspace_id'], {'expected_revision': 7,
                            'section': 'simulated_persona_profile', 'value': {}}, None, self.db)
        self.assertEqual('\n'.join(self.db.iterdump()), original)

    def test_profile_save_cannot_spoof_a_different_persona_or_workspace(self):
        with self.assertRaises(BankError):
            self.state._save('f' * 32, {'expected_revision': 7, 'section': 'profile',
                             'value': deepcopy(self.initial['sections']['profile'])}, None, self.db)
        with self.assertRaises(BankError):
            self.state._save(self.binding['workspace_id'], {'expected_revision': 7,
                             'section': 'profile', 'value': deepcopy(self.initial['sections']['profile']),
                             'persona_id': 'retired_entrepreneur'}, None, self.db)

    def test_invalid_binding_or_changed_template_cannot_borrow_another_persona(self):
        for value in ({}, {**self.binding, 'persona_id': 'other'},
                      {**self.binding, 'workspace_id': '../tycoon'}):
            result = project_simulated_persona_profile(value)
            self.assertEqual(result['status'], 'unavailable')
            self.assertNotIn('profile', result)
        with patch.object(type(TEMPLATE), 'read_bytes', return_value=b'{}'):
            result = project_simulated_persona_profile(self.binding)
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('profile', result)
        self.assertEqual(project_simulated_persona_profile(self.binding)['status'], 'available')

    def test_projection_instances_do_not_share_mutable_state(self):
        first = project_simulated_persona_profile(self.binding)
        first['profile']['full_name'] = '只改本次返回值'
        second = project_simulated_persona_profile(self.binding)
        self.assertNotEqual(first['profile']['full_name'], second['profile']['full_name'])
        self.assertEqual(second['registered_user_id'], self.owner)


if __name__ == '__main__':
    unittest.main()
