"""FB10 原四画像的临时注册适配；保留旧业务事实，仅允许每个 bank 一个 owner。"""
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import secrets
import shutil
from unittest.mock import patch

from registered_fixture_template import (
    FrozenFixture, canonical, digest, reject, _temporary, SECRET_COLUMNS,
    materializer_class,
)

CONTRACT = 'navren.original97.registered-profile-template/1'


@dataclass(frozen=True)
class ProfileTemplate:
    selected: str
    templates: dict
    history_path: Path
    history_sha256: str
    source_sha256: dict

    @property
    def template(self):
        return self.templates[self.selected]

    @property
    def templates_by_persona(self):
        return self.templates


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_profile_template(root, state, persona, contract):
    """只调用原 legacy generator；state 必须是调用方持有的独立临时目录。"""
    from finance_gateway import FinanceGateway
    from demo_accounts import USER_IDS
    if persona not in USER_IDS:
        reject('HARNESS_PROFILE_NOT_FOUND')
    root = Path(root).resolve()
    state = _temporary(Path(state) / 'profile-reference')
    state.mkdir(parents=True, exist_ok=False)
    source = contract['fixture_contract']['four_profiles_oracle_v1']
    copied = state / 'source'
    copied.mkdir()
    hashes = deepcopy(source['source_file_sha256'])
    for name, expected in hashes.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            reject('HARNESS_PROFILE_SOURCE_PATH_INVALID')
        origin = root / source['source_directory'] / relative
        target = copied / relative
        if origin.is_symlink() or _sha(origin) != expected:
            reject('HARNESS_PROFILE_SOURCE_CHANGED')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
        if _sha(target) != expected:
            reject('HARNESS_PROFILE_COPY_CHANGED')
    gateway = FinanceGateway(state / 'reference.sqlite3', registered_mode=False)
    gateway.demo_accounts.source_dir = copied
    imported = gateway.demo_accounts.initialize_offline(allow_offline=True)
    if imported['initialized_count'] != 4:
        reject('HARNESS_PROFILE_IMPORT_INCOMPLETE')
    templates = {}
    for uid in USER_IDS:
        workspace = gateway.demo_accounts.stable_workspace(uid)
        templates[uid] = FrozenFixture.capture(gateway.bank, workspace,
            source_contract=CONTRACT, input_sha256=hashes)
    with closing(gateway.bank.connect()) as db:
        records = [dict(r) for r in db.execute('SELECT * FROM navren_demo_accounts')]
    paths = {r['history_path'] for r in records}
    sums = {r['history_sha256'] for r in records}
    if len(paths) != 1 or len(sums) != 1:
        reject('HARNESS_PROFILE_HISTORY_NOT_UNIQUE')
    history = Path(next(iter(paths)))
    history_sum = next(iter(sums))
    if history.is_symlink() or _sha(history) != history_sum:
        reject('HARNESS_PROFILE_HISTORY_CHANGED')
    return ProfileTemplate(persona, templates, history, history_sum, hashes)


