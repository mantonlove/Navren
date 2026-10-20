"""Process-local MarketStore authority; database metadata never supplies a path.

Bindings are immutable exact-row lineage, not an LLM judgement or a cryptographic
proof against trusted Python/SQL administrators. A restart must reinject its public
MarketStore. Reads freeze one upstream transaction; writes take the upstream lock
first and hold it through KG commit. No authority result survives a call.
"""
from contextlib import contextmanager
from dataclasses import dataclass,asdict
from pathlib import Path
import json,sqlite3,threading,asyncio
from .store import KnowledgeError,sha,packed,MAX_TEXT

CONTRACT='market-permission-article-lineage/2'
KEYS={'contract','authority_id','source_id','permission_revision','registry_sha256','definition_sha256',
      'article_id','article_version','article_body_sha256','parsed_sha256'}
PRIVATE={'workspaces','user_memories','agent_dialogues','personal_decisions','accounts','transactions','user_profiles','personal_attachments'}

@dataclass(frozen=True)
class MarketCapture:
    article_json:str
    binding_json:str
    @property
    def article(self):return json.loads(self.article_json)
    @property
    def binding(self):return json.loads(self.binding_json)

class MarketAuthority:
    def __init__(self,store):
        from market_content import MarketStore
        if type(store) is not MarketStore:raise KnowledgeError('invalid_market_authority')
        self.store=store
        self.path=Path(store.path).resolve(strict=True)
        st=self.path.stat();self.file_identity=(st.st_dev,st.st_ino)
        self.identity=sha(packed([CONTRACT,str(self.path),*self.file_identity]))

    @contextmanager
    def snapshot(self, *, write=False):
        db=None;snapshot=None
        try:
            path=Path(self.store.path).resolve(strict=True);st=path.stat()
            if path!=self.path or (st.st_dev,st.st_ino)!=self.file_identity:raise KnowledgeError('market_authority_identity_changed')
            db=sqlite3.connect(self.path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=5)
            db.row_factory=sqlite3.Row;db.execute('PRAGMA busy_timeout=5000')
            names={row[0] for row in db.execute("SELECT name FROM main.sqlite_schema WHERE type='table'")}
            if names&PRIVATE or not {'market_articles','market_source_permissions','market_sources'}<=names:
                raise KnowledgeError('invalid_public_market_authority')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            snapshot=MarketSnapshot(self,db);yield snapshot
            db.commit()
        except (sqlite3.Error,OSError,KnowledgeError) as error:
            # Startup/open/schema errors fail closed while nonmarket data stays
            # available. Errors raised by the caller after yield must propagate.
            if snapshot is not None:raise
            yield None
        finally:
            if snapshot is not None:
                snapshot.ready=False;snapshot.cache.clear();snapshot.permissions.clear();snapshot.definitions.clear()
            if db is not None:db.close()

def _execution_owner():
    try:task=asyncio.current_task()
    except RuntimeError:task=None
    return (threading.get_ident(),id(task) if task is not None else None)

class MarketSnapshot:
    def __init__(self,authority,db):
        from market_sources import REGISTRY
        self.authority,self.db=authority,db;self.ready=True;self.cache={};self.owner=_execution_owner()
        self.registry={key:packed(asdict(source)) for key,source in REGISTRY.items()}
        self.permissions={row['source_id']:dict(row) for row in db.execute('SELECT * FROM main.market_source_permissions')}
        self.definitions={row['id']:row['definition'] for row in db.execute('SELECT id,definition FROM main.market_sources')}

    def reusable(self):
        return self.ready and self.owner==_execution_owner()

    def latest_ids(self,limit):
        if not self.reusable():raise KnowledgeError('market_authority_scope_expired')
        return [row[0] for row in self.db.execute('''SELECT a.id FROM main.market_articles a JOIN
            (SELECT id,MAX(version) version FROM main.market_articles GROUP BY id) b
            ON a.id=b.id AND a.version=b.version ORDER BY a.checked_at DESC,a.id LIMIT ?''',(limit,))]

    def capture(self,article_id):
        if not self.reusable():raise KnowledgeError('market_authority_scope_expired')
        if not isinstance(article_id,str):raise KnowledgeError('source_permission_missing')
        if article_id in self.cache:return self.cache[article_id]
        row=self.db.execute('SELECT * FROM main.market_articles WHERE id=? ORDER BY version DESC LIMIT 1',(article_id,)).fetchone()
        if row is None or row['status']!='active':raise KnowledgeError('withdrawn_evidence')
        try:
            a=json.loads(row['body']);source_id=a['source_id'];source=json.loads(self.registry[source_id]);permit=self.permissions[source_id];definition=json.loads(self.definitions[source_id])
            if (source['enabled'] is not True or source['scope']!='official_facts'
                    or definition.get('enabled') is not True or definition.get('scope')!='official_facts'
                    or permit['enabled']!=1 or type(permit['revision']) is not int or permit['revision']<1):
                raise KnowledgeError('source_permission_missing')
            from market_sources import Source,safe_url
            source_object=Source(**source)
            if (a.get('id')!=article_id or a.get('version')!=row['version'] or type(row['version']) is not int
                    or row['version']<1 or a.get('read_scope')!='full_text' or a.get('link_status')!='verified'
                    or a.get('evidence_truncated') is not False
                    or (a.get('access_status')!='free_full_text' and 'access_status' in a)
                    or a.get('content_mode')=='source_brief' or a.get('source_content_format') not in (None,'text_article')
                    or a.get('page_has_video') is True
                    or safe_url(source_object,a['url'])!=a['url']):raise KnowledgeError('incomplete_full_text')
            text=a.get('evidence_text')
            if not isinstance(text,str) or not text.strip() or len(text)>MAX_TEXT:raise KnowledgeError('incomplete_full_text')
            a['checked_at']=row['checked_at']
            binding={'contract':CONTRACT,'authority_id':self.authority.identity,'source_id':source_id,
                     'permission_revision':permit['revision'],'registry_sha256':sha(self.registry[source_id]),
                     'definition_sha256':sha(packed(definition)),'article_id':article_id,'article_version':row['version'],
                     'article_body_sha256':sha(row['body']),'parsed_sha256':sha(text)}
            result=MarketCapture(packed(a),packed(binding));self.cache[article_id]=result;return result
        except (KeyError,ValueError,TypeError) as error:
            if isinstance(error,KnowledgeError):raise
            raise KnowledgeError('source_structure_drift') from None

    def valid(self,binding):
        if not self.reusable() or not isinstance(binding,dict) or set(binding)!=KEYS or binding.get('contract')!=CONTRACT or binding.get('authority_id')!=self.authority.identity:return False
        try:return self.capture(binding['article_id']).binding==binding
        except (KnowledgeError,TypeError,ValueError):return False
