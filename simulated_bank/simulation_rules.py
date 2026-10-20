"""纯生活情境规划器候选：无 IO，不记账，不读取余额，不执行金融操作。

plan_day 返回当地完整一天的计划，可能含当前时刻之后的事件。调用者必须
过滤 scheduled_at <= now，且不能因首次读取全天计划就把当天标为完成。
RULES_VERSION 仅留证，不能进入事件幂等键。默认金额是模拟设定，不是票据。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import date
import hashlib
import json
import random
from typing import Mapping


RULES_VERSION = "navren.life-rules.v1"
PERSONA_IDS = ("tycoon", "grain_farmer", "working_landlady", "retired_entrepreneur")
ACCOUNT_ROLES = frozenset({"personal_daily", "business", "salary", "rent", "wallet"})
EVENT_FIELDS = frozenset({"rule_id", "scheduled_at", "kind", "category", "merchant",
                          "amount_minor", "account_role", "occurrence_index"})
_ALL_DAYS = tuple(range(7))
_OPEN_DAYS = (1, 2, 3, 4, 5, 6)  # 餐饮经营情境：周一休息；不是法定节假日判断。
_OVERRIDES = frozenset({"enabled", "amount_minor", "amount_range_minor", "probability_bps",
                        "day_of_month", "months", "weekday_only", "monthly_occurrences"})
_MAX_AMOUNT = 100_000_000_000  # 只限制配置安全范围；不是银行交易或许可限额。


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    kind: str
    category: str
    merchants: tuple[str, ...]
    amount_range: tuple[int, int]
    account_role: str = "personal_daily"
    weekday_bps: int = 10_000
    weekend_bps: int = 10_000
    day_of_month: int | None = None
    months: tuple[int, ...] = tuple(range(1, 13))
    weekdays: tuple[int, ...] = _ALL_DAYS
    hours: tuple[int, ...] = tuple(range(9, 22))
    occurrences: int = 1
    price_points: tuple[int, ...] = ()
    enabled: bool = True
    monthly_occurrences: int | None = None


def _daily(key, category, merchants, low, high, weekday_bps, weekend_bps, **kw):
    return _Rule(key, "expense", category, tuple(merchants), (low, high),
                 weekday_bps=weekday_bps, weekend_bps=weekend_bps, **kw)


def _periodic(key, kind, category, merchant, amount, day, *, role="personal_daily",
              months=tuple(range(1, 13))):
    return _Rule(key, kind, category, (merchant,), (amount, amount), role,
                 day_of_month=day, months=tuple(months), hours=(9,))


_FOOD = ("知味观", "新白鹿餐厅", "外婆家")
_QUICK_FOOD = ("兰州拉面", "沙县小吃", "社区快餐店")
_MARKET = ("永辉超市", "盒马鲜生", "社区菜场", "世纪联华")
_SHOPPING = ("京东商城", "天猫超市", "银泰百货", "优衣库")
_DELIVERY = ("美团外卖·邻里厨房", "饿了么·家常饭", "美团外卖·粥铺")
_PHARMACY = ("海王星辰药房", "老百姓大药房", "益丰大药房")
# 每个价格及其商户对应原三年情境菜单；不是实时菜单或商户报价。
_DRINKS = (("蜜雪冰城", (400, 500, 600, 800, 1000, 1200)),
           ("瑞幸咖啡", (990, 1290, 1590, 1990, 2200, 2500)),
           ("霸王茶姬", (1800, 2000, 2300, 2600, 2800, 3200, 3500)),
           ("星巴克", (2800, 3000, 3200, 3500, 3800, 4200)))


def _drink(prob_week, prob_end, *, wealthy=False):
    # 商户与菜单价格在 plan_day 内联动选择，不从区间中虚构单杯价格。
    return _daily("life.drink", "饮品", tuple(m for m, _ in _DRINKS), 400, 8400 if wealthy else 4200,
                  prob_week, prob_end, hours=(8, 9, 14, 15),
                  price_points=tuple(p * cups for _, menu in _DRINKS for p in menu
                                     for cups in ((1, 2) if wealthy else (1,))))


_COMMON = {
    "tycoon": (
        _daily("life.meal", "餐饮堂食", ("西湖宴请餐厅", "酒店餐饮服务"), 30000, 160000,
               3500, 6500, hours=(11, 12, 18, 19)),
        _daily("life.groceries", "超市生鲜", _MARKET, 35000, 150000, 5000, 7000),
        _daily("life.delivery", "外卖", _DELIVERY, 4500, 16000, 2500, 3500, hours=(11, 12, 18)),
        _drink(5000, 4500, wealthy=True),
        _daily("life.shopping", "购物", _SHOPPING, 300000, 1800000, 900, 2100),
        _daily("life.travel", "私人出行服务", ("专车接送服务", "私人行程服务"),
               80000, 420000, 2800, 2200, hours=(8, 10, 16, 20)),
        _daily("life.housekeeping", "家政服务", ("家政服务中心", "家庭保洁服务"),
               150000, 450000, 2300, 3000),
        _daily("life.pharmacy", "药店购药", _PHARMACY, 4000, 38000, 500, 700),
        _daily("life.leisure", "休闲娱乐", ("市文化中心", "健身中心", "电影院"),
               30000, 180000, 700, 1800, hours=(16, 19, 20)),
        _periodic("life.utilities", "expense", "水电燃气", "家庭水电燃气结算", 250000, 18),
        _periodic("life.communication", "expense", "通信账单", "中国移动", 58800, 12),
        _periodic("life.family_support", "expense", "赡养与家庭往来", "长辈生活费", 30000000, 15),
        _periodic("property.management", "expense", "物业服务", "物业服务中心", 12000000, 18, role="rent"),
        _periodic("property.maintenance", "expense", "物业维修", "住房维修服务", 60000000, 22,
                  role="rent", months=(4, 10)),
        _periodic("income.home_rent", "income", "个人住宅租金收入", "住宅承租人", 25500000, 8, role="rent"),
        _periodic("income.shop_rent", "income", "个人商铺租金收入", "商铺承租人", 32500000, 10, role="rent"),
        _periodic("income.business_distribution", "income", "个人持股分红收入", "个人持股企业分配", 1500000000,
                  20, months=(6, 12)),
    ),
    "grain_farmer": (
        _daily("life.meal", "餐饮堂食", _FOOD, 3800, 14000, 2400, 4200, hours=(11, 12, 18)),
        _daily("life.groceries", "超市生鲜", _MARKET, 4000, 16000, 6500, 7500),
        _daily("life.delivery", "外卖", _DELIVERY, 2000, 4800, 600, 1000, hours=(11, 18)),
        _drink(1200, 1800),
        _daily("life.shopping", "购物", _SHOPPING, 5000, 35000, 500, 1000),
        _daily("life.fuel", "燃油费", ("中国石化加油站", "中国石油加油站"), 18000, 45000, 700, 500),
        _daily("life.pharmacy", "药店购药", _PHARMACY, 2500, 16000, 600, 700),
        _daily("life.daily_goods", "日用品", ("世纪联华", "京东自营"), 1500, 8500, 1700, 2300),
        _periodic("life.utilities", "expense", "水电燃气", "家庭水电燃气结算", 42000, 18),
        _periodic("life.communication", "expense", "通信账单", "中国移动", 18800, 12),
        _periodic("life.family_support", "expense", "赡养与家庭往来", "家庭成员生活费", 350000, 15),
        _periodic("farm.storage", "expense", "农业投入", "农业经营仓储服务", 700000, 28, role="business"),
        _periodic("farm.land", "expense", "土地租赁", "土地流转服务中心", 67500000, 3,
                  role="business", months=(3, 9)),
        _periodic("farm.inputs", "expense", "农业投入", "丰禾农资经营部", 30000000, 6,
                  role="business", months=(2, 5, 8)),
        _periodic("farm.harvest_labour", "expense", "农业作业", "收获作业服务队", 29000000, 8,
                  role="business", months=(6, 10)),
        _periodic("farm.machinery_maintenance", "expense", "农机维护", "农机维修服务中心", 7000000, 16,
                  role="business", months=(1, 7)),
        _periodic("income.grain_sales", "income", "粮食销售回款", "粮食收购企业", 210000000, 20,
                  role="business", months=(6, 10)),
        _periodic("income.machinery_services", "income", "农机作业服务回款", "农业机械服务结算", 16000000,
                  24, role="business", months=(6, 10)),
        _periodic("income.agricultural_support", "income", "农业经营补贴", "农业经营补贴结算", 12000000,
                  25, role="business", months=(8,)),
    ),
    "working_landlady": (
        _daily("life.meal", "餐饮堂食", _FOOD, 3800, 12000, 2500, 5500, hours=(11, 12, 18, 19)),
        _daily("life.groceries", "超市生鲜", _MARKET, 3500, 13000, 5000, 7000),
        _daily("life.delivery", "外卖", _DELIVERY, 2200, 4800, 4200, 2500, hours=(11, 12, 18)),
        _drink(6800, 4000),
        _daily("life.commute", "公共交通", ("杭州公交", "杭州地铁"), 200, 800, 8200, 0,
               hours=(7, 18), occurrences=2, price_points=(200, 300, 400, 500, 600, 700, 800)),
        _daily("life.taxi", "打车", ("滴滴出行", "高德打车", "曹操出行"), 1800, 8000,
               900, 1500, hours=(8, 18, 21)),
        _daily("life.shopping", "购物", _SHOPPING, 8000, 68000, 700, 1800),
        _daily("life.pharmacy", "药店购药", _PHARMACY, 2500, 18000, 500, 700),
        _daily("life.daily_goods", "日用品", ("世纪联华", "京东自营", "屈臣氏"), 1500, 10000,
               1700, 2100),
        _daily("life.leisure", "休闲娱乐", ("电影院", "市文化中心", "健身中心"), 4000, 24000,
               400, 1500, hours=(17, 19, 20)),
        _periodic("life.utilities", "expense", "水电燃气", "家庭水电燃气结算", 52000, 18),
        _periodic("life.communication", "expense", "通信账单", "中国移动", 18800, 12),
        _periodic("property.management", "expense", "物业服务", "物业服务中心", 145000, 22, role="rent"),
        _periodic("property.maintenance", "expense", "物业维修", "住房维修服务", 2200000, 20,
                  role="rent", months=(4, 10)),
        _periodic("income.salary", "income", "工资收入", "工作单位工资结算", 1700000, 25, role="salary"),
        _periodic("income.rent_one", "income", "出租住宅一租金收入", "住宅一承租人", 580000, 5, role="rent"),
        _periodic("income.rent_two", "income", "出租住宅二租金收入", "住宅二承租人", 557000, 10, role="rent"),
        _periodic("income.rent_three", "income", "出租住宅三租金收入", "住宅三承租人", 488000, 15, role="rent"),
    ),
    "retired_entrepreneur": (
        _daily("life.meal", "餐饮堂食", _QUICK_FOOD, 1800, 4800, 1400, 3500, hours=(11, 12, 18)),
        _daily("life.groceries", "超市生鲜", _MARKET, 2500, 9000, 5500, 6500),
        _daily("life.delivery", "外卖", _DELIVERY, 1800, 3800, 1000, 1800, hours=(11, 18)),
        _drink(900, 1300),
        _daily("life.transit", "公共交通", ("杭州公交", "杭州地铁"), 200, 800, 5000, 3000,
               hours=(8, 17), price_points=(200, 300, 400, 500, 600, 700, 800)),
        _daily("life.shopping", "购物", _SHOPPING, 5000, 28000, 400, 1000),
        _daily("life.pharmacy", "药店购药", _PHARMACY, 4500, 22000, 1100, 1300),
        _daily("life.daily_goods", "日用品", ("世纪联华", "京东自营"), 1500, 6500, 1300, 2000),
        _periodic("life.utilities", "expense", "水电燃气", "家庭水电燃气结算", 45000, 20),
        _periodic("life.communication", "expense", "通信账单", "中国移动", 13800, 12),
        _periodic("life.family_support", "expense", "赡养与家庭往来", "家庭成员生活费", 150000, 15),
        _periodic("income.pension", "income", "退休金收入", "养老金发放结算", 720000, 15, role="salary"),
        _Rule("income.restaurant_sales", "income", "自营餐饮回款", ("门店营业收款结算",),
              (110000, 180000), "business", weekdays=_OPEN_DAYS, hours=(21,)),
        _daily("business.ingredients", "餐饮原料", ("菜场批发配送", "粮油批发商行", "冻品配送中心"),
               45000, 75000, 10000, 10000, account_role="business", weekdays=_OPEN_DAYS, hours=(6, 7)),
        _periodic("business.shop_rent", "expense", "经营租金", "门店出租人", 720000, 12, role="business"),
        _periodic("business.payroll", "expense", "经营工资", "门店员工工资", 520000, 18, role="business"),
        _periodic("business.utilities", "expense", "餐饮经营账单", "门店水电与包装结算", 180000, 22, role="business"),
        _periodic("business.tax", "expense", "经营税费", "税务缴费", 90000, 25, role="business"),
    ),
}


def _as_date(day):
    if type(day) is date:
        return day
    if type(day) is str:
        try:
            parsed = date.fromisoformat(day)
            if parsed.isoformat() == day:
                return parsed
        except ValueError:
            pass
    raise ValueError("day 必须是规范 YYYY-MM-DD 或 date，不能是 datetime。")


def _int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} 必须是 {low} 至 {high} 的整数。")
    return value


def _configured(rules, observations):
    if observations is None:
        return rules
    if not isinstance(observations, Mapping) or set(observations) - {"rules"}:
        raise ValueError("observations 只接受 rules 配置，不接受余额、操作或任意数据。")
    changes = observations.get("rules", {})
    known = {rule.rule_id: rule for rule in rules}
    if not isinstance(changes, Mapping) or set(changes) - set(known):
        raise ValueError("observations.rules 包含未登记规则。")
    configured = []
    for rule in rules:
        override = changes.get(rule.rule_id, {})
        if not isinstance(override, Mapping) or set(override) - _OVERRIDES:
            raise ValueError("规则配置包含未允许的字段。")
        values = {}
        for key in ("enabled", "weekday_only"):
            if key in override and type(override[key]) is not bool:
                raise ValueError(key + " 必须为布尔值。")
        if "enabled" in override:
            values["enabled"] = override["enabled"]
        if "weekday_only" in override:
            values["weekdays"] = tuple(range(5)) if override["weekday_only"] else _ALL_DAYS
        if "amount_minor" in override and "amount_range_minor" in override:
            raise ValueError("固定金额与金额范围不能同时配置。")
        if "amount_minor" in override:
            amount = _int(override["amount_minor"], "amount_minor", 1, _MAX_AMOUNT)
            values.update(amount_range=(amount, amount), price_points=())
        if "amount_range_minor" in override:
            bounds = override["amount_range_minor"]
            if type(bounds) not in (tuple, list) or len(bounds) != 2:
                raise ValueError("amount_range_minor 需要两个整数分。")
            low, high = (_int(v, "金额范围", 1, _MAX_AMOUNT) for v in bounds)
            if low > high:
                raise ValueError("金额范围下界不能大于上界。")
            values.update(amount_range=(low, high), price_points=())
        if "probability_bps" in override:
            probability = _int(override["probability_bps"], "probability_bps", 0, 10000)
            values.update(weekday_bps=probability, weekend_bps=probability)
        if "monthly_occurrences" in override:
            if rule.day_of_month is not None or "probability_bps" in override:
                raise ValueError("固定月次数仅用于每日规则，不能与概率或月度到期规则混用。")
            values["monthly_occurrences"] = _int(override["monthly_occurrences"], "monthly_occurrences", 0, 62)
        if "day_of_month" in override:
            if rule.day_of_month is None:
                raise ValueError("每日规则不能变成月度规则；请使用模板已登记的周期规则。")
            values["day_of_month"] = _int(override["day_of_month"], "day_of_month", 1, 31)
        if "months" in override:
            months = override["months"]
            if type(months) not in (tuple, list) or not months:
                raise ValueError("months 需要非空月份列表。")
            checked = tuple(_int(v, "month", 1, 12) for v in months)
            if len(set(checked)) != len(checked):
                raise ValueError("months 不能重复。")
            values["months"] = tuple(sorted(checked))
        configured.append(replace(rule, **values))
    return tuple(configured)


def _fixed_month_slots(rule, on, persona, namespace):
    """纯确定性月计划，避免逐日 Bernoulli 无法兑现模板固定频次。"""
    if rule.monthly_occurrences is None:
        return None
    slots = [(date(on.year, on.month, day), occurrence)
             for day in range(1, calendar.monthrange(on.year, on.month)[1] + 1)
             if date(on.year, on.month, day).weekday() in rule.weekdays
             and (rule.weekend_bps if date(on.year, on.month, day).weekday() >= 5
                  else rule.weekday_bps) > 0
             for occurrence in range(rule.occurrences)]
    if rule.monthly_occurrences > len(slots):
        raise ValueError("固定月频次超过本月允许日期与日内次数，不能静默少生成。")
    def rank(slot):
        material = json.dumps([namespace, persona, slot[0].isoformat(), rule.rule_id, slot[1], "monthly-frequency"],
                              ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).digest()
    return set(sorted(slots, key=rank)[:rule.monthly_occurrences])


def rule_catalog(persona_id):
    """返回允许模板覆盖的规则元信息新副本；不含账户或用户身份。"""
    if type(persona_id) is not str or persona_id not in _COMMON:
        raise ValueError("persona_id 不是允许的四种画像。")
    return [{"rule_id": r.rule_id, "kind": r.kind, "category": r.category,
             "account_role": r.account_role, "cadence": "monthly" if r.day_of_month else "daily",
             "day_of_month": r.day_of_month, "months": list(r.months),
             "amount_range_minor": list(r.amount_range), "occurrences_per_day": r.occurrences,
             "weekday_probability_bps": r.weekday_bps, "weekend_probability_bps": r.weekend_bps}
            for r in _COMMON[persona_id]]


def plan_day(persona_id, day, *, seed_namespace, observations=None):
    """返回指定日期的独立确定性收支计划（当前时刻过滤由 worker 完成）。

    seed_namespace 必须为稳定私有实例标识，不能使用手机号或轮换会话令牌。
    observations 仅支持 {"rules": {已登记 rule_id: 白名单覆盖字段}}。
    月度第29/30/31日规则在短月收敛到该月最后一天；年/月末只出现一次。
    """
    if type(persona_id) is not str or persona_id not in _COMMON:
        raise ValueError("persona_id 不是允许的四种画像。")
    on = _as_date(day)
    if type(seed_namespace) is not str or not seed_namespace.strip() or len(seed_namespace) > 256:
        raise ValueError("seed_namespace 需要 1 至 256 字符的稳定非空字符串。")
    rules = _configured(_COMMON[persona_id], observations)
    output = []
    for rule in rules:
        if not rule.enabled or on.month not in rule.months or on.weekday() not in rule.weekdays:
            continue
        if rule.day_of_month is not None:
            due = min(rule.day_of_month, calendar.monthrange(on.year, on.month)[1])
            if on.day != due:
                continue
        fixed_slots = _fixed_month_slots(rule, on, persona_id, seed_namespace)
        for occurrence in range(rule.occurrences):
            if fixed_slots is not None and (on, occurrence) not in fixed_slots:
                continue
            material = json.dumps([seed_namespace, persona_id, on.isoformat(), rule.rule_id, occurrence],
                                  ensure_ascii=False, separators=(",", ":"))
            rng = random.Random(int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest(), "big"))
            chance = rule.weekend_bps if on.weekday() >= 5 else rule.weekday_bps
            if fixed_slots is None and rng.randrange(10000) >= chance:
                continue
            merchant = rng.choice(rule.merchants)
            if rule.category == "饮品" and rule.price_points:
                menu = dict(_DRINKS)[merchant]
                maximum = rule.amount_range[1]
                points = [p * cups for p in menu for cups in ((1, 2) if maximum > 4200 else (1,))
                          if rule.amount_range[0] <= p * cups <= maximum]
                amount = rng.choice(points)
            elif rule.price_points:
                amount = rng.choice(rule.price_points)
                if rule.category == "公共交通" and merchant == "杭州公交":
                    amount = 200
            else:
                amount = rng.randint(*rule.amount_range)
            hour = rule.hours[occurrence] if rule.occurrences > 1 else rng.choice(rule.hours)
            stamp = f"{on.isoformat()}T{hour:02}:{rng.randrange(60):02}:{rng.randrange(60):02}+08:00"
            output.append({"rule_id": rule.rule_id, "scheduled_at": stamp,
                           "kind": rule.kind, "category": rule.category, "merchant": merchant,
                           "amount_minor": amount, "account_role": rule.account_role,
                           "occurrence_index": occurrence})
    return sorted(output, key=lambda item: (item["scheduled_at"], item["rule_id"], item["occurrence_index"]))
