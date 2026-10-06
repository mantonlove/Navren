"""公共证据图：固定类型、谓词、来源边与最多四跳的 SQL 遍历。

不接收生成式抽取、任意查询语言或私有资料。自动部分仅建立受审核的资料绑定
与原文映射；金融机构关系须由本地人员审核原文后明确录入。
"""
from __future__ import annotations
from collections import deque
import json

from .store import KnowledgeError,packed,sha,day,FACETS

KINDS={'product','share_class','channel','institution','clause','document_version','document','source',
       'fee_rule','risk_disclosure','liquidity_rule','eligibility_rule','strategy_description',
       'sale_observation','return_observation','terms','general','effective_period'}
RULE_KIND={'fees':'fee_rule','risk':'risk_disclosure','liquidity':'liquidity_rule','eligibility':'eligibility_rule',
           'strategy':'strategy_description','sale_status':'sale_observation','returns':'return_observation',
           'terms':'terms','general':'general'}
PREDICATES={
 'has_disclosure':({ 'product' },{'clause'}),
 'has_share_class':({'product'},{'share_class'}),
 'has_share_terms':({'share_class'},{'clause'}),
 'channel_discloses':({'channel'},{'clause'}),
 'quoted_in':({'clause'},{'document_version'}),
 'version_of':({'document_version'},{'document'}),
 'published_by':({'document'},{'source'}),
 'has_rule':({'clause'},set(RULE_KIND.values())),
 'effective_during':({'clause'},{'effective_period'}),
 'issued_by':({'product'},{'institution'}),
 'managed_by':({'product','share_class'},{'institution'}),
 'distributed_by':({'product','share_class'},{'channel'}),
}

SCHEMA='''
CREATE TABLE IF NOT EXISTS graph_entities(
 id TEXT PRIMARY KEY NOT NULL,kind TEXT NOT NULL CHECK(kind IN('product','share_class','channel','institution','clause','document_version','document','source',
 'fee_rule','risk_disclosure','liquidity_rule','eligibility_rule','strategy_description','sale_observation','return_observation','terms','general','effective_period')),
 public_ref TEXT NOT NULL,label TEXT NOT NULL,
 attributes TEXT NOT NULL CHECK(json_valid(attributes)),scope TEXT NOT NULL CHECK(scope='public'),
 UNIQUE(kind,public_ref));
CREATE TABLE IF NOT EXISTS graph_relations(
 id TEXT PRIMARY KEY NOT NULL,subject_id TEXT NOT NULL REFERENCES graph_entities(id),
 object_id TEXT NOT NULL REFERENCES graph_entities(id),predicate TEXT NOT NULL CHECK(predicate IN('has_disclosure','has_share_class','has_share_terms',
 'channel_discloses','quoted_in','version_of','published_by','has_rule','effective_during','issued_by','managed_by','distributed_by')),
 version_id TEXT NOT NULL REFERENCES document_versions(id),citation_id TEXT NOT NULL REFERENCES chunks(id),
 valid_from TEXT,valid_to TEXT,status TEXT NOT NULL CHECK(status IN('verified','withdrawn')),
 reviewed_by TEXT NOT NULL,CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_from<valid_to),
 UNIQUE(subject_id,object_id,predicate,version_id,citation_id));
CREATE TABLE IF NOT EXISTS graph_provenance(
 relation_id TEXT PRIMARY KEY NOT NULL REFERENCES graph_relations(id),citation_id TEXT NOT NULL REFERENCES chunks(id),
 version_id TEXT NOT NULL REFERENCES document_versions(id),start_char INTEGER NOT NULL,end_char INTEGER NOT NULL,
 quote TEXT NOT NULL,quote_sha256 TEXT NOT NULL,assertion_kind TEXT NOT NULL CHECK(assertion_kind IN('reviewed_binding','reviewed_financial_relation')),
 CHECK(start_char>=0 AND end_char>start_char));
CREATE INDEX IF NOT EXISTS graph_edges_forward ON graph_relations(subject_id,predicate,status,version_id);
CREATE INDEX IF NOT EXISTS graph_edges_reverse ON graph_relations(object_id,predicate,status,version_id);
CREATE INDEX IF NOT EXISTS graph_edges_citation ON graph_relations(citation_id,status);
CREATE TRIGGER IF NOT EXISTS immutable_graph_provenance BEFORE UPDATE ON graph_provenance
 BEGIN SELECT RAISE(ABORT,'immutable_graph_provenance'); END;
CREATE TRIGGER IF NOT EXISTS immutable_graph_relation BEFORE UPDATE ON graph_relations
 WHEN NEW.id IS NOT OLD.id OR NEW.subject_id IS NOT OLD.subject_id OR NEW.object_id IS NOT OLD.object_id
 OR NEW.predicate IS NOT OLD.predicate OR NEW.version_id IS NOT OLD.version_id OR NEW.citation_id IS NOT OLD.citation_id
 OR NEW.valid_from IS NOT OLD.valid_from OR NEW.valid_to IS NOT OLD.valid_to OR NEW.reviewed_by IS NOT OLD.reviewed_by
 BEGIN SELECT RAISE(ABORT,'immutable_graph_relation'); END;
CREATE TRIGGER IF NOT EXISTS graph_quote_required BEFORE INSERT ON graph_provenance
 WHEN NOT EXISTS(SELECT 1 FROM chunks c JOIN document_versions v ON v.id=c.version_id JOIN graph_relations r ON r.id=NEW.relation_id
 WHERE c.id=NEW.citation_id AND c.version_id=NEW.version_id AND r.citation_id=NEW.citation_id AND r.version_id=NEW.version_id
 AND NEW.start_char>=c.start_char AND NEW.end_char<=c.end_char AND NEW.end_char-NEW.start_char=length(NEW.quote)
 AND substr(v.parsed_text,NEW.start_char+1,NEW.end_char-NEW.start_char)=NEW.quote)
 BEGIN SELECT RAISE(ABORT,'graph_quote_mismatch'); END;
'''


