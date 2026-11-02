"""Natural goal business body; all original guards and full review decide publication."""
from copy import deepcopy
import hashlib
import json
import unicodedata
from .metric_claims import monetary_occurrences, MetricClaimError
from simulated_bank import BankError
from .tool_routing import TurnToolScope

CONTRACT='navren.goal-clarification-selection/1'
STEP_TEXT={
    'define_goal':'先明确目标的衡量口径和用途。',
    'review_surplus':'核对收入是否稳定，以及每期能持续留下多少。',
    'review_obligations':'把已有负担和必要生活支出一起考虑，避免挤占日常安排。',
    'adjust_pace':'比较增加收入、减少可调整开支和调整期限的可行性。',
}
DIMENSIONS={
    'measure':{'kind':'single_choice','prompt':'你希望这个目标主要衡量什么？','options':[
        {'id':'cash','label':'可支配现金'},{'id':'net_assets','label':'净资产'},{'id':'long_term','label':'长期投资本金'}]},
    'currency':{'kind':'single_choice','prompt':'这个目标按哪种币种衡量？','options':[
        {'id':'cny','label':'人民币'},{'id':'usd','label':'美元'},{'id':'other','label':'其他币种'}]},
    'horizon':{'kind':'single_choice','prompt':'你希望按怎样的时间范围规划？','options':[
        {'id':'soon','label':'希望尽快实现'},{'id':'longer','label':'愿意长期积累'},{'id':'undecided','label':'还没确定期限'}]},
}
NOTICE=('当前answer_reviewed只使用goal_analysis.body写完整自然正文；如有必要可在正文末尾自然提问，也可以不问。'
    '根据用户原话展开有用分析和具体可行动的例子，自由组织段落；不按固定路径数、条件/机制/代价/下一步字段或机械标题拼接。'
    '未给出的职业、收入或资产只能按条件讨论，不能当本人事实。不要复述目标数额、编造储蓄额/收益率/达成时间；这个阶段用“这个目标”指代，确需精算再走原正式金额工具。'
    '当个人资源、能力、负债、账户或风险承受能力缺少证据时，既不声称用户拥有，也不声称用户缺少；必要时以明确条件说明或提问。'
    '对各类选择分别说明适用条件与可能损失；没有可信条件和范围时，不把经营、劳动、投资等损失限定为轻微、确定或可控，也不宣称某路径绝对最好或回报最高。'
    '只在关键未知会改变下一步时提问，也可不问；不要用问卷代替回答。仅在用户问题或明确分析的方案确实涉及相关金融产品时，才讨论申赎、到账、产品期限或费用；普通学习等非金融目标不要添加与问题无关的金融产品或交易提示。正文中的问题只在必要时提出一次。'
    '未掌握用户具体数据、行业、时间及可验证假设时，不把路径快慢、最优性、直接性、持久性或稳定性作为普遍事实或相对结论；条件词本身不是比较依据。可以解释不同条件下机制、投入、代价和待验证未知，不承诺达成或快慢。'
    '解释结果时区分实际来源、影响条件和累积或放大机制；机制或时间本身不保证产生正向结果，缺少依据不把潜在机会说成已经发生或必然取得的收益。'
    '提问应允许用户自由说明或暂不选择；不要把可组合的方向或并非穷尽的例子当成必须二选一的路径，也不要预设用户已具备某种资源。'
    '围绕用户真实问题取舍内容；用户没有提及且理解当前问题并不需要时，不要因为可以联想到费用或预算就默认增加财务规划，用户明确要求或问题确实需要时则正常回答。'
    'body必须是完整自然业务答复，不是隐藏推理；禁止夹带text/reply_parts/claims/操作参数。原完整审核后才可展示。')
CLARIFICATION_SCHEMA={'type':'object','properties':{'goal_clarification':{'type':'object','properties':{
    'steps':{'type':'array','items':{'type':'string','enum':list(STEP_TEXT)},'minItems':2,'maxItems':3,'uniqueItems':True},
    'clarification_dimension':{'type':'string','enum':list(DIMENSIONS)}},
    'required':['steps','clarification_dimension'],'additionalProperties':False}},
    'required':['goal_clarification'],'additionalProperties':False}


