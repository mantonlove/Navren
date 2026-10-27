"""No-load persisted-index check before any background model work."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE=Path(__file__).parent
sys.path.insert(0,str(BASE/'worker-integration-runtime'))
sys.path.insert(0,str(BASE))

from local_knowledge import retrieval_encoder
from local_knowledge import vectors


class Provider:
    vector_encoder=None
    def __init__(self,path):self.path=path;self.build_calls=[]
    @contextmanager
    def db(self):yield None
    def build_vector_index(self,release):self.build_calls.append(release)


class WorkerSchedule(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name)
        (self.directory/'manifest.json').write_text('{}')
        self.previous={key:os.environ.get(key) for key in ('NAVREN_VECTOR_WORKER_SOCKET','NAVREN_ENCODER_BACKEND')}
        os.environ['NAVREN_VECTOR_WORKER_SOCKET']='/run/unused-prototype-worker.sock'
        os.environ['NAVREN_ENCODER_BACKEND']='simulate'
        self.provider=Provider(self.directory/'public.sqlite3')

    def tearDown(self):
        vectors._retry_timers.clear()
        for key,value in self.previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        self.temp.cleanup()

    def test_existing_dual_index_does_not_load_or_build(self):
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',return_value=([],[],[])),\
             patch.object(vectors.time,'sleep',side_effect=AssertionError('should_not_sleep')):
            vectors.schedule(self.provider,'pub-existing')
            key=(str(self.provider.path.resolve()),'pub-existing')
            vectors._jobs[key].result(timeout=5)
        self.assertEqual(self.provider.build_calls,[])

    def test_missing_index_builds_after_bounded_grace(self):
        waits=[]
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('missing-index')) ,\
             patch.object(vectors.time,'sleep',side_effect=waits.append):
            vectors.schedule(self.provider,'pub-new')
            key=(str(self.provider.path.resolve()),'pub-new')
            vectors._jobs[key].result(timeout=5)
        self.assertEqual(waits,[15])
        self.assertEqual(self.provider.build_calls,['pub-new'])

    def test_transient_worker_failure_gets_low_rate_retry(self):
        class Transient(Exception):code='vector_inference_failed'
        class FakeTimer:
            instances=[]
            def __init__(self,seconds,callback):
                self.seconds,self.callback=seconds,callback
                self.daemon=False
                self.started=False
                self.instances.append(self)
            def start(self):self.started=True
        calls=[]
        def recovering_build(release):
            calls.append(release)
            if len(calls)==1:raise Transient()
        self.provider.build_vector_index=recovering_build
        class DB:
            def execute(self,sql):return self
            def fetchone(self):return ('pub-worker-down',)
        @contextmanager
        def current_db():yield DB()
        self.provider.db=current_db
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('missing-index')),\
             patch.object(vectors.time,'sleep',return_value=None),\
             patch.object(vectors.threading,'Timer',FakeTimer):
            vectors.schedule(self.provider,'pub-worker-down')
            key=(str(self.provider.path.resolve()),'pub-worker-down')
            vectors._jobs[key].result(timeout=5)
            FakeTimer.instances[0].callback()
            vectors._jobs[key].result(timeout=5)
        self.assertEqual(len(FakeTimer.instances),1)
        self.assertEqual(FakeTimer.instances[0].seconds,900)
        self.assertTrue(FakeTimer.instances[0].daemon)
        self.assertTrue(FakeTimer.instances[0].started)
        self.assertEqual(calls,['pub-worker-down','pub-worker-down'])


if __name__=='__main__':unittest.main()
