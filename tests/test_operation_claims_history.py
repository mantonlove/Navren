"""只验证 S01 对宿主历史证明 API 的消费，不冒充 QuoteProof 签发验收。"""
import unittest
from types import SimpleNamespace

from agent_runtime.operation_claims import validate_operation_wording
from simulated_bank import BankError


class OperationHistoryProjectionTests(unittest.TestCase):
    def tools(self, final, *, spans=None, claims=frozenset()):
        quote = '已转账成功'
        text = final['text']
        start = text.index(quote)
        verified = ((start, start + len(quote)),) if spans is None else spans
        calls = []

        def body(value, *, bank_db=None):
            self.assertIs(value, final)
            calls.append(('body', bank_db))
            return verified

        def claimed(value, *, bank_db=None):
            self.assertIs(value, final)
            calls.append(('claims', bank_db))
            return claims

        return SimpleNamespace(evidence={}, validated_history_quote_spans=body,
                               validated_history_quote_claims=claimed), calls

    def denied(self, final, tools, code):
        with self.assertRaises(BankError) as caught:
            validate_operation_wording(final, tools)
        self.assertEqual(caught.exception.code, code)

    def test_original_offsets_before_nfkc_and_exact_verified_claim(self):
        # The ligature expands under NFKC; offsets refer to the original text.
        final = {'text': 'ﬃ历史原文：“已转账成功”。',
                 'claims': [{'text': '“已转账成功”', 'evidence_ids': ['history-local']}]}
        tools, calls = self.tools(final, claims=frozenset({'“已转账成功”'}))
        db = object()
        self.assertEqual(validate_operation_wording(final, tools, bank_db=db)['bindings'], [])
        self.assertEqual(calls, [('body', db), ('claims', db)])

    def test_quote_does_not_exempt_own_suffix_or_another_claim(self):
        for text, claims in [
                ('历史原文：“已转账成功”。本轮已转账成功。', []),
                ('历史原文：“已转账成功”。', [{'text': '本轮已转账成功'}])]:
            with self.subTest(text=text, claims=claims):
                final = {'text': text, 'claims': claims}
                tools, _ = self.tools(final, claims=frozenset({'“已转账成功”'}))
                self.denied(final, tools, 'OPERATION_STATUS_EVIDENCE_REQUIRED')

    def test_identical_unverified_claim_cannot_borrow_body_span(self):
        final = {'text': '历史原文：“已转账成功”。', 'claims': [{'text': '已转账成功'}]}
        tools, _ = self.tools(final, claims=frozenset({'“已转账成功”'}))
        self.denied(final, tools, 'OPERATION_STATUS_EVIDENCE_REQUIRED')

    def test_invalid_span_shape_bool_overlap_and_range_fail_closed(self):
        final = {'text': '历史原文：“已转账成功”。', 'claims': []}
        for spans in [[(7, 12)], ((True, 12),), ((7, 12), (8, 10)), ((7, 100),), ((7, 7),)]:
            with self.subTest(spans=spans):
                tools, _ = self.tools(final, spans=spans)
                self.denied(final, tools, 'OPERATION_HISTORY_QUOTE_SPAN_INVALID')

    def test_invalid_claim_set_and_missing_issuer_fail_closed(self):
        final = {'text': '历史原文：“已转账成功”。', 'claims': []}
        for claims in [{'已转账成功'}, frozenset({1})]:
            with self.subTest(claims=claims):
                tools, _ = self.tools(final, claims=claims)
                self.denied(final, tools, 'OPERATION_HISTORY_QUOTE_CLAIM_INVALID')
        tools, _ = self.tools(final)
        del tools.validated_history_quote_claims
        self.denied(final, tools, 'OPERATION_HISTORY_QUOTE_CLAIM_INVALID')

    def test_model_spans_and_failed_source_proof_never_exempt(self):
        final = {'text': '已转账成功。', 'claims': [],
                 'history_quote_spans': ((0, 6),), 'QuotedSource': 'model-invented'}
        self.denied(final, SimpleNamespace(evidence={}), 'OPERATION_STATUS_EVIDENCE_REQUIRED')
        tools, _ = self.tools(final)

        def invalid_source(value, *, bank_db=None):
            raise BankError('HISTORY_QUOTE_UNKNOWN_SOURCE', '本地证明拒绝', 422)

        tools.validated_history_quote_spans = invalid_source
        self.denied(final, tools, 'HISTORY_QUOTE_UNKNOWN_SOURCE')


if __name__ == '__main__':
    unittest.main()
