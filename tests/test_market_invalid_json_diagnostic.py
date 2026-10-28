"""公共 Market 模型坏 JSON 的有界留证，不改变失败关闭。"""

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.model_adapters import DEEPSEEK_URL, ModelAdapterError
from market_model import MarketJsonClient


class Response:
    status = 200

    def __init__(self, content, *, finish_reason='stop', reasoning_content='PRIVATE_COT'):
        self.raw = json.dumps({'choices': [{'finish_reason': finish_reason,
            'message': {'content': content, 'reasoning_content': reasoning_content}}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 7, 'total_tokens': 19}}).encode()

    def __enter__(self):return self
    def __exit__(self, *_):return False
    def read(self, _limit):return self.raw


def client(directory, response, calls):
    def urlopen(_request, **_kwargs):
        calls.append(True)
        return response
    return MarketJsonClient(provider='deepseek', model_id='deepseek-flash',
        url=DEEPSEEK_URL, env_name='DEEPSEEK_API_KEY',
        keychain_service='CC/DEEPSEEK_API_KEY', timeout=1, max_retries=0,
        key_loader=lambda: 'FAKE_SECRET', urlopen=urlopen,
        log_path=str(directory / 'transport.jsonl'))


class MarketInvalidJsonDiagnosticTests(unittest.TestCase):
    def test_bad_public_content_saved_privately_without_changing_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);calls=[];content='```json\n{"ok":true}\n```'
            with patch.dict(os.environ, NAVREN_MARKET_MODEL_LOG=str(root/'market-model-calls.jsonl')):
                with self.assertRaises(ModelAdapterError) as caught:
                    client(root,Response(content),calls).complete_json(
                        system='PUBLIC_SYSTEM',user='PUBLIC_MARKET_MATERIAL',
                        stage='market.events',request_id='a'*32)
            self.assertEqual(caught.exception.code,'invalid_model_contract')
            self.assertEqual(calls,[True])
            path=root/'market-model-invalid-content.jsonl'
            record=json.loads(path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(record['content'],content)
            self.assertEqual(record['content_sha256'],hashlib.sha256(content.encode()).hexdigest())
            self.assertEqual(record['json_error_pos'],0)
            self.assertEqual(record['http_status'],200)
            self.assertEqual(record['stage'],'market.events')
            self.assertEqual(record['error_code'],'invalid_model_contract')
            self.assertNotIn('PRIVATE_COT',path.read_text())
            self.assertNotIn('FAKE_SECRET',path.read_text())
            normal=(root/'market-model-calls.jsonl').read_text()
            self.assertIn('content_json_invalid',normal)
            self.assertNotIn(content,normal)
            self.assertNotIn('PRIVATE_COT',normal)

    def test_long_content_uses_bounded_context_and_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);content='x'*9000;calls=[]
            with patch.dict(os.environ, NAVREN_MARKET_MODEL_LOG=str(root/'market-model-calls.jsonl')):
                with self.assertRaises(ModelAdapterError):
                    client(root,Response(content),calls).complete_json(
                        system='s',user='u',stage='market.events',request_id='b'*32)
            path=root/'market-model-invalid-content.jsonl'
            record=json.loads(path.read_text())
            self.assertTrue(record['content_truncated'])
            self.assertNotIn('content',record)
            self.assertEqual(record['content_utf8_bytes'],9000)
            self.assertEqual(record['content_sha256'],hashlib.sha256(content.encode()).hexdigest())
            self.assertLess(path.stat().st_size,2500)
            self.assertEqual(calls,[True])

    def test_private_stage_never_writes_public_content_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);calls=[]
            with patch.dict(os.environ, NAVREN_MARKET_MODEL_LOG=str(root/'market-model-calls.jsonl')):
                with self.assertRaises(ModelAdapterError):
                    client(root,Response('not json',reasoning_content='SECRET_COT'),calls).complete_json(
                        system='PRIVATE_SYSTEM',user='PRIVATE_USER',
                        stage='reviewer.conversation',request_id='c'*32)
            self.assertEqual(calls,[True])
            self.assertFalse((root/'market-model-invalid-content.jsonl').exists())
            self.assertNotIn('PRIVATE_USER',(root/'market-model-calls.jsonl').read_text())

    def test_reasoning_tag_inside_content_is_not_saved_as_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);calls=[];content='<think>DO_NOT_STORE_THOUGHT</think>{}'
            with patch.dict(os.environ, NAVREN_MARKET_MODEL_LOG=str(root/'market-model-calls.jsonl')):
                with self.assertRaises(ModelAdapterError):
                    client(root,Response(content),calls).complete_json(
                        system='s',user='u',stage='market.events',request_id='e'*32)
            path=root/'market-model-invalid-content.jsonl'
            record=json.loads(path.read_text())
            self.assertEqual(record['content_suppressed'],'possible_reasoning')
            self.assertNotIn('content',record)
            self.assertNotIn('DO_NOT_STORE_THOUGHT',path.read_text())
            self.assertEqual(calls,[True])

    def test_diagnostic_write_failure_does_not_replace_model_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);calls=[]
            with patch.dict(os.environ, NAVREN_MARKET_MODEL_LOG=str(root/'market-model-calls.jsonl')):
                with patch('market_model._write_market_bad_json',side_effect=RuntimeError('diagnostic_bug')):
                    with self.assertRaises(ModelAdapterError) as caught:
                        client(root,Response('bad json'),calls).complete_json(
                            system='s',user='u',stage='market.events',request_id='d'*32)
            self.assertEqual(caught.exception.code,'invalid_model_contract')
            self.assertEqual(calls,[True])
            self.assertFalse((root/'market-model-invalid-content.jsonl').exists())
            self.assertIn('content_json_invalid',(root/'market-model-calls.jsonl').read_text())

    def test_busy_diagnostic_lock_never_waits_or_replaces_model_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);calls=[];lock_flags=[]
            def busy(_fd,flags):
                lock_flags.append(flags)
                raise BlockingIOError('another writer owns the lock')
            with patch.dict(os.environ, NAVREN_MARKET_MODEL_LOG=str(root/'market-model-calls.jsonl')):
                with patch('market_model.fcntl.flock',side_effect=busy):
                    with self.assertRaises(ModelAdapterError) as caught:
                        client(root,Response('bad json'),calls).complete_json(
                            system='s',user='u',stage='market.events',request_id='f'*32)
            self.assertEqual(caught.exception.code,'invalid_model_contract')
            self.assertEqual(calls,[True])
            self.assertEqual(lock_flags,[fcntl.LOCK_EX|fcntl.LOCK_NB])
            self.assertIn('content_json_invalid',(root/'market-model-calls.jsonl').read_text())
            diagnostic=root/'market-model-invalid-content.jsonl'
            self.assertEqual(diagnostic.stat().st_size,0)


if __name__=='__main__':unittest.main()