ANALYSIS_CONTRACT='navren.contextual-goal-analysis/5'
QUOTE_ANALYSIS_CONTRACT='navren.contextual-goal-analysis/6'
BODY_LIMIT=1962
ANALYSIS_SCHEMA={'type':'object','properties':{
    'body':{'type':'string','minLength':1,'maxLength':BODY_LIMIT}},
    'required':['body'],'additionalProperties':False}
SCHEMA={'type':'object','properties':{'goal_analysis':ANALYSIS_SCHEMA},
    'required':['goal_analysis'],'additionalProperties':False}

# Project only the exact host-maintained ordinary-interaction sentence. The
# separate operation, calendar and memory confirmation rules remain intact.
_PRIMARY_INTERACTION = ('确实需要用户确认或选择时，在answer_reviewed的interaction字段给出confirmation或single_choice，'
    '正文简短说明原因，followups为空；确认/拒绝由页面显示，不要求用户输入确认文字。')
_PRIMARY_GOAL_INTERACTION = ('本轮普通目标信息澄清可在正文中自然开放提问，也可不提问；'
    '这不构成任何操作、日历变更或记忆授权。')
_REVIEW_INTERACTION = ('普通对话需要澄清、询问继续或提供有限选项时，必须用draft.interaction表达，'
    '同审prompt和options，不能只有正文提问或把选择藏在followups；')
_REVIEW_GOAL_INTERACTION = ('本轮普通目标信息澄清可自然开放提问或不提问；')

def _project_interaction_literal(policy, original, replacement):
    if type(policy) is not str or policy.count(original)>1:
        raise BankError('REQUEST_QUOTE_CONTROL_INVALID','目标分析提示范围无效。',422)
    # An absent or modified sentence is left untouched, never guessed.
    return policy.replace(original,replacement) if original in policy else policy

def project_goal_review_interaction(policy):
    """For the caller's host-validated goal packet, not model-provided text."""
    return _project_interaction_literal(policy,_REVIEW_INTERACTION,_REVIEW_GOAL_INTERACTION)

def eligible(scope):
    return (type(scope)is TurnToolScope and scope.active_skills==['goal-planning']
        and scope.goal_stage=='clarify')

def project(schemas,scope,*,allow_degraded=False,quote_source=None):
    if not eligible(scope):return schemas
    result=deepcopy(schemas)
    if quote_source is not None:
        from .request_quotes import assert_source
        assert_source(quote_source)
        if allow_degraded is True:raise BankError('REQUEST_QUOTE_SCOPE_INVALID','当前引用不适用于降级选择。',422)
    for schema in result:
        if schema['function']['name']=='answer_reviewed':
            schema['function']['parameters']=deepcopy(CLARIFICATION_SCHEMA if allow_degraded is True else SCHEMA)
            if quote_source is not None:
                schema['function']['parameters']['properties']['goal_analysis']['properties']['user_request_quote_id']={'type':'string','enum':[quote_source.quote_ref]}
                schema['function']['parameters']['properties']['goal_analysis']['properties']['body']['description']='写完整自然分析。原问题中的数额是未经核实的用户目标；若需展示原数额，只选择当前user_request_quote_id，由宿主独立归因展示。body用“这个目标”指代，不在正文复述金额，不把愿望当作资产、收益、币种或交易事实。'
            schema['function']['description']=('仅安全澄清/受控降级，不是开放规划完成。'if allow_degraded is True else
                '使用goal_analysis.body给出完整自然业务答复，可自由组织段落和例子，必要问题直接写在正文中。')+'全部自由字段仍经原金额、完整GLM与发布守卫。'
    return result

