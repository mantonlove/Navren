"""Finite public retrieval vocabulary, not translations of evidence or facts.

Aliases are query features only. They supply no dates, amounts, rates or claims,
never become graph assertions, and never leave the local retrieval process.
Unrecognized topics keep the original query and receive no invented English.
"""
from __future__ import annotations
import re

POLICY = 'public-financial-query-aliases/1'
FINANCIAL_ANCHOR = re.compile(r'金融|理财|投资|基金|股票|债券|保险|银行|央行|利率|汇率|贷款|存款|证券|融资|货币|通胀|抵押|信用|欧元|美联储|拉加德|监管|议息')
# Institution names and ordinary financial terminology; no question IDs or URLs.
ALIASES = (
    (('欧洲央行', '欧元区央行'), 'European Central Bank ECB'),
    (('美联储', '美国央行', '美国议息'), 'Federal Reserve FOMC'),
    (('中国人民银行', '国内央行'), 'People Bank China'),
    (('拉加德',), 'Lagarde'),
    (('欧元',), 'euro'), (('纸币',), 'banknotes'), (('图案', '设计'), 'design'),
    (('调查',), 'survey'), (('结果',), 'results'),
    (('私营部门',), 'private sector'), (('资产',), 'assets'),
    (('抵押', '抵押品'), 'collateral'), (('评级',), 'credit rating'),
    (('最好', '最高'), 'best highest'), (('第二高',), 'second best'),
    (('新规', '规则',), 'rules guidelines'), (('适用', '生效'), 'applicable effective'),
    (('什么时候', '何时', '多久'), 'when time'),
    (('人工智能', '智能模型'), 'artificial intelligence AI'),
    (('金融系统', '金融体系'), 'financial system'), (('金融机构',), 'financial institutions'),
    (('风险',), 'risk'), (('类似', '相似'), 'similar'),
    (('交易',), 'trades trading'), (('市场',), 'market'), (('价格',), 'prices'),
    (('变化',), 'changes moves'), (('放大',), 'amplify reinforce'),
    (('网络韧性', '网络安全'), 'cyber resilience'), (('地缘政治',), 'geopolitics'),
    (('议息会议',), 'monetary policy committee meeting'), (('会议纪要',), 'meeting minutes'),
    (('发布', '公布'), 'published release'), (('决策', '决定'), 'policy decision'),
    (('信息', '消息'), 'information'), (('依据',), 'based available'),
    (('结束后', '之后', '会后'), 'after meeting'),
    (('证券融资',), 'securities financing'), (('融资',), 'financing'),
    (('衍生品',), 'derivatives'), (('场外',), 'OTC'), (('信用条款',), 'credit terms'),
    (('监管',), 'supervision supervisory'), (('整改',), 'remediation'),
    (('宏观',), 'macroeconomic'), (('经济预测', '经济展望'), 'economic projections outlook'),
    (('利率',), 'interest rates'), (('通胀', '物价'), 'inflation'),
    (('贷款',), 'loans lending'), (('存款',), 'deposits'), (('债券',), 'bonds'),
    (('股票',), 'equity stock'), (('汇率',), 'exchange rate'), (('货币政策',), 'monetary policy'),
)


def query_projection(query):
    if not re.search(r'[\u3400-\u9fff]', query) or not FINANCIAL_ANCHOR.search(query):
        return None
    found = [english for chinese, english in ALIASES if any(term in query for term in chinese)]
    if len(found) < 2:
        return None
    # A bounded bag of public concepts, never a generated sentence or answer.
    words = list(dict.fromkeys(' '.join(found).split()))
    return ' '.join(words)[:500]


def concept_coverage(query):
    """Fraction of Han characters covered by the fixed ordinary term table.

    A query-language feature, not financial support or model confidence. Spaces,
    punctuation and Latin identifiers do not change its denominator.
    """
    han={index for index,char in enumerate(query) if re.match(r'[\u3400-\u9fff]',char)}
    if not han:return 0.0
    covered=set()
    for chinese,_ in ALIASES:
        for term in chinese:
            for match in re.finditer(re.escape(term),query):
                covered.update(range(match.start(),match.end()))
    return len(han&covered)/len(han)
