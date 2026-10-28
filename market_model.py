"""市场官方模型接线、独立用量收据与单次运行止损。"""
from contextvars import ContextVar
from datetime import datetime,timezone
from functools import wraps
from pathlib import Path
import fcntl,hashlib,json,os,re,stat
from agent_runtime.model_adapters import OpenAICompatibleJsonClient,ModelAdapterError
from market_budget import MarketBudgetMixin,MarketBudgetGuard,MarketBudgetError

_observation=ContextVar('navren_market_model_usage',default=None)
_run_blocks=ContextVar('navren_market_run_provider_blocks',default=None)
_BAD_CONTENT_FILE_LIMIT=8*1024*1024
_BAD_CONTENT_FULL_LIMIT=8*1024

def market_model_session(function):
    """一次刷新内的402止损；不跨运行缓存，也不触碰其他业务客户端。"""
    @wraps(function)
    def guarded(*args,**kwargs):
        token=_run_blocks.set(set())
        try:return function(*args,**kwargs)
        finally:_run_blocks.reset(token)
    return guarded

class _DuplicateMarketJsonKey(ValueError):
    pass


def _market_response_contract_diagnostic(raw):
    """仅返回固定结构错误码；不修复响应、不保存正文/键名/异常文本。"""
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise _DuplicateMarketJsonKey()
            result[key]=value
        return result
    try:
        try:envelope=json.loads(raw.decode('utf-8'))
        except UnicodeError:return 'provider_utf8_invalid'
        except json.JSONDecodeError:return 'provider_json_invalid'
        if (not isinstance(envelope,dict) or not isinstance(envelope.get('choices'),list)
                or not envelope['choices'] or not isinstance(envelope['choices'][0],dict)):
            return 'provider_envelope_invalid'
        choice=envelope['choices'][0];message=choice.get('message')
        if not isinstance(message,dict):return 'provider_message_invalid'
        # length有原有明确拒绝码，不归入JSON结构子类。
        if choice.get('finish_reason')=='length':return None
        content=message.get('content')
        if not isinstance(content,str):return 'model_content_not_string'
        if not content.strip():return 'model_content_empty'
        if len(content)>24000:return 'model_content_too_long'
        try:parsed=json.loads(content,object_pairs_hook=unique)
        except json.JSONDecodeError:return 'content_json_invalid'
        except _DuplicateMarketJsonKey:return 'content_duplicate_key'
        return None if isinstance(parsed,dict) else 'content_object_required'
    except Exception:
        # 观察器不可改变父适配器是否接受、错误分类或原有调用次数。
        return 'contract_detail_unavailable'


def _market_bad_json_content(raw):
    """取模型的公开 Market content，不取响应其他字段或思考内容。"""
    try:
        envelope=json.loads(raw.decode('utf-8'))
        content=envelope['choices'][0]['message']['content']
        if not isinstance(content,str) or not content.strip() or len(content)>24000:return None
        try:json.loads(content)
        except json.JSONDecodeError as error:
            encoded=content.encode('utf-8')
            detail={'content_sha256':hashlib.sha256(encoded).hexdigest(),
                    'content_utf8_bytes':len(encoded),'content_chars':len(content),
                    'json_error_pos':error.pos,'json_error_line':error.lineno,
                    'json_error_column':error.colno}
            # 少数兼容层会把推理标签混进 content；这种内容只留不可逆摘要。
            if re.search(r'<\s*/?\s*(?:think|thinking|analysis)\b|reasoning_content',content,re.I):
                detail['content_suppressed']='possible_reasoning'
                return detail
            if len(encoded)<=_BAD_CONTENT_FULL_LIMIT:
                detail['content']=content
            else:
                begin=max(0,error.pos-320);end=min(len(content),error.pos+320)
                detail.update(content_truncated=True,
                              content_head=content[:160],
                              content_error_excerpt=content[begin:end],
                              content_error_excerpt_start=begin,
                              content_tail=content[-160:])
            return detail
    except (ValueError,UnicodeError,KeyError,IndexError,TypeError,AttributeError):
        pass
    return None