def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _compose_clarification(args,*,use_reply_parts):
    def invalid():raise BankError('INVALID_FINAL_CONTRACT','目标澄清只接受本轮已展示的步骤与澄清维度。',422)
    if type(use_reply_parts)is not bool or type(args)is not dict or set(args)!={'goal_clarification'}:invalid()
    selection=args['goal_clarification']
    if type(selection)is not dict or set(selection)!={'steps','clarification_dimension'}:invalid()
    steps=selection['steps'];dimension=selection['clarification_dimension']
    if (type(steps)is not list or not 2<=len(steps)<=3
            or any(type(step)is not str or step not in STEP_TEXT for step in steps)
            or len(steps)!=len(set(steps))or type(dimension)is not str or dimension not in DIMENSIONS):invalid()
    text=''.join(STEP_TEXT[step]for step in steps)
    result={'claims':[],'draft_ids':[],'followups':[],'interaction':deepcopy(DIMENSIONS[dimension])}
    if use_reply_parts:result['reply_parts']=[{'text':text}]
    else:result['text']=text
    receipt={'contract':CONTRACT,'composition_strategy':'trusted_goal_clarification_selection',
        'model_original_answer_validated':False,'model_selection_sha256':_digest(args),
        'selected_step_keys':list(steps),'clarification_dimension':dimension,
        'trusted_template_sha256':_digest({'contract':CONTRACT,'steps':STEP_TEXT,'dimensions':DIMENSIONS}),
        'composed_arguments_sha256':_digest(result),'composed_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'no_financial_calculation':True,'no_target_currency_or_amount_inferred':True,
        'no_goal_or_memory_saved':True,'additional_primary_calls':0,'additional_business_calls':0,
        'original_guards_status':'pending_amount_operation_privacy_full_GLM_FinalProof_and_publication',
        'approval_or_publication_authority':False}
    return result,receipt

def _compose_analysis(args,*,use_reply_parts):
    fields={}
    def reject(code,field=None,cause=None):
        error=BankError(code,'本轮目标路径的金额或字段校验未通过；原分析未发布。',422)
        error.goal_analysis_rejection={'contract':ANALYSIS_CONTRACT,'response_mode':'contextual_analysis_rejected',
            'original_failure_code':code,'field':field,'model_original_answer_validated':False,
            'unvalidated_model_input_sha256':_digest(args),'unvalidated_free_field_sha256':{
                path:hashlib.sha256(text.encode()).hexdigest()for path,text in fields.items()},
            'original_content_removed_or_rewritten':False,'automatic_fallback':False,
            'approval_or_publication_authority':False,'open_goal_quality_eligible':False}
        if cause is not None:raise error from cause
        raise error
    def text(value,path,limit):
        if type(value)is not str or not value.strip()or len(value)>limit:reject('INVALID_FINAL_CONTRACT',path)
        fields[path]=value
        try:occurrences=monetary_occurrences(unicodedata.normalize('NFKC',value))
        except MetricClaimError as cause:reject(cause.code,path,cause)
        if occurrences:reject('METRIC_AMOUNT_UNBOUND',path)
        return value
    if type(use_reply_parts)is not bool or type(args)is not dict or set(args)!={'goal_analysis'}:reject('INVALID_FINAL_CONTRACT')
    plan=args['goal_analysis']
    if type(plan)is not dict or set(plan)!={'body'}:reject('INVALID_FINAL_CONTRACT','goal_analysis')
    if len(json.dumps(args,ensure_ascii=False,allow_nan=False).encode())>6144:reject('INVALID_FINAL_CONTRACT','goal_analysis')
    body=text(plan['body'],'goal_analysis.body',BODY_LIMIT)
    # The only free-text field is the original complete model body.
    question=None
    from .request_quotes import _body_question_display
    display,question_start,question_end,question_deduplicated=_body_question_display(body,question)
    # Original field and complete-display parsers remain mandatory; no target
    # exemption, value replacement, truncation or host-generated prose.
    try:occurrences=monetary_occurrences(unicodedata.normalize('NFKC',display))
    except MetricClaimError as cause:reject(cause.code,'goal_analysis.rendered_body',cause)
    if occurrences:reject('METRIC_AMOUNT_UNBOUND','goal_analysis.rendered_body')
    result={'claims':[],'draft_ids':[],'followups':[]}
    if use_reply_parts:result['reply_parts']=[{'text':display}]
    else:result['text']=display
    receipt={'contract':ANALYSIS_CONTRACT,'response_mode':'contextual_analysis',
        'composition_strategy':'model_natural_single_body',
        'model_original_answer_validated':False,'unvalidated_model_input_sha256':_digest(args),
        'model_free_field_sha256':{path:hashlib.sha256(value.encode()).hexdigest()for path,value in fields.items()},
        'body_raw_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'body_display_sha256':hashlib.sha256(unicodedata.normalize('NFKC',body).encode()).hexdigest(),
        'body_character_count':len(body),'body_utf8_bytes':len(body.encode()),
        'body_is_model_business_output':True,'body_preserved_verbatim':True,
        'clarification_mode':'body_may_contain_question','body_may_contain_natural_question':True,
        'question_sha256':hashlib.sha256(question.encode()).hexdigest()if question is not None else None,
        'question_display_sha256':hashlib.sha256(unicodedata.normalize('NFKC',question).encode()).hexdigest()if question is not None else None,
        'question_body_start_nfkc':question_start,
        'question_body_end_nfkc':question_end,
        'question_deduplicated_against_body':question_deduplicated,
        'composed_display_sha256':hashlib.sha256(unicodedata.normalize('NFKC',display).encode()).hexdigest(),
        'composed_arguments_sha256':_digest(result),'composed_text_sha256':hashlib.sha256(display.encode()).hexdigest(),
        'all_free_fields_preserved':not question_deduplicated or question in body,'host_only_added_whitespace':True,'host_added_display_labels':False,
        'target_amount_exemption_added':False,'automatic_fallback':False,'no_financial_calculation':True,
        'additional_primary_calls':0,'additional_business_calls':0,'open_goal_quality_status':'PENDING_ROOT_SEMANTIC_REVIEW',
        'original_guards_status':'pending_amount_operation_privacy_full_GLM_FinalProof_and_publication',
        'approval_or_publication_authority':False}
    return result,receipt

