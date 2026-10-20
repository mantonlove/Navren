"""原97题专用持久预算；默认无授权拒绝，逐attempt及实际HTTP单笔记账。"""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading
import urllib.parse
import urllib.request

PREFLIGHT = 'outputs/window-progress/agent/baseline-remediation-20261014/preflight/preflight.py'
PAYLOAD_FIELDS = {'model','messages','tools','tool_choice','temperature','max_tokens',
                  'thinking','reasoning_effort','response_format'}
MODELS = {'DeepSeek-V4-Flash','GLM-5.3-Flash'}


def load_campaign(root, manifest_path):
    root = Path(root).resolve()
    tree = ast.parse((root/'scripts/run_window_integration.py').read_text())
    pins = [ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
        and any(isinstance(t,ast.Name) and t.id=='PAID_PREFLIGHT_SCRIPT_SHA256' for t in node.targets)]
    path = root/PREFLIGHT
    if len(pins)!=1 or hashlib.sha256(path.read_bytes()).hexdigest()!=pins[0]:
        raise ValueError('campaign_actual_preflight_pin_mismatch')
    spec=importlib.util.spec_from_file_location('_navren_campaign_preflight',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    manifest_path=Path(manifest_path).resolve()
    if not manifest_path.is_relative_to(root) or manifest_path.is_symlink():
        raise ValueError('campaign_manifest_outside_actual_source')
    manifest=module.read(manifest_path)
    if manifest.get('stage')!='paid' or manifest.get('purpose')!='baseline_acceptance':
        raise ValueError('campaign_paid_baseline_manifest_required')
    admission=module.evaluate(root,manifest)
    if admission.get('paid_start_allowed') is not True:
        raise ValueError('campaign_actual_source_runtime_full_preflight_rejected:'+str(admission.get('blockers')))
    return Campaign(root,manifest_path,module)


class Campaign:
    def __init__(self, root, manifest_path, preflight):
        self.root,self.manifest_path,self.p=Path(root).resolve(),Path(manifest_path),preflight
        self.manifest=self.p.read(self.manifest_path)
        self.manifest_sha=self.p.sha(self.manifest_path)
        self.path=Path(self.manifest['ledger']['path'])
        if not self.path.is_absolute():self.path=self.root/self.path
        self.price=self.p.baseline_price_basis(self.root,self.manifest['price_basis'])
        self.p.baseline_authorization(self.root,self.manifest['paid_budget_authorization'])
        self._mutex=threading.RLock()
        self._validate()
    def _validate(self):
        if self.p.sha(self.manifest_path)!=self.manifest_sha:raise ValueError('campaign_manifest_drift')
        state=self.p.baseline_budget_status(self.root,self.manifest['ledger'],self.manifest['paid_budget_authorization'])
        ledger=self.p.read(self.path)
        if (ledger['source_manifest_sha256']!=self.manifest['source_manifest']['sha256'] or
                self.p.sha(self.p.checked(self.root,self.manifest['source_manifest']))!=ledger['source_manifest_sha256']):
            raise ValueError('campaign_source_snapshot_not_same_admitted_version')
        return ledger,state
    @contextmanager
    def _locked(self):
        with self._mutex, self.p.baseline_cumulative_lock():
            lock_path=self.path.with_suffix('.lock')
            if lock_path.is_symlink():raise ValueError('campaign_lock_redirected')
            with lock_path.open('a+b') as lock:
                try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError as error:raise ValueError('campaign_concurrent_ledger_writer') from error
                try:yield self._validate()
                finally:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
    def _commit(self,ledger):
        previous=self.p.read(self.path)
        ledger['revision']=previous['revision']+1
        ledger['previous_state_sha256']=self.p.canonical_sha(previous)
        rows=ledger['starts']
        ledger['totals']={'known_tokens':sum(r['cost']['tokens_lower_bound'] for r in rows),
            'known_cny':str(sum((Decimal(r['cost']['estimated_upper_cny_lower_bound']) for r in rows),Decimal(0))),
            'unknown_calls':sum(r['cost']['unknown_usage_calls'] for r in rows),
            'reserved_tokens':sum(r['reservation']['tokens'] for r in rows),
            'reserved_cny':str(sum((Decimal(r['reservation']['cny']) for r in rows),Decimal(0)))}
        journal=self.path.with_suffix('.journal');journal.mkdir(exist_ok=True)
        if journal.is_symlink():raise ValueError('campaign_journal_redirected')
        target=journal/(str(ledger['revision']).zfill(6)+'.json')
        genesis=self.p.read(self.p.checked(self.root,self.manifest['ledger']['genesis']))
        fixed=set(genesis)-{'starts','totals','revision','previous_state_sha256'}
        certificate={'schema':'navren.baseline-ledger-revision-certificate/1',
            'revision':ledger['revision'],'prev_state_sha256':ledger['previous_state_sha256'],
            'current_state_sha256':self.p.canonical_sha(ledger),
            'fixed_genesis_scope_sha256':self.p.canonical_sha({key:genesis[key] for key in fixed})}
        data=json.dumps(certificate,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        with target.open('x') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        fd=os.open(journal,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        self.p.write(self.path,ledger)
        self._validate()
    def reconcile_artifact_failure(self,closure_ref):
        """仅结清已绑定的已知传输用量；保留原异常/None audit，不产生题目成绩。"""
        with self._locked() as (ledger,_):
            closure=self.p.baseline_artifact_failure_closure(self.root,closure_ref)
            before=closure['before'];receipt=closure['receipt']
            if Path(receipt['ledger_current_path'])!=self.path:
                raise ValueError('campaign_artifact_settlement_wrong_ledger')
            if ledger!=before or self.p.sha(self.path)!=receipt['evidence']['ledger_before']['sha256']:
                raise ValueError('campaign_artifact_settlement_actual_before_changed')
            row=self._row(ledger,receipt['attempt_id'])
            row['state']='closed';row['artifact_failure_settlement']=deepcopy(closure_ref)
            self._commit(ledger)
            return {'revision':ledger['revision'],'ledger_sha256':self.p.sha(self.path),
                'semantic_status':'unscorable_original_failure','known_transport_total':row['cost']['tokens_lower_bound']}
    @staticmethod
    def _row(ledger,attempt_id):
        found=[r for r in ledger['starts'] if r['attempt_id']==attempt_id]
        if len(found)!=1:raise ValueError('campaign_exact_attempt_missing')
        return found[0]
    def begin(self,*,suite_id,case_id,round_index,variant,output):
        from paid_entry_admission import _output_directory
        output=_output_directory(self.root,suite_id,output)
        entrypoint={
            'formal-business-original39':'tests/window_integration/formal_business_runner.py',
            'semantic-original10-three-rounds':'tests/window_integration/semantic_runner_adapter.py',
            'integration-public-original-P10-P02':'tests/window_integration/public_probe_runner.py',
            'integration-finance-original-S01-S02-S03-S10':'tests/window_integration/finance_probe_runner.py',
            'G3-original-public12-finance10':'tests/window_integration/regression_cloud_runner.py'}[suite_id]
        sources=self.p.read(self.p.checked(self.root,self.manifest['source_manifest']))
        actual_entry=self.root/entrypoint
        if sources.get(entrypoint)!=self.p.sha(actual_entry):
            raise ValueError('campaign_actual_entry_not_same_frozen_source')
        requests=self.manifest.get('execution_requests')
        if not isinstance(requests,list):raise ValueError('campaign_execution_requests_required')
        matches=[]
        for request in requests:
            if (not isinstance(request,dict) or request.get('suite_id')!=suite_id
                    or request.get('entrypoint')!=entrypoint or request.get('case_id')is not None
                    or request.get('entrypoint_sha256')!=sources[entrypoint]
                    or request.get('source_manifest_sha256')!=self.manifest['source_manifest']['sha256']):
                continue
            if suite_id=='semantic-original10-three-rounds' and request.get('round_index')!=round_index:
                continue
            parent=_output_directory(self.root,suite_id,request['output_dir'])
            if suite_id=='formal-business-original39':
                expected=parent/('round-'+str(round_index))/(case_id+('-'+variant if variant else ''))
            elif suite_id=='G3-original-public12-finance10':
                expected=parent/str(variant)/case_id
            else:expected=parent/case_id
            if output==expected:matches.append(request)
        if len(matches)!=1:
            raise ValueError('campaign_exact_declared_suite_case_round_output_required')
        expected=self.p.suite_rows(self.root,suite_id)
        valid={r['id'] for r in expected['cases']}
        if case_id not in valid:raise ValueError('campaign_not_original_case')
        if suite_id=='formal-business-original39':
            if round_index not in (1,2,3) or (case_id=='FB10') != bool(variant):
                raise ValueError('campaign_formal_original_round_variant_required')
            if variant and variant not in {'grain_farmer','retired_entrepreneur','tycoon','working_landlady'}:
                raise ValueError('campaign_formal_original_profile_required')
        elif suite_id=='semantic-original10-three-rounds':
            if round_index not in (1,2,3) or variant is not None:raise ValueError('campaign_semantic_original_three_rounds')
        elif suite_id=='G3-original-public12-finance10':
            if round_index!=1 or not any(r['id']==case_id and r['family']==variant for r in expected['cases']):
                raise ValueError('campaign_original_G3_family_case_required')
        elif round_index!=1 or variant is not None:
            raise ValueError('campaign_original_single_probe_round')
        identity='|'.join((suite_id,str(round_index),variant or '',case_id))
        with self._locked() as (ledger,state):
            if (state['accounting_blocked'] or state['active_attempts'] or state['attempts_started']>=97
                    or any(r['attempt_id']==identity for r in ledger['starts'])):
                raise ValueError('campaign_prior_failure_active_or_duplicate_attempt')
            reserve_cny=max(Decimal('.28'),Decimal(100000)*self.price/1000000)+Decimal('.28')
            unstarted_after_this = 97 - state['attempts_started'] - 1
            whole_tokens = 200000 + unstarted_after_this * 100000
            whole_cny = reserve_cny + Decimal(unstarted_after_this) * Decimal('.28')
            if state['remaining_tokens']<whole_tokens or Decimal(state['remaining_cny_estimate'])<whole_cny:
                raise ValueError('campaign_insufficient_all_unstarted_original_tasks_and_current_unknown_hold')
            self.p.baseline_continuation_claim(state['continuation_chain'], commit=True)
            ledger['starts'].append({'attempt_id':identity,'suite_id':suite_id,'case_id':case_id,
                'round_index':round_index,'variant':variant,'output_dir':str(output),
                'source_manifest_sha256':ledger['source_manifest_sha256'],'state':'in_flight',
                'owner_pid':os.getpid(),'calls':[],
                'cost':{'tokens_lower_bound':0,'estimated_upper_cny_lower_bound':'0','unknown_usage_calls':0},
                'reservation':{'tokens':200000,'cny':str(reserve_cny)}})
            self._commit(ledger)
        return identity
    def delegated(self,attempt_id,*,case_id,output):
        with self._locked() as (ledger,state):
            row=self._row(ledger,attempt_id)
            if (state['accounting_blocked'] or row['state']!='in_flight' or row['case_id']!=case_id
                    or not Path(output).resolve().is_relative_to(Path(row['output_dir']))):
                raise ValueError('campaign_delegated_child_not_actual_active_case')
        return attempt_id
    def before_request(self,attempt_id,request):
        url=urllib.parse.urlsplit(request.full_url)
        if (url.scheme,url.netloc,url.path,url.query)!=( 'https','aiping.cn','/api/v1/chat/completions',''):
            raise ValueError('campaign_only_exact_AiPing_endpoint')
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value:raise ValueError('campaign_duplicate_payload_key')
                value[key]=item
            return value
        payload=json.loads(request.data,object_pairs_hook=unique,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('campaign_nonfinite_payload')))
        if (not isinstance(payload,dict) or set(payload)-PAYLOAD_FIELDS or payload.get('model') not in MODELS
                or not isinstance(payload.get('messages'),list) or type(payload.get('max_tokens'))is not int
                or not 0<payload['max_tokens']<=4000):
            raise ValueError('campaign_actual_payload_auto_route_and_model_required')
        with self._locked() as (ledger,state):
            row=self._row(ledger,attempt_id)
            if state['accounting_blocked'] or row['state']!='in_flight' or any(c['state']=='in_flight' for c in row['calls']):
                raise ValueError('campaign_request_after_unknown_or_concurrent_call')
            row['calls'].append({'index':len(row['calls'])+1,'state':'in_flight',
                'model':payload['model'],'route':'auto_no_preferences',
                'payload_sha256':hashlib.sha256(request.data).hexdigest(),'request_body_persisted':False})
            self._commit(ledger)
            return len(row['calls'])
    def after_request(self,attempt_id,index,raw=None,error=None):
        usage=None
        try:
            parsed=json.loads(raw)
            candidate=parsed['usage']
            if (not isinstance(candidate,dict) or any(type(candidate.get(k))is not int or candidate[k]<0
                    for k in ('prompt_tokens','completion_tokens','total_tokens'))
                    or candidate['prompt_tokens']+candidate['completion_tokens']!=candidate['total_tokens']):
                raise ValueError('unknown')
            usage={k:candidate[k] for k in ('prompt_tokens','completion_tokens','total_tokens')}
        except (ValueError,TypeError,KeyError):pass
        with self._locked() as (ledger,state):
            row=self._row(ledger,attempt_id);call=row['calls'][index-1]
            if call['state']!='in_flight':raise ValueError('campaign_call_already_settled')
            call.update(state='closed',usage=usage,error_type=type(error).__name__ if error is not None else None)
            cost=row['cost']
            if usage is None:
                cost['unknown_usage_calls']+=1;row['state']='unknown'
                row['reservation']={'tokens':0,'cny':'0'}
            else:
                cost['tokens_lower_bound']+=usage['total_tokens']
                cost['estimated_upper_cny_lower_bound']=str(Decimal(cost['tokens_lower_bound'])*self.price/1000000)
                if cost['tokens_lower_bound']>100000:
                    # 原实际超限仍保留，不以截断用量伪装在限内；下次校验拒绝。
                    row['state']='blocked';row['reservation']={'tokens':0,'cny':'0'}
                else:
                    remainder=100000-cost['tokens_lower_bound']
                    row['reservation']={'tokens':remainder+100000,
                        'cny':str(max(Decimal('.28'),Decimal(remainder)*self.price/1000000)+Decimal('.28'))}
            self._commit(ledger)
    def finish(self,attempt_id,*,audit_total=None,failed=False):
        with self._locked() as (ledger,state):
            row=self._row(ledger,attempt_id)
            pending=[c for c in row['calls'] if c['state']=='in_flight']
            if pending:
                row['cost']['unknown_usage_calls']+=len(pending)
                for call in pending:call.update(state='closed',usage=None,error_type='InterruptedBeforeSettlement')
                failed=True
            known=row['cost']['tokens_lower_bound']
            unknown=row['cost']['unknown_usage_calls']
            if row['calls'] and not unknown and (type(audit_total)is not int or audit_total!=known):failed=True
            if row['state']=='blocked':failed=True
            row.update(state='unknown' if unknown and row['state']!='blocked' else 'blocked' if failed else 'closed',
                attempt_audit_total=audit_total,execution_raised=bool(failed))
            row['reservation']={'tokens':0,'cny':'0'}
            self._commit(ledger)
            return deepcopy(row)
    @contextmanager
    def transport(self,attempt_id):
        original=urllib.request.urlopen
        campaign=self
        class Response:
            def __init__(self,response,index):self.response,self.index,self.done=response,index,False
            def __getattr__(self,name):return getattr(self.response,name)
            def __enter__(self):self.response.__enter__();return self
            def settle(self,raw=None,error=None):
                if not self.done:
                    self.done=True;campaign.after_request(attempt_id,self.index,raw,error)
            def read(self,*args,**kwargs):
                try:raw=self.response.read(*args,**kwargs)
                except BaseException as error:self.settle(error=error);raise
                self.settle(raw=raw);return raw
            def __exit__(self,*args):
                self.settle(error=args[1] if args and args[0] else RuntimeError('response_not_read'))
                return self.response.__exit__(*args)
        def guarded(request,*args,**kwargs):
            url=getattr(request,'full_url',str(request));host=urllib.parse.urlsplit(url).hostname
            if host in {'localhost','127.0.0.1','::1'}:return original(request,*args,**kwargs)
            index=campaign.before_request(attempt_id,request)
            try:response=original(request,*args,**kwargs)
            except BaseException as error:campaign.after_request(attempt_id,index,error=error);raise
            return Response(response,index)
        urllib.request.urlopen=guarded
        try:yield
        finally:
            if urllib.request.urlopen is not guarded:raise ValueError('campaign_transport_guard_was_replaced')
            urllib.request.urlopen=original


def audit_total(result):
    if not isinstance(result,dict):return None
    if isinstance(result.get('budget'),dict):return result['budget'].get('actual_provider_total_tokens')
    if isinstance(result.get('paid_usage_audit'),dict):return result['paid_usage_audit'].get('actual_total_tokens')
    if isinstance(result.get('result'),dict):return result['result'].get('agent_runtime',{}).get('total_tokens')
    return result.get('actual_total_tokens')


def execute(campaign,*,suite_id,case_id,round_index=1,variant=None,output,call):
    identity=campaign.begin(suite_id=suite_id,case_id=case_id,round_index=round_index,variant=variant,output=output)
    result=None;failed=True
    try:
        with campaign.transport(identity):result=call(identity)
        failed=False
        return result
    finally:
        receipt=campaign.finish(identity,audit_total=audit_total(result),failed=failed)
        campaign.p.write(Path(output)/'paid-campaign-attempt.json',receipt)
        if isinstance(result,dict):
            result['paid_campaign']={'attempt_id':identity,'ledger':campaign.manifest['ledger'],
                'manifest_sha256':campaign.manifest_sha,'receipt':str(Path(output)/'paid-campaign-attempt.json'),
                'receipt_sha256':campaign.p.sha(Path(output)/'paid-campaign-attempt.json')}