def entity_id(kind,ref):
    return 'ge-'+kind+'-'+sha(ref)[:32]


def entity(db,kind,ref,label,attributes=None):
    if kind not in KINDS:raise KnowledgeError('graph_entity_type_forbidden')
    gid=entity_id(kind,ref)
    db.execute('INSERT OR IGNORE INTO graph_entities VALUES(?,?,?,?,?,?)',
               (gid,kind,ref,label,packed(attributes or {}),'public'))
    return gid


def edge(db,subject,object_,predicate,version,chunk,reviewed_by, *, assertion_kind='reviewed_binding',
         start_char=None,end_char=None):
    if predicate not in PREDICATES:raise KnowledgeError('graph_predicate_forbidden')
    subject_kind=db.execute('SELECT kind FROM graph_entities WHERE id=?',(subject,)).fetchone()
    object_kind=db.execute('SELECT kind FROM graph_entities WHERE id=?',(object_,)).fetchone()
    if not subject_kind or not object_kind or subject_kind[0] not in PREDICATES[predicate][0] or object_kind[0] not in PREDICATES[predicate][1]:
        raise KnowledgeError('graph_relation_type_mismatch')
    start_char=chunk['start_char'] if start_char is None else start_char
    end_char=chunk['end_char'] if end_char is None else end_char
    if type(start_char) is not int or type(end_char) is not int or not chunk['start_char']<=start_char<end_char<=chunk['end_char']:
        raise KnowledgeError('quote_mismatch')
    quote=version['parsed_text'][start_char:end_char]
    eid='gr-'+sha(packed([subject,object_,predicate,version['id'],chunk['id']]))[:40]
    db.execute('INSERT OR IGNORE INTO graph_relations VALUES(?,?,?,?,?,?,?,?,?,?)',
               (eid,subject,object_,predicate,version['id'],chunk['id'],version['effective_from'],version['effective_to'],'verified',reviewed_by))
    db.execute('INSERT OR IGNORE INTO graph_provenance VALUES(?,?,?,?,?,?,?,?)',
               (eid,chunk['id'],version['id'],start_char,end_char,quote,sha(quote),assertion_kind))
    return eid