def _materializer(bundle):
    from registered_instances import RegisteredTemplateMaterializer, SealedTemplate
    from demo_accounts import USER_IDS
    from simulated_bank.service import digest as secret_digest

    class LegacyProfileMaterializer(RegisteredTemplateMaterializer):
        def __init__(self, gateway):
            self.g, self.bank = gateway, gateway.bank
            _temporary(self.bank.database)
            for template in bundle.templates.values():
                template.assert_sealed()
                if self.bank.database.resolve() == template.source_bank_path:
                    reject('HARNESS_REFERENCE_TARGET_COLLISION')
            self.template_hash = digest({uid: t.sha256 for uid, t in bundle.templates.items()})
            self.version = CONTRACT + ':' + self.template_hash
            self.bank.fixture = deepcopy(bundle.template.fixture)
            self.bank.seed_hash = bundle.template.seed_hash
            self.history = self.bank.database.with_name('original-fb10-history.sqlite3')
            if self.history.exists() or bundle.history_path.is_symlink() or _sha(bundle.history_path) != bundle.history_sha256:
                reject('HARNESS_PROFILE_HISTORY_CONFLICT')
            shutil.copy2(bundle.history_path, self.history)
            if _sha(self.history) != bundle.history_sha256:
                reject('HARNESS_PROFILE_HISTORY_CHANGED')
            self.history.chmod(0o400)
            public_rows = [deepcopy(r) for r in bundle.template.rows['products']
                           if 'workspace_id' not in json.loads(r['data'])]
            for template in bundle.templates.values():
                candidate_public = [r for r in template.rows['products']
                                    if 'workspace_id' not in json.loads(r['data'])]
                if canonical(candidate_public) != canonical(public_rows):
                    reject('HARNESS_PROFILE_PUBLIC_PRODUCTS_CHANGED')
            with self.bank.transaction() as db:
                if db.execute('SELECT 1 FROM workspaces LIMIT 1').fetchone():
                    reject('HARNESS_TARGET_NOT_EMPTY')
                db.execute('DELETE FROM products')
                for row in public_rows:
                    self._insert(db, 'products', row)
                db.execute("UPDATE metadata SET value=? WHERE key='seed_hash'", (self.bank.seed_hash,))

        @staticmethod
        def _insert(db, table, row):
            names = list(row)
            db.execute('INSERT INTO "' + table + '"(' + ','.join('"' + k + '"' for k in names) + ') VALUES(' + ','.join('?' for _ in names) + ')', [row[k] for k in names])

        def template_for_persona(self, persona):
            if persona not in USER_IDS:
                reject('HARNESS_PROFILE_NOT_FOUND')
            template = bundle.templates[persona]
            template.assert_sealed()
            record = template.rows['navren_demo_accounts'][0]
            return SealedTemplate(persona, self.version, template.sha256, record['as_of'], record['display_name'])

        def initialize_bank(self, db, workspace, sealed):
            template = bundle.templates[sealed.persona_id]
            template.assert_sealed()
            if sealed.sha256 != template.sha256:
                reject('HARNESS_PROFILE_TEMPLATE_CHANGED')
            rows = template.projected(workspace)
            # 旧 DEMO ID 是全局主键。不同 owner 的同画像不能共享一条带 workspace 的产品。
            selected = [r for r in rows['products'] if json.loads(r['data']).get('workspace_id') == workspace]
            if any(db.execute('SELECT 1 FROM products WHERE id=?', (r['id'],)).fetchone() for r in selected):
                reject('HARNESS_LEGACY_PRODUCT_SECOND_OWNER_UNSUPPORTED')
            db.execute('PRAGMA defer_foreign_keys=ON')
            for row in selected:
                self._insert(db, 'products', row)
            for table, values in rows.items():
                if table in template.global_tables or table in ('transactions', 'disputes', 'navren_demo_accounts'):
                    continue
                actual = [r[1] for r in db.execute('PRAGMA table_info("' + table + '")')]
                if actual != template.columns[table]:
                    reject('HARNESS_PROFILE_SCHEMA_CHANGED')
                for row in values:
                    row = deepcopy(row)
                    if table == 'workspaces':
                        for field in SECRET_COLUMNS:
                            row[field] = secret_digest(secrets.token_urlsafe(32))
                    self._insert(db, table, row)
            source = deepcopy(rows['navren_demo_accounts'][0])
            source['history_path'] = self.history.name
            # 来源 ready 是原 generator 已审计的来源状态；实例 ready/会话仍由生产 provider 管理。
            self._insert(db, 'registered_demo_sources', source)
            if db.execute('PRAGMA foreign_key_check').fetchone():
                reject('HARNESS_PROFILE_FOREIGN_KEY_FAILURE')

        initialize_private = RegisteredTemplateMaterializer.initialize_private
        assert_private_binding = RegisteredTemplateMaterializer.assert_private_binding

        def service_for(self, workspace):
            # 原 FB10 采用十年 legacy 来源，不冒充三年 FinancialDataService。
            return None

        def complete_scenario(self, lease):
            persona = lease.get('account_id')
            if persona not in bundle.templates:
                reject('HARNESS_PROFILE_LEASE_MISMATCH')
            return materializer_class(bundle.templates[persona]).complete_scenario(self, lease)

    return LegacyProfileMaterializer


@contextmanager
def registered_profile_factory(bundle):
    """仅构造时注入；调用方必须在监听 HTTP 前退出上下文。"""
    import registered_instances
    from base_runtime.service_assembly import AssembledGateway
    candidate = _materializer(bundle)
    def factory(database=None, **options):
        if 'registered_mode' in options:
            reject('HARNESS_REGISTERED_MODE_OVERRIDE')
        return AssembledGateway(database, registered_mode=True, **options)
    with patch.object(registered_instances, 'RegisteredTemplateMaterializer', candidate):
        yield factory


def complete_profile_scenario(gateway, lease, bundle):
    return gateway.experience_provider.materializer.complete_scenario(lease)


