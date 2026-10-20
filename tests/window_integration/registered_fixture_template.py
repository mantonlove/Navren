"""原97题注册身份适配候选：只用于独立临时业务夹具，不是生产画像模板。"""
from contextlib import closing,contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import hashlib,json,re,secrets,tempfile
from unittest.mock import patch

CONTRACT='navren.original97.registered-fixture-template/1'
SCENE_KEY='test-only-original97-scene:'
DELAYED=('transactions','disputes')
SECRET_COLUMNS=frozenset({'token_hash','agent_token_hash','pin_hash'})

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

def reject(code):
    from simulated_bank import BankError
    raise BankError(code,'原题注册测试模板映射未通过，不能启动模型。',503)

def _temporary(path):
    path=Path(path).resolve();root=Path(tempfile.gettempdir()).resolve()
    if path==root or not path.is_relative_to(root)or path.is_symlink():reject('HARNESS_TEMPORARY_DATABASE_REQUIRED')
    return path

@dataclass(frozen=True)
class FrozenFixture:
    """来源必须是本次原generator实际生成的临时bank；拒绝现役库。"""
    source_workspace:str
    source_bank_path:Path
    rows:dict
    columns:dict
    global_tables:list
    fixture:dict
    seed_hash:str
    source_contract:str
    input_sha256:dict
    sha256:str

    @classmethod
    def capture(cls,bank,workspace,*,source_contract,input_sha256):
        path=_temporary(bank.database)
        if not isinstance(source_contract,str)or not source_contract or type(input_sha256)is not dict or not input_sha256:
            reject('HARNESS_SEALED_INPUT_REQUIRED')
        rows,columns,global_tables={},{},[]
        with closing(bank.connect())as db:
            ws=db.execute('SELECT * FROM workspaces WHERE id=?',(workspace,)).fetchone()
            if ws is None:reject('HARNESS_SOURCE_WORKSPACE_REQUIRED')
            for found in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                name=found[0]
                if re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',name)is None:reject('HARNESS_TABLE_NAME_INVALID')
                cols=[r[1]for r in db.execute('PRAGMA table_info("'+name+'")')]
                if name in ('audit','metadata'):continue
                if name=='workspaces':selected=[dict(ws)]
                elif 'workspace_id'in cols:selected=[dict(r)for r in db.execute('SELECT * FROM "'+name+'" WHERE workspace_id=?',(workspace,))]
                elif 'workspace'in cols:selected=[dict(r)for r in db.execute('SELECT * FROM "'+name+'" WHERE workspace=?',(workspace,))]
                else:
                    selected=[dict(r)for r in db.execute('SELECT * FROM "'+name+'"')];global_tables.append(name)
                if name in ('operations','orders')and selected:reject('HARNESS_ORIGINAL_HAS_OPERATIONS')
                rows[name]=sorted(selected,key=canonical);columns[name]=cols
        body={'source_workspace':workspace,'rows':rows,'columns':columns,'global_tables':global_tables,'fixture':deepcopy(bank.fixture),
            'seed_hash':bank.seed_hash,'source_contract':source_contract,'input_sha256':deepcopy(input_sha256)}
        canonical(body)
        return cls(source_bank_path=path,sha256=digest(body),**body)

    def assert_sealed(self):
        body={k:getattr(self,k)for k in ('source_workspace','rows','columns','global_tables','fixture','seed_hash','source_contract','input_sha256')}
        if digest(body)!=self.sha256:reject('HARNESS_FROZEN_TEMPLATE_CHANGED')

    def projected(self,workspace):
        self.assert_sealed()
        # 原题ID由workspace及其12字前缀派生。只作租户身份替换，保留全部值/类型。
        return json.loads(canonical(self.rows).replace(self.source_workspace,workspace).replace(self.source_workspace[:12],workspace[:12]))

