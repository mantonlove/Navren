"""使用 AG-UI 官方事件编码器；仅展示运行状态，不传递模型隐式推理和工具敏感参数。"""
import re

from ag_ui.core import (RunStartedEvent, RunFinishedEvent, CustomEvent,
                        StepStartedEvent, StepFinishedEvent, ToolCallStartEvent, ToolCallEndEvent)
from ag_ui.encoder import EventEncoder


_TOKEN = re.compile(r"[A-Za-z0-9_.:/-]{1,120}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:\.\.\d{4}-\d{2}-\d{2})?")
_KINDS = frozenset(("tool", "skill", "data", "model", "review", "reply"))
_STATUSES = frozenset(("started", "completed", "failed"))
_SUMMARY_FIELDS = {
    "tool": frozenset(("source", "period", "record_count")),
    "data": frozenset(("source", "period", "record_count")),
    "skill": frozenset(("skill",)),
    "model": frozenset(("provider", "model", "round", "failure_type")),
    "review": frozenset(("provider", "model", "round", "failure_type", "verdict")),
    "reply": frozenset(("source",)),
}
_SOURCES = frozenset(("public_knowledge", "local_knowledge_status",
    "verified_public_market", "webank_public_disclosure", "profile_sources",
    "maintainer_skill", "conversation_candidate", "simulation", "imported",
    "local_rule"))
_VERDICTS = frozenset(("approve", "revise", "reject", "insufficient_evidence"))


class RuntimeEvents:
    def __init__(self, writer, thread_id, run_id):
        self.writer,self.thread_id,self.run_id=writer,thread_id,run_id
        self.encoder=EventEncoder()
        self.connected=True

    def emit(self, event):
        if not self.connected:return
        try:self.writer(self.encoder.encode(event).encode('utf8'))
        except (ConnectionError,OSError):self.connected=False

    def start(self):
        self.emit(RunStartedEvent(thread_id=self.thread_id,run_id=self.run_id))

    def progress(self, event):
        kind=event['kind']
        if kind=='tool_start':
            name=event.get('name')
            if not isinstance(name,str):name='unknown_tool'
            if name!='unknown_tool':
                from .bank_tools import SPECS
                if name not in SPECS:name='unknown_tool'
            self.emit(ToolCallStartEvent(tool_call_id=event['id'],tool_call_name=name))
        elif kind=='tool_end':
            self.emit(ToolCallEndEvent(tool_call_id=event['id'],metadata={'status':event['status']}))
        elif kind=='step_start':self.emit(StepStartedEvent(step_name=event['name']))
        elif kind=='step_end':self.emit(StepFinishedEvent(step_name=event['name']))
        elif kind=='activity':
            value=self._activity_value(event)
            if value is not None:self.emit(CustomEvent(name='navren.activity',value=value))

    def _activity_value(self, event):
        """Build a small typed allowlist; never forward arbitrary progress payloads."""
        activity_kind=event.get('activity_kind')
        status=event.get('status')
        call_id=event.get('call_id')
        name=event.get('name')
        label=event.get('label')
        if (activity_kind not in _KINDS or status not in _STATUSES
                or not isinstance(call_id,str) or not _TOKEN.fullmatch(call_id)
                or not isinstance(name,str) or not _TOKEN.fullmatch(name)
                or not isinstance(label,str) or not 1 <= len(label) <= 100
                or any(ord(char)<32 for char in label)):
            return None
        if activity_kind in ('tool','data'):
            from .bank_tools import SPECS
            if name not in SPECS:return None
        elif activity_kind=='model' and name not in ('primary','reviewer'):
            return None
        elif activity_kind=='review' and name not in (
                'independent_review','answer_boundary','amount_contract','evidence_reference'):
            return None
        elif activity_kind=='skill' and name not in ('load_skill','intent_route'):
            return None
        elif activity_kind=='reply' and name!='social_reply':
            return None
        attempt=event.get('attempt')
        if attempt is not None and (type(attempt) is not int or not 1 <= attempt <= 10000):
            return None
        summary=event.get('summary',{})
        fields=frozenset() if activity_kind=='skill' and name=='intent_route' else _SUMMARY_FIELDS[activity_kind]
        if not isinstance(summary,dict) or set(summary)-fields:
            return None
        safe_summary={}
        for key,value in summary.items():
            if key=='source':
                if not isinstance(value,str) or value not in _SOURCES:
                    return None
                safe_summary[key]=value
            elif key=='skill':
                if not isinstance(value,str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*",value):
                    return None
                safe_summary[key]=value
            elif key=='period':
                if not isinstance(value,str) or not _DATE.fullmatch(value):
                    return None
                safe_summary[key]=value
            elif key=='record_count':
                if type(value) is not int or not 0 <= value <= 1000000000:
                    return None
                safe_summary[key]=value
            elif key in ('provider','model'):
                if not isinstance(value,str) or not _TOKEN.fullmatch(value):
                    return None
                safe_summary[key]=value
            elif key in ('round',):
                if type(value) is not int or not 1 <= value <= 10000:
                    return None
                safe_summary[key]=value
            elif key=='failure_type':
                if not isinstance(value,str) or not _TOKEN.fullmatch(value):
                    return None
                safe_summary[key]=value
            elif key=='verdict':
                if value not in _VERDICTS:
                    return None
                safe_summary[key]=value
        result={'schema_version':1,'request_id':self.run_id,'call_id':call_id,
                'kind':activity_kind,'name':name,'label':label,'status':status}
        if attempt is not None:result['attempt']=attempt
        if safe_summary:result['summary']=safe_summary
        return result

    def finish(self, result):
        self.emit(CustomEvent(name='navren.result',value=result))
        self.emit(RunFinishedEvent(thread_id=self.thread_id,run_id=self.run_id))
