"""生成与路由对应的OpenAPI接口合同，不生成业务实现。"""
import json
from pathlib import Path

PREFIX = '/api/sim-bank'


def object_schema(properties, required):
    return {'type':'object','properties':properties,'required':required,'additionalProperties':False}


def main():
    string = {'type':'string','minLength':1,'maxLength':128}
    amount = {'type':'integer','minimum':1,'maximum':100000000,'description':'人民币分，禁止浮点和布尔值'}
    actions=['purchase','redeem','close_mandate','card_control','report_card_lost','request_card_limit','apply_card',
             'transfer','schedule_transfer','cancel_scheduled_payment','request_split','collect_split_payment',
             'cancel_subscription','reserve_funds','release_funds','book_services']
    prepare = object_schema({'action':{'enum':actions,'description':'各action的专属必填字段由服务端校验'},
        'account_id':string,'idempotency_key':string,'product_id':string,'amount_minor':amount,
        'mandate_id':string,'card_id':string,'control_type':{'enum':['online','overseas','contactless','single_limit']},
        'enabled':{'type':'boolean'},'limit_minor':amount,'requested_type':{'enum':['debit','credit']},
        'requested_limit_minor':amount,'beneficiary_id':string,'reference':{'type':'string','maxLength':128},
        'frequency':{'enum':['once','monthly']},'next_due_on':{'type':'string','format':'date'},
        'end_on':{'type':'string','format':'date'},'scheduled_payment_id':string,'split_id':string,
        'participant_id':string,'subscription_id':string,'task_id':string,'expires_on':{'type':'string','format':'date'},
        'hold_id':string,'quote_ids':{'type':'array','minItems':1,'maxItems':6,'uniqueItems':True,'items':string}},
        ['action','account_id','idempotency_key'])
    prepare['description']='模拟业务操作请求；操作专属字段由服务端按action严格校验。金额使用人民币分。'
    schemas = {
        'PrepareRequest':prepare,
        'ConfirmRequest':object_schema({'confirmed':{'const':True},'confirmation_digest':{'type':'string'},
                                        'verification_token':{'type':'string'}},['confirmed','confirmation_digest']),
        'VerifyRequest':object_schema({'verification_code':{'type':'string','pattern':r'^\d{6}$'}},['verification_code']),
        'ProfileRequest':object_schema({'risk_level':{'type':'integer','minimum':1,'maximum':5},
                                        'horizon_days':{'type':'integer','minimum':0,'maximum':3650},
                                        'reserve_minor':{'type':'integer','minimum':0,'maximum':100000000},
                                        'confirmed':{'const':True}},['risk_level','horizon_days','reserve_minor','confirmed']),
        'CompareRequest':object_schema({'product_ids':{'type':'array','minItems':1,'maxItems':20,'items':{'type':'string'}}},['product_ids']),
        'PeriodicCandidateMarkRequest':object_schema({'decision':{'enum':['confirmed','ignored']}},['decision']),
        'EmptyRequest':object_schema({},[]),
        'Operation':{'type':'object','properties':{'id':{'type':'string'},'action':{'enum':actions},
                    'permission':{'enum':['red','yellow']},'state':{'enum':['pending_confirmation','succeeded','pending_settlement','settled','cancelled','expired','invalidated']},
                    'confirmation_digest':{'type':'string'},'version':{'type':'integer'},'expires_at':{'type':'number'},
                    'payload':{'type':'object'},'result':{'type':['object','null']}},
                    'required':['id','action','permission','state','confirmation_digest','version','expires_at','payload','result']},
        'Error':{'type':'object','properties':{'error':{'type':'object','properties':{'code':{'type':'string'},'message':{'type':'string'},'environment':{'const':'simulation'}}}}},
    }
    paths = {}

    def route(path, method, summary, request=None, response=None, human=False, public=False, month=False, creation=False):
        op = {'summary':summary,'description':('用户专用凭证；Agent凭证返回403。' if human else '独立模拟环境。'),
              'responses':{('201' if creation else '200'):{'description':'成功', 'content':{'application/json':{'schema':response or {}}}},
                           'default':{'description':'明确失败或拒绝；不得解释为业务成功','content':{'application/json':{'schema':{'$ref':'#/components/schemas/Error'}}}}}}
        if not public:
            op['security']=[{'bearerAuth':[]}]
        if request:
            op['requestBody']={'required':True,'content':{'application/json':{'schema':{'$ref':'#/components/schemas/'+request}}}}
        parameters=[]
        if '{operation_id}' in path:
            parameters.append({'in':'path','name':'operation_id','required':True,'schema':{'type':'string','pattern':'^[a-f0-9]{32}$'}})
        if '{candidate_id}' in path:
            parameters.append({'in':'path','name':'candidate_id','required':True,'schema':{'type':'string','pattern':'^[a-f0-9]{64}$'}})
        if month:
            parameters.append({'in':'query','name':'month','schema':{'type':'string','pattern':r'^\d{4}-(0[1-9]|1[0-2])$'},'description':'月报与快照默认2026-09；流水省略时返回所有模拟记录。'})
        if parameters:
            op['parameters']=parameters
        paths.setdefault(path,{})[method]=op

    route('/health','get','模拟服务健康状态',public=True)
    route('/openapi.json','get','本接口合同',public=True)
    route(PREFIX+'/workspaces','post','初始化独立模拟工作区，凭证和验证码仅返回一次',request='EmptyRequest',public=True,creation=True)
    for path,title in [('capabilities','能力与边界'),('snapshot','同版本账户快照'),('transactions','原始模拟流水'),
                       ('analysis','单月消费统计'),('anomalies','可追溯的异常提示'),('periodic-charges','周期扣费证据'),
                       ('mandates','银行侧模拟代扣协议'),('products','模拟产品及画像匹配理由'),('profile','模拟风险画像'),
                       ('holdings','模拟持仓本金'),('orders','订单及结算状态'),('audit','脱敏审计事件')]:
        route(PREFIX+'/'+path,'get',title,month=path in ('snapshot','transactions','analysis'))
    route(PREFIX+'/profile','post','明确确认后修改模拟风险画像',request='ProfileRequest',human=True)
    route(PREFIX+'/customer-archive','get','完整客户档案及覆盖、资金核验结果')
    route(PREFIX+'/data-contract','get','全领域字段字典、来源与业务边界')
    route(PREFIX+'/annual-analysis','get','封存及活动银行流水年度汇总')
    paths[PREFIX+'/annual-analysis']['get']['parameters']=[{'in':'query','name':'year','schema':{'type':'integer','minimum':2025,'maximum':2100},'description':'默认2025；完整2025年度样本，2026仅截至模拟日期。'}]
    route(PREFIX+'/products/compare','post','比较指定模拟产品',request='CompareRequest')
    route(PREFIX+'/periodic-charges/{candidate_id}/mark','post','用户确认或忽略本地周期扣费候选',
          request='PeriodicCandidateMarkRequest',human=True)
    operation_ref={'$ref':'#/components/schemas/Operation'}
    route(PREFIX+'/operations/prepare','post','冻结具体操作并准备确认',request='PrepareRequest',response=operation_ref)
    route(PREFIX+'/operations/{operation_id}','get','查询真实模拟操作状态',response=operation_ref)
    route(PREFIX+'/operations/{operation_id}/verify','post','绑定具体红色操作的模拟额外验证',request='VerifyRequest',human=True)
    route(PREFIX+'/operations/{operation_id}/confirm','post','经用户确认后原子执行业务',request='ConfirmRequest',response=operation_ref,human=True)
    route(PREFIX+'/operations/{operation_id}/cancel','post','取消尚未提交的操作',request='EmptyRequest',response=operation_ref)
    contract={'openapi':'3.1.0','info':{'title':'Navren模拟银行API','version':'2.0.0',
                'description':'全部数据与业务为本机模拟；用户确认页与Agent使用不同权限。无真实银行接入。'},
              'servers':[{'url':'http://127.0.0.1:18766'}],'paths':paths,
              'components':{'securitySchemes':{'bearerAuth':{'type':'http','scheme':'bearer'}},'schemas':schemas}}
    (Path(__file__).parent/'openapi.json').write_text(json.dumps(contract,ensure_ascii=False,indent=2)+'\n')
    print(f'已生成{sum(len(v) for v in paths.values())}项HTTP接口合同。')


if __name__=='__main__':
    main()