def materializer_class(template):
    from registered_instances import RegisteredTemplateMaterializer,SealedTemplate,persona_id
    from simulated_bank.service import digest as secret_digest
    template.assert_sealed()
    class OriginalFixtureMaterializer(RegisteredTemplateMaterializer):
        def __init__(self,gateway):
            self.g,self.bank=gateway,gateway.bank
            _temporary(self.bank.database);template.assert_sealed()
            if self.bank.database.resolve()==template.source_bank_path:reject('HARNESS_REFERENCE_TARGET_COLLISION')
            self.template_hash=template.sha256;self.version=CONTRACT+':'+template.sha256
            self.bank.fixture=deepcopy(template.fixture);self.bank.seed_hash=template.seed_hash
            with self.bank.transaction()as db:
                if db.execute('SELECT 1 FROM workspaces LIMIT 1').fetchone():reject('HARNESS_TARGET_NOT_EMPTY')
                db.execute('DELETE FROM products')
                for row in template.rows['products']:self._insert(db,'products',row)
                db.execute("UPDATE metadata SET value=? WHERE key='seed_hash'",(template.seed_hash,))

        @staticmethod
        def _insert(db,table,row):
            names=list(row)
            db.execute('INSERT INTO "'+table+'"('+','.join('"'+k+'"'for k in names)+') VALUES('+','.join('?'for _ in names)+')',[row[k]for k in names])

        def template_for_persona(self,persona):
            persona_id(persona)
            return SealedTemplate(persona,self.version,self.template_hash,template.fixture['as_of'],'原题受控测试模板 '+persona)

        def initialize_bank(self,db,workspace,sealed):
            template.assert_sealed()
            if sealed.sha256!=template.sha256:reject('HARNESS_TEMPLATE_BINDING_CHANGED')
            db.execute('PRAGMA defer_foreign_keys=ON')
            rows=template.projected(workspace)
            for table,values in rows.items():
                if table in template.global_tables or table in DELAYED:continue
                actual=[r[1]for r in db.execute('PRAGMA table_info("'+table+'")')]
                if actual!=template.columns[table]:reject('HARNESS_SOURCE_TARGET_SCHEMA_CHANGED')
                for row in values:
                    row=deepcopy(row)
                    if table=='workspaces':
                        for field in SECRET_COLUMNS:row[field]=secret_digest(secrets.token_urlsafe(32))
                    self._insert(db,table,row)
            if db.execute('PRAGMA foreign_key_check').fetchone():reject('HARNESS_INITIAL_FOREIGN_KEY_FAILURE')

        # 私库凭据和ready约束完全复用实际生产实现，不另造owner/binding表。
        initialize_private=RegisteredTemplateMaterializer.initialize_private
        assert_private_binding=RegisteredTemplateMaterializer.assert_private_binding

        def service_for(self,workspace):
            # default/core原题不是三年历史源，不借测试模板伪装该生产来源。
            reject('HARNESS_THREE_YEAR_HISTORY_NOT_IN_ORIGINAL_FIXTURE')

        def complete_scenario(self,lease):
            """只在真实provider发lease后、模型前装载同一原题封存历史行。"""
            provider=self.g.experience_provider;cookies=lease.get('upstream_cookies',{})
            token=cookies.get('qz_bank_user');workspace=lease.get('workspace_id')
            actual=provider.validate(token,workspace)
            if (actual['registered_user_id']!=lease.get('registered_user_id')or actual['persona_id']!=lease.get('account_id')):
                reject('HARNESS_REGISTERED_LEASE_MISMATCH')
            binding=provider.instances.owner_for_workspace(workspace)
            if binding['template_sha256']!=template.sha256:reject('HARNESS_REGISTERED_TEMPLATE_MISMATCH')
            rows=template.projected(workspace);key=SCENE_KEY+workspace
            receipt={'contract':CONTRACT,'template_sha256':template.sha256,'workspace_id':workspace,
                'registered_user_id':actual['registered_user_id'],'persona_id':actual['persona_id'],
                'source_contract':template.source_contract,'historical_rows_sha256':digest({t:rows[t]for t in DELAYED})}
            with self.bank.transaction()as db:
                previous=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
                if previous:
                    if json.loads(previous[0])!=receipt:reject('HARNESS_SCENARIO_RECEIPT_CHANGED')
                    return receipt
                for table in DELAYED:
                    if db.execute('SELECT 1 FROM "'+table+'" WHERE workspace_id=? LIMIT 1',(workspace,)).fetchone():reject('HARNESS_SCENARIO_NOT_CLEAN')
                    for row in rows[table]:self._insert(db,table,row)
                db.execute('INSERT INTO metadata VALUES(?,?)',(key,canonical(receipt)))
                if db.execute('PRAGMA foreign_key_check').fetchone():reject('HARNESS_COMPLETED_FOREIGN_KEY_FAILURE')
            return receipt
    return OriginalFixtureMaterializer

@contextmanager
def registered_template_factory(template):
    """仅gateway构造区临时注入；进入HTTP线程前必须退出该patch。"""
    import registered_instances
    from base_runtime.service_assembly import AssembledGateway
    template.assert_sealed();candidate=materializer_class(template)
    def factory(database=None,**options):
        if 'registered_mode'in options:reject('HARNESS_REGISTERED_MODE_OVERRIDE')
        return AssembledGateway(database,registered_mode=True,**options)
    with patch.object(registered_instances,'RegisteredTemplateMaterializer',candidate):yield factory

def assert_business_equivalent(gateway,workspace,template):
    """租户行逐字段比较；公共products全表比较，额外3persona不混入本题。"""
    template.assert_sealed();expected=template.projected(workspace);checked={}
    with closing(gateway.bank.connect())as db:
        for table,rows in expected.items():
            if table in template.global_tables:actual=[dict(r)for r in db.execute('SELECT * FROM "'+table+'"')]
            elif table=='workspaces':actual=[dict(r)for r in db.execute('SELECT * FROM workspaces WHERE id=?',(workspace,))]
            else:
                owner_column='workspace_id'if'workspace_id'in template.columns[table]else'workspace'
                actual=[dict(r)for r in db.execute('SELECT * FROM "'+table+'" WHERE '+owner_column+'=?',(workspace,))]
            def normalized(values):
                constructor_times={'schema_migrations':{'applied_at'},'workspace_datasets':{'populated_at'}}
                return sorted([{k:v for k,v in r.items()if not(table=='workspaces'and k in SECRET_COLUMNS)
                    and k not in {'created_at'}|constructor_times.get(table,set())}for r in values],key=canonical)
            if normalized(actual)!=normalized(rows):reject('HARNESS_BUSINESS_BASELINE_MISMATCH_'+table)
            checked[table]={'rows':len(rows),'exact_sha256':digest(normalized(actual))}
    return {'contract':CONTRACT,'template_sha256':template.sha256,'workspace_id':workspace,'tables':checked,
        'global_products_compared':True,'current_production_persona_dataset_claim':False,'API_calls':0}
