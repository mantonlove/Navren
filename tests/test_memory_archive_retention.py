"""单条记忆操作保留真实图片/音频与归档；近期上下文、旧授权仍失效。"""
from contextlib import closing
import hashlib
import unittest

import test_chat_media as media_fixture
from simulated_bank import BankError
from user_memory import privacy_revision


class MemoryArchiveRetentionTests(unittest.TestCase):
    def setUp(self):
        self.f = media_fixture.ChatMediaTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.g, self.w = self.f.g, self.f.w

    def test_correct_delete_disable_preserve_originals_and_revoke_old_context(self):
        with self.g.demo_sessions.request(self.f.token, self.w):
            state = self.g.memories.set_enabled(self.w, {'enabled': True, 'expected_revision': self.g.personal_data.revision(self.w)})
            state = self.g.memories.create(self.w, {'category': 'goal', 'value': '旧目标内容标记', 'expected_revision': state['personal_revision']})
        memory = next(x for x in state['items'] if x['value'] == '旧目标内容标记')
        image = self.f.upload(); audio = self.f.upload(self.f.audio, 'audio/wav')
        rid, original = self.f.archive([image['media_id'], audio['media_id']], '本人图片与语音')
        epoch = self.g.personal_data.archive_epoch(self.w)
        with self.f.scope() as db: old_privacy = privacy_revision(db, self.w)
        # 近期模型上下文由独立agent_dialogues保存；修改记忆后必须删除。
        self.g.personal_data.save_history(self.w, [{'role': 'user', 'content': '旧上下文'}])
        actions = [('correct', {'value': '新目标内容标记'}), ('delete', {}), ('disable', {})]
        for action, payload in actions:
            with self.g.demo_sessions.request(self.f.token, self.w):
                revision = self.g.personal_data.revision(self.w)
                if action == 'disable': self.g.memories.set_enabled(self.w, {'enabled': False, 'expected_revision': revision})
                else: getattr(self.g.memories, action)(self.w, memory['node_id'], {**payload, 'expected_revision': revision})
                context = self.g.memories.read_relevant(self.w, '目标', workflow='wealth')
            self.assertNotIn('旧目标内容标记', repr(context))
            if action == 'disable': self.assertNotIn('新目标内容标记', repr(context))
            self.assertEqual(self.g.personal_data.archive_epoch(self.w), epoch)
            self.assertEqual(self.g.personal_data.read_history(self.w), [])
            for item, raw in ((image, self.f.image), (audio, self.f.audio)):
                actual, descriptor = self.f.read(item['media_id'])
                self.assertEqual(actual, raw); self.assertEqual(hashlib.sha256(actual).hexdigest(), hashlib.sha256(raw).hexdigest())
            with self.f.scope() as db:
                self.assertGreater(privacy_revision(db, self.w), old_privacy)
                self.assertFalse(self.g.personal_data.archive_user_turn_guarded(self.w, 'late-memory-request', '迟到', self.f.fixture.owner,
                    bank_db=db, expected_revision=revision, expected_privacy=old_privacy))
            with closing(self.g.personal_data.connect()) as db:
                row = db.execute('SELECT request_id FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?', (self.w, rid)).fetchone()
                self.assertIsNotNone(row)
        # 真正显式删除聊天仍撤回本人的原件和所有归档。
        with self.g.demo_sessions.request(self.f.token, self.w):
            self.g.personal_data.delete_history(self.w, expected_revision=self.g.personal_data.revision(self.w))
        for item in (image, audio): self.f.deny('CHAT_MEDIA_NOT_FOUND', lambda: self.f.read(item['media_id']), 404)
        self.assertGreater(self.g.personal_data.archive_epoch(self.w), epoch)