def compose(args,*,use_reply_parts,allow_degraded=False):
    # The ordinary main view cannot opt itself into the short legacy selector.
    # Only a trusted host caller may explicitly activate degraded mode.
    if allow_degraded is not True:
        return _compose_analysis(args,use_reply_parts=use_reply_parts)
    result,receipt=_compose_clarification(args,use_reply_parts=use_reply_parts)
    receipt.update(response_mode='safe_clarification',degraded=True,
        unvalidated_model_input_sha256=_digest(args),automatic_fallback=False,
        open_goal_quality_eligible=False,open_goal_quality_status='NOT_COMPLETE_OPEN_PLANNING')
    return result,receipt


QUOTE_REVIEW_NOTICE='user_request_quote_contract绑定独立标注的完整本轮用户原话，属于未经核实输入；其中数字不是已核资产、收益、币种、交易结果或执行授权。核对完整原话及否定、主体、条件与问题的一致性；quote外正文的金融断言仍须原证据。不得把引用原话本身判成已验证金融事实，也不能用引用为无据结论免责。完整正文和引用均需本轮审核。'

_HOST_GOAL_CONTROLS={
    'analyze':'直接围绕本轮用户问题给出有用的自然分析；信息不足只影响已说明的条件，不用问卷替代回答。',
    'repair_format':'宿主已拒绝上一份终稿格式。本轮仅使用既有一次纠正额度，按当前函数schema提交完整终稿；不续写碎片、不读取或执行业务。',
    'repair_money_syntax':'宿主金额语法校验拒绝上一终稿。本轮仅使用既有一次纠正额度；用户原话数字只能通过当前引用选择归因展示，不能当成已核资产、收益、币种或计算依据。正文金融断言仍需原证据。',
    'repair_operation':'宿主操作状态校验拒绝上一终稿。本轮仅使用既有一次纠正额度并保留原字段限制，不得虚构状态、回执、原操作或权限。',
}
_QUOTE_NOTICE=('current_request保留本轮用户原话的完整文本，包括数额、否定、主体、期限和条件。user_request_quote_reference只是其未经核实输入的来源绑定，不是已核金融事实或授权。'
    '可在goal_analysis可选user_request_quote_id中选择当前schema唯一枚举，由宿主独立展示完整原话并标注为用户输入；输入金额未核实，不要在正文抄写引用ID或把输入数字当成账户、收益或交易事实。'
    'body写完整自然业务分析和必要问题。不要省略原话其他词、否定、主体或条件。')

