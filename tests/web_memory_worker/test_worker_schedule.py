"""No-load persisted-index check before any background model work."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
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


class FakeTimer:
    instances=[]
    def __init__(self,seconds,callback):
        self.seconds,self.callback=seconds,callback
        self.daemon=False
        self.started=False
        self.cancelled=False
        self.instances.append(self)
    def start(self):self.started=True
    def cancel(self):self.cancelled=True


@contextmanager
def current_db(release):
    class DB:
        def execute(self,sql):return self
        def fetchone(self):return (release,)
    yield DB()


class WorkerSchedule(unittest.TestCase):
    def setUp(self):
        FakeTimer.instances=[]
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name)
        (self.directory/'manifest.json').write_text('{}')
        self.previous={key:os.environ.get(key) for key in ('NAVREN_VECTOR_WORKER_SOCKET','NAVREN_ENCODER_BACKEND')}
        os.environ['NAVREN_VECTOR_WORKER_SOCKET']='/run/unused-prototype-worker.sock'
        os.environ['NAVREN_ENCODER_BACKEND']='simulate'
        self.provider=Provider(self.directory/'public.sqlite3')

    def tearDown(self):
        for timer in vectors._retry_timers.values():timer.cancel()
        vectors._retry_timers.clear()
        vectors._build_states.clear()
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
        calls=[]
        def recovering_build(release):
            calls.append(release)
            if len(calls)==1:raise Transient()
        self.provider.build_vector_index=recovering_build
        self.provider.db=lambda:current_db('pub-worker-down')
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

    def test_build_timeout_backoff_is_bounded_and_cannot_be_bypassed(self):
        class TimedOut(Exception):code='vector_build_timeout'
        release='pub-new-too-slow'
        self.provider.db=lambda:current_db(release)
        calls=[]
        def timed_out_build(value):
            calls.append(value)
            raise TimedOut()
        self.provider.build_vector_index=timed_out_build
        key=(str(self.provider.path.resolve()),release)
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('missing-index')),\
             patch.object(vectors.time,'sleep',return_value=None),\
             patch.object(vectors.threading,'Timer',FakeTimer):
            vectors.schedule(self.provider,release)
            vectors._jobs[key].result(timeout=5)
            # Repeated provider construction during the wait cannot defeat
            # the 900-second backoff or start concurrent build work.
            vectors.schedule(self.provider,release)
            self.assertEqual(calls,[release])
            self.assertEqual(vectors._build_states[key]['state'],'retry_wait')
            for index,delay in enumerate((900,1800,3600)):
                self.assertEqual(FakeTimer.instances[index].seconds,delay)
                FakeTimer.instances[index].callback()
                vectors._jobs[key].result(timeout=5)
            self.assertEqual(len(FakeTimer.instances),3)
            self.assertEqual(calls,[release]*4)
            self.assertEqual(vectors._build_states[key],{
                'state':'retry_exhausted','code':'vector_build_timeout',
                'retries_scheduled':3,
            })
            vectors.schedule(self.provider,release)
            self.assertEqual(calls,[release]*4)

    def test_sqlite_busy_retries_once_then_clears_failure_state(self):
        release='pub-db-briefly-locked'
        self.provider.db=lambda:current_db(release)
        calls=[]
        def build(value):
            calls.append(value)
            if len(calls)==1:raise sqlite3.OperationalError('database is locked')
        self.provider.build_vector_index=build
        key=(str(self.provider.path.resolve()),release)
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('missing-index')),\
             patch.object(vectors.time,'sleep',return_value=None),\
             patch.object(vectors.threading,'Timer',FakeTimer):
            vectors.schedule(self.provider,release)
            vectors._jobs[key].result(timeout=5)
            self.assertEqual(vectors._build_states[key]['code'],'sqlite_busy')
            self.assertEqual(FakeTimer.instances[0].seconds,900)
            FakeTimer.instances[0].callback()
            vectors._jobs[key].result(timeout=5)
        self.assertEqual(calls,[release,release])
        self.assertNotIn(key,vectors._build_states)

    def test_retry_stops_when_current_public_release_changes(self):
        class TimedOut(Exception):code='vector_build_timeout'
        release='pub-now-stale'
        self.provider.db=lambda:current_db('pub-replaced')
        calls=[]
        def build(value):
            calls.append(value)
            raise TimedOut()
        self.provider.build_vector_index=build
        key=(str(self.provider.path.resolve()),release)
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('missing-index')),\
             patch.object(vectors.time,'sleep',return_value=None),\
             patch.object(vectors.threading,'Timer',FakeTimer):
            vectors.schedule(self.provider,release)
            vectors._jobs[key].result(timeout=5)
            FakeTimer.instances[0].callback()
            vectors.schedule(self.provider,release)
        self.assertEqual(calls,[release])
        self.assertEqual(vectors._build_states[key]['state'],'stale_release')
        self.assertNotIn(key,vectors._retry_timers)

    def test_retry_precheck_sqlite_busy_consumes_bounded_attempt(self):
        class TimedOut(Exception):code='vector_build_timeout'
        release='pub-check-busy'
        self.provider.build_vector_index=lambda value:(_ for _ in ()).throw(TimedOut())
        @contextmanager
        def busy_db():
            raise sqlite3.OperationalError('database is busy')
            yield
        self.provider.db=busy_db
        key=(str(self.provider.path.resolve()),release)
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('missing-index')),\
             patch.object(vectors.time,'sleep',return_value=None),\
             patch.object(vectors.threading,'Timer',FakeTimer):
            vectors.schedule(self.provider,release)
            vectors._jobs[key].result(timeout=5)
            FakeTimer.instances[0].callback()
        self.assertEqual([t.seconds for t in FakeTimer.instances],[900,1800])
        self.assertEqual(vectors._build_states[key]['code'],'sqlite_busy')

    def test_missing_model_manifest_at_retry_requires_attention(self):
        class TimedOut(Exception):code='vector_build_timeout'
        release='pub-model-removed'
        self.provider.db=lambda:current_db(release)
        self.provider.build_vector_index=lambda value:(_ for _ in ()).throw(TimedOut())
        key=(str(self.provider.path.resolve()),release)
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('missing-index')),\
             patch.object(vectors.time,'sleep',return_value=None),\
             patch.object(vectors.threading,'Timer',FakeTimer):
            vectors.schedule(self.provider,release)
            vectors._jobs[key].result(timeout=5)
            (self.directory/'manifest.json').unlink()
            FakeTimer.instances[0].callback()
        self.assertEqual(len(FakeTimer.instances),1)
        self.assertEqual(vectors._build_states[key]['state'],'manual_attention')
        self.assertEqual(vectors._build_states[key]['code'],'vector_model_missing')

    def test_permanent_evidence_error_requires_manual_attention(self):
        class InvalidEvidence(Exception):code='vector_evidence_changed'
        release='pub-invalid-evidence'
        calls=[]
        def build(value):
            calls.append(value)
            raise InvalidEvidence()
        self.provider.build_vector_index=build
        key=(str(self.provider.path.resolve()),release)
        with patch.object(retrieval_encoder,'selected_model_directory',return_value=self.directory),\
             patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('missing-index')),\
             patch.object(vectors.time,'sleep',return_value=None),\
             patch.object(vectors.threading,'Timer',FakeTimer):
            vectors.schedule(self.provider,release)
            vectors._jobs[key].result(timeout=5)
            vectors.schedule(self.provider,release)
        self.assertEqual(calls,[release])
        self.assertEqual(FakeTimer.instances,[])
        self.assertEqual(vectors._build_states[key]['state'],'manual_attention')
        self.assertEqual(vectors._build_states[key]['code'],'vector_evidence_changed')

    def test_status_exposes_only_bounded_failure_code(self):
        release='pub-status'
        key=(str(self.provider.path.resolve()),release)
        vectors._build_states[key]={'state':'retry_wait','code':'sqlite_busy',
                                    'retries_scheduled':1,'retry_delay_seconds':900}
        encoder=type('Encoder',(),{'profile_id':'fixture-profile','dimension':384})()
        with patch.object(vectors.VectorIndex,'_load',side_effect=RuntimeError('private path must not escape')):
            status=vectors.VectorIndex(self.provider,encoder).status(None,release)
        self.assertFalse(status['ready'])
        self.assertEqual(status['build_state'],vectors._build_states[key])
        self.assertNotIn('private path',str(status))


if __name__=='__main__':unittest.main()
