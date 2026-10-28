"""Event output remains one strict root object; no business model calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from market_content import MarketStore
from market_editor import MarketEditor


class FixedClient:
    model_id = 'test-event-envelope-no-network'

    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(self.result)


class EventEnvelopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = MarketStore(Path(temp.name) / 'market.sqlite3')
        self.store = store
        article = {
            'id': 'article-envelope', 'source_id': 'nbs', 'publisher': '国家统计局',
            'title': '隔离验收资料',
            'url': 'https://www.stats.gov.cn/sj/zxfb/202610/t20261028_1965449.html',
            'published_at': '2026-10-28T10:00:00+08:00', 'published_precision': 'minute',
            'checked_at': '2026-10-28T10:01:00+08:00', 'read_scope': 'full_text',
            'link_status': 'verified', 'access_status': 'free_full_text',
            'evidence_text': '隔离验收资料，具体执行条件仍然持续有效。',
            'evidence_truncated': False, 'identity': 'official_release',
            'fingerprint': 'isolated-envelope', 'version': 1,
        }
        store.save_article(article)
        active = next(row for row in store.articles() if row['id'] == article['id'])
        captured = store.capture_event_generation([{'id': 'event-envelope', 'articles': [active]}])
        self.groups = captured['groups']
        self.generations = captured['generations']
        self.draft = {
            'id': 'event-envelope', 'headline': '隔离验收资料',
            'summary': '隔离验收资料中的执行条件仍有效。',
            'analysis': '实际影响取决于执行条件是否继续成立。',
            'uncertainty': '后续执行进度尚待核实。',
            'facts': [{'text': '具体执行条件仍然持续有效。',
                       'article_id': article['id'], 'quote': '具体执行条件仍然持续有效。'}],
            'selection_attention': None,
        }

    def editor(self, response):
        primary = FixedClient(response)
        reviewer = FixedClient({'verdict': 'approve', 'reason': '隔离证据核验通过。', 'issues': []})
        return MarketEditor(primary, reviewer), primary, reviewer

    def test_one_complete_example_and_nested_attention_are_accepted(self):
        attention = {
            'important': True, 'reason': '原文明确具体执行条件仍然持续有效。',
            'expires_at': '2026-10-29T10:00:00+08:00',
            'evidence': [{'article_id': 'article-envelope', 'version': 1,
                          'quote': '具体执行条件仍然持续有效。'}],
        }
        draft = {**self.draft, 'selection_attention': attention}
        editor, primary, reviewer = self.editor({'events': [draft]})
        result = editor.events(self.groups, 'event-envelope-test', generations=self.generations)
        self.assertEqual(result[0]['selection_attention'], attention)
        self.assertEqual(len(reviewer.calls), 1)
        packet, example = primary.calls[0]['user'].split('\n输出结构示例（', 1)
        example = example.split('）：\n', 1)[1]
        self.assertNotIn('schema', json.loads(packet))
        structure = json.loads(example)
        self.assertEqual(set(structure), {'events'})
        self.assertIn('selection_attention', structure['events'][0])

    def test_unexpected_root_fields_fail_before_independent_review(self):
        for extra in ('selection_attention', 'type', 'response_format'):
            with self.subTest(extra=extra):
                editor, _, reviewer = self.editor({'events': [self.draft], extra: None})
                with self.assertRaisesRegex(ValueError, 'invalid_event_root_schema'):
                    editor.events(self.groups, 'extra-root-test', generations=self.generations)
                self.assertEqual(reviewer.calls, [])

    def test_disclosed_audience_vs_undisclosed_details_stays_in_review(self):
        source = ('公司甲称新模型是其旗下首个可生成音频的实时世界模型。'
                  '目前向受信任测试者开放，未来计划向高级订阅者和开发平台早期合作伙伴开放。'
                  '具体开放时间和详细覆盖条件尚未披露。')
        article = {
            'id': 'article-scope', 'source_id': 'yicai', 'publisher': '第一财经',
            'origin_publisher': '第一财经', 'publication_platform': '第一财经',
            'reporting_body': '第一财经', 'title': '公司甲公布新模型',
            'url': 'https://www.yicai.com/news/12345678.html',
            'published_at': '2026-10-28T10:00:00+08:00',
            'original_published_at': '2026-10-28T10:00:00+08:00',
            'first_publication_status': 'verified_original_page',
            'published_precision': 'minute', 'checked_at': '2026-10-28T10:01:00+08:00',
            'read_scope': 'full_text', 'link_status': 'verified',
            'access_status': 'free_full_text', 'evidence_text': source,
            'evidence_truncated': False, 'identity': 'media_report',
            'fingerprint': 'isolated-scope', 'version': 1,
            'source_attribution': {'contract': 'yicai-visible-source-v1',
                                   'status': 'self_published',
                                   'visible_source_credits': ['第一财经'],
                                   'visible_date_matches_published_at': True},
        }
        self.store.save_article(article)
        active = next(row for row in self.store.articles() if row['id'] == article['id'])
        captured = self.store.capture_event_generation([{'id': 'event-scope', 'articles': [active]}])
        base = {
            'id': 'event-scope', 'headline': '公司甲公布新模型',
            'analysis': '后续应用取决于实际开放时间与覆盖条件。',
            'facts': [{'text': '未来计划向高级订阅者和开发平台早期合作伙伴开放。',
                       'article_id': article['id'],
                       'quote': '未来计划向高级订阅者和开发平台早期合作伙伴开放。'}],
            'selection_attention': None,
        }
        bad = {**base, 'summary': '公司甲称这是首个可生成音频的实时世界模型。',
               'uncertainty': '订阅者与早期合作伙伴范围均未明确。'}
        primary = FixedClient({'events': [bad]})
        reviewer = FixedClient({'verdict': 'reject', 'reason': '扩大首个范围并否认已披露对象。',
                                'issues': ['summary_scope', 'uncertainty_scope']})
        with self.assertRaisesRegex(ValueError, 'independent_review_rejected'):
            MarketEditor(primary, reviewer).events(captured['groups'], 'scope-reject',
                                                   generations=captured['generations'])
        self.assertIn('原文已明确开放对象', primary.calls[0]['user'])
        self.assertIn('逐句反查', reviewer.calls[0]['system'])
        self.assertIn(source, reviewer.calls[0]['user'])
        good = {**base, 'summary': '公司甲称新模型是其旗下首个可生成音频的实时世界模型。',
                'uncertainty': '具体开放时间和详细覆盖条件尚未披露。'}
        primary = FixedClient({'events': [good]})
        reviewer = FixedClient({'verdict': 'approve', 'reason': '范围与未知细节均有原文支持。',
                                'issues': []})
        accepted = MarketEditor(primary, reviewer).events(captured['groups'], 'scope-approve',
                                                          generations=captured['generations'])
        self.assertEqual(accepted[0]['uncertainty'], good['uncertainty'])


if __name__ == '__main__':
    unittest.main()
