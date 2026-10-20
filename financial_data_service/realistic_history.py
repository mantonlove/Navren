"""独立三年情境历史：守住来源预算与期末，不修改十年构建器或现役库。"""
from __future__ import annotations
from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta
import calendar
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import uuid

START, END = '2023-10-04', '2026-10-03'
VERSION = 'realistic-history.v1'
SUBJECTS = {'tycoon', 'grain_farmer', 'working_landlady', 'retired_entrepreneur'}
INSTITUTIONS = {'工行合成连接':'中国工商银行','建行合成连接':'中国建设银行','农行合成连接':'中国农业银行',
 '邮储合成连接':'中国邮政储蓄银行','招行合成连接':'招商银行','农商行合成连接':'浙江农商联合银行',
 '中信证券合成连接':'中信证券','支付宝合成连接':'支付宝','微信支付合成连接':'微信支付','保险合成连接':'保险服务'}
MERCHANTS = {
 '餐饮堂食':['知味观','新白鹿餐厅','外婆家','兰州拉面'], '外卖':['美团外卖·邻里厨房','饿了么·家常饭','美团外卖·粥铺'],
 '饮品':['瑞幸咖啡','霸王茶姬','蜜雪冰城','星巴克'], '超市生鲜':['永辉超市','盒马鲜生','社区菜场','世纪联华'],
 '购物':['京东商城','天猫超市','银泰百货','优衣库'], '日用品':['世纪联华','京东自营','屈臣氏'],
 '打车':['滴滴出行','高德打车','曹操出行'], '公共交通':['杭州地铁','杭州公交'], '铁路出行':['铁路12306'],
 '燃油费':['中国石化加油站','中国石油加油站'], '车辆充电':['国家电网充电','特来电充电'],
 '药店购药':['海王星辰药房','老百姓大药房','益丰大药房'], '医院门诊':['市第一人民医院','社区卫生服务中心','口腔门诊'],
 '水电燃气':['国网浙江电力','杭州水务','杭州天然气'], '通信账单':['中国移动','中国电信','中国联通'],
 '休闲娱乐':['电影院','市文化中心','健身中心'], '家居采购':['宜家家居','京东家居','家居生活馆'],
 '家政服务':['家政服务中心','家庭保洁服务'], '赡养与家庭往来':['家庭成员','长辈生活费'],
 '旅行住宿':['携程旅行','酒店住宿','铁路12306'], '私人出行服务':['专车接送服务','私人行程服务','旅行社定制行程'],
 '物业服务':['物业服务中心'], '物业维修':['住房维修服务','机电维修工程','装修维护服务'],
 '农业投入':['丰禾农资经营部','种子服务中心','农资合作社'], '农业作业':['农业机械服务队','收获作业服务队'],
 '土地租赁':['土地流转服务中心'], '农机维护':['农机维修服务中心','农业设备配件商店'],
 '餐饮原料':['菜场批发配送','粮油批发商行','冻品配送中心'], '餐饮经营账单':['国网浙江电力','商用燃气服务','餐饮包装配送'],
 '经营工资':['门店员工工资'], '经营租金':['门店出租人'], '经营税费':['税务缴费'],
 '公益捐赠':['公益基金会'], '生活采购':['世纪联华','社区生活服务','家居生活馆'],
}
# 情境价格档位，不声明为商户实时菜单或实际票据。
DRINK_MENUS = {'蜜雪冰城':(400,500,600,800,1000,1200),
              '瑞幸咖啡':(990,1290,1590,1990,2200,2500),
              '霸王茶姬':(1800,2000,2300,2600,2800,3200,3500),
              '星巴克':(2800,3000,3200,3500,3800,4200)}


