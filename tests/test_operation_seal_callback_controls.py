"""只测已封回调/首次无 DB 的局部防退化；不签发真实批准或豁免发布。"""
from collections import OrderedDict
from copy import deepcopy
from types import SimpleNamespace
import time
import unittest

from agent_runtime.final_proof import FinalResponseProofs
from financial_metrics import fingerprint
from simulated_bank import BankError


class OperationSealCallbackControls(unittest.TestCase):
    def local_object(self, sealed):
        response = {'language': 'zh', 'workspace_id': 'local-object-workspace',
            'bank_response_id': 'local-object-request', 'isFallback': False,
            'reply': '已准备模拟操作草案，尚未执行。',
            'agent_review': {'status': 'approved', 'model_id': 'GLM-5.3-Flash',
                             'review_id': 'local-object-request'}}
        proof = FinalResponseProofs()
        gateway = SimpleNamespace(bank=object())
        # Controlled private entry only; it never enters approved/store/HTTP.
        entry = {'language': 'zh', 'workspace': response['workspace_id'],
            'request_id': response['bank_response_id'], 'reviewer_model': 'GLM-5.3-Flash',
            'projection': {'draft': {'text': response['reply']}},
            'deadline': time.monotonic() + 30, 'gateway': gateway,
            'staged': deepcopy(response), 'response_hash': fingerprint(response) if sealed else None,
            'historical_quote_session': None, 'historical_quote_proof': None,
            'generated_code_session': None, 'operation_tools': object(),
            'operation_bindings': {'version': 'host-operation-wording/1',
                                   'bindings': [{'state': 'pending_confirmation'}]}}
        proof._entries = OrderedDict([('local-object-token', entry)])
        return proof, response

    def test_sealed_callback_tampering_still_denied(self):
        for change, code in [
                ({'reply': '已转账成功。'}, 'AGENT_FINAL_PROOF_INVALID'),
                ({'bank_actions': [{'local_unapproved_extra': True}]}, 'AGENT_FINAL_RESPONSE_CHANGED')]:
            with self.subTest(change=change):
                proof, response = self.local_object(sealed=True)
                response.update(change)
                with self.assertRaises(BankError) as caught:
                    proof.seal('local-object-token', response, language='zh')
                self.assertEqual(caught.exception.code, code)

    def test_first_seal_without_bank_db_still_requires_guard(self):
        proof, response = self.local_object(sealed=False)
        with self.assertRaises(BankError) as caught:
            proof.seal('local-object-token', response, language='zh')
        self.assertEqual(caught.exception.code, 'AGENT_FINAL_GUARD_DB_REQUIRED')


if __name__ == '__main__':
    unittest.main()
