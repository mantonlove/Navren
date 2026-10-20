"""完整Agent原审核回复的持久提供端；批准仅由可信Agent终结回查提供。"""
from copy import deepcopy
from contextlib import contextmanager,closing
import hashlib
import json
from pathlib import Path
import re
import threading

from financial_metrics import fingerprint
from simulated_bank import BankError
from user_memory import privacy_revision

CONTRACT='navren.approved-response/1'
COMMIT='navren.response-publication/1'
CODE=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DISPLAY=('bank_response_id','request_id','message_id','reply','presentation','metric_batch','wire_ref','message_state',
         'presentation_review','follow_up_questions','agent_review')

def fail(code='APPROVED_RESPONSE_INVALID',status=409):
    raise BankError(code,'原审核回复、完成回执或当前身份无效，已停止显示。',status)

class ApprovedResponseStore:
    def __init__(self,g,agent_final_validator=None):
        if agent_final_validator is not None and not callable(agent_final_validator):raise TypeError('trusted_agent_validator_required')
        self.g,self.p,self.store=g,g.presentations,g.personal_data
        self.validator=agent_final_validator
        self._pending={};self._lock=threading.RLock()

    def _actor(self,w,db):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=CODE:fail('APPROVED_RESPONSE_RESTART_REQUIRED',503)
        self.p._session_binding(w,db,required=True)
        demo=self.g.demo_accounts._record(w) if self.g.demo_accounts.is_demo(w) else None
        return {'workspace_id':w,'demo_user_id':demo['user_id'] if demo else None}

    def _seal(self,contract,body):
        return {'contract':contract,'sealed_original':self.store.protect(json.dumps(
            {'code':CODE,'body':body,'body_hash':fingerprint(body)},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False))}

    def _open(self,obj,contract):
        if type(obj) is not dict or set(obj)!={'contract','sealed_original'} or obj['contract']!=contract:
            fail('APPROVED_RESPONSE_TAMPERED')
        seal=obj['sealed_original']
        if type(seal) is not str or not seal.startswith('pd1:'):fail('APPROVED_RESPONSE_TAMPERED')
        try:value=json.loads(self.store._decode(seal))
        except (BankError,ValueError,TypeError):fail('APPROVED_RESPONSE_TAMPERED')
        if (type(value) is not dict or set(value)!={'code','body','body_hash'} or value['code']!=CODE
            or fingerprint(value['body'])!=value['body_hash']):fail('APPROVED_RESPONSE_TAMPERED')
        return value['body']

    def prepare(self,w,response,*,bank_db,language):
        """只由原server before_send调用；没有可信Agent回查时关闭批准提供端。"""
        if self.validator is None:fail('AGENT_FINAL_VALIDATOR_REQUIRED',503)
        if type(response) is not dict or language not in {'zh','en'}:fail()
        rid=response.get('bank_response_id')
        if type(rid) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',rid):fail()
        with self.p._publication(bank_db) as db:
            actor=self._actor(w,db)
            canonical=deepcopy(response)
            proof=self.validator(w,deepcopy(canonical),bank_db=db,language=language)
            required={'contract','request_id','message_id','workspace_id','language','approved_response_hash',
                      'approved_content','approved_projection_hash','approved_review_projection','reviewer_model','review_id','verdict',
                      'body_amount_occurrence_count','total_amount_occurrence_count','original_provider_receipt'}
            if type(proof) is not dict or set(proof)!=required or proof['contract']!='navren.agent-final-proof/1':fail('AGENT_FINAL_PROOF_INVALID')
            if (proof['workspace_id']!=w or proof['request_id']!=rid or proof['message_id']!=rid or proof['language']!=language
                or proof['approved_response_hash']!=fingerprint(canonical) or proof['approved_content']!=canonical.get('reply')
                or type(proof['approved_projection_hash']) is not str or re.fullmatch(r'[a-f0-9]{64}',proof['approved_projection_hash']) is None
                or type(proof['approved_review_projection']) is not dict
                or fingerprint(proof['approved_review_projection'])!=proof['approved_projection_hash']
                or type(proof['reviewer_model']) is not str or not proof['reviewer_model']
                or type(proof['review_id']) is not str or not proof['review_id']
                or type(proof['verdict']) is not dict or proof['verdict'].get('verdict')!='approve'
                or type(canonical.get('agent_review')) is not dict
                or canonical['agent_review'].get('status')!='approved'
                or proof['reviewer_model']!=canonical['agent_review'].get('model_id')
                or proof['review_id']!=canonical['agent_review'].get('review_id')
                or canonical.get('isFallback')):fail('AGENT_FINAL_PROOF_INVALID')
            body_count,total=proof['body_amount_occurrence_count'],proof['total_amount_occurrence_count']
            if type(body_count) is not int or type(total) is not int or not 0<=body_count<=total<=24:fail('AGENT_FINAL_AMOUNT_LIMIT')
            pres,batch=canonical.get('presentation'),canonical.get('metric_batch')
            if (pres is None)!=(batch is None):fail('APPROVED_RESPONSE_ROOT_WIRE')
            if pres is not None:
                if pres.get('request_id')!=rid or pres.get('message_id')!=rid or pres.get('workspace_binding')!=w:fail('APPROVED_RESPONSE_BINDING')
                receipt=proof['original_provider_receipt']
                checked=self.g.presentation_provider.verify(w,receipt,trusted_current_request=rid,trusted_current_message=rid,
                                                           language=language,bank_db=db)
                complete=receipt['projection']['complete_review']
                if (complete['presentation']!=pres or complete['text']!=canonical['reply']
                    or checked['bodyAmountOccurrenceCount']!=body_count
                    or complete['visible_amount_occurrence_count']!=total
                    or not self._contains(proof['approved_review_projection'],receipt)):fail('APPROVED_RESPONSE_REVIEW_MISMATCH')
                self.g.financial_metrics.validate(w,batch,db)
                state=canonical.get('message_state')
                if state is not None and (state.get('language')!=language or state.get('message_id')!=rid):fail('APPROVED_RESPONSE_BINDING')
            elif proof['original_provider_receipt'] is not None:fail('AGENT_FINAL_PROOF_INVALID')
            if (canonical.get('workspace_id')!=w or canonical.get('privacy_revision')!=privacy_revision(db,w)
                or canonical.get('personal_revision')!=self.store.revision(w)):fail('APPROVED_RESPONSE_BINDING')
            packet={'contract':CONTRACT,'response_id':rid,'actor':actor,'language':language,'original_response':canonical,
                    'original_response_hash':fingerprint(canonical),'agent_final_proof':deepcopy(proof),
                    'source_versions':self.p.source_versions(w,db),'personal_revision':self.store.revision(w),
                    'privacy_revision':privacy_revision(db,w),'prepared_at':self.g.bank.clock(),
                    'server_state':'prepared_under_original_publication_guard','execution_authority':False}
            envelope=self._seal(CONTRACT,packet)
            with self._lock:self._pending[(w,rid)]={'packet':packet,'envelope':envelope,'publication':None}
            return deepcopy(envelope)

    def _open_historical_codec(self,obj,contract,*,issuer_code,codec):
        """只供有pd1逐轮发行证明的只读reader；不提供当前批准、执行或旧包补签。"""
        if (codec!='navren.approved-original-seal/1'
            or contract not in (CONTRACT,COMMIT,'navren.local-rule-response/1')
            or type(issuer_code) is not str or re.fullmatch(r'[a-f0-9]{64}',issuer_code) is None
            or type(obj) is not dict or set(obj)!={'contract','sealed_original'} or obj['contract']!=contract):
            fail('HISTORICAL_ORIGINAL_CODEC_INVALID')
        seal=obj['sealed_original']
        if type(seal) is not str or not seal.startswith('pd1:'):fail('HISTORICAL_ORIGINAL_CODEC_INVALID')
        try:value=json.loads(self.store._decode(seal))
        except (BankError,ValueError,TypeError):fail('HISTORICAL_ORIGINAL_CODEC_INVALID')
        if (type(value) is not dict or set(value)!={'code','body','body_hash'}
            or value['code']!=issuer_code or type(value['body']) is not dict
            or fingerprint(value['body'])!=value['body_hash'] or value['body'].get('contract')!=contract):
            fail('HISTORICAL_ORIGINAL_CODEC_INVALID')
        body=value['body']
        if contract==COMMIT:
            required={'contract','response_id','original_response_hash','prepared_envelope_hash','published_at',
                      'publication_state','client_acknowledged','bank_binding_committed_before_publication',
                      'original_guarded_writer_committed'}
        else:
            required={'contract','response_id','actor','language','original_response','original_response_hash',
                      'source_versions','personal_revision','privacy_revision','prepared_at','server_state','execution_authority'}
            required.update({'agent_final_proof'} if contract==CONTRACT else {'local_rule_id','local_rule_code'})
        if set(body)!=required:fail('HISTORICAL_ORIGINAL_SCHEMA_UNSUPPORTED')
        return body

    def prepare_local_rule(self,w,response,*,bank_db):
        """原固定规则校验后进入同一密文/CAS发布链，不生成模型审核批准。"""
        from agent_runtime import social_reply
        if type(response) is not dict:fail('LOCAL_RULE_RESPONSE_INVALID')
        canonical=deepcopy(response)
        try:social_reply.validate_social_reply_result(canonical)
        except (ValueError,TypeError):fail('LOCAL_RULE_RESPONSE_INVALID')
        rid=canonical.get('bank_response_id')
        if type(rid) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',rid):fail()
        with self.p._publication(bank_db) as db:
            actor=self._actor(w,db)
            if (canonical.get('workspace_id')!=w
                or canonical.get('privacy_revision')!=privacy_revision(db,w)
                or canonical.get('personal_revision')!=self.store.revision(w)):
                fail('APPROVED_RESPONSE_BINDING')
            local_contract='navren.local-rule-response/1'
            packet={'contract':local_contract,'response_id':rid,'actor':actor,
                    'language':canonical['social_reply']['language'],'original_response':canonical,
                    'original_response_hash':fingerprint(canonical),
                    'local_rule_id':canonical['social_reply']['rule_id'],
                    'local_rule_code':hashlib.sha256(Path(social_reply.__file__).read_bytes()).hexdigest(),
                    'source_versions':self.p.source_versions(w,db),'personal_revision':self.store.revision(w),
                    'privacy_revision':privacy_revision(db,w),'prepared_at':self.g.bank.clock(),
                    'server_state':'prepared_under_original_publication_guard','execution_authority':False}
            with self._lock:
                existing=self._pending.get((w,rid))
                if existing is not None:
                    if (existing['publication'] is not None or existing['packet'].get('contract')!=local_contract
                        or existing['packet']['original_response_hash']!=packet['original_response_hash']
                        or existing['packet']['source_versions']!=packet['source_versions']
                        or existing['packet']['local_rule_code']!=packet['local_rule_code']):
                        fail('APPROVED_RESPONSE_ALREADY_PREPARED')
                    return deepcopy(existing['envelope'])
                envelope=self._seal(local_contract,packet)
                self._pending[(w,rid)]={'packet':packet,'envelope':envelope,'publication':None}
            return deepcopy(envelope)

    @classmethod
    def _contains(cls,value,original):
        if value==original:return True
        if type(value) is dict:return any(cls._contains(v,original) for v in value.values())
        if type(value) is list:return any(cls._contains(v,original) for v in value)
        return False

    def guarded_writer(self,w,response,writer,*,bank_db,language):
        def save(owner,messages,response_id,widgets,**kwargs):
            if owner!=w or response_id!=response['bank_response_id'] or type(widgets) is not list:fail('APPROVED_RESPONSE_BINDING')
            if not messages or messages[-1].get('role')!='assistant' or messages[-1].get('content')!=response['reply']:fail('APPROVED_RESPONSE_BINDING')
            envelope=self.prepare(w,response,bank_db=bank_db,language=language)
            # 新整轮封套由可信Agent validator核验；不把v2硬塞旧message_state v1。
            kwargs.pop('message_state',None);kwargs.pop('validate_message_state',None)
            return writer(owner,messages,response_id,[*widgets,envelope],**kwargs)
        return save

    def published(self,w,response_id,*,bank_db):
        """原HTTP/SSE最终写入成功返回后调用；不代表客户端已确认收到。"""
        with self.p._publication(bank_db) as db:
            self._actor(w,db)
            with self._lock:entry=deepcopy(self._pending.get((w,response_id)))
            if not entry:fail('APPROVED_RESPONSE_NOT_PREPARED')
            packet=entry['packet']
            if packet['source_versions']!=self.p.source_versions(w,db):fail('APPROVED_RESPONSE_STALE')
            marker=entry['publication'] or self._seal(COMMIT,{'contract':COMMIT,'response_id':response_id,
                'original_response_hash':packet['original_response_hash'],'prepared_envelope_hash':fingerprint(entry['envelope']),
                'published_at':self.g.bank.clock(),'publication_state':'server_send_completed','client_acknowledged':False,
                'bank_binding_committed_before_publication':True,'original_guarded_writer_committed':True})
            if not self.store.append_turn_widget_guarded(w,response_id,marker,bank_db=db,
                expected_revision=packet['personal_revision'],expected_privacy=packet['privacy_revision']):fail('APPROVED_RESPONSE_COMMIT_CAS')
            with self._lock:
                self._pending[(w,response_id)]['publication']=marker
                while len(self._pending)>128:self._pending.pop(next(iter(self._pending)))
            return deepcopy(marker)

    def discard_unpublished(self,w,response_id,response,*,expected_revision,expected_privacy):
        """仅撤回本进程实际prepared而未published的原件，不授予旧身份读取/写入权限。"""
        with self._lock:entry=deepcopy(self._pending.get((w,response_id)))
        if entry is None:return None
        packet=entry['packet']
        if (entry['publication'] is not None or packet['response_id']!=response_id
                or packet['actor']['workspace_id']!=w
                or packet['original_response_hash']!=fingerprint(response)
                or type(expected_revision) is not int or type(expected_privacy) is not int
                or packet['personal_revision']!=expected_revision or packet['privacy_revision']!=expected_privacy):
            fail('APPROVED_RESPONSE_DISCARD_BINDING')
        # 原discard已完成时只清本请求内prepared记录。版本漂移时，剩余撤回
        # 以原密文/代次CAS为界；不以当前授权获得任何新数据或金融执行权限。
        with self.g.bank.transaction() as bank_db,closing(self.store.connect()) as db:
            if not self.store._guarded_bank_owner(w,bank_db):fail('APPROVED_RESPONSE_DISCARD_BINDING')
            # 等待原publication银行锁期间可能已追加成功marker。必须在
            # 同一银行事务取得后再看本请求记录，不能用等锁前的None撤回成功原件。
            with self._lock:current=deepcopy(self._pending.get((w,response_id)))
            if current is None or current['envelope']!=entry['envelope'] or current['publication'] is not None:
                fail('APPROVED_RESPONSE_DISCARD_BINDING')
            db.execute('BEGIN IMMEDIATE')
            try:
                saved=db.execute('SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?',(w,response_id)).fetchone()
                if saved is None:
                    db.rollback();complete=False
                elif (saved['personal_revision']!=expected_revision or saved['privacy_revision']!=expected_privacy):
                    fail('APPROVED_RESPONSE_DISCARD_BINDING')
                elif saved['status'] in ('discarded','discarded_partial'):
                    complete=saved['status']=='discarded';db.rollback()
                elif saved['status']!='saved':
                    fail('APPROVED_RESPONSE_DISCARD_BINDING')
                else:
                    widget=self.store._raw_widget(db,w,response_id)
                    widget_matches=(widget is not None and saved['widget_hash']==self.store._guarded_row_hash(widget,'widgets_cipher','created_at'))
                    if widget_matches:
                        widgets=json.loads(self.store._decode(widget['widgets_cipher']))
                        if entry['envelope'] not in widgets:fail('APPROVED_RESPONSE_DISCARD_BINDING')
                    dialogue=self.store._raw_dialogue(db,w)
                    history_matches=saved['history_hash']==self.store._guarded_row_hash(dialogue,'messages_cipher','updated_at')
                    previous=self.store._previous_guarded_history(db,w,saved) if history_matches else None
                    restored=history_matches and previous is not None
                    if restored:
                        if previous['messages_cipher'] is None:
                            db.execute('DELETE FROM agent_dialogues WHERE workspace_id=? AND messages=? AND updated_at=?',
                                (w,dialogue['messages_cipher'],dialogue['updated_at']))
                        else:
                            db.execute('UPDATE agent_dialogues SET messages=?,updated_at=? WHERE workspace_id=? AND messages=? AND updated_at=?',
                                (previous['messages_cipher'],previous['updated_at'],w,dialogue['messages_cipher'],dialogue['updated_at']))
                    if widget_matches:
                        db.execute('DELETE FROM agent_widget_history WHERE workspace_id=? AND response_id=? AND widgets=? AND created_at=?',
                            (w,response_id,widget['widgets_cipher'],widget['created_at']))
                    complete=bool(restored and widget_matches)
                    db.execute("UPDATE guarded_turn_saves SET status=? WHERE workspace_id=? AND response_id=? AND save_generation=? AND status='saved'",
                        ('discarded' if complete else 'discarded_partial',w,response_id,saved['save_generation']))
                    db.commit()
            except Exception:
                db.rollback();raise
        with self._lock:
            current=self._pending.get((w,response_id))
            if current is not None and current['envelope']==entry['envelope'] and current['publication'] is None:
                self._pending.pop((w,response_id),None)
        return complete

    def read(self,w,response_id,*,language='zh',bank_db=None,_send_capture=None):
        if type(response_id) is not str or not 1<=len(response_id)<=160 or language not in {'zh','en'}:fail()
        with self.p._publication(bank_db) as db:
            actor=self._actor(w,db)
            with self.store.connection(w) as private:
                widgets=self.store.published_turn_widgets(private,w,response_id)
                if widgets is None:fail('APPROVED_RESPONSE_NOT_FOUND',404)
                originals=[x for x in widgets if type(x) is dict and x.get('contract')==CONTRACT]
                markers=[x for x in widgets if type(x) is dict and x.get('contract')==COMMIT]
                if len(originals)!=1 or len(markers)!=1:fail('APPROVED_RESPONSE_NOT_PUBLISHED')
                packet=self._open(originals[0],CONTRACT);publication=self._open(markers[0],COMMIT)
                saved=private.execute("SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=? AND status='saved'",(w,response_id)).fetchone()
                raw=self.store._raw_widget(private,w,response_id)
                if not saved or saved['widget_hash']!=self.store._guarded_row_hash(raw,'widgets_cipher','created_at'):fail('APPROVED_RESPONSE_SAVE_CHANGED')
                latest=private.execute("SELECT response_id FROM guarded_turn_saves WHERE workspace_id=? AND status='saved' ORDER BY rowid DESC LIMIT 1",(w,)).fetchone()
                current=bool(latest and latest['response_id']==response_id and saved['history_hash']==self.store._guarded_row_hash(
                    self.store._raw_dialogue(private,w),'messages_cipher','updated_at'))
                history_pin={'latest':dict(latest) if latest else None,'dialogue_hash':self.store._guarded_row_hash(
                    self.store._raw_dialogue(private,w),'messages_cipher','updated_at')}
            if packet['actor']!=actor or packet['response_id']!=response_id or packet['language']!=language:fail('APPROVED_RESPONSE_BINDING',403)
            original=packet['original_response']
            if (fingerprint(original)!=packet['original_response_hash'] or publication['response_id']!=response_id
                or publication['original_response_hash']!=packet['original_response_hash']
                or publication['prepared_envelope_hash']!=fingerprint(originals[0]) or publication['publication_state']!='server_send_completed'):fail('APPROVED_RESPONSE_TAMPERED')
            if (packet['source_versions']!=self.p.source_versions(w,db) or packet['privacy_revision']!=privacy_revision(db,w)
                or packet['personal_revision']!=self.store.revision(w)):fail('APPROVED_RESPONSE_STALE')
            # 金融展示来源不等于公开新闻/条款来源；恢复时也沿原公开原件守卫亲读回查。
            from local_knowledge.catalog_information_host import response_snapshot
            catalog_information=response_snapshot(original)
            if catalog_information is not None:
                self.g.catalog_information_host.lookup(catalog_information,workspace=w,
                    privacy_revision=packet['privacy_revision'],personal_revision=packet['personal_revision'],request_id=response_id)
            public={'market':original.get('market_snapshot'),'knowledge':original.get('knowledge_snapshot')}
            with self.g.operation_evidence.public_guard(public):pass
            display={k:deepcopy(original[k]) for k in DISPLAY if k in original}
            display.update(bank_actions=[],widgets=[],readonly_detail_available=False)
            proof=packet['agent_final_proof']
            result={'status':'current' if current else 'historical_readonly','workspace_id':w,'privacy_revision':packet['privacy_revision'],
                    'language':language,'response':display,'original_response_hash':packet['original_response_hash'],
                    'original_agent_final_proof':deepcopy(proof),'original_publication_receipt':publication,
                    'no_financial_side_effect':True,'execution_authority':False,'can_reissue_old_approval':False,
                    'verification':{'original_message_verified':True,'identity_verified':True,'language_verified':True,
                        'source_versions_verified':True,'semantic_review_status':'approved','final_guard_status':'verified',
                        'body_amount_occurrence_count':proof['body_amount_occurrence_count'],
                        'total_amount_occurrence_count':proof['total_amount_occurrence_count'],
                        'knowledge_history_verified':True}}
            if _send_capture is not None:
                _send_capture.update(packet=deepcopy(packet),saved_hash=fingerprint(dict(saved)),
                                     history_pin=history_pin,response_hash=fingerprint(result))
            return result

    def _check_send(self,w,capture,response,db):
        packet=capture['packet'];rid=packet['response_id']
        from local_knowledge.catalog_information_host import response_snapshot
        catalog_information=response_snapshot(packet['original_response'])
        if catalog_information is not None:
            self.g.catalog_information_host.lookup(catalog_information,workspace=w,
                privacy_revision=packet['privacy_revision'],personal_revision=packet['personal_revision'],request_id=rid)
        if packet['actor']!=self._actor(w,db) or fingerprint(response)!=capture['response_hash']:
            fail('APPROVED_RESPONSE_SEND_CHANGED')
        if (packet['source_versions']!=self.p.source_versions(w,db) or packet['privacy_revision']!=privacy_revision(db,w)
            or packet['personal_revision']!=self.store.revision(w)):fail('APPROVED_RESPONSE_STALE')
        with self.store.connection(w) as private:
            saved=private.execute("SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=? AND status='saved'",(w,rid)).fetchone()
            raw=self.store._raw_widget(private,w,rid)
            if (not saved or fingerprint(dict(saved))!=capture['saved_hash'] or
                saved['widget_hash']!=self.store._guarded_row_hash(raw,'widgets_cipher','created_at')):fail('APPROVED_RESPONSE_SAVE_CHANGED')
            latest=private.execute("SELECT response_id FROM guarded_turn_saves WHERE workspace_id=? AND status='saved' ORDER BY rowid DESC LIMIT 1",(w,)).fetchone()
            pin={'latest':dict(latest) if latest else None,'dialogue_hash':self.store._guarded_row_hash(
                self.store._raw_dialogue(private,w),'messages_cipher','updated_at')}
            if pin!=capture['history_pin']:fail('APPROVED_RESPONSE_SEND_CHANGED')

    @contextmanager
    def send_guard(self,w,capture,response):
        """原实际JSON发送入口才取得原发布锁；每个原checkpoint重验同原件，不重签。"""
        packet=capture['packet'];original=packet['original_response']
        from local_knowledge.catalog_information_host import response_snapshot
        token=self.g._restore_send_check.set(lambda db:self._check_send(w,capture,response,db))
        try:
            with self.g.publication_guard(w,packet['privacy_revision'],packet['personal_revision'],
                original.get('knowledge_snapshot'),market_snapshot=original.get('market_snapshot'),
                service_snapshot=original.get('service_snapshot'),
                catalog_information=response_snapshot(original)):
                yield
        finally:self.g._restore_send_check.reset(token)