def assert_profile_equivalent(gateway, workspace, bundle):
    """核本题全部租户业务字段；来源表重命名与历史副本路径单独正规化。"""
    template = bundle.template
    template.assert_sealed()
    rows = template.projected(workspace)
    checked = {}
    with closing(gateway.bank.connect()) as db:
        binding = gateway.experience_provider.instances.owner_for_workspace(workspace, db=db)
        if binding['persona_id'] != bundle.selected or binding['template_sha256'] != template.sha256:
            reject('HARNESS_PROFILE_BINDING_CHANGED')
        sessions = [dict(r) for r in db.execute('''SELECT s.*,
            d.workspace_id AS bank_workspace,d.revoked AS bank_revoked,d.expires_at AS bank_expires,
            i.registered_user_id AS instance_owner,i.persona_id AS instance_persona,i.status AS instance_status,
            a.revoked AS subject_revoked,a.generation AS subject_generation,a.expires_at AS subject_expires
            FROM registered_instance_sessions s
            JOIN demo_sessions d ON d.token_hash=s.token_hash
            JOIN registered_user_instances i ON i.workspace_id=s.workspace_id
            JOIN registered_auth_subjects a ON a.registered_user_id=s.registered_user_id AND a.auth_subject=s.auth_subject
            WHERE s.workspace_id=? AND s.revoked=0''', (workspace,))]
        bank_sessions = db.execute('SELECT token_hash FROM demo_sessions WHERE workspace_id=? AND revoked=0',
                                   (workspace,)).fetchall()
        now = gateway.bank.clock()
        if len(sessions) != 1 or len(bank_sessions) != 1:
            reject('HARNESS_PROFILE_ACTIVE_SESSION_NOT_UNIQUE')
        session = sessions[0]
        if (bank_sessions[0]['token_hash'] != session['token_hash']
                or session['registered_user_id'] != binding['registered_user_id']
                or session['instance_owner'] != binding['registered_user_id']
                or session['persona_id'] != bundle.selected or session['instance_persona'] != bundle.selected
                or session['instance_status'] != 'ready' or session['bank_workspace'] != workspace
                or session['bank_revoked'] or session['subject_revoked']
                or session['subject_generation'] != session['generation']
                or session['subject_expires'] != session['auth_expires_at']
                or min(session['expires_at'], session['bank_expires'], session['auth_expires_at']) <= now
                or max(session['expires_at'], session['bank_expires']) > session['auth_expires_at']):
            reject('HARNESS_PROFILE_REGISTERED_SESSION_MISMATCH')
        gateway.experience_provider.materializer.assert_private_binding(binding)
        checked['registered_session_binding'] = {'workspace_id': workspace,
            'registered_user_id': binding['registered_user_id'], 'persona_id': bundle.selected,
            'instance_status': session['instance_status'], 'generation': session['generation'],
            'bank_and_registered_session_join_checked': True, 'private_binding_checked': True}
        public_expected = [r for r in rows['products']
                           if 'workspace_id' not in json.loads(r['data'])]
        public_actual = [dict(r) for r in db.execute('SELECT * FROM products')
                         if 'workspace_id' not in json.loads(r['data'])]
        if sorted(public_actual, key=canonical) != sorted(public_expected, key=canonical):
            reject('HARNESS_PROFILE_PUBLIC_PRODUCTS_MISMATCH')
        checked['public_products'] = {'rows': len(public_expected),
            'sha256': digest(sorted(public_actual, key=canonical))}
        for table, expected in rows.items():
            # 原参考无凭据；本入口实际 provider 授予的会话在上方独立联结核验。
            if table == 'demo_sessions':
                continue
            if table in template.global_tables:
                if table != 'products':
                    continue
                expected = [r for r in expected if json.loads(r['data']).get('workspace_id') == workspace]
                actual = [dict(db.execute('SELECT * FROM products WHERE id=?', (r['id'],)).fetchone()) for r in expected]
            else:
                target = 'registered_demo_sources' if table == 'navren_demo_accounts' else table
                key = 'id' if table == 'workspaces' else ('workspace_id' if 'workspace_id' in template.columns[table] else 'workspace')
                actual = [dict(r) for r in db.execute('SELECT * FROM "' + target + '" WHERE ' + key + '=?', (workspace,))]
            def normalize(values):
                # 所有业务创建时间由原模板逐值复制，不能宽泛豁免。
                omitted = set()
                if table == 'workspaces':
                    omitted |= SECRET_COLUMNS
                if table == 'navren_demo_accounts':
                    omitted |= {'history_path'}
                return sorted([{k: v for k, v in row.items() if k not in omitted} for row in values], key=canonical)
            if normalize(actual) != normalize(expected):
                reject('HARNESS_PROFILE_BASELINE_MISMATCH_' + table)
            checked[table] = {'rows': len(expected), 'sha256': digest(normalize(actual))}
        source = db.execute('SELECT * FROM registered_demo_sources WHERE workspace_id=?', (workspace,)).fetchone()
        if source is None or _sha(gateway.bank.database.parent / source['history_path']) != source['history_sha256']:
            reject('HARNESS_PROFILE_HISTORY_CHANGED')
    return {'contract': CONTRACT, 'persona': bundle.selected, 'tables': checked,
        'legacy_product_ids_preserved': True, 'multi_owner_same_bank_supported': False,
        'production_three_year_dataset_claim': False, 'API_calls': 0}