def _write_market_bad_json(note,detail):
    """只写失败的公共生成片段；单文件有界且权限固定为0600。"""
    path=Path(os.environ.get('NAVREN_MARKET_MODEL_LOG',
        str(Path.home()/'Library/Logs/Navren/market-model-calls.jsonl'))).with_name(
        'market-model-invalid-content.jsonl')
    if not path.parent.is_dir():return
    record={key:note.get(key) for key in ('request_id','stage','provider','model_id',
                                        'started_at','http_status','error_code')}
    record.update(detail)
    line=(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8')
    flags=(os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NONBLOCK
           |getattr(os,'O_NOFOLLOW',0))
    fd=os.open(path,flags,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                or info.st_nlink!=1):return
        os.fchmod(fd,0o600)
        if info.st_size+len(line)>_BAD_CONTENT_FILE_LIMIT:return
        view=memoryview(line)
        while view:
            written=os.write(fd,view)
            if written<=0:raise OSError('market_diagnostic_write_incomplete')
            view=view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


class _ObservedResponse:
    def __init__(self,response):self.response=response
    def __getattr__(self,name):return getattr(self.response,name)
    def __enter__(self):self.response.__enter__();return self
    def __exit__(self,*args):return self.response.__exit__(*args)
    def read(self,*args,**kwargs):
        raw=self.response.read(*args,**kwargs)
        note=_observation.get()
        if note is not None:
            note['_response_contract_detail']=_market_response_contract_diagnostic(raw)
            if (note.get('stage')=='market.events' and note.get('provider')=='deepseek'
                    and note.get('model_id')=='deepseek-flash'
                    and note['_response_contract_detail']=='content_json_invalid'):
                note['_bad_json_content']=_market_bad_json_content(raw)
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
    def _request_payload(self, *, system, user, stage):
        # 父适配器通过纯 payload hook 分派；审核材料、响应校验与审计不另实现。
        payload=super()._request_payload(system=system,user=user,stage=stage)
        if self.provider=='zhipu' and self.model_id=='glm-5.3-flash':
            if stage!='reviewer.market':
                raise ValueError('official_market_reviewer_stage_required')
            payload['thinking']={'type':'enabled'}
            payload['reasoning_effort']='low'
            payload['response_format']={'type':'json_object'}
            # 含必需思考的有界输出预留；须在真实业务前纳入独立 Market 预算。
            payload['max_tokens']=8192
        return payload
    def complete_json(self,*,system,user,stage,request_id):
        blocks=_run_blocks.get()
        # 路由只含公开配置，不读取或持久化凭据/hash。两模型同一AiPing
        # credential的402不应被当作另一篇稿件的可恢复错误继续出站。
        route=(self.provider,self.url,self.env_name,self.keychain_service)
        if blocks is not None and ('official_workflow_http402',) in blocks:
            # 审核或生成任一官方通道402后，本轮已无法形成完整审核发布。
            # 后续角色没有实际HTTP响应，不替它们伪记402或unknown用量。
            raise ModelAdapterError('market_workflow_stopped_after_provider402',model_id=self.model_id,attempts=0)
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
            if error.http_status==402 and error.attempts>0 and blocks is not None:
                if self.provider=='ai_ping':blocks.add(route)
                elif self.provider in ('deepseek','zhipu'):blocks.add(('official_workflow_http402',))
            raise
        finally:
            detail=note.pop('_response_contract_detail',None)
            bad_content=note.pop('_bad_json_content',None)
            if note.get('error_code')=='invalid_model_contract':
                note['contract_diagnostic']=detail or 'contract_detail_unavailable'
                if (detail=='content_json_invalid' and bad_content is not None
                        and note.get('http_status')==200
                        and re.fullmatch(r'[0-9a-f]{32}',str(request_id))):
                    try:_write_market_bad_json(note,bad_content)
                    except Exception:pass  # 诊断故障不能覆盖原模型拒绝
            _observation.reset(token);note['ended_at']=datetime.now(timezone.utc).isoformat();note['usage_unknown']=note['usage'] is None
            path=Path(os.environ.get('NAVREN_MARKET_MODEL_LOG',str(Path.home()/'Library/Logs/Navren/market-model-calls.jsonl')))
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('a')as f:f.write(json.dumps(note,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())


class BudgetedMarketJsonClient(MarketBudgetMixin,MarketJsonClient):
    pass


def official_market_client(role, *, urlopen=None, key_loader=None, log_path=None, budget_guard=None, budget_phase=None):
    """三个正式角色固定官方路由；可注入隔离传输，不接受环境路由回退。"""
    if role not in ('primary','reviewer','weekly'):
        raise ValueError('unknown_market_model_role')
    reviewer=role=='reviewer'
    if budget_guard is None:
        # 仅公开控制元数据路径；不读取密钥、不推断余额或授权金额。
        names=('NAVREN_MARKET_BUDGET_CONTROL','NAVREN_MARKET_BUDGET_CONTROL_SHA256',
               'NAVREN_MARKET_BUDGET_LEDGER','NAVREN_MARKET_BUDGET_PHASE')
        values={name:os.environ.get(name,'') for name in names}
        if not all(values.values()):raise MarketBudgetError('market_budget_control_required')
        budget_phase=budget_phase or values[names[3]]
        if budget_phase=='native_scheduled':
            # 临时验收账只适用于指定恢复与下一早slot。后续普通产品业务
            # 继续原scheduler的日期/slot/3次、18候选、deadline与逐笔usage。
            if os.environ.get('NAVREN_MARKET_SCHEDULER_MODE')!='server_timer':
                raise MarketBudgetError('native_scheduled_trusted_launcher_required')
            return MarketJsonClient(
                provider='zhipu' if reviewer else 'deepseek',
                model_id='glm-5.3-flash' if reviewer else 'deepseek-flash',
                url='https://open.bigmodel.cn/api/paas/v4/chat/completions' if reviewer else 'https://api.deepseek.com/chat/completions',
                env_name='GLM_API_KEY' if reviewer else 'DEEPSEEK_API_KEY',
                keychain_service='CC/GLM_API_KEY' if reviewer else 'CC/DEEPSEEK_API_KEY',
                timeout=90 if role=='weekly' else 45,max_retries=0,
                urlopen=urlopen,key_loader=key_loader,log_path=log_path)
        budget_guard=MarketBudgetGuard.from_control_file(
            control_path=values[names[0]],expected_file_sha256=values[names[1]],ledger_path=values[names[2]])
    return BudgetedMarketJsonClient(
        budget_guard=budget_guard,budget_phase=budget_phase,
        provider='zhipu' if reviewer else 'deepseek',
        model_id='glm-5.3-flash' if reviewer else 'deepseek-flash',
        url='https://open.bigmodel.cn/api/paas/v4/chat/completions' if reviewer else 'https://api.deepseek.com/chat/completions',
        env_name='GLM_API_KEY' if reviewer else 'DEEPSEEK_API_KEY',
        keychain_service='CC/GLM_API_KEY' if reviewer else 'CC/DEEPSEEK_API_KEY',
        timeout=90 if role=='weekly' else 45,max_retries=0,
        urlopen=urlopen,key_loader=key_loader,log_path=log_path)
