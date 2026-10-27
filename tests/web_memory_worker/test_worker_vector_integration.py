"""VectorIndex control-flow parity with simulated Unix-socket encoders."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / 'worker-integration-runtime'))
sys.path.insert(0, str(BASE))

import numpy as np
from local_knowledge.retrieval_encoder import get_retrieval_encoder
from local_knowledge.vectors import VectorIndex


class WorkerVectorIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / 'worker.sock'
        self.previous = {key:os.environ.get(key) for key in ('NAVREN_VECTOR_WORKER_SOCKET','NAVREN_VECTOR_ENCODER','NAVREN_ENCODER_BACKEND')}
        os.environ['NAVREN_VECTOR_WORKER_SOCKET'] = str(self.socket_path)
        os.environ['NAVREN_VECTOR_ENCODER'] = 'dual'
        os.environ['NAVREN_ENCODER_BACKEND'] = 'simulate'
        self.process = subprocess.Popen(
            [sys.executable,'-B','-m','worker_proto.supervisor','--socket',str(self.socket_path),
             '--backend','simulate','--load-timeout','5','--request-timeout','5'],
            cwd=str(BASE), env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        deadline=time.monotonic()+5
        while not self.socket_path.exists() and time.monotonic()<deadline:
            time.sleep(0.02)
        if not self.socket_path.exists():
            raise AssertionError('supervisor_start_failed')

    def tearDown(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.process.stderr.close()
        for key,value in self.previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        self.temp.cleanup()

    def test_dual_views_and_crash_fallback_without_web_onnx(self):
        query='公开检索问题'
        direction=hashlib.sha256(query.encode()).digest()[0]
        vector=np.zeros((1,384),dtype='<f4')
        vector[0,direction]=1
        old=VectorIndex._load
        VectorIndex._load=lambda self,db,release_id,encoder: ([object()],['public-citation'],vector)
        try:
            index=VectorIndex(object(),get_retrieval_encoder())
            cold=index.status(None,'public-release')
            self.assertFalse(cold['ready'])
            self.assertTrue(cold['index_ready'])
            self.assertTrue(all(item.get('index_ready') for item in cold['profiles']))
            self.assertTrue(all(item['reason']=='vector_model_not_loaded' for item in cold['profiles']))
            self.assertNotIn('onnxruntime',sys.modules)
            result=index.search(None,'public-release',query,{'public-citation'},cross_language=True)
            self.assertEqual(result['mode'],'vector')
            self.assertEqual(len(result['branches']),2)
            self.assertEqual(set(result['views']),{'0:original','1:original'})
            self.assertNotIn('onnxruntime',sys.modules)
            warm=index.status(None,'public-release')
            self.assertFalse(warm['ready'])
            self.assertTrue(warm['profiles'][1]['ready'])
            self.assertEqual(warm['profiles'][0]['reason'],'vector_model_not_loaded')
            failed=index.search(None,'public-release','__crash__',{'public-citation'},cross_language=False)
            self.assertEqual(failed['mode'],'fts5')
            self.assertEqual(failed['reason'],'vector_inference_failed')
            self.assertNotIn('onnxruntime',sys.modules)
        finally:
            VectorIndex._load=old


if __name__ == '__main__':
    unittest.main()
