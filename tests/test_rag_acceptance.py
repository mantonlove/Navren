"""验收器不能将完整产物、非空语音或HTTP兜底当作验收通过。"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('rag_acceptance',ROOT/'scripts/run_rag_acceptance.py')
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class AcceptanceDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base=ROOT/'outputs/window-progress/rag/acceptance-20261006'
        cls.offline=json.loads((base/'offline-full-02/results.json').read_text())
        cls.cloud=json.loads((base/'cloud-full-01/results.json').read_text())

    def test_actual_complete_offline_protocol_including_normalized_speech(self):
        self.assertTrue(all(runner.protocol_checks(self.offline,12).values()))
        self.assertEqual(self.offline['mode'],'offline')
        self.assertEqual(self.offline['summary']['semantic_passes'],'not_scored_by_runner')

    def test_actual_twelve_http_200_fallbacks_are_not_passes(self):
        self.assertEqual(len(self.cloud['records']),12)
        self.assertTrue(all(r['http_status']==200 for r in self.cloud['records']))
        checks=runner.protocol_checks(self.cloud,12)
        self.assertTrue(checks['complete_collection'])
        self.assertFalse(checks['approved_replies'])
        self.assertFalse(checks['expected_evidence'])
        self.assertFalse(checks['speech_guard'])

    def test_missing_one_required_original_rejects_even_approved_reply(self):
        report=deepcopy(self.offline)
        report['records'][0]['all_expected_evidence_retrieved']=False
        self.assertFalse(all(runner.protocol_checks(report,12).values()))

    def test_valid_but_wrong_speech_content_rejects(self):
        report=deepcopy(self.offline)
        report['records'][0]['speech_text']='另一份额适用的费率'
        checks=runner.protocol_checks(report,12)
        self.assertTrue(checks['speech_guard'])
        self.assertFalse(checks['speech_content'])

    def test_question_or_source_drift_and_incomplete_collection_reject(self):
        for key in ('candidate_unchanged','public_input_unchanged','question_input_unchanged'):
            report=deepcopy(self.offline);report[key]=False
            self.assertFalse(all(runner.protocol_checks(report,12).values()),key)
        self.assertFalse(runner.protocol_checks(self.offline,13)['complete_collection'])


if __name__=='__main__': unittest.main()
