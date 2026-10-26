"""市场业务调用的独立用量收据；不改变模型、路由、次数或原超时。"""
from contextvars import ContextVar
from datetime import datetime,timezone
from functools import wraps
from pathlib import Path
import json,os
from agent_runtime.model_adapters import OpenAICompatibleJsonClient,ModelAdapterError

_observation=ContextVar('navren_market_model_usage',default=None)
_run_blocks=ContextVar('navren_market_run_provider_blocks',default=None)

def market_model_session(function):
    """一次刷新内的402止损；不跨运行缓存，也不触碰其他业务客户端。"""
    @wraps(function)
    def guarded(*args,**kwargs):
        token=_run_blocks.set(set())
        try:return function(*args,**kwargs)
        finally:_run_blocks.reset(token)
    return guarded

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
        blocks=_run_blocks.get()
        # 路由只含公开配置，不读取或持久化凭据/hash。两模型同一AiPing
        # credential的402不应被当作另一篇稿件的可恢复错误继续出站。
        route=(self.provider,self.url,self.env_name,self.keychain_service)
        if self.provider=='ai_ping' and blocks is not None and route in blocks:
            # 没有出站，不进入父适配器和用量收据；不可伪记一次unknown调用。
            raise ModelAdapterError('provider_http402_circuit_open',model_id=self.model_id,attempts=0,http_status=402)
        note={'request_id':request_id,'stage':stage,'provider':self.provider,'model_id':self.model_id,'started_at':datetime.now(timezone.utc).isoformat(),'usage':None,'outcome':'failed'}
        token=_observation.set(note)
        try:
            result=super().complete_json(system=system,user=user,stage=stage,request_id=request_id)
            note['outcome']='complete';return result
        except ModelAdapterError as error:
            note.update(http_status=error.http_status,error_code=error.code,attempts=error.attempts)
            if self.provider=='ai_ping' and error.http_status==402 and error.attempts>0 and blocks is not None:
                blocks.add(route)
            raise
        finally:
            _observation.reset(token);note['ended_at']=datetime.now(timezone.utc).isoformat();note['usage_unknown']=note['usage'] is None
            path=Path(os.environ.get('NAVREN_MARKET_MODEL_LOG',str(Path.home()/'Library/Logs/Navren/market-model-calls.jsonl')))
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('a')as f:f.write(json.dumps(note,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