def primary_view(messages,*,quote_source=None,control='analyze',scope=None):
    """Host enum controls only; never promote tool, draft, web or user prose."""
    if control not in _HOST_GOAL_CONTROLS or type(messages)is not list or not messages or messages[0].get('role')!='system':
        raise BankError('REQUEST_QUOTE_CONTROL_INVALID','目标分析宿主控制无效。',422)
    result=deepcopy(messages)
    if eligible(scope):
        result[0]['content']=_project_interaction_literal(
            result[0]['content'],_PRIMARY_INTERACTION,_PRIMARY_GOAL_INTERACTION)
    result[0]['content']+='\n目标分析宿主控制 '+control+'：'+_HOST_GOAL_CONTROLS[control]
    if quote_source is None:
        result[0]['content']+='\n'+NOTICE
        return result
    from .request_quotes import assert_source,lexeme_roles
    assert_source(quote_source)
    source=quote_source.text;roles=lexeme_roles(source);last=0;role_metadata=[]
    for index,item in enumerate(roles):
        if (type(item)is not dict or set(item)!={'kind','start','end','text'}
                or item['kind']not in ('money','percentage')or type(item['start'])is not int
                or type(item['end'])is not int or not last<=item['start']<item['end']<=len(source)
                or source[item['start']:item['end']]!=item['text']):
            raise BankError('REQUEST_QUOTE_PROJECTION_INVALID','本轮输入引用投影无效。',422)
        role_metadata.append({key:item[key]for key in ('kind','start','end')});last=item['end']
    targets=[]
    for index,message in enumerate(result):
        if message.get('role')!='user' or type(message.get('content'))is not str:continue
        try:payload=json.loads(message['content'])
        except (ValueError,TypeError):continue
        if (type(payload)is dict and set(payload)=={'current_request','server_context'}
                and type(payload['current_request'])is str
                and unicodedata.normalize('NFKC',payload['current_request'])==unicodedata.normalize('NFKC',source)):
            targets.append((index,payload))
    if len(targets)!=1:raise BankError('REQUEST_QUOTE_PROJECTION_INVALID','本轮输入引用与当前请求不一致。',422)
    index,payload=targets[0]
    # Restore the already signed original only after exact NFKC equivalence.
    payload['current_request']=source
    # Preserve every original character; role metadata classifies provenance,
    # never replaces user intent or interprets a value/currency as a fact.
    payload['user_request_quote_reference']={'quote_ref':quote_source.quote_ref,'source_role':'unverified_user_input',
        'lexeme_roles':role_metadata,'financial_fact_eligible':False,'execution_authority':False}
    result[index]['content']=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    result[0]['content']+='\n'+NOTICE+'\n'+_QUOTE_NOTICE
    return result


