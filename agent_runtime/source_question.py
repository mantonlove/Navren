"""Finite product-source question only; no product analysis or authority."""
from copy import deepcopy
import hashlib
import json
from simulated_bank import BankError
from .tool_routing import TurnToolScope

CONTRACT='navren.product-source-question-selection/1'
TEXT='先确认资料来源，避免混用。'
OPTIONS=({'id':'current_simulation','label':'本机演示规则'},
         {'id':'real_public','label':'真实发行产品'})
SCHEMA={'type':'object','properties':{'source_clarification':{'type':'boolean','enum':[True]}},
        'required':['source_clarification'],'additionalProperties':False}
NOTICE=('当前answer_reviewed仅接受{"source_clarification":true}。'
    '宿主将用本轮已受边界控制的原问题生成自包含普通来源选择；'
    '不填自由text、reply_parts、claims、产品结论、画像、操作或金融参数。'
    '这里只确认来源，不代表已完成比较。原金额、隐私、操作措辞、完整GLM和最终发布守卫保持。')

def eligible(scope):
    return (type(scope)is TurnToolScope and scope.active_skills==['product-research']
            and scope.product_source_scope=='clarify')

def project(schemas,scope):
    if not eligible(scope):return schemas
    result=deepcopy(schemas)
    for schema in result:
        if schema['function']['name']=='answer_reviewed':
            schema['function']['parameters']=deepcopy(SCHEMA)
            schema['function']['description']='仅选择source_clarification=true，由宿主生成普通来源选择；不提供自由正文或产品结论，仍经原完整审核和最终发布。'
    return result

def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()

def compose(args,*,question,use_reply_parts):
    def invalid():raise BankError('INVALID_FINAL_CONTRACT','来源澄清只接受本轮展示的有限选择。',422)
    if (type(args)is not dict or set(args)!={'source_clarification'}
            or args['source_clarification']is not True or type(use_reply_parts)is not bool):invalid()
    # question is supplied by the host from its same-turn boundary projection,
    # never by the model selector. Existing alias restoration and all original
    # interaction, money, privacy and operation guards still validate the result.
    if (type(question)is not str or not 1<=len(question)<=160
            or any(ord(char)<32 for char in question)):invalid()
    interaction={'kind':'single_choice','prompt':'你提到的「'+question+'」，要按哪种资料来源继续？',
                 'options':[dict(option)for option in OPTIONS]}
    result={'claims':[],'draft_ids':[],'followups':[],'interaction':interaction}
    if use_reply_parts:result['reply_parts']=[{'text':TEXT}]
    else:result['text']=TEXT
    receipt={'contract':CONTRACT,'composition_strategy':'trusted_product_source_question',
        'model_original_answer_validated':False,'model_selection_sha256':_digest(args),
        'same_turn_boundary_question_sha256':hashlib.sha256(question.encode()).hexdigest(),
        'question_binding':'host_same_turn_boundary_current_request_then_original_alias_restore',
        'trusted_template_sha256':_digest({'contract':CONTRACT,'text':TEXT,'options':OPTIONS}),
        'composed_arguments_sha256':_digest(result),'composed_text_sha256':hashlib.sha256(TEXT.encode()).hexdigest(),
        'no_product_comparison_or_personal_status_claim':True,'no_financial_calculation':True,
        'no_new_public_identity_or_action_authority':True,'additional_primary_calls':0,'additional_business_calls':0,
        'original_guards_status':'pending_original_alias_restore_amount_privacy_operation_full_GLM_FinalProof_and_publication',
        'approval_or_publication_authority':False}
    return result,receipt
