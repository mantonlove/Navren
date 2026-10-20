"""Disposable real HTTP stack, controlled only by its parent acceptance process.

No production state, provider credentials, models or real SMS. The parent's
ephemeral encryption key survives child replacement and is never written out.
"""
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from functools import partial

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from server import create_server, ProxyHandler
from base_runtime.service_assembly import AssembledGateway
from local_knowledge.runtime import EvidenceRuntime
from local_knowledge.store import LocalKnowledgeProvider
from research_evidence import ResearchEvidenceStore
from test_chat_media_wiring import NoModel, SpeechFixture
from test_sms_auth import TestTransport
from test_sms_permissions import LocalSms
from voice_gateway import VoiceGateway
from welcome_auth import create_welcome_server


def main():
    config = json.loads(sys.stdin.readline())
    state = Path(config['state'])
    assert state.is_dir() and state.name.startswith('navren-pi-joint-')
    assert os.environ.get('NAVREN_PERSONAL_DATA_KEY')
    os.environ.update(NAVREN_KNOWLEDGE_DB=str(state / 'knowledge.sqlite3'),
        NAVREN_PUBLIC_SYNC_ENABLED='false', NAVREN_MESSAGE_WORKER_ENABLED='false',
        NAVREN_LIVING_WORKER_ENABLED='false')
    ProxyHandler.log_message = lambda *_: None
    agent = NoModel()
    evidence = EvidenceRuntime(LocalKnowledgeProvider(state / 'knowledge.sqlite3'), background=False)
    voice = VoiceGateway(key='isolated-no-provider', url='ws://127.0.0.1:1', task_factory=SpeechFixture)
    voice.local_asr = voice.local_tts = None
    upstream = create_server(0, state / 'bank.sqlite3', voice=voice, conversation_agent=agent,
        evidence_runtime=evidence, market_database=state / 'market.sqlite3', public_sync_enabled=False,
        finance_factory=partial(AssembledGateway, registered_mode=True,
            financial_database=ROOT / 'outputs/realistic-history/navren-financial.sqlite3'))
    g = upstream.finance
    g.bank.clock = lambda: config['clock']
    g.bank.permissions.provider = LocalSms(g.bank.clock)
    g.bank.permissions.allow_test_provider = True
    g.research_workspace._provider = ResearchEvidenceStore(root=state / 'public-evidence')
    # Only this disposable child watches this parent's local fault marker.
    # The hook is after private commit and before the surrounding bank commit.
    def failpoint(stage):
        if stage == 'after_private_commit' and (state / 'crash-after-private-commit').exists():
            os._exit(86)
    g.research_workspace._failpoint = failpoint
    original_attachment=g.research_workspace.attachment
    def delayed_attachment(*args,**kwargs):
        if (state/'delay-attachment').exists():
            (state/'attachment-read-entered').touch()
            until=time.monotonic()+15
            while not (state/'release-attachment').exists() and time.monotonic()<until:time.sleep(.02)
        return original_attachment(*args,**kwargs)
    g.research_workspace.attachment=delayed_attachment
    welcome = create_welcome_server(config.get('welcome_port', 0), state / 'auth.sqlite3', root=ROOT,
        upstream_port=upstream.server_port, clock=g.bank.clock, sms_transport=TestTransport(),
        allow_test_sms=True, experience_provider=g.experience_provider)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, welcome)]
    for t in threads: t.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(json.dumps({'pid': os.getpid(), 'welcome_port': welcome.server_port,
        'upstream_port': upstream.server_port, 'provider_revision': g.research_workspace._provider.revision(),
        'registered_mode': True, 'test_sms': True, 'models': 0}), flush=True)
    try:
        stop.wait(1800)
    finally:
        for s in (welcome, upstream): s.shutdown(); s.server_close()
        for t in threads: t.join(5)
        print(json.dumps({'stopped': True, 'model_calls': agent.calls}), flush=True)


if __name__ == '__main__': main()