def materialize(db,version_id,reviewed_by):
    v=db.execute('SELECT v.*,d.source_id,d.source_url FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?',(version_id,)).fetchone()
    source=db.execute('SELECT publisher FROM sources WHERE id=?',(v['source_id'],)).fetchone()
    version=entity(db,'document_version',version_id,v['title'],{'published_on':v['published_on']})
    document=entity(db,'document',v['document_id'],v['title'],{'source_url':v['source_url']})
    publisher=entity(db,'source',v['source_id'],source['publisher'])
    for c in db.execute('SELECT * FROM chunks WHERE version_id=?',(version_id,)).fetchall():
        clause=entity(db,'clause',c['id'],c['paragraph'])
        edge(db,clause,version,'quoted_in',v,c,reviewed_by)
        edge(db,version,document,'version_of',v,c,reviewed_by)
        edge(db,document,publisher,'published_by',v,c,reviewed_by)
        product=None
        if c['product_id']:
            p=db.execute('SELECT name FROM products WHERE id=?',(c['product_id'],)).fetchone()
            product=entity(db,'product',c['product_id'],p['name'])
            edge(db,product,clause,'has_disclosure',v,c,reviewed_by)
        if c['share_class_id']:
            sc=db.execute('SELECT * FROM share_classes WHERE id=?',(c['share_class_id'],)).fetchone()
            share=entity(db,'share_class',c['share_class_id'],sc['code'],{'currency':sc['currency']})
            edge(db,product,share,'has_share_class',v,c,reviewed_by)
            edge(db,share,clause,'has_share_terms',v,c,reviewed_by)
        if c['channel_id']:
            ch=db.execute('SELECT name FROM channels WHERE id=?',(c['channel_id'],)).fetchone()
            channel=entity(db,'channel',c['channel_id'],ch['name'])
            edge(db,channel,clause,'channel_discloses',v,c,reviewed_by)
        for facet in json.loads(c['facets']):
            kind=RULE_KIND[facet]
            rule=entity(db,kind,c['id']+':'+facet,facet,{'facet':facet,'unstructured_quote_only':True})
            edge(db,clause,rule,'has_rule',v,c,reviewed_by)
        if v['effective_from'] or v['effective_to']:
            period=entity(db,'effective_period',version_id+':period',packed([v['effective_from'],v['effective_to']]))
            edge(db,clause,period,'effective_during',v,c,reviewed_by)


def _usable_edge(db,row,as_of):
    if row['status']!='verified' or (row['valid_from'] and row['valid_from']>as_of) or (row['valid_to'] and row['valid_to']<=as_of):return False
    if row['predicate'] not in PREDICATES:return False
    sk=db.execute('SELECT kind FROM graph_entities WHERE id=?',(row['subject_id'],)).fetchone()
    ok=db.execute('SELECT kind FROM graph_entities WHERE id=?',(row['object_id'],)).fetchone()
    if not sk or not ok or sk[0] not in PREDICATES[row['predicate']][0] or ok[0] not in PREDICATES[row['predicate']][1]:return False
    provenance=db.execute('''SELECT gp.*,v.parsed_text,c.start_char chunk_start,c.end_char chunk_end,c.version_id chunk_version
                FROM graph_provenance gp JOIN document_versions v ON v.id=gp.version_id JOIN chunks c ON c.id=gp.citation_id
                WHERE gp.relation_id=?''',(row['id'],)).fetchone()
    return bool(provenance and provenance['version_id']==row['version_id'] and provenance['citation_id']==row['citation_id']
                and provenance['chunk_version']==row['version_id']
                and provenance['chunk_start']<=provenance['start_char']<provenance['end_char']<=provenance['chunk_end']
                and provenance['parsed_text'][provenance['start_char']:provenance['end_char']]==provenance['quote']
                and sha(provenance['quote'])==provenance['quote_sha256'])


def paths(db,chunk,as_of, *, request=None,facets=None,max_depth=4):
    """固定上下文通路：每条边必须属于本引文版本，防止跨份额/渠道偷换。"""
    day(as_of);max_depth=min(4,max(2,max_depth))
    request=request or {};facets=facets or json.loads(chunk['facets'])
    clause=entity_id('clause',chunk['id'])
    edges=[dict(e) for e in db.execute('SELECT * FROM graph_relations WHERE citation_id=? AND version_id=?',
                                     (chunk['id'],chunk['version_id'])) if _usable_edge(db,e,as_of)]
    adjacency={}
    for e in edges:adjacency.setdefault(e['subject_id'],[]).append(e)
    def walk(start,target):
        queue=deque([(start,[],{start})]);visits=0
        while queue and visits<64:
            at,path,seen=queue.popleft();visits+=1
            if at==target:return path
            if len(path)>=max_depth:continue
            for e in adjacency.get(at,[]):
                if e['object_id'] not in seen:queue.append((e['object_id'],path+[e],seen|{e['object_id']}))
        return None
    traces=[]
    starts=[]
    for key,kind in (('product_id','product'),('share_class_id','share_class'),('channel_id','channel')):
        ref=request.get(key) or chunk[key]
        if ref is not None:starts.append((kind,ref))
    source_row=db.execute('''SELECT d.source_id FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.id=?''',(chunk['version_id'],)).fetchone()
    source=entity_id('source',source_row[0])
    provenance_path=walk(clause,source)
    if provenance_path is None or len(provenance_path)<2:return None
    traces.append({'purpose':'original_source','relations':provenance_path})
    for kind,ref in starts:
        path=walk(entity_id(kind,ref),clause)
        if path is None:return None
        for facet in facets:
            rule=entity_id(RULE_KIND[facet],chunk['id']+':'+facet)
            suffix=walk(clause,rule)
            if suffix is None:return None
            traces.append({'purpose':facet,'subject_type':kind,'subject_ref':ref,'relations':path+suffix})
    if not starts:
        for facet in facets:
            if walk(clause,entity_id(RULE_KIND[facet],chunk['id']+':'+facet)) is None:return None
    return traces[:8]


