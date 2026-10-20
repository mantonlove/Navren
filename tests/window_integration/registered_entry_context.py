"""原97执行器的可信注册测试入口；不更改Agent、工具、gold或普通API。"""
from contextlib import closing,contextmanager
from http.cookiejar import Cookie
from pathlib import Path
import hashlib,importlib,sys,types,uuid
from urllib.request import HTTPCookieProcessor

from registered_fixture_template import (FrozenFixture,registered_template_factory,
    assert_business_equivalent,canonical,digest,reject,SECRET_COLUMNS,_temporary)

_HOSTS={}

def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def build_template(root,state,core=None):
    """与原题同一个实际generator；参考库和目标库均由外层TDB持有。"""
    from simulated_bank import SimulatedBank
    root=Path(root).resolve();state=_temporary(state);folder=state/'registered-reference';folder.mkdir(exist_ok=False)
    if core is None:
        bank=SimulatedBank(folder/'default.sqlite3')
        inputs={'simulated_bank/fixtures/demo.json':_sha(root/'simulated_bank/fixtures/demo.json')}
        contract='original97-default'
        workspace=bank.create_workspace()['workspace_id']
    else:
        if _sha(root/core['core_path'])!=core['core_sha256']:reject('HARNESS_ORIGINAL_CORE_CHANGED')
        package_name='_original97_registered_reference_'+uuid.uuid4().hex
        package=types.ModuleType(package_name);package.__path__=[str(root/core['generation_sources_archive']/'simulated_bank')];sys.modules[package_name]=package
        try:
            for name,expected in core['generation_source_sha256'].items():
                if _sha(root/core['generation_sources_archive']/name)!=expected:reject('HARNESS_REFERENCE_GENERATOR_CHANGED')
            original=importlib.import_module(package_name+'.service')
            bank=original.SimulatedBank(folder/'core.sqlite3',fixture=root/core['core_path'],clock=lambda:core['clock_epoch'])
            workspace=bank.create_workspace()['workspace_id']
        finally:
            for name in list(sys.modules):
                if name==package_name or name.startswith(package_name+'.'):sys.modules.pop(name,None)
        inputs={core['core_path']:core['core_sha256'],**core['generation_source_sha256']};contract='original97-core'
    return FrozenFixture.capture(bank,workspace,source_contract=contract,input_sha256=inputs)

def _strict_tenant_times(g,workspace,template):
    """独立于05旧normalizer：业务created_at/populated_at也必须逐值相同。"""
    expected=template.projected(workspace);tables={}
    with closing(g.bank.connect())as db:
        for table,rows in expected.items():
            if table in template.global_tables:continue
            key='id'if table=='workspaces'else('workspace_id'if'workspace_id'in template.columns[table]else'workspace')
            current=[dict(r)for r in db.execute('SELECT * FROM "'+table+'" WHERE '+key+'=?',(workspace,))]
            omit=SECRET_COLUMNS if table=='workspaces'else set()
            normalized=lambda values:sorted([{k:v for k,v in row.items()if k not in omit}for row in values],key=canonical)
            if normalized(current)!=normalized(rows):reject('HARNESS_EXACT_CONSTRUCTOR_COPY_MISMATCH_'+table)
            tables[table]={'rows':len(rows),'created_at_exact':True,'sha256':digest(normalized(current))}
    return tables

@contextmanager
def template_factory(template):
    from registered_profile_templates import ProfileTemplate,registered_profile_factory
    factory=registered_profile_factory(template)if isinstance(template,ProfileTemplate)else registered_template_factory(template)
    with factory as actual:yield actual

