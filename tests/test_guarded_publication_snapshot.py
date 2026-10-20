"""发布标记/失败轮回退的实际SQLite密文控制；不是Agent或原SHD方法验收。"""
from contextlib import closing
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from base_runtime.approved_response_store import ApprovedResponseStore, COMMIT
from personal_data import PersonalDataStore
from simulated_bank import SimulatedBank, BankError
from user_memory import UserMemoryStore


class GuardedPublicationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-shd-raw-')
        self.addCleanup(self.temp.cleanup)
        self.now = [1792013383.0]
        self.bank = SimulatedBank(Path(self.temp.name) / 'bank.sqlite3', clock=lambda: self.now[0])
        self.w = self.bank.create_workspace()['workspace_id']
        self.key = Fernet.generate_key()  # 临时内存夹具，不写配置或日志。
        self.store = PersonalDataStore(self.bank, encryption_key=self.key)
        self.sealer = ApprovedResponseStore(SimpleNamespace(presentations=None, personal_data=self.store))

    def messages(self, *turns):
        return [entry for name in turns for entry in
                ({'role': 'user', 'content': name + '问题'}, {'role': 'assistant', 'content': name + '答复'})]

    def save(self, response, *turns, revision=0, privacy=0):
        self.now[0] += 1
        with self.bank.transaction() as db:
            return self.store.save_turn_guarded(self.w, self.messages(*turns), response,
                [{'kind': 'storage-fixture', 'id': response}], bank_db=db,
                expected_revision=revision, expected_privacy=privacy)

    def marker(self, response):
        # 用原加密封印，仅测试存储；不构造或宣称Agent批准证明。
        return self.sealer._seal(COMMIT, {'response_id': response, 'storage_control_only': True})

    def publish(self, response, marker=None):
        marker = self.marker(response) if marker is None else marker
        with self.bank.transaction() as db:
            return self.store.append_turn_widget_guarded(self.w, response, marker,
                bank_db=db, expected_revision=0, expected_privacy=0)

    def discard(self, response, revision=0, privacy=0):
        with self.bank.transaction() as db:
            return self.store.discard_turn_guarded(self.w, response, bank_db=db,
                expected_revision=revision, expected_privacy=privacy)

    def raw(self):
        with closing(self.store.connect()) as db:
            dialogue = self.store._raw_dialogue(db, self.w)
            widgets = db.execute('SELECT response_id,widgets AS widgets_cipher,created_at '
                                 'FROM agent_widget_history WHERE workspace_id=? ORDER BY response_id', (self.w,)).fetchall()
        return {'dialogue': dict(dialogue) if dialogue else None, 'widgets': [dict(row) for row in widgets]}

    def test_publication_preserves_cipher_timestamp_and_original_guarded_hashes(self):
        self.assertTrue(self.save('prior', 'prior'))
        before = self.raw()
        with closing(self.store.connect()) as db:
            hashes = tuple(db.execute('SELECT history_hash,widget_hash,payload_hash FROM guarded_turn_saves '
                                     'WHERE workspace_id=? AND response_id=?', (self.w, 'prior')).fetchone())
        marker = self.marker('prior')
        self.assertTrue(self.publish('prior', marker))
        self.assertTrue(self.publish('prior', marker))
        self.assertEqual(self.raw(), before)
        with closing(self.store.connect()) as db:
            self.assertEqual(tuple(db.execute('SELECT history_hash,widget_hash,payload_hash FROM guarded_turn_saves '
                                             'WHERE workspace_id=? AND response_id=?', (self.w, 'prior')).fetchone()), hashes)
            widgets = self.store.published_turn_widgets(db, self.w, 'prior')
            self.assertEqual(widgets[-1], marker)
            self.assertEqual(self.sealer._open(widgets[-1], COMMIT)['response_id'], 'prior')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM guarded_turn_publications').fetchone()[0], 1)

    def test_prior_snapshot_before_publication_is_exact_after_failed_turn_cleanup(self):
        self.assertTrue(self.save('prior', 'prior'))
        original = self.raw()  # 原HTTP可在后置发布标记完成前抓到这个时点。
        self.assertTrue(self.publish('prior'))
        self.assertTrue(self.save('failed', 'prior', 'failed'))
        self.assertTrue(self.discard('failed'))
        self.assertEqual(self.raw(), original)
        with closing(self.store.connect()) as db:
            self.assertEqual(self.store.published_turn_widgets(db, self.w, 'prior')[-1]['contract'], COMMIT)

    def test_late_cleanup_preserves_later_originals_and_prior_successful_publications(self):
        self.assertTrue(self.save('prior', 'prior'))
        self.assertTrue(self.publish('prior'))
        self.assertTrue(self.save('failed', 'prior', 'failed'))
        self.assertTrue(self.save('later', 'prior', 'failed', 'later'))
        later = self.raw()
        self.assertTrue(self.publish('later'))
        self.assertFalse(self.discard('failed'))
        expected = {'dialogue': later['dialogue'], 'widgets': [w for w in later['widgets'] if w['response_id'] != 'failed']}
        self.assertEqual(self.raw(), expected)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM guarded_turn_publications').fetchone()[0], 2)
            self.assertEqual(self.store.published_turn_widgets(db, self.w, 'later')[-1]['contract'], COMMIT)
        self.assertFalse(self.discard('failed'))
        self.assertEqual(self.raw(), expected)

    def test_private_commit_then_permissions_error_restores_exact_prior(self):
        self.assertTrue(self.save('prior', 'prior'))
        original = self.raw()
        self.assertTrue(self.publish('prior'))
        with patch.object(self.store, '_permissions', side_effect=RuntimeError('storage_control_permissions_fault')):
            with self.assertRaises(RuntimeError):
                self.save('failed', 'prior', 'failed')
        with closing(self.store.connect()) as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?',
                                           (self.w, 'failed')).fetchone())
        self.assertTrue(self.discard('failed'))
        self.assertEqual(self.raw(), original)

    def test_real_personal_change_prevents_old_cleanup_and_removes_its_publication(self):
        self.assertTrue(self.save('prior', 'prior'))
        self.assertTrue(self.publish('prior'))
        self.assertTrue(self.save('failed', 'prior', 'failed'))
        UserMemoryStore(self.bank).set_enabled(self.w, {'enabled': True, 'expected_revision': 0})
        self.assertTrue(self.save('new', 'new', revision=1, privacy=1))
        actual = self.raw()
        self.assertFalse(self.discard('failed'))
        self.assertEqual(self.raw(), actual)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM guarded_turn_publications').fetchone()[0], 0)

    def test_publication_owner_and_base_hash_tampering_are_rejected(self):
        self.assertTrue(self.save('prior', 'prior'))
        self.assertTrue(self.publish('prior'))
        other = self.bank.create_workspace()['workspace_id']
        with self.bank.transaction() as db:
            self.assertFalse(self.store.append_turn_widget_guarded(other, 'prior', self.marker('prior'),
                bank_db=db, expected_revision=0, expected_privacy=0))
        with closing(self.store.connect()) as db, db:
            db.execute('UPDATE guarded_turn_publications SET widget_hash=? WHERE workspace_id=?', ('invalid', self.w))
        with closing(self.store.connect()) as db:
            with self.assertRaises(BankError) as error:
                self.store.published_turn_widgets(db, self.w, 'prior')
        self.assertEqual(error.exception.code, 'HISTORY_PUBLICATION_CHANGED')

    def test_legacy_inline_marker_and_reopened_current_store_remain_readable(self):
        legacy_marker = self.marker('legacy')
        with self.bank.transaction() as db:
            self.assertTrue(self.store.save_turn_guarded(self.w, self.messages('legacy'), 'legacy', [legacy_marker],
                bank_db=db, expected_revision=0, expected_privacy=0))
        before = self.raw()
        self.assertTrue(self.publish('legacy', legacy_marker))
        self.assertEqual(self.raw(), before)
        self.assertTrue(self.save('current', 'legacy', 'current'))
        marker = self.marker('current')
        self.assertTrue(self.publish('current', marker))
        reopened = PersonalDataStore(self.bank, encryption_key=self.key)
        with closing(reopened.connect()) as db:
            self.assertEqual(reopened.published_turn_widgets(db, self.w, 'legacy'), [legacy_marker])
            self.assertEqual(reopened.published_turn_widgets(db, self.w, 'current')[-1], marker)


if __name__ == '__main__':
    unittest.main(verbosity=2)