def reviewed_relation(provider, *, subject_kind,subject_ref,predicate,object_kind,object_ref,object_label,
                      citation_id,reviewed_by,start_char,end_char):
    """只允许明确审核的机构关系；名称共现不能自动进入本接口。"""
    if predicate not in ('issued_by','managed_by','distributed_by') or not isinstance(reviewed_by,str) or not reviewed_by.strip():
        raise KnowledgeError('graph_relation_review_required')
    with provider.db() as db:
        c=db.execute('SELECT * FROM chunks WHERE id=?',(citation_id,)).fetchone()
        if c is None:raise KnowledgeError('evidence_unavailable')
        v=db.execute('SELECT * FROM document_versions WHERE id=?',(c['version_id'],)).fetchone()
        if v['status']!='verified':raise KnowledgeError('unverified_relation_evidence')
        expected={'product':c['product_id'],'share_class':c['share_class_id']}
        if expected.get(subject_kind)!=subject_ref:raise KnowledgeError('subject_mismatch')
        if object_kind=='channel' and object_ref!=c['channel_id']:raise KnowledgeError('subject_mismatch')
        if type(start_char) is not int or type(end_char) is not int or not c['start_char']<=start_char<end_char<=c['end_char']:
            raise KnowledgeError('quote_mismatch')
        if not isinstance(object_label,str) or not object_label.strip() or object_label not in v['parsed_text'][start_char:end_char]:
            raise KnowledgeError('relation_not_in_quote')
        subject=entity_id(subject_kind,subject_ref)
        object_=entity(db,object_kind,object_ref,object_label)
        return edge(db,subject,object_,predicate,v,c,reviewed_by,assertion_kind='reviewed_financial_relation',
                    start_char=start_char,end_char=end_char)


def related_products(provider,product_id,release_id,as_of, *, limit=8):
    """两跳同行机构查询；两个产品的关系都必须有各自已发布原文。"""
    day(as_of)
    if type(limit) is not int or not 1<=limit<=8:raise KnowledgeError('invalid_limit')
    output=[];seen=set()
    with provider.db() as db:
        subject=entity_id('product',product_id)
        origins=db.execute("SELECT * FROM graph_relations WHERE subject_id=? AND predicate='issued_by' AND status='verified' LIMIT 16",(subject,)).fetchall()
        for origin in origins:
            if not _usable_edge(db,origin,as_of):continue
            if not provider.resolve_public_evidence(origin['citation_id'],release_id,as_of):continue
            peers=db.execute("SELECT * FROM graph_relations WHERE object_id=? AND predicate='issued_by' AND status='verified' LIMIT 64",(origin['object_id'],)).fetchall()
            for peer in peers:
                if peer['subject_id']==subject or not _usable_edge(db,peer,as_of):continue
                if not provider.resolve_public_evidence(peer['citation_id'],release_id,as_of):continue
                entity_row=db.execute('SELECT public_ref FROM graph_entities WHERE id=?',(peer['subject_id'],)).fetchone()
                if entity_row is None or entity_row[0] in seen:continue
                seen.add(entity_row[0])
                issuer=db.execute('SELECT label FROM graph_entities WHERE id=?',(origin['object_id'],)).fetchone()[0]
                output.append({'product_id':entity_row[0],'institution':issuer,
                               'path':[dict(origin),{**dict(peer),'traversal':'reverse'}],
                               'relation_interpretation':'same_disclosed_issuer_only_not_quality_or_eligibility'})
                if len(output)>=limit:return output
    return output
