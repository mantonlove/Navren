"""AA单笔消费安全摘要与私有图片；不使用OCR、外部存储或模型。"""
import base64
from contextlib import closing
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import uuid
import warnings

SCHEMA='''
CREATE TABLE IF NOT EXISTS navren_aa_attachments(
 id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 mime_type TEXT NOT NULL CHECK(mime_type='image/png'),content BLOB NOT NULL,
 byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 2097152),
 width INTEGER NOT NULL CHECK(width>0),height INTEGER NOT NULL CHECK(height>0),
 sha256 TEXT NOT NULL,created_at REAL NOT NULL,CHECK(width*height<=4000000));
CREATE TABLE IF NOT EXISTS navren_aa_draft_attachments(
 operation_id TEXT NOT NULL REFERENCES operations(id),attachment_id TEXT NOT NULL REFERENCES navren_aa_attachments(id) ON DELETE CASCADE,
 PRIMARY KEY(operation_id,attachment_id));
CREATE TABLE IF NOT EXISTS navren_aa_bill_attachments(
 bill_id TEXT NOT NULL REFERENCES navren_aa_bills(id),attachment_id TEXT NOT NULL UNIQUE REFERENCES navren_aa_attachments(id),
 snapshot TEXT NOT NULL CHECK(json_valid(snapshot)),
 PRIMARY KEY(bill_id,attachment_id));
CREATE TABLE IF NOT EXISTS navren_aa_bill_consumptions(
 bill_id TEXT PRIMARY KEY REFERENCES navren_aa_bills(id),workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 reference TEXT NOT NULL,quota_key TEXT NOT NULL,fingerprint TEXT NOT NULL,snapshot TEXT NOT NULL CHECK(json_valid(snapshot)));
CREATE INDEX IF NOT EXISTS navren_aa_consumption_quota ON navren_aa_bill_consumptions(workspace_id,quota_key);
'''
PUBLIC_FIELDS=('id','source','merchant','purpose','occurred_at','paid_minor','refunded_minor','eligible_minor','record_type','environment')
EVIDENCE_FIELDS={'source_consumption_id','attachment_ids','consumption_snapshot','attachment_snapshots','consumption_fingerprint'}


def initialize(db):db.executescript(SCHEMA)


def error(code,message,status=422):
    from .service import BankError
    raise BankError(code,message,status)


def safe_text(value,maximum=100):
    if not isinstance(value,str) or any(ord(c)<32 or ord(c)==127 for c in value):
        error('AA_CONSUMPTION_INVALID','消费摘要格式无法核对。')
    return value[:maximum]


def reference(value):
    from .aa_payments import text
    text(value,'source_consumption_id',180)
    if not value.startswith(('runtime:','history:')) or not value.split(':',1)[1]:
        error('AA_CONSUMPTION_INVALID','请选择服务端返回的消费记录标识。')
    return value.split(':',1)