def compose_with_quote(args,source,*,use_reply_parts):
    """Optional full-current-input quote; free body remains the original /3 path."""
    from .request_quotes import assert_source,compose as compose_quote
    assert_source(source)
    plan=args.get('goal_analysis')if type(args)is dict else None
    if (type(args)is not dict or set(args)!={'goal_analysis'}or type(plan)is not dict
            or set(plan)!={'body','user_request_quote_id'}
            or type(plan['user_request_quote_id'])is not str or plan['user_request_quote_id']!=source.quote_ref):
        error=BankError('INVALID_FINAL_CONTRACT','本轮原话引用选择无效。',422)
        error.goal_analysis_rejection={'contract':QUOTE_ANALYSIS_CONTRACT,'response_mode':'contextual_analysis_rejected',
            'original_failure_code':error.code,'field':'goal_analysis.user_request_quote_id',
            'model_original_answer_validated':False,'unvalidated_model_input_sha256':_digest(args),
            'unvalidated_free_field_sha256':{},'approval_or_publication_authority':False}
        raise error
    plain={'goal_analysis':{'body':plan['body']}}
    try:result,receipt=_compose_analysis(plain,use_reply_parts=use_reply_parts)
    except BankError as error:
        rejection=getattr(error,'goal_analysis_rejection',None)
        if isinstance(rejection,dict):
            rejection['contract']=QUOTE_ANALYSIS_CONTRACT;rejection['unvalidated_model_input_sha256']=_digest(args)
        raise
    oversized=('goal_analysis.body' if len(unicodedata.normalize('NFKC',plan['body']))>BODY_LIMIT else None)
    if oversized is not None:
        error=BankError('INVALID_FINAL_CONTRACT','引用分析规范化后的字段超出原长度限制。',422)
        error.goal_analysis_rejection={'contract':QUOTE_ANALYSIS_CONTRACT,'response_mode':'contextual_analysis_rejected',
            'original_failure_code':error.code,'field':oversized,'model_original_answer_validated':False,
            'unvalidated_model_input_sha256':_digest(args),
            'unvalidated_free_field_sha256':dict(receipt['model_free_field_sha256']),
            'approval_or_publication_authority':False}
        raise error
    if len(json.dumps(args,ensure_ascii=False,allow_nan=False).encode())>6144:
        raise BankError('INVALID_FINAL_CONTRACT','本轮目标分析参数超出原限制。',422)
    display,proof=compose_quote(source,plan['body'],None)
    snapshot=proof.snapshot();proof.validate_display(display)
    if use_reply_parts:result['reply_parts']=[{'text':display}]
    else:result['text']=display
    question=None;question_start=None;question_end=None
    from .request_quotes import _body_question_display
    body_display,body_start,body_end,deduplicated=_body_question_display(plan['body'],question)
    if not display.endswith(body_display):
        raise BankError('REQUEST_QUOTE_PROOF_INVALID','引用展示正文绑定无效。',422)
    if question is not None:
        prefix_length=len(unicodedata.normalize('NFKC',display[:-len(body_display)]))
        question_start=prefix_length+body_start;question_end=prefix_length+body_end
    receipt.update(contract=QUOTE_ANALYSIS_CONTRACT,
        composition_strategy='model_natural_body_with_independent_unverified_current_request_quote',
        unvalidated_model_input_sha256=_digest(args),
        user_request_quote_id=source.quote_ref,user_request_quote_contract_sha256=_digest(snapshot),
        user_request_quote_counts={key:snapshot[key]for key in (
            'raw_money_lexeme_count','source_quote_money_lexeme_count','financial_text_money_lexeme_count',
            'raw_percentage_lexeme_count','source_quote_percentage_lexeme_count','financial_text_percentage_lexeme_count')},
        composed_display_sha256=hashlib.sha256(unicodedata.normalize('NFKC',display).encode()).hexdigest(),
        composed_text_sha256=hashlib.sha256(display.encode()).hexdigest(),composed_arguments_sha256=_digest(result),
        question_body_start_nfkc=question_start,
        question_body_end_nfkc=question_end,
        question_deduplicated_against_body=deduplicated,
        host_only_added_whitespace=False,host_added_display_labels=True,
        user_request_quote_role='unverified_user_input_not_financial_fact',quote_is_not_metric_or_authorization=True)
    return result,receipt,proof


def quote_review_growth_bound(source,*,original_draft_growth_bytes=8192):
    """Conservative serialized-byte growth; no fake validated quote is issued."""
    from .request_quotes import assert_source, LABEL, PROOF_CONTRACT
    assert_source(source)
    source_characters=max(source.snapshot()['source_character_count'],len(unicodedata.normalize('NFKC',source.text)))
    offset_bound=max(8192,source_characters+BODY_LIMIT+160+64)
    skeleton={'contract':PROOF_CONTRACT,'source':source.snapshot(),
        'attribution_label':LABEL,
        **{key:'0'*64 for key in ('final_raw_sha256','final_nfkc_sha256','body_raw_sha256',
            'body_nfkc_sha256','question_raw_sha256','question_nfkc_sha256')},
        **{key:offset_bound for key in ('quote_start','quote_end','quote_start_nfkc','quote_end_nfkc',
            'raw_money_lexeme_count','source_quote_money_lexeme_count','financial_text_money_lexeme_count',
            'raw_percentage_lexeme_count','source_quote_percentage_lexeme_count','financial_text_percentage_lexeme_count')},
        'model_body_preserved_verbatim':True,'source_preserved_verbatim':True,
        'host_added_standalone_attribution':True,'financial_fact_eligible':False,'execution_authority':False}
    metadata_bytes=len(json.dumps({'user_request_quote_contract':skeleton},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())
    # JSON needs <=6 bytes per raw character (including escapes). Preserve the
    # existing draft allowance, adding only a proven upper bound of its growth.
    # The actual wire carries one snapshot; amount contract keeps four fields.
    draft_json_bound=6*(source_characters+BODY_LIMIT+160+len(LABEL)+8)+2
    notice_bytes=len(json.dumps('\n'+QUOTE_REVIEW_NOTICE,ensure_ascii=False).encode())
    # Existing occurrence count may grow from one digit to at most four.
    count_scalar_growth=len(str(offset_bound))-1
    return max(0,draft_json_bound-original_draft_growth_bytes)+metadata_bytes+notice_bytes+count_scalar_growth
