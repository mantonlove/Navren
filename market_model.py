"""市场业务调用的独立用量收据；不改变模型、路由、次数或原超时。"""
from contextvars import ContextVar
from datetime import datetime,timezone
from pathlib import Path
import json,os
from agent_runtime.model_adapters import OpenAICompatibleJsonClient

_observation=ContextVar('navren_market_model_usage',default=None)

class _ObservedResponse:
    def __init__(self,response):self.response=response
    def __getattr__(self,name):return getattr(self.response,name)
    def __enter__(self):self.response.__enter__();return self
    def __exit__(self,*args):return self.response.__exit__(*args)
    def read(self,*args,**kwargs):
        raw=self.response.read(*args,**kwargs)
        note=_observation.get()
        if note is not None:
            try:
                envelope=json.loads(raw.decode('utf-8'));usage=envelope.get('usage')
                if isinstance(usage,dict):
                    values={k:usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens') if type(usage.get(k))is int and usage[k]>=0}
                    if 'total_tokens' in values:note['usage']=values
            except (ValueError,UnicodeError,AttributeError):pass
        return raw

class MarketJsonClient(OpenAICompatibleJsonClient):
    def __init__(self,**kwargs):
        super().__init__(**kwargs);transport=self.urlopen
        self.urlopen=lambda *args,**kwargs:_ObservedResponse(transport(*args,**kwargs))
    def complete_json(self,*,system,user,stage,request_id):
        note={'request_id':request_id,'stage':stage,'model_id':self.model_id,'started_at':datetime.now(timezone.utc).isoformat(),'usage':None,'outcome':'failed'}
        token=_observation.set(note)
        try:
            result=super().complete_json(system=system,user=user,stage=stage,request_id=request_id)
            note['outcome']='complete';return result
        finally:
            _observation.reset(token);note['ended_at']=datetime.now(timezone.utc).isoformat();note['usage_unknown']=note['usage'] is None
            path=Path(os.environ.get('NAVREN_MARKET_MODEL_LOG',str(Path.home()/'Library/Logs/Navren/market-model-calls.jsonl')))
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('a')as f:f.write(json.dumps(note,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
