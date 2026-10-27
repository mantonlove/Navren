"""Compare static metadata against current constructors before any ONNX session."""
from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest

BASE=Path(__file__).parent
sys.path.insert(0,str(BASE/'worker-integration-runtime'))
sys.path.insert(0,str(BASE))

from worker_proto.profile_spec import profile_spec
from local_knowledge import multilingual_embeddings as e5
from local_knowledge import aligned_embeddings as aligned


class StaticProfileSpec(unittest.TestCase):
    def test_exact_constructor_fingerprint_without_model_or_ort(self):
        previous_ort=sys.modules.get('onnxruntime')
        previous_tokenizers=sys.modules.get('tokenizers')
        fake_ort=types.ModuleType('onnxruntime')
        fake_ort.disable_telemetry_events=lambda: (_ for _ in ()).throw(RuntimeError('stop-before-session'))
        fake_tokenizers=types.ModuleType('tokenizers')
        fake_tokenizers.Tokenizer=object
        sys.modules['onnxruntime']=fake_ort
        sys.modules['tokenizers']=fake_tokenizers
        try:
            for profile,module,klass in [('e5',e5,e5.MultilingualEncoder),
                                         ('aligned',aligned,aligned.AlignedEncoder)]:
                original=module._load_verified_artifacts
                module._load_verified_artifacts=lambda directory: {}
                try:
                    instance=klass.__new__(klass)
                    with self.assertRaises(module.VectorError):
                        klass.__init__(instance,Path('/unread-model'))
                    self.assertEqual(profile_spec(profile,'real')['profile_id'],instance.profile_id)
                    self.assertEqual(profile_spec(profile,'real')['dimension'],instance.dimension)
                finally:
                    module._load_verified_artifacts=original
        finally:
            if previous_ort is None:sys.modules.pop('onnxruntime',None)
            else:sys.modules['onnxruntime']=previous_ort
            if previous_tokenizers is None:sys.modules.pop('tokenizers',None)
            else:sys.modules['tokenizers']=previous_tokenizers


if __name__=='__main__':
    unittest.main()