def priced_visits(category,count,budget,rng,wealthy=False):
    minimum=200 if category=='公共交通' else 400
    count=min(count,budget//minimum)
    visits=[];available=budget
    for index in range(count):
        ceiling=available-(count-index-1)*minimum
        if category=='公共交通':
            options=[(200,'杭州公交',{'fare_type':'普通公交票价情境','fare_minor':200})]
            options += [(fare*100,'杭州地铁',{'fare_type':'里程票价情境','fare_minor':fare*100}) for fare in range(2,9)]
        else:
            cups=2 if wealthy and rng.random()<.12 else 1
            options=[(price*cups,merchant,{'cups':cups,'unit_price_minor':price,'price_basis':'scenario_menu_band'})
                     for merchant,prices in DRINK_MENUS.items() for price in prices]
            if not any(value<=ceiling for value,_,_ in options):
                options=[(price,merchant,{'cups':1,'unit_price_minor':price,'price_basis':'scenario_menu_band'})
                         for merchant,prices in DRINK_MENUS.items() for price in prices]
        value,merchant,details=rng.choice([option for option in options if option[0]<=ceiling])
        visits.append((value,merchant,details));available-=value
    return visits


CAPS = {'饮品':8500,'外卖':38000,'公共交通':2000,'车辆充电':30000,'药店购药':50000,
        '燃油费':120000,'日用品':100000,'生活采购':50000,'铁路出行':300000}

def js(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def ro(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db

def clean(value):
    if not isinstance(value, str):
        return value
    for old, new in [('（合成演示）',''),('合成连接',''),('合成',''),('模拟',''),('演示',''),('SIM-','')]:
        value = value.replace(old, new)
    return value.strip() or '账户服务'

def seed(*parts):
    return random.Random(int(hashlib.sha256(js(parts).encode()).hexdigest(),16))

def split_exact(total, count, rng, cap=None, weights=None):
    if cap is not None:count=max(count,math.ceil(total/cap))
    count = max(1, min(count, total))
    weights = weights or [max(.15, math.exp(rng.gauss(0, .42))) for _ in range(count)]
    if len(weights)!=count:raise ValueError("权重与频次不匹配")
    floor = [max(1, int((total-count)*w/sum(weights))+1) for w in weights]
    remaining = total-sum(floor)
    for index in rng.sample(range(count), count):
        if remaining == 0:break
        add = 1 if remaining > 0 else -1
        if floor[index] + add > 0:
            floor[index] += add; remaining -= add
    floor[-1] += remaining
    if cap is not None:
        excess=sum(max(0,v-cap) for v in floor);floor=[min(v,cap) for v in floor]
        for i in rng.sample(range(count),count):
            take=min(excess,cap-floor[i]);floor[i]+=take;excess-=take
        if excess:raise ValueError('客单价上限不能满足预算')
    if min(floor) <= 0 or sum(floor) != total:raise ValueError('整分拆分失败')
    return floor

def month_days(month):
    first = date.fromisoformat(month+'-01')
    a, b = max(date.fromisoformat(START),first), min(date.fromisoformat(END),first.replace(day=calendar.monthrange(first.year,first.month)[1]))
    return [(a+timedelta(days=n)).isoformat() for n in range((b-a).days+1)]

def recipe(uid, parent, month):
    """频次/客单价先定义，再受真实来源预算约束；余款落到对应生活事项。"""
    wealthy = uid == 'tycoon'; farmer = uid == 'grain_farmer'; retired = uid == 'retired_entrepreneur'
    if '农业土地' in parent:return [], '土地租赁', 15000000
    if '农业种子' in parent:return [], '农业投入', 2000000
    if '农业雇工' in parent:return [], '农业作业', 3000000
    if '农业经营仓储' in parent:return [('餐饮经营账单',2,60000)], '农业投入', 200000
    if '农机年度' in parent:return [('燃油费',4,90000)], '农机维护', 250000
    if '原料与包装' in parent:return [], '餐饮原料', 25000
    if '自营水电' in parent:return [], '餐饮经营账单', 35000
    if '自营用工' in parent:return [], '经营工资', 260000
    if '自营门店' in parent:return [], '经营租金', 650000
    if '自营税费' in parent:return [], '经营税费', 80000
    if '物业维修' in parent:return [], '物业维修', 1500000 if wealthy else 180000
    if '物业管理' in parent:return [], '物业服务', 1500000 if wealthy else 18000
    if '公益' in parent:return [], '公益捐赠', 15000000
    if parent == '健康医疗':return [('药店购药',3,9000 if retired else 5500)], '医院门诊', 15000 if not wealthy else 150000
    if parent == '家庭往来':return [], '赡养与家庭往来', 150000 if wealthy else 30000
    if parent == '家居用品':return [], '家居采购', 300000 if wealthy else 20000
    if '家庭出行' in parent:return [], '旅行住宿', 250000 if wealthy else 75000
    if parent == '个人购物':return [], '购物', 250000 if wealthy else 8500
    if parent == '日用品':return [], '日用品', 20000 if wealthy else 2800
    if parent == '餐饮':return [('外卖',7 if not farmer else 3,3500 if not wealthy else 14000),('饮品',9 if not farmer else 5,2200 if not wealthy else 4800)], '餐饮堂食', 80000 if wealthy else 5200
    if parent == '休闲娱乐':return [], '休闲娱乐', 75000 if wealthy else 7000
    if parent == '交通':return [('铁路出行' if wealthy else '公共交通',1 if wealthy else 12 if not retired else 15,400 if not wealthy else 65000),('车辆充电',2,6000 if wealthy else 3200)], '私人出行服务' if wealthy else '打车', 500000 if wealthy else 3800
    if '交通通信' in parent or '通勤' in parent or '日常与交通' in parent:
        return [('铁路出行' if wealthy else '公共交通',1 if wealthy else 35 if uid=='working_landlady' else 10,400 if not wealthy else 65000),
                ('通信账单',1,18800),('燃油费',2 if not retired else 1,60000 if wealthy else 25000),
                ('车辆充电',2,7000 if wealthy else 3200),('打车',8 if wealthy else 4,25000 if wealthy else 3500),
                ('饮品',18 if uid=='working_landlady' else 7,2200 if not wealthy else 4800)], '私人出行服务' if wealthy else '生活采购', 3000000 if wealthy else 6500
    if '住房与保障' in parent:return [('水电燃气',3,25000),('物业服务',1,22000)], '家居采购', 18000
    if '家庭日常' in parent or '个人日常' in parent or '家庭生活' in parent:
        winter = int(month[-2:]) in (1,2,11,12)
        return [('超市生鲜',22 if farmer else 16 if retired else 18,45000 if wealthy else 9800 if farmer else 7800),
                ('餐饮堂食',9 if not retired else 5,120000 if wealthy else 9500 if farmer else 6800),
                ('外卖',3 if farmer else 5 if retired else 12,12000 if wealthy else 3400),
                ('饮品',5 if farmer else 4 if retired else 15,4800 if wealthy else 1800),
                ('药店购药',4 if retired and winter else 2,18000 if retired else 4500),
                ('水电燃气',3,85000 if wealthy else 18000),('日用品',6,25000 if wealthy else 3700),
                ('家政服务',8 if wealthy else 2,500000 if wealthy else 20000)], '赡养与家庭往来' if wealthy else '生活采购', 3000000 if wealthy else 12000
    return [], clean(parent), 30000

def _clock(category, rng):
    if category in ('餐饮堂食','外卖'):
        hour = rng.choice([11,12,18,19]);minute=rng.randrange(8,57)
    elif category=='饮品':hour=rng.choice([8,9,14,15]);minute=rng.randrange(60)
    elif category=='公共交通':hour=rng.choice([7,8,17,18]);minute=rng.randrange(60)
    elif category=='打车':hour=rng.choice([7,8,18,21,22]);minute=rng.randrange(60)
    else:hour=rng.randrange(9,22);minute=rng.randrange(60)
    return hour,minute,rng.randrange(60)

def _stamp(day, category, rng):
    h,m,s=_clock(category,rng)
    return f'{day}T{h:02}:{m:02}:{s:02}+08:00'

def _choose_day(days, category, rng):
    weekend = category in ('购物','餐饮堂食','休闲娱乐','超市生鲜','家居采购','旅行住宿')
    weekday = category in ('公共交通','饮品')
    weights=[]
    for day in days:
        is_weekend=date.fromisoformat(day).weekday()>=5
        weights.append(2.0 if weekend and is_weekend or weekday and not is_weekend else .55 if weekday else 1.0)
    return rng.choices(days, weights=weights)[0]

class HistoryBuilder:
    def __init__(self, source, target):
        self.source,self.target=source,target
        self.rows=[];self.budgets=[];self.transfers=[];self.seq=0
        self.accounts={r['account_id']:dict(r) for r in source.execute('SELECT * FROM accounts')}
        self.parts=defaultdict(list);self.day_counts=defaultdict(int)

    def schedule(self, uid, days, category, count, rng):
        if category=='公共交通':
            eligible=[d for d in days if date.fromisoformat(d).weekday()<5]
            rng.shuffle(eligible)
            slots=[d for d in eligible for _ in range(max(0,2-self.day_counts[(uid,d,category)]))]
            selected=slots[:count]
            if len(selected)!=count:raise ValueError('通勤频次超出工作日早晚容量')
        else:
            selected=[]
            for _ in range(count):
                available=[d for d in days if self.day_counts[(uid,d,category)]+selected.count(d)<(3 if category in ('生活采购','购物','餐饮堂食','赡养与家庭往来','私人出行服务') else 2)]
                if not available:raise ValueError('生活消费频次超出每日上限 '+uid+' '+category)
                selected.append(_choose_day(available,category,rng))
        result=[]
        for day in selected:
            ordinal=self.day_counts[(uid,day,category)];self.day_counts[(uid,day,category)]+=1
            stamp=_stamp(day,category,rng)
            if category=='公共交通':stamp=day+f'T{7 if ordinal%2==0 else 18:02}:{rng.randrange(15,55):02}:{rng.randrange(60):02}+08:00'
            result.append((day,stamp))
        return result

    def _new(self, original, amount, category, merchant, stamp, budget, kind=None, expense=None):
        self.seq+=1
        row=dict(original)
        old_details=json.loads(row['details']) if isinstance(row['details'],str) else row['details']
        row.update(transaction_id=original['subject_id']+'::life:'+f'{self.seq:08d}',occurred_at=stamp,booked_on=stamp[:10],
                   amount_minor=amount,signed_minor=amount if original['signed_minor']>0 else -amount,
                   category=category,counterparty=merchant,kind=kind or original['kind'],transfer_group_id=None,
                   original_transaction_id=None,source_transaction_id=None,source_event_id=original.get('source_event_id'),
                   details={**old_details,'source':'navren_synthetic','scenario_version':VERSION,'budget_id':budget,
                            'original_category':original['category'],'legacy_source_transaction_ids':[]})
        row['expense_minor']=expense if expense is not None else amount if row['kind'] in ('expense','insurance_premium') else -amount if row['kind']=='refund' else original['expense_minor']
        if row['kind']=='expense' and category=='餐饮堂食':
            if amount<=5000:
                row['counterparty']=seed(row['transaction_id']).choice(['兰州拉面','沙县小吃','社区快餐店'])
                diners=1
            elif amount<=60000:
                row['counterparty']=seed(row['transaction_id']).choice(['知味观','新白鹿餐厅','外婆家'])
                diners=max(1,math.ceil(amount/12000))
            else:
                row['counterparty']=seed(row['transaction_id']).choice(['西湖宴请餐厅','酒店餐饮服务'])
                diners=max(4,math.ceil(amount/22000))
            row['details'].update(meal_party_size=diners,
                meal_occasion='个人用餐' if diners==1 else '家庭聚餐' if row['scope']=='household' else '朋友聚餐',
                price_basis='scenario_bill_and_party_size')
        financial = row['kind'] in ('investment_purchase','investment_redemption','loan_repayment','insurance_premium')
        row['economic_event_id']=row['transaction_id']
        row['details']['original_economic_event_id']=original['economic_event_id']
        if not financial:row['source_event_id']=None
        self.rows.append(row)
        if row['kind']=='expense':self.parts[budget].append(row)
        return row

    def prepare(self):
        grouped=defaultdict(list)
        original_rows=[dict(r) for r in self.source.execute('SELECT * FROM transactions WHERE booked_on BETWEEN ? AND ? ORDER BY occurred_at,transaction_id',(START,END))]
        transfer_groups=defaultdict(list)
        for row in original_rows:
            if row['kind']=='internal_transfer':transfer_groups[row['transfer_group_id']].append(row)
            else:grouped[(row['subject_id'],row['booked_on'][:7],row['account_id'],row['scope'],row['category'],row['kind'])].append(row)
        for key, rows in sorted(grouped.items()):
            uid,month,account,scope,parent,kind=key
            ident='budget-'+hashlib.sha256(js(key).encode()).hexdigest()[:24]
            amount=sum(r['amount_minor'] for r in rows);signed=sum(r['signed_minor'] for r in rows);expense=sum(r['expense_minor'] for r in rows)
            self.budgets.append((ident,uid,month,account,scope,parent,kind,amount,signed,expense,js([r['transaction_id'] for r in rows])))
            if kind=='refund':continue
            base=rows[0];rng=seed(VERSION,*key);days=month_days(month);group_start=len(self.rows)
            if kind in ('investment_purchase','investment_redemption','loan_repayment','insurance_premium'):
                for original in rows:
                    category={'investment_purchase':'基金申购','investment_redemption':'基金赎回','loan_repayment':'贷款还款','insurance_premium':'保险保费'}[kind]
                    made=self._new(original,original['amount_minor'],category,clean(original['counterparty']),original['occurred_at'],ident,expense=original['expense_minor'])
                    made['details']['legacy_source_transaction_ids']=[original['transaction_id']]
            elif kind=='income':
                if parent=='自营餐饮回款':
                    # 午市/晚市营业，每日不同，不把原月结一笔重复当收入。
                    slots=[(day,part) for day in days for part in range(2 if date.fromisoformat(day).weekday()<5 else 3)]
                    weights=[(1.3 if date.fromisoformat(day).weekday()>=5 else 1.0)*rng.uniform(.65,1.35) for day,_ in slots]
                    amounts=split_exact(amount,len(slots),rng,weights=weights)
                    for (day,part),value in zip(slots,amounts):
                        stamp=f'{day}T{[13,20,21][part]:02}:{rng.randrange(20,55):02}:{rng.randrange(60):02}+08:00'
                        made=self._new(base,value,'餐饮经营收款',rng.choice(['微信支付商户结算','支付宝商户结算']),stamp,ident,expense=0)
                        made['details']['settlement_batch']='午市' if part==0 else '晚市'
                elif parent=='工资收入' and month[-2:]=='01':
                    bonus=amount//5
                    self._new(base,amount-bonus,'工资','所在单位工资代发',month+'-25T09:05:00+08:00',ident,expense=0)
                    self._new(base,bonus,'年终奖金','所在单位奖金代发',month+'-28T09:10:00+08:00',ident,expense=0)
                else:
                    names={'工资收入':('工资','所在单位工资代发'),'退休金收入':('养老金','养老金代发'),
                           '存款利息':('存款利息','银行利息入账'),'粮食销售回款':('粮食销售回款','粮食收购企业'),
                           '农机作业服务回款':('农机作业服务回款','农业服务合作社'),'农业经营补贴':('农业经营补贴','农业补贴拨付')}
                    for original in rows:
                        cat,merchant=names.get(parent,(clean(parent),clean(original['counterparty'])))
                        self._new(original,original['amount_minor'],cat,merchant,original['occurred_at'],ident,expense=0)
            else:
                specs,remainder_category,mean=recipe(uid,parent,month)
                available=amount
                seasonal=1.15 if month[-2:] in ('01','02','11','12') else 1.0
                for category,count,price in specs:
                    desired=max(1,round(count*len(days)/calendar.monthrange(int(month[:4]),int(month[-2:]))[1]*rng.uniform(.65,1.35)))
                    cap_daily=3 if category in ('生活采购','购物','餐饮堂食','赡养与家庭往来','私人出行服务') else 2
                    desired=min(desired,sum(max(0,cap_daily-self.day_counts[(uid,d,category)]) for d in days if category!='公共交通' or date.fromisoformat(d).weekday()<5))
                    budget=min(available//2,round(desired*price*rng.uniform(.85,1.15)*seasonal))
                    if budget<300 or desired==0:continue
                    desired=min(desired,max(1,budget//max(300,price//3)))
                    if category in ('公共交通','饮品'):
                        visits=priced_visits(category,desired,budget,rng,wealthy=uid=='tycoon')
                        budget=sum(value for value,_,_ in visits)
                        for (value,merchant,extra),(day,stamp) in zip(visits,self.schedule(uid,days,category,len(visits),rng)):
                            made=self._new(base,value,category,merchant,stamp,ident)
                            made['details'].update(extra)
                        available-=budget
                        continue
                    amounts=split_exact(budget,desired,rng,CAPS.get(category))
                    for value,(day,stamp) in zip(amounts,self.schedule(uid,days,category,len(amounts),rng)):
                        merchant=rng.choice(MERCHANTS.get(category,[clean(parent)+'服务']))
                        if category=='餐饮堂食' and value>100000:merchant=rng.choice(['西湖宴请餐厅','酒店餐饮服务'])
                        self._new(base,value,category,merchant,stamp,ident)
                    available-=budget
                if available:
                    count=max(1,min(85,round(available/mean*rng.uniform(.9,1.12))))
                    # 农业大额、物业/原料合同按实际采购月份及原凭证日期分批。
                    business=scope=='self_employed' and parent!='自营餐饮原料与包装投入'
                    if '物业维修' in parent:count=min(count,4)
                    if not business:
                        cap_daily=3 if remainder_category in ('生活采购','购物','餐饮堂食','赡养与家庭往来','私人出行服务') else 2
                        count=min(count,sum(max(0,cap_daily-self.day_counts[(uid,d,remainder_category)]) for d in days))
                        if not count:raise ValueError('来源预算没有合理频次容量 '+uid+' '+month+' '+parent)
                    amounts=split_exact(available,count,rng,CAPS.get(remainder_category))
                    times=[(rng.choice(rows)['booked_on'],None) for _ in amounts] if business or '公益' in parent else self.schedule(uid,days,remainder_category,len(amounts),rng)
                    for value,(day,stamp) in zip(amounts,times):
                        merchant=rng.choice(MERCHANTS.get(remainder_category,[clean(parent)+'服务']))
                        if remainder_category=='餐饮堂食' and value>100000:merchant=rng.choice(['西湖宴请餐厅','酒店餐饮服务'])
                        self._new(base,value,remainder_category,merchant,stamp or _stamp(day,remainder_category,rng),ident)
            for made in self.rows[group_start:]:
                if made['details']['budget_id']==ident and not made['details']['legacy_source_transaction_ids']:
                    made['details']['legacy_source_transaction_ids']=[r['transaction_id'] for r in rows]
                    made['details']['source_event_ids']=[r['source_event_id'] for r in rows if r['source_event_id']]
        # 退款选择同主体、同原分类、同用途与原支付账户的购买分项；累计不超过购买额。
        refunded=defaultdict(int)
        for key, rows in sorted(grouped.items()):
            uid,month,account,scope,parent,kind=key
            if kind!='refund':continue
            ident='budget-'+hashlib.sha256(js(key).encode()).hexdigest()[:24]
            for original in rows:
                amount=original['amount_minor'];day=original['booked_on']
                eligible=[r for r in self.rows if r['kind']=='expense' and r['subject_id']==uid and r['account_id']==account and r['scope']==scope and r['details']['original_category']==parent and r['occurred_at']<=day+'T22:10:00+08:00']
                if not eligible:
                    eligible=[r for r in self.rows if r['kind']=='expense' and r['subject_id']==uid and r['account_id']==account and r['scope']==scope and r['details']['original_category']==parent and r['booked_on'][:7]==month]
                    if eligible:
                        first=min(eligible,key=lambda r:r['occurred_at']);first.update(booked_on=day,occurred_at=day+'T09:05:00+08:00')
                eligible.sort(key=lambda r:(r['booked_on'][:7]!=month,-r['amount_minor'],r['transaction_id']))
                for purchase in eligible:
                    take=min(amount,purchase['amount_minor']-refunded[purchase['transaction_id']])
                    if take<=0:continue
                    made=self._new(original,take,purchase['category'],purchase['counterparty']+'退款',day+'T22:10:00+08:00',ident,expense=-take)
                    made['original_transaction_id']=purchase['transaction_id'];made['details'].update(refund_relationship='reconstructed_purchase_reference',legacy_source_transaction_ids=[original['transaction_id']])
                    refunded[purchase['transaction_id']]+=take;amount-=take
                    if not amount:break
                if amount:raise ValueError('退款缺少可关联购买分项 '+original['transaction_id'])
        for group, legs in transfer_groups.items():
            if len(legs)!=2 or sum(r['signed_minor'] for r in legs)!=0:raise ValueError('原转账配对不完整')
            outgoing=next(r for r in legs if r['signed_minor']<0);incoming=next(r for r in legs if r['signed_minor']>0)
            self.transfers.append({'key':group,'month':outgoing['booked_on'][:7],'from':outgoing['account_id'],'to':incoming['account_id'],
                                   'amount':outgoing['amount_minor'],'remaining':outgoing['amount_minor'],'stamp':outgoing['occurred_at'],
                                   'reason':clean(outgoing['counterparty']),'scope':outgoing['scope'],'subject_id':outgoing['subject_id'],
                                   'original_ids':[r['transaction_id'] for r in legs]})

    def replay(self, bridge_plan=None, discover=False):
        initial={r['account_id']:r['balance_minor'] for r in self.source.execute('SELECT * FROM account_balances WHERE date=?',((date.fromisoformat(START)-timedelta(days=1)).isoformat(),))}
        balances=dict(initial);posted=set();result=[];snapshots=[];fund_parts=defaultdict(int);rescheduled=0
        clocks={};posting_sequence=0;bridge_needs=Counter();bridge_records=[]
        month_plans=defaultdict(list);incoming_plans=defaultdict(list)
        for plan in self.transfers:
            month_plans[plan['month']].append(plan);incoming_plans[(plan['month'],plan['to'])].append(plan)
        def post(row, stamp):
            nonlocal posting_sequence
            day=stamp[:10];desired=datetime.fromisoformat(stamp)
            clock=clocks.get(day)
            if clock is not None:desired=max(desired,clock+timedelta(seconds=1))
            if desired.date().isoformat()!=day:raise ValueError('日内交易容量超限 '+day)
            clocks[day]=desired;posting_sequence+=1
            row.update(occurred_at=desired.isoformat(),booked_on=day)
            balances[row['account_id']]+=row['signed_minor']
            row['details'].update(booked_sequence=posting_sequence,balance_after_minor=balances[row['account_id']])
            result.append(row)
            posted.add(row['transaction_id'])
        def move(plan,amount,stamp):
            if not amount:return
            if balances[plan['from']]<amount:raise ValueError('调拨资金不足 '+plan['key']+' '+plan['from']+' available='+str(balances[plan['from']])+' need='+str(amount))
            fund_parts[plan['key']]+=1;group=plan['key']+'::life:'+str(fund_parts[plan['key']])
            base={'subject_id':plan['subject_id'],'status':'booked','currency':'CNY','counterparty':plan['reason'],'category':plan['reason'],
                  'kind':'internal_transfer','scope':plan['scope'],'card_id':None,'contract_id':None,'mandate_id':None,
                  'original_transaction_id':None,'transfer_group_id':group,'source_event_id':None,'source_transaction_id':None,
                  'economic_event_id':group,'expense_minor':0,'details':{'source':'navren_synthetic','scenario_version':VERSION,
                  'original_transfer_group_id':plan['key'],'legacy_source_transaction_ids':plan['original_ids'],'expense_counted':False,
                  'schedule_reason':'原已存在的资金安排随实际付款分次拨付，无新增收入'}}
            if plan.get('bridge_id'):
                base['details'].update(original_transfer_group_id=None,history_bridge_id=plan['bridge_id'],temporary_advance=True,bridge_direction=plan['bridge_direction'],schedule_reason='月初同用途范围现金备用金，月末原额归还，无收入费用和利息')
            for account,signed,suffix in [(plan['from'],-amount,'out'),(plan['to'],amount,'in')]:
                row={**base,'details':dict(base['details']),'transaction_id':group+':'+suffix,'account_id':account,'amount_minor':amount,'signed_minor':signed,'occurred_at':stamp,'booked_on':stamp[:10]}
                post(row,stamp)
            plan['remaining']-=amount
        def fund(account,need,stamp,month,visited=None):
            visited=set(visited or ())
            if account in visited:return
            visited.add(account)
            for plan in sorted((p for p in incoming_plans[(month,account)] if p['remaining'] and p['from'] not in visited),key=lambda p:p['stamp']):
                shortage=need-balances[account]
                if shortage<=0:return
                source=plan['from'];want=min(shortage,plan['remaining'])
                if self.accounts[source]['account_type']=='credit_card':continue
                fund(source,want,stamp,month,visited)
                take=min(want,max(0,balances[source]))
                move(plan,take,stamp)
        byday=defaultdict(list)
        for row in self.rows:byday[row['booked_on']].append(row)
        pending=[];transit_posted=Counter();life_posted=Counter()
        def allowed(row,day):
            if row['kind']!='expense' or row['scope']=='self_employed':return True
            category=row['category']
            maximum=3 if category in ('生活采购','购物','餐饮堂食','赡养与家庭往来','私人出行服务') else 2
            return life_posted[(row['subject_id'],day,category)]<maximum
        def effective_stamp(row,day):
            if row['category']=='公共交通' and row['booked_on']!=day:
                scheduled=[r for r in byday[day] if r['category']=='公共交通' and r['subject_id']==row['subject_id']]
                hour=18 if any(r['occurred_at'][11:13] in ('07','08') for r in scheduled) else 7
                return day+f'T{hour:02}:'+row['occurred_at'][14:]
            return day+row['occurred_at'][10:]
        for day in ((date.fromisoformat(START)+timedelta(days=n)).isoformat() for n in range((date.fromisoformat(END)-date.fromisoformat(START)).days+1)):
            month=day[:7]
            first=day==START or day.endswith('-01')
            if first and (not discover or bridge_plan):
                for (planned_month,account),amount in sorted((bridge_plan or {}).items()):
                    if planned_month!=month or not amount:continue
                    borrower=self.accounts[account]
                    donors=[i for i,a in self.accounts.items() if i!=account and a['subject_id']==borrower['subject_id'] and a['currency']==borrower['currency'] and a['scope']==borrower['scope'] and a['account_type']!='credit_card' and balances[i]>=amount]
                    if not donors:raise ValueError('月初备用金缺少同主体同用途实际现金 '+month+' '+account+' '+str(amount))
                    donor=max(donors,key=lambda i:(self.accounts[i]['source_pool_id']==borrower['source_pool_id'],balances[i],i))
                    ident=borrower['subject_id']+'::bridge:'+month+':'+account.rsplit(':',1)[-1]
                    plan={'key':ident+':advance','subject_id':borrower['subject_id'],'scope':borrower['scope'],'from':donor,'to':account,'amount':amount,'remaining':amount,'reason':'月初本人备用金调拨','original_ids':[],'bridge_id':ident,'bridge_direction':'advance'}
                    move(plan,amount,day+'T00:05:00+08:00')
                    bridge_records.append({'bridge_id':ident,'month':month,'subject_id':borrower['subject_id'],'scope':borrower['scope'],'donor':donor,'borrower':account,'amount_minor':amount,'from_pool':self.accounts[donor]['source_pool_id'],'to_pool':borrower['source_pool_id']})
            rows=pending+byday[day];pending=[]
            rows.sort(key=lambda r:(effective_stamp(r,day),r['transaction_id']))
            for row in rows:
                account=row['account_id'];amount=row['amount_minor'];typ=self.accounts[account]['account_type']
                if not allowed(row,day):pending.append(row);continue
                if row['kind']=='refund' and row['original_transaction_id'] not in posted:
                    pending.append(row);continue
                stamp=effective_stamp(row,day)
                if row['category']=='公共交通':
                    reserved=sum(r['category']=='公共交通' and r['subject_id']==row['subject_id'] for r in byday[day]) if row['booked_on']!=day else 0
                    if date.fromisoformat(day).weekday()>=5 or transit_posted[(row['subject_id'],day)]+reserved>=2:
                        pending.append(row);continue
                if row['signed_minor']<0 and typ!='credit_card':
                    fund(account,amount,stamp,month)
                    if balances[account]<amount:
                        if discover:
                            need=amount-balances[account];bridge_needs[(month,account)]+=need;balances[account]+=need
                        else:
                            pending.append(row);continue
                if row['booked_on']!=day:
                    row['details']['planned_occurred_at']=row['occurred_at'];rescheduled+=1
                post(row,stamp)
                if row['kind']=='expense':life_posted[(row['subject_id'],day,row['category'])]+=1
                if row['category']=='公共交通':transit_posted[(row['subject_id'],day)]+=1
            # 旧计划调拨只能使用已有资金；钱包经营收款随营业结算，不提前透支。
            for plan in sorted((p for p in month_plans[month] if p['remaining'] and p['stamp'][:10]<=day),key=lambda p:p['stamp']):
                source=plan['from'];amount=min(plan['remaining'],max(0,balances[source]))
                move(plan,amount,day+'T23:40:00+08:00')
            last=day==END or date.fromisoformat(day).day==calendar.monthrange(int(day[:4]),int(day[5:7]))[1]
            if last:
                # 用当月已到的资金完成被延后的真实事项；不跨月借未来收入。
                for iteration in range(12):
                    progress=False
                    stamp=day+f'T23:{41+iteration:02}:00+08:00'
                    for row in list(pending):
                        account=row['account_id'];typ=self.accounts[account]['account_type'];amount=row['amount_minor']
                        if not allowed(row,day):continue
                        if row['kind']=='refund' and row['original_transaction_id'] not in posted:continue
                        if row['signed_minor']<0 and typ!='credit_card':fund(account,amount,stamp,month)
                        if row['signed_minor']<0 and typ!='credit_card' and balances[account]<amount:continue
                        row['details']['planned_occurred_at']=row['occurred_at'];rescheduled+=1
                        post(row,stamp)
                        if row['kind']=='expense':life_posted[(row['subject_id'],day,row['category'])]+=1
                        pending.remove(row);progress=True
                    for plan in (p for p in month_plans[month] if p['remaining']):
                        amount=min(plan['remaining'],max(0,balances[plan['from']]))
                        if amount:move(plan,amount,stamp);progress=True
                    if not progress:break
                if pending:raise ValueError('月内未完成事项 '+day+' '+js([{'account':r['account_id'],'category':r['category'],'scope':r['scope'],'planned':r['occurred_at'],'amount':r['amount_minor'],'balance':balances[r['account_id']],'posted_today':life_posted[(r['subject_id'],day,r['category'])]} for r in pending[:10]]))
                if any(p['remaining'] for p in self.transfers if p['month']==month):raise ValueError('原资金安排月内无法完成 '+day)
                if discover:
                    for (m,account),amount in bridge_needs.items():
                        if m==month:balances[account]-=amount
                for bridge in reversed(bridge_records):
                    if bridge['month']!=month:continue
                    plan={'key':bridge['bridge_id']+':repayment','subject_id':bridge['subject_id'],'scope':bridge['scope'],'from':bridge['borrower'],'to':bridge['donor'],'amount':bridge['amount_minor'],'remaining':bridge['amount_minor'],'reason':'月末本人备用金归还','original_ids':[],'bridge_id':bridge['bridge_id'],'bridge_direction':'repayment'}
                    move(plan,bridge['amount_minor'],day+'T23:56:00+08:00')
                expected={r['account_id']:r['balance_minor'] for r in self.source.execute('SELECT * FROM account_balances WHERE date=?',(day,))}
                if balances!=expected:raise ValueError('月末账户余额不一致 '+day)
            for account,value in balances.items():
                if self.accounts[account]['account_type']!='credit_card' and value<0:raise ValueError('现金负余额')
                snapshots.append((self.accounts[account]['subject_id'],account,day,value))
        self.rows=result
        if self.target is not None:
            self.target.executemany('INSERT INTO account_balances VALUES(?,?,?,?)',snapshots)
            for account,opening in initial.items():
                self.target.execute('UPDATE accounts SET opening_balance_minor=? WHERE account_id=?',(opening,account))
        late=sum(r['kind'] not in ('internal_transfer','income') and r['occurred_at'][11:16]>='23:40' for r in result)
        return {'bridge_plan':bridge_needs if discover else None,'monthly_cash_bridges':bridge_records,'rescheduled_for_available_funds':rescheduled,'late_postings':late,'economic_rows':sum(r['kind']!='internal_transfer' for r in result),'opening_reconstructed':False,'income_added_for_shortfall':False,'monthly_account_endpoints_preserved':True}

    def write(self):
        keys=[r[1] for r in self.target.execute('PRAGMA table_info(transactions)')]
        for row in self.rows:row['details']=js(row['details'])
        self.target.executemany('INSERT INTO transactions('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[[row[k] for k in keys] for row in self.rows])
        self.target.executemany('INSERT INTO history_budget_controls VALUES(?,?,?,?,?,?,?,?,?,?,?)',self.budgets)
        self.target.executemany('INSERT INTO history_lineage VALUES(?,?)',[(r['transaction_id'],json.loads(r['details']).get('budget_id')) for r in self.rows])

def build_realistic_history(source_path,output_path):
    source_path,output_path=Path(source_path).expanduser().resolve(),Path(output_path).expanduser().resolve()
    if source_path==output_path or output_path.exists():raise ValueError('候选必须为新路径，禁止覆盖来源或旧成果')
    output_path.parent.mkdir(parents=True,exist_ok=True)
    source_hash=sha(source_path);temp=output_path.with_name(output_path.name+'.building-'+uuid.uuid4().hex)
    with closing(ro(source_path)) as source:
        metadata=dict(source.execute('SELECT key,value FROM metadata'))
        if metadata.get('source')!='navren_synthetic' or metadata.get('end_date')!=END:raise ValueError('来源不是批准的截止日财务版本')
        if {r[0] for r in source.execute('SELECT subject_id FROM subjects')}!=SUBJECTS:raise ValueError('来源不是完整四体验主体')
        with closing(sqlite3.connect(temp)) as target:
            target.row_factory=sqlite3.Row;source.backup(target)
            target.execute('PRAGMA foreign_keys=ON');target.execute('BEGIN');target.execute('PRAGMA defer_foreign_keys=ON')
            target.execute('DELETE FROM transactions');target.execute('DELETE FROM account_balances')
            for table in ('valuations','loan_balances'):
                target.execute('DELETE FROM '+table+' WHERE date<? OR date>?',(START,END))
            target.execute("DELETE FROM source_links WHERE target_type IN ('transaction','transactions')")
            target.execute('CREATE TABLE history_budget_controls(budget_id TEXT PRIMARY KEY,subject_id TEXT,month TEXT,account_id TEXT,scope TEXT,original_category TEXT,kind TEXT,amount_minor INTEGER,signed_minor INTEGER,expense_minor INTEGER,source_transaction_ids TEXT)')
            target.execute('CREATE TABLE history_lineage(transaction_id TEXT PRIMARY KEY,budget_id TEXT REFERENCES history_budget_controls(budget_id))')
            builder=HistoryBuilder(source,target);builder.prepare()
            bridge_plan=Counter()
            for refinement in range(8):
                trial=HistoryBuilder(source,None);trial.rows=deepcopy(builder.rows);trial.transfers=deepcopy(builder.transfers)
                additional=trial.replay(bridge_plan=bridge_plan,discover=True)['bridge_plan']
                if not additional:break
                bridge_plan.update(additional)
            else:raise ValueError('备用金规划未在有限迭代内收敛，禁止发布')
            replay=builder.replay(bridge_plan=bridge_plan);builder.write()
            replay.pop('bridge_plan',None)
            for table,columns in [('connections',['institution_name']),('accounts',['name']),('assets',['name']),('contracts',['name']),('mandates',['merchant'])]:
                for row in target.execute('SELECT rowid,* FROM '+table).fetchall():
                    for column in columns:
                        value=INSTITUTIONS.get(row[column],clean(row[column])) if column=='institution_name' else clean(row[column])
                        target.execute('UPDATE '+table+' SET '+column+'=? WHERE rowid=?',(value,row['rowid']))
            for row in target.execute('SELECT rowid,details FROM policies').fetchall():
                details=json.loads(row['details'])
                if 'policy_number' in details:
                    details['original_policy_number']=details['policy_number']
                    details['policy_number']='PN-'+hashlib.sha256(details['policy_number'].encode()).hexdigest()[:12].upper()
                    target.execute('UPDATE policies SET details=? WHERE rowid=?',(js(details),row['rowid']))
            for row in target.execute('SELECT rowid,masked_number FROM cards').fetchall():
                target.execute('UPDATE cards SET masked_number=? WHERE rowid=?',(clean(row['masked_number']),row['rowid']))
            code={p.name:sha(p) for p in [Path(__file__),Path(__file__).with_name('validate_realistic.py')] if p.exists()}
            version='navren-life-'+hashlib.sha256(js({'source':source_hash,'code':code,'start':START,'end':END,'scenario':VERSION}).encode()).hexdigest()[:16]
            updates={'dataset_version':version,'start_date':START,'end_date':END,'historical_parent_dataset_version':metadata['dataset_version'],
                     'historical_parent_path':str(source_path),'historical_parent_sha256':source_hash,'scenario_version':VERSION,
                     'history_reconstruction':'source_budget_constrained_life_schedules','history_profile':'realistic_three_year',
                     'display_disclosure':'entry_notice','source':'navren_synthetic'}
            target.executemany('INSERT OR REPLACE INTO metadata VALUES(?,?)',updates.items())
            if target.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('候选外键不完整')
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('候选数据库不完整')
            target.commit()
    if sha(source_path)!=source_hash:raise ValueError('生成期间来源变动')
    with temp.open('rb') as f:os.fsync(f.fileno())
    temp.chmod(0o600);os.replace(temp,output_path)
    with closing(ro(output_path)) as db:
        count=db.execute('SELECT count(*) FROM transactions').fetchone()[0]
    return {'database_path':str(output_path),'database_sha256':sha(output_path),'dataset_version':version,
            'parent_path':str(source_path),'parent_sha256':source_hash,'parent_dataset_version':metadata['dataset_version'],
            'coverage':{'start':START,'end':END,'days':1096},'transactions':count,'source':'navren_synthetic',
            'runtime_enabled':False,'existing_bank_modified':False,'source_unchanged':True,**replay}
