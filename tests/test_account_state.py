"""真实注册实例、加密私库及兼容资料入口；隔离、无付费提供器。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import patch
import uuid

import test_registered_instances as fixture_module
from base_runtime.account_state import DEFAULTS
from base_runtime.service_assembly import AssembledGateway
from personal_profile import FIELDS
from simulated_bank import BankError
from user_memory import privacy_revision


class AccountStateTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture_module.RegisteredInstancesTests()
        self.f.gateway = lambda registered=True: AssembledGateway(self.f.path,
            financial_database=fixture_module.FINANCIAL, registered_mode=registered)
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.g = self.f.g; self.a = self.f.select(); self.w = self.a['workspace_id']

    @contextmanager
    def scope(self, lease=None):
        lease = lease or self.a
        with self.g.demo_sessions.request(self.f.token(lease), lease['workspace_id']): yield lease['workspace_id']

    def read(self, lease=None):
        with self.scope(lease) as w: return self.g.account_state.read(w)

    def write(self, section, value, revision=None, lease=None, **extra):
        with self.scope(lease) as w:
            if revision is None: revision = self.g.account_state.read(w)['revision']
            return self.g.account_state.save(w, {'expected_revision': revision, 'section': section, 'value': value, **extra})

    def deny(self, code, fn):
        with self.assertRaises(BankError) as error: fn()
        self.assertEqual(error.exception.code, code)

    def test_owner_isolation_persona_sharing_encryption_and_same_key_restart(self):
        with closing(self.g.bank.connect()) as db:
            bank_before = tuple(db.execute('SELECT version FROM workspaces WHERE id=?', (self.w,)).fetchone())
            privacy_before = privacy_revision(db, self.w)
        personal_before = self.g.personal_data.revision(self.w)
        profile = {**DEFAULTS['profile'], 'nickname': '本人资料加密标记', 'city': '杭州'}
        saved = self.write('profile', profile)
        prefs = {**DEFAULTS['preferences'], 'weeklyReport': False}
        self.write('preferences', prefs)
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(tuple(db.execute('SELECT version FROM workspaces WHERE id=?', (self.w,)).fetchone()), bank_before)
            self.assertEqual(privacy_revision(db, self.w), privacy_before)
        self.assertEqual(self.g.personal_data.revision(self.w), personal_before)
        second = self.f.select(owner=uuid.uuid4().hex, subject='welcome-'+uuid.uuid4().hex)
        self.assertEqual(self.read(second)['sections']['profile']['nickname'], '我的账户')
        self.assertEqual(self.read(second)['revision'], 0)
        persona = self.f.select('grain_farmer', expected=self.w)
        self.assertEqual(self.read(persona)['sections']['profile'], profile)
        self.assertFalse(self.read(persona)['sections']['preferences']['weeklyReport'])
        # 本人档案只写一次；persona财务基线不因共同资料写入改变。
        with closing(sqlite3.connect(self.g.personal_data.database)) as db:
            raw = db.execute('SELECT value FROM registered_account_state WHERE registered_user_id=?', (self.f.owner,)).fetchone()[0]
            self.assertTrue(raw.startswith('pd1:')); self.assertNotIn('本人资料加密标记', raw)
        with self.scope(persona) as w:
            restarted = AssembledGateway(self.f.path, financial_database=fixture_module.FINANCIAL, registered_mode=True)
            restarted.bank.clock = self.g.bank.clock
            with restarted.demo_sessions.request(self.f.token(persona), w):
                actual = restarted.account_state.read(w)
            self.assertEqual(actual['sections']['profile'], profile)
            self.assertEqual(actual['revision'], saved['revision']+1)

    def test_cas_forgery_migration_and_late_send(self):
        initial = self.read()
        settings = {**deepcopy(DEFAULTS['settings']), 'theme': 'dark', 'mode_font_sizes': {'senior': 23}}
        saved = self.write('settings', settings, initial['revision'])
        self.deny('ACCOUNT_STATE_VERSION_CONFLICT', lambda: self.write('settings', settings, initial['revision']))
        self.deny('ACCOUNT_STATE_INVALID', lambda: self.write('settings', settings, registered_user_id=self.f.owner))
        source = {'kind': 'owned_browser_cache', 'workspace_id': self.w, 'auth_subject': self.f.subject, 'privacy_revision': self.a['privacy_revision']}
        self.deny('ACCOUNT_STATE_ALREADY_INITIALIZED', lambda: self.write('settings', settings, operation='migrate', source=source))
        self.deny('ACCOUNT_STATE_MIGRATION_BINDING_CHANGED', lambda: self.write('preferences', DEFAULTS['preferences'], operation='migrate', source={**source, 'auth_subject': 'welcome-'+uuid.uuid4().hex}))
        self.write('preferences', DEFAULTS['preferences'], operation='migrate', source=source)
        with self.scope():
            self.deny('ACCOUNT_STATE_VERSION_CONFLICT', lambda: self._send(saved['revision']))
        self.g.experience_provider.revoke_subject(self.f.owner, self.f.subject)
        with self.scope():
            with self.assertRaises(BankError): self.g.account_state.read(self.w)
        with self.assertRaises(BankError): self.g.account_state.read(self.w)

    def _send(self, revision):
        with self.g.account_state.send_guard(self.w, revision): pass

    def test_private_favorites_keep_workspace_and_public_version_metadata(self):
        public = {'category': 'market_weekly', 'object_kind': 'weekly', 'object_id': '2026-W40',
                  'version': 3, 'archive': {'edition_date': '2026-10-03', 'revision': 2, 'event_version': 3}, 'text': '公开周报'}
        private = {'text': '本人财务收藏', 'workspace_id': self.w, 'registered_user_id': self.f.owner, 'privacy_revision': self.a['privacy_revision']}
        self.write('favorites', [public, private])
        persona = self.f.select('grain_farmer', expected=self.w)
        self.assertEqual(self.read(persona)['sections']['favorites'], [public])
        w2 = persona['workspace_id']
        self.write('favorites', [public, {**private, 'workspace_id': w2, 'text': '农户私人收藏'}], lease=persona)
        self.assertEqual(len(self.read(persona)['sections']['favorites']), 2)
        with self.scope(persona) as w:
            self.deny('ACCOUNT_STATE_FOREIGN_FAVORITE', lambda: self.g.account_state.save(w, {'section': 'favorites', 'value': [private], 'expected_revision': self.read(persona)['revision']}))
        back = self.f.select('tycoon', expected=w2)
        self.assertEqual(self.read(back)['sections']['favorites'], [private, public])
        self.write('favorites', [], lease=back)
        again = self.f.select('grain_farmer', expected=self.w)
        self.assertEqual(self.read(again)['sections']['favorites'][0]['text'], '农户私人收藏')

    def test_legacy_profile_migration_conflicts_and_compatibility_one_canonical_store(self):
        legacy = {f: DEFAULTS['profile'][f] for f in FIELDS}; legacy['nickname'] = '原资料'
        with closing(self.g.personal_data.connect()) as db, db:
            db.execute('INSERT INTO personal_profiles VALUES(?,?,?,?)', (self.w, self.g.personal_data.protect(json.dumps(legacy)), 4, self.f.now[0]))
        state = self.read(); self.assertEqual(state['migration']['legacy_profile'], 'imported')
        self.assertEqual(state['sections']['profile']['nickname'], '原资料')
        with self.scope():
            old = self.g.personal_profile.read(self.w)
            clean = {f: old['profile'][f] for f in FIELDS}; clean['nickname'] = '新资料'
            changed = self.g.personal_profile.save(self.w, {'expected_revision': old['profile']['revision'], 'profile': clean})
            display = self.g.account_profile.read(self.w)
            self.assertEqual(display['display']['display_name'], '新资料')
            self.g.account_profile.update_display(self.w, {'expected_version': display['expected_version'], 'display_name': '统一资料', 'avatar': 'wave', 'confirmed': True})
            self.assertEqual(self.g.personal_profile.read(self.w)['profile']['nickname'], '统一资料')
        with closing(self.g.personal_data.connect()) as db:
            self.assertEqual(json.loads(db.execute('SELECT value FROM personal_profiles WHERE workspace_id=?', (self.w,)).fetchone()[0])['nickname'], '原资料')
            self.assertEqual(db.execute("SELECT count(*) FROM user_memories WHERE workspace_id=? AND id='account-display'", (self.w,)).fetchone()[0], 0)

    def test_two_concurrent_cas_writes_only_one_commits(self):
        revision = self.read()['revision']
        def write(theme):
            try: return self.write('settings', {**deepcopy(DEFAULTS['settings']), 'theme': theme}, revision)['revision']
            except BankError as e: return e.code
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(write, ('light', 'dark')))
        self.assertCountEqual(results, [revision+1, 'ACCOUNT_STATE_VERSION_CONFLICT'])

    def test_conflicting_legacy_profiles_remain_for_review_without_arbitrary_merge(self):
        with closing(self.g.bank.connect()) as bank:
            other = bank.execute('SELECT workspace_id FROM registered_user_instances WHERE registered_user_id=? AND workspace_id!=? LIMIT 1', (self.f.owner, self.w)).fetchone()[0]
        with closing(self.g.personal_data.connect()) as db, db:
            for w, name in ((self.w, '原工作区甲'), (other, '原工作区乙')):
                value = {f: DEFAULTS['profile'][f] for f in FIELDS}; value['nickname'] = name
                db.execute('INSERT INTO personal_profiles VALUES(?,?,?,?)', (w, self.g.personal_data.protect(json.dumps(value)), 1, self.f.now[0]))
        state = self.read()
        self.assertEqual(state['migration']['legacy_profile'], 'conflict')
        self.assertEqual(state['sections']['profile'], DEFAULTS['profile'])
        self.assertNotIn('profile', state['initialized_sections'])
        self.assertEqual(len(state['migration']['conflicts']), 2)
        self.write('profile', {**DEFAULTS['profile'], 'nickname': '本人明确填写'})
        with closing(self.g.personal_data.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM personal_profiles WHERE workspace_id IN (?,?)', (self.w, other)).fetchone()[0], 2)