def history_source(bank,db,workspace):
    from .aa_payments import identity
    user=identity(db,workspace=workspace)
    demos=getattr(bank,'demo_accounts',None)
    gateway=getattr(demos,'gateway',None)
    if getattr(gateway,'registered_mode',False):
        provider=getattr(gateway,'experience_provider',None)
        if provider is None:error('AA_CONSUMPTION_INVALID','当前注册实例未配置受控历史消费来源。')
        # 注册实例固定在各自开账时的封存来源，不能用启动参数的当前模板代替。
        service=provider.materializer.service_for(workspace)
    else:
        service=getattr(gateway,'financial_data',None)
    if not service:error('AA_CONSUMPTION_INVALID','当前用户未配置受控历史消费来源。')
    binding=db.execute('SELECT * FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone()
    if not binding or binding['subject_id']!=user['user_id'] or binding['dataset_version']!=service.dataset_version or Path(binding['source_path']).resolve()!=service.database_path:
        error('AA_CONSUMPTION_INVALID','历史消费与当前用户的受控来源绑定不一致。')
    # 只读服务路径来自已登记迁移，绝不读取客户端路径。
    try:
        stat=service.database_path.stat()
        if service._signature()!=service._stat:
            error('AA_CONSUMPTION_CHANGED','历史消费来源已更新，请重新装配并核对。',409)
    except OSError:error('AA_CONSUMPTION_CHANGED','历史消费来源当前无法核对。',409)
    signature=(str(service.database_path),stat.st_size,stat.st_mtime_ns,stat.st_ino,binding['source_sha256'])
    cache=getattr(bank,'_aa_history_verified',None)
    if cache!=signature:
        if hashlib.sha256(service.database_path.read_bytes()).hexdigest()!=binding['source_sha256']:
            error('AA_CONSUMPTION_INVALID','历史消费来源散列发生变化。')
        bank._aa_history_verified=signature
    return service,user['user_id']


def resolve(bank,db,workspace,ref,*,history_data=None):
    source,ident=reference(ref)
    if source=='runtime':
        row=db.execute('SELECT * FROM transactions WHERE workspace_id=? AND id=?',(workspace,ident)).fetchone()
        if not row or row['kind']!='expense' or row['delta_minor']>=0 or row['amount_minor']!=-row['delta_minor']:
            error('AA_CONSUMPTION_INVALID','只有本人已记账负向消费可关联AA。')
        # Runtime没有退款关联字段；不按商家或相似金额猜退款归属。
        ambiguous=db.execute("SELECT 1 FROM transactions WHERE workspace_id=? AND account_id=? AND kind='refund' AND delta_minor>0 AND occurred_at>=? LIMIT 1",(workspace,row['account_id'],row['occurred_at'])).fetchone()
        paid=row['amount_minor'];refunded=0;eligible=0 if ambiguous else paid;quota='runtime:'+ident
        merchant=row['merchant'];purpose=row['category'];occurred=row['occurred_at'];revision='runtime'
    else:
        service,subject=history_source(bank,db,workspace)
        revision=service.dataset_version+':'+db.execute('SELECT source_sha256 FROM connected_asset_migrations WHERE workspace_id=?',(workspace,)).fetchone()[0]
        if history_data is not None:
            row=history_data[0].get(ident);refunds=history_data[1].get(('event',row['economic_event_id']) if row and row['economic_event_id'] else ('transaction',ident),0)
        else:
            with closing(service._connect()) as history:
                history.execute('BEGIN')
                row=history.execute('SELECT * FROM transactions WHERE subject_id=? AND transaction_id=?',(subject,ident)).fetchone()
                event=row['economic_event_id'] if row else ''
                refunds=history.execute("SELECT coalesce(sum(r.signed_minor),0) FROM transactions r JOIN transactions original ON original.subject_id=r.subject_id AND original.transaction_id=r.original_transaction_id WHERE r.subject_id=? AND r.status='booked' AND r.kind='refund' AND r.signed_minor>0 AND (original.transaction_id=? OR (?<>'' AND original.economic_event_id=?))",(subject,ident,event or '',event or '')).fetchone()[0]
        if not row or row['status']!='booked' or row['kind']!='expense' or row['signed_minor']>=0 or row['amount_minor']!=-row['signed_minor'] or row['currency']!='CNY':
            error('AA_CONSUMPTION_INVALID','只有本人已入账人民币消费可关联AA。')
        event=row['economic_event_id'];quota='history-economic:'+event if event else 'history:'+ident
        paid=row['amount_minor'];refunded=min(paid,refunds);eligible=max(0,paid-refunded)
        merchant=row['counterparty'];purpose=row['category'];occurred=row['occurred_at']
    snapshot=consumption_snapshot(ref,source,merchant,purpose,occurred,paid,refunded,eligible)
    fingerprint=hashlib.sha256(json.dumps({'snapshot':snapshot,'source_revision':revision},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return snapshot,quota,fingerprint


def consumption_snapshot(ref,source,merchant,purpose,occurred,paid,refunded,eligible):
    reference(ref)
    try:date.fromisoformat(occurred[:10])
    except (ValueError,TypeError):error('AA_CONSUMPTION_INVALID','消费时间无法核对。')
    return {'id':ref,'source':source,'merchant':safe_text(merchant),'purpose':safe_text(purpose),
        'occurred_at':safe_text(occurred,40),'paid_minor':paid,'refunded_minor':refunded,'eligible_minor':eligible,
        'record_type':'consumption_record','environment':'simulation'}


def allocated(db,workspace,quota,*,runtime_id=None,exclude_bill=None):
    # 非取消账单占总额，取消后已收款继续占用；旧runtime关联同样计入。
    rows=db.execute('''SELECT b.id,b.total_minor,b.status,
      coalesce((SELECT sum(s.paid_minor) FROM navren_aa_shares s WHERE s.bill_id=b.id),0) AS collected
      FROM navren_aa_bills b LEFT JOIN navren_aa_bill_consumptions e ON e.bill_id=b.id
      WHERE b.creator_workspace=? AND (e.quota_key=? OR (e.bill_id IS NULL AND b.source_transaction_id=?))''',
      (workspace,quota,runtime_id)).fetchall()
    return sum(r['collected'] if r['status']=='cancelled' else r['total_minor'] for r in rows if r['id']!=exclude_bill)


def consumptions(bank,workspace,*,q='',from_day=None,to_day=None,limit=20,offset=0):
    from .service import integer
    from .aa_payments import identity
    integer(limit,'limit',1,50);integer(offset,'offset',0,10000)
    if not isinstance(q,str) or len(q)>80 or any(ord(c)<32 for c in q):error('INVALID_QUERY','消费搜索格式无效。',400)
    for day in (from_day,to_day):
        if day is not None:
            try:
                if date.fromisoformat(day).isoformat()!=day:raise ValueError()
            except (ValueError,TypeError):error('INVALID_QUERY','消费日期必须为YYYY-MM-DD。',400)
    if from_day and to_day and from_day>to_day:error('INVALID_QUERY','开始日期不能晚于结束日期。',400)
    with closing(bank.connect()) as db:
        db.execute('BEGIN');identity(db,workspace=workspace)
        runtime=list(db.execute("SELECT * FROM transactions WHERE workspace_id=? AND kind='expense' AND delta_minor<0 AND amount_minor=-delta_minor ORDER BY occurred_at DESC,id",(workspace,)))
        # 同账户后续退款仍保守阻止分摊；一次聚合代替逐笔EXISTS。
        runtime_refunds={r['account_id']:r['latest'] for r in db.execute("SELECT account_id,max(occurred_at) AS latest FROM transactions WHERE workspace_id=? AND kind='refund' AND delta_minor>0 GROUP BY account_id",(workspace,))}
        records=[]
        for row in runtime:
            ref='runtime:'+row['id'];paid=row['amount_minor']
            ambiguous=runtime_refunds.get(row['account_id'],'')>=row['occurred_at']
            records.append((consumption_snapshot(ref,'runtime',row['merchant'],row['category'],row['occurred_at'],paid,0,0 if ambiguous else paid),ref))
        if getattr(getattr(getattr(bank,'demo_accounts',None),'gateway',None),'financial_data',None):
            # 绑定、版本及原件散列仅在本次只读快照中核验一次。
            service,subject=history_source(bank,db,workspace)
            with closing(service._connect()) as history:
                history.execute('BEGIN')
                raw={r['transaction_id']:dict(r) for r in history.execute("SELECT * FROM transactions WHERE subject_id=? AND kind='expense' AND status='booked' AND signed_minor<0 AND currency='CNY' AND amount_minor=-signed_minor ORDER BY occurred_at DESC,transaction_id",(subject,))}
                refunds={}
                for r in history.execute("SELECT original.transaction_id,original.economic_event_id,sum(r.signed_minor) AS refunded FROM transactions r JOIN transactions original ON original.subject_id=r.subject_id AND original.transaction_id=r.original_transaction_id WHERE r.subject_id=? AND r.status='booked' AND r.kind='refund' AND r.signed_minor>0 GROUP BY original.transaction_id,original.economic_event_id",(subject,)):
                    key=('event',r['economic_event_id']) if r['economic_event_id'] else ('transaction',r['transaction_id'])
                    refunds[key]=refunds.get(key,0)+r['refunded']
                for ident,row in raw.items():
                    event=row['economic_event_id'];quota='history-economic:'+event if event else 'history:'+ident
                    refunded=min(row['amount_minor'],refunds.get(('event',event) if event else ('transaction',ident),0))
                    records.append((consumption_snapshot('history:'+ident,'history',row['counterparty'],row['category'],row['occurred_at'],row['amount_minor'],refunded,max(0,row['amount_minor']-refunded)),quota))
        # 与allocated相同的正式账单占用规则；旧runtime外键关联一并计入。
        allocations={}
        for row in db.execute('''SELECT b.id,b.total_minor,b.status,b.source_transaction_id,e.quota_key,
          coalesce(sum(s.paid_minor),0) AS collected
          FROM navren_aa_bills b LEFT JOIN navren_aa_bill_consumptions e ON e.bill_id=b.id
          LEFT JOIN navren_aa_shares s ON s.bill_id=b.id WHERE b.creator_workspace=? GROUP BY b.id''',(workspace,)):
            quota=row['quota_key']
            if quota is None and row['source_transaction_id'] is not None:quota='runtime:'+row['source_transaction_id']
            if quota is not None:allocations[quota]=allocations.get(quota,0)+(row['collected'] if row['status']=='cancelled' else row['total_minor'])
        items=[];seen=set()
        for snap,quota in records:
            if quota in seen:continue
            seen.add(quota)
            day=snap['occurred_at'][:10]
            if from_day and day<from_day or to_day and day>to_day or q.casefold() not in (snap['merchant']+' '+snap['purpose']).casefold():continue
            used=allocations.get(quota,0)
            items.append({**snap,'allocated_minor':used,'available_minor':max(0,snap['eligible_minor']-used)})
        items.sort(key=lambda r:(r['occurred_at'],r['id']),reverse=True)
        return {'consumptions':items[offset:offset+limit],'total':len(items),'limit':limit,'offset':offset,
                'has_more':offset+limit<len(items),'environment':'simulation'}


def descriptor(row):
    return {**{k:row[k] for k in ('id','mime_type','byte_size','width','height','sha256')},
        'provenance':'user_supplied_unverified','environment':'simulation'}


def attachment_row(db,workspace,ident):
    from .aa_payments import text
    text(ident,'attachment_id')
    row=db.execute('SELECT * FROM navren_aa_attachments WHERE workspace_id=? AND id=?',(workspace,ident)).fetchone()
    if not row:error('AA_ATTACHMENT_NOT_FOUND','凭证不存在或无权访问。',404)
    if row['byte_size']!=len(row['content']) or hashlib.sha256(row['content']).hexdigest()!=row['sha256']:
        error('AA_ATTACHMENT_INVALID','已保存图片凭证摘要不一致。')
    return row


def upload(bank,workspace,payload):
    from .service import strict_fields
    from .aa_payments import identity
    strict_fields(payload,{'mime_type','data_base64'},{'mime_type','data_base64'})
    kind=payload['mime_type'];data=payload['data_base64']
    if kind not in ('image/jpeg','image/png','image/webp') or not isinstance(data,str) or not 1<=len(data)<=699052:
        error('AA_ATTACHMENT_INVALID','仅支持512KiB以内JPEG/PNG/WebP图片。')
    try:raw=base64.b64decode(data,validate=True)
    except (ValueError,TypeError):error('AA_ATTACHMENT_INVALID','图片base64格式无效。')
    if not 1<=len(raw)<=524288:error('AA_ATTACHMENT_INVALID','单张上传超过512KiB。')
    with closing(bank.connect()) as db:identity(db,workspace=workspace)
    try:
        from PIL import Image,ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as img:
                if img.format!={'image/jpeg':'JPEG','image/png':'PNG','image/webp':'WEBP'}[kind] or getattr(img,'n_frames',1)!=1 or img.width*img.height>4000000:
                    error('AA_ATTACHMENT_INVALID','图片类型不符、包含动画或像素超过400万。')
                img.load();pixels=ImageOps.exif_transpose(img).convert('RGBA' if 'A' in img.getbands() or 'transparency' in img.info else 'RGB')
                clean=Image.frombytes(pixels.mode,pixels.size,pixels.tobytes());target=io.BytesIO();clean.save(target,format='PNG')
                content=target.getvalue();width,height=clean.size
    except ImportError:error('AA_ATTACHMENT_UNAVAILABLE','服务端未安装Pillow图片解码依赖。',503)
    except Exception as exc:
        from .service import BankError
        if isinstance(exc,BankError):raise
        error('AA_ATTACHMENT_INVALID','图片无法真实解码。')
    if len(content)>2097152:error('AA_ATTACHMENT_INVALID','处理后图片过大，请先缩小图片。')
    with bank.transaction() as db:
        identity(db,workspace=workspace)
        count=db.execute('SELECT count(*) FROM navren_aa_attachments a WHERE workspace_id=? AND NOT EXISTS(SELECT 1 FROM navren_aa_bill_attachments b WHERE b.attachment_id=a.id)',(workspace,)).fetchone()[0]
        if count>=12:error('AA_ATTACHMENT_LIMIT','请先移除未使用的图片凭证。')
        ident=uuid.uuid4().hex
        db.execute('INSERT INTO navren_aa_attachments VALUES(?,?,?,?,?,?,?,?,?)',(ident,workspace,'image/png',content,len(content),width,height,hashlib.sha256(content).hexdigest(),bank.clock()))
        return {'attachment':descriptor(attachment_row(db,workspace,ident)),'environment':'simulation'}


def read_attachment(bank,workspace,ident):
    from .aa_payments import identity,text
    text(ident,'attachment_id')
    with closing(bank.connect()) as db:
        db.execute('BEGIN');me=identity(db,workspace=workspace)
        guard=getattr(bank,'request_identity_guard',None)
        if guard:guard(db)
        row=db.execute('SELECT * FROM navren_aa_attachments WHERE id=?',(ident,)).fetchone()
        member=db.execute('SELECT 1 FROM navren_aa_bill_attachments a JOIN navren_aa_shares s ON s.bill_id=a.bill_id WHERE a.attachment_id=? AND s.workspace_id=? AND s.user_id=?',(ident,workspace,me['user_id'])).fetchone()
        if not row or row['workspace_id']!=workspace and not member:error('AA_ATTACHMENT_NOT_FOUND','凭证不存在或无权访问。',404)
        verified=attachment_row(db,row['workspace_id'],ident)
        frozen=db.execute('SELECT snapshot FROM navren_aa_bill_attachments WHERE attachment_id=?',(ident,)).fetchone()
        if frozen and json.loads(frozen['snapshot'])!=descriptor(verified):error('AA_ATTACHMENT_INVALID','图片与账单冻结摘要不一致。')
        return {'attachment':descriptor(verified),'data_base64':base64.b64encode(verified['content']).decode(),'environment':'simulation'}


def delete_attachment(bank,workspace,ident):
    from .aa_payments import identity
    with bank.transaction() as db:
        identity(db,workspace=workspace);attachment_row(db,workspace,ident)
        if bound(db,ident,bank.clock()):error('AA_ATTACHMENT_BOUND','图片已绑定账单或待确认草案，不能删除。',409)
        db.execute('DELETE FROM navren_aa_attachments WHERE workspace_id=? AND id=?',(workspace,ident))
        return {'deleted':True,'id':ident,'environment':'simulation'}


def bound(db,ident,now,operation=None):
    return (db.execute('SELECT 1 FROM navren_aa_bill_attachments WHERE attachment_id=?',(ident,)).fetchone() or
        db.execute("SELECT 1 FROM navren_aa_draft_attachments d JOIN operations o ON o.id=d.operation_id WHERE d.attachment_id=? AND o.state='pending_confirmation' AND o.expires_at>? AND o.id<>?",(ident,now,operation or '')).fetchone())


def canonical_evidence(bank,db,workspace,form,saved=None):
    # 同键重放使用既有服务端快照；客户端内部快照始终被覆盖。
    requested=form.get('source_consumption_id');legacy=form.get('source_transaction_id');legacy_only=bool(legacy) and not requested
    if legacy:
        from .aa_payments import text
        text(legacy,'source_transaction_id')
        runtime='runtime:'+legacy
        if requested and requested!=runtime:error('AA_CONSUMPTION_INVALID','两个消费关联标识不一致。')
        requested=runtime
    if requested:
        source,ident=reference(requested)
        form={**form,'source_transaction_id':ident if source=='runtime' else None}
    ids=form.get('attachment_ids',[])
    if not isinstance(ids,list) or len(ids)>3 or any(not isinstance(v,str) for v in ids) or len(set(ids))!=len(ids):
        error('AA_ATTACHMENT_LIMIT','单账单最多3张且不得重复。')
    if saved is not None:
        # 旧草案形状保持兼容；新增证据须新幂等键重新核对。
        if 'attachment_ids' not in saved and not any(k in form for k in ('attachment_ids','source_consumption_id')):return form
        return {**form,'source_consumption_id':requested,'attachment_ids':ids,
            'consumption_snapshot':saved.get('consumption_snapshot'),'consumption_fingerprint':saved.get('consumption_fingerprint'),
            'attachment_snapshots':saved.get('attachment_snapshots',[])}
    snapshot=fingerprint=None
    if requested:
        from .service import BankError
        try:snapshot,_,fingerprint=resolve(bank,db,workspace,requested)
        except BankError as exc:
            if legacy_only and exc.code=='AA_CONSUMPTION_INVALID':error('AA_SOURCE_INVALID',exc.message,exc.status)
            raise
    attachments=[descriptor(attachment_row(db,workspace,i)) for i in ids]
    return {**form,'source_consumption_id':requested,'attachment_ids':ids,
        'consumption_snapshot':snapshot,'consumption_fingerprint':fingerprint,'attachment_snapshots':attachments}


def validate_create(bank,db,workspace,form,total,operation=None):
    ref=form.get('source_consumption_id') or ('runtime:'+form['source_transaction_id'] if form.get('source_transaction_id') else None)
    if ref:
        snapshot,quota,fingerprint=resolve(bank,db,workspace,ref)
        if form.get('consumption_snapshot') is not None and (form['consumption_snapshot']!=snapshot or form.get('consumption_fingerprint')!=fingerprint):
            error('AA_CONSUMPTION_CHANGED','原消费内容或退款状态变化，请重新核对。',409)
        used=allocated(db,workspace,quota,runtime_id=ref.split(':',1)[1] if snapshot['source']=='runtime' else None)
        if total+used>snapshot['eligible_minor']:error('AA_CONSUMPTION_LIMIT','累计AA账单和已收款超过本笔消费剩余可分摊金额。')
    ids=form.get('attachment_ids',[])
    current=[descriptor(attachment_row(db,workspace,i)) for i in ids]
    if current!=form.get('attachment_snapshots',[]):error('AA_ATTACHMENT_INVALID','图片凭证与已核对摘要不一致。')
    if any(bound(db,i,bank.clock(),operation) for i in ids):error('AA_ATTACHMENT_BOUND','图片已绑定其他账单或待确认草案。',409)


def bind_draft(bank,db,workspace,operation,form):
    for ident in form.get('attachment_ids',[]):
        attachment_row(db,workspace,ident)
        if bound(db,ident,bank.clock(),operation):error('AA_ATTACHMENT_BOUND','图片已绑定其他账单或待确认草案。',409)
        db.execute('INSERT OR IGNORE INTO navren_aa_draft_attachments VALUES(?,?)',(operation,ident))


def save_bill(bank,db,workspace,bill_id,form):
    ref=form.get('source_consumption_id')
    if ref:
        snapshot,quota,fingerprint=resolve(bank,db,workspace,ref)
        if snapshot!=form['consumption_snapshot'] or fingerprint!=form['consumption_fingerprint']:
            error('AA_CONSUMPTION_CHANGED','原消费在账单提交前变化，请重新核对。',409)
        db.execute('INSERT INTO navren_aa_bill_consumptions VALUES(?,?,?,?,?,?)',
            (bill_id,workspace,ref,quota,fingerprint,json.dumps(form['consumption_snapshot'],ensure_ascii=False)))
    for ident in form.get('attachment_ids',[]):
        row=attachment_row(db,workspace,ident)
        db.execute('INSERT INTO navren_aa_bill_attachments VALUES(?,?,?)',(bill_id,ident,json.dumps(descriptor(row),ensure_ascii=False)))


def bill_evidence(db,bill_id):
    row=db.execute('SELECT snapshot FROM navren_aa_bill_consumptions WHERE bill_id=?',(bill_id,)).fetchone()
    attachments=[json.loads(r['snapshot']) for r in db.execute('SELECT b.snapshot FROM navren_aa_bill_attachments b JOIN navren_aa_attachments a ON a.id=b.attachment_id WHERE b.bill_id=? ORDER BY a.created_at,a.id',(bill_id,))]
    return {'consumption':json.loads(row['snapshot']) if row else None,'attachments':attachments}


def validate_payment(bank,db,bill):
    row=db.execute('SELECT * FROM navren_aa_bill_consumptions WHERE bill_id=?',(bill['id'],)).fetchone()
    if row:
        snapshot,quota,fingerprint=resolve(bank,db,bill['creator_workspace'],row['reference'])
        if fingerprint!=row['fingerprint'] or snapshot!=json.loads(row['snapshot']):
            error('AA_CONSUMPTION_CHANGED','原消费或来源版本已变化，尚未执行本份额支付。',409)
        used=allocated(db,bill['creator_workspace'],quota,runtime_id=row['reference'].split(':',1)[1] if snapshot['source']=='runtime' else None)
        if used>snapshot['eligible_minor']:error('AA_CONSUMPTION_LIMIT','原消费退款或剩余可分摊额度不足，尚未执行本份额支付。')
    for item in bill_evidence(db,bill['id'])['attachments']:
        if descriptor(attachment_row(db,bill['creator_workspace'],item['id']))!=item:error('AA_ATTACHMENT_INVALID','图片与账单冻结摘要不一致。')