class RegisteredEntryHost:
    def __init__(self,server,template,persona='tycoon'):
        from registered_instances import RegisteredExperienceProvider,InstanceRegistry
        self.server,self.g,self.template,self.persona=server,server.finance,template,persona
        if not self.g.registered_mode or type(self.g.experience_provider)is not RegisteredExperienceProvider or type(self.g.experience_provider.instances)is not InstanceRegistry:
            reject('HARNESS_ACTUAL_REGISTERED_PROVIDER_REQUIRED')
        self.browsers={};self.receipts=[];self.completed={}

    def _complete(self,lease):
        from registered_profile_templates import ProfileTemplate,complete_profile_scenario,assert_profile_equivalent
        workspace=lease['workspace_id']
        if workspace in self.completed:return self.completed[workspace]
        if isinstance(self.template,ProfileTemplate):
            complete_profile_scenario(self.g,lease,self.template)
            proof=assert_profile_equivalent(self.g,lease['workspace_id'],self.template)
            exact_times={'verified_by':'profile full row equality including created_at'}
        else:
            self.g.experience_provider.materializer.complete_scenario(lease)
            proof=assert_business_equivalent(self.g,lease['workspace_id'],self.template)
            exact_times=_strict_tenant_times(self.g,lease['workspace_id'],self.template)
        self.receipts.append({'owner':lease['registered_user_id'],'persona':lease['account_id'],
            'workspace_id':lease['workspace_id'],'baseline':proof,'exact_business_timestamps':exact_times})
        self.completed[workspace]=proof
        return proof

    def bootstrap(self,opener):
        key=id(opener)
        if key in self.browsers:return
        owner=uuid.uuid4().hex;subject='original97-trusted-fixture-'+uuid.uuid4().hex
        lease=self.g.experience_provider.select_registered(owner,self.persona,subject,auth_expires_at=self.g.bank.clock()+600)
        self._complete(lease)
        jar=_jar(opener);_install_cookie(jar,lease['upstream_cookies']['qz_bank_user'])
        self.browsers[key]={'registered_user_id':owner,'auth_subject':subject,'workspace_id':lease['workspace_id']}

    def consume(self,opener,session):
        token=_token(opener);record=self.g.experience_provider.validate(token,session['workspace_id'])
        expected=self.browsers.get(id(opener))
        if (expected is None or record['registered_user_id']!=expected['registered_user_id']
                or session.get('registered_user_id')!=record['registered_user_id']or session.get('persona_id')!=record['persona_id']):
            reject('HARNESS_HTTP_REGISTERED_BINDING_CHANGED')
        lease={'registered_user_id':record['registered_user_id'],'account_id':record['persona_id'],
            'workspace_id':record['workspace_id'],'upstream_cookies':{'qz_bank_user':token}}
        self._complete(lease);expected['workspace_id']=record['workspace_id'];expected['token']=token
        return token

def _jar(opener):
    found=[h.cookiejar for h in opener.handlers if isinstance(h,HTTPCookieProcessor)]
    if len(found)!=1:reject('HARNESS_ORIGINAL_COOKIEJAR_REQUIRED')
    return found[0]

def _install_cookie(jar,token):
    jar.set_cookie(Cookie(0,'qz_bank_user',token,None,False,'127.0.0.1',False,False,'/',True,False,None,True,None,None,{'HttpOnly':None},False))

def _token(opener):
    values=[c.value for c in _jar(opener)if c.name=='qz_bank_user'and c.domain=='127.0.0.1'and c.path=='/']
    if len(values)!=1:reject('HARNESS_REGISTERED_COOKIE_REQUIRED')
    return values[0]

def attach_server(server,template,persona='tycoon'):
    host=RegisteredEntryHost(server,template,persona);base='http://127.0.0.1:'+str(server.server_port)
    if base in _HOSTS:reject('HARNESS_SERVER_BINDING_COLLISION')
    _HOSTS[base]=host;server.finance._original97_registered_host=host
    old_close=server.server_close
    def close():
        try:return old_close()
        finally:
            if _HOSTS.get(base)is host:_HOSTS.pop(base,None)
    server.server_close=close
    return host

def browser_bootstrap(opener,base):
    if base not in _HOSTS:reject('HARNESS_REGISTERED_SERVER_REQUIRED')
    _HOSTS[base].bootstrap(opener)

def browser_consume(opener,base,session):
    if base not in _HOSTS:reject('HARNESS_REGISTERED_SERVER_REQUIRED')
    return _HOSTS[base].consume(opener,session)

def request_context(gateway,workspace):
    host=getattr(gateway,'_original97_registered_host',None)
    if host is None:reject('HARNESS_REGISTERED_CONTEXT_REQUIRED')
    matches=[row for row in host.browsers.values()if row['workspace_id']==workspace]
    if len(matches)!=1:reject('HARNESS_REGISTERED_CONTEXT_AMBIGUOUS')
    # 从provider当前真实会话找token不能反解hash；令牌只在原opener CookieJar内。
    binding=matches[0];token=binding.get('token')
    if not token:reject('HARNESS_REGISTERED_CONTEXT_TOKEN_REQUIRED')
    gateway.experience_provider.validate(token,workspace)
    return gateway.demo_sessions.request(token,workspace)

def wrap_semantic_server(create_server,root):
    def create(*args,**kwargs):
        from voice_gateway import VoiceGateway
        database=kwargs.get('database')
        if database is None:reject('HARNESS_EXPLICIT_TEMPORARY_DATABASE_REQUIRED')
        template=build_template(root,Path(database).parent)
        if 'finance_factory'in kwargs:reject('HARNESS_FACTORY_ALREADY_SUPPLIED')
        if 'voice'not in kwargs:kwargs['voice']=VoiceGateway(key='',allow_cloud_asr=False)
        with template_factory(template)as factory:server=create_server(*args,**kwargs,finance_factory=factory)
        attach_server(server,template);return server
    return create

def wrap_semantic_request(request):
    def call(opener,base,path,*args,**kwargs):
        if path=='/api/finance/session':browser_bootstrap(opener,base)
        result=request(opener,base,path,*args,**kwargs)
        if path=='/api/finance/session'and result[0]==200:browser_consume(opener,base,result[1])
        return result
    return call
