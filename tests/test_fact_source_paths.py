"""Local fact provenance metadata; no gateway writes, cloud clients or credentials."""
from copy import deepcopy
import hashlib
import json
import unittest

from agent_runtime.bank_tools import BankTools
from agent_runtime.data_boundary import CloudDataBoundary


LOCAL_KEYS = {'path', 'source_path', 'field', 'label', 'name'}


def facts_for(data, path, currency='CNY'):
    tools = BankTools.__new__(BankTools)
    tools.facts = {}
    tools.evidence = {'e-fixture': {'id': 'e-fixture', 'data': deepcopy(data)}}
    tools.add_facts('e-fixture', data, path, currency)
    return tools


def resolve_pointer(data, pointer):
    value = data
    currency = value.get('currency', 'CNY') if isinstance(value, dict) else 'CNY'
    for part in pointer.split('/')[1:]:
        key = part.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if isinstance(value, list) else value[key]
        if isinstance(value, dict):
            currency = value.get('currency', currency)
    return value, currency


class FactSourcePathTest(unittest.TestCase):
    def test_original_ids_values_types_currency_and_text_are_unchanged(self):
        data = {'currency':'USD','account_name':'模拟日常账户','balance_minor':12345,
            'reserve_minor':2345,'risk_level':2,'opening_balance_minor':99999,
            'is_ready':True,'float_minor':1.5,
            'nested':{'name':'组/名~','currency':'EUR','amount_minor':6789},
            'category_expense_minor':{'餐饮':123,'购物/服务~':456}}
        before = deepcopy(data)
        tools = facts_for(data, 'budget')
        # Pinned from the real pre-change implementation, not another new parser.
        expected = [
            ('f480176f0da0c7f4308ab',12345,'minor_currency','USD','日常账户 · 已结算余额：123.45 USD'),
            ('fbb51bb01ad4dfae140db',2345,'minor_currency','USD','日常账户 · 应急保留：23.45 USD'),
            ('f41b077b2ace762291a32',2,'scalar',None,'日常账户 · 正式模拟风险等级：2'),
            ('fbe47c5ec605795af412f',6789,'minor_currency','EUR','组/名~ · amount：67.89 EUR'),
            ('f6aef362362ee5b78590a',123,'minor_currency','USD','查询记录 · 餐饮：1.23 USD'),
            ('fae04761dfaa77a8d5f8d',456,'minor_currency','USD','查询记录 · 购物/服务~：4.56 USD'),
        ]
        self.assertEqual([(f['id'],f['value'],f['unit'],f['currency'],f['text'])
            for f in tools.facts.values()], expected)
        self.assertEqual(data, before)
        self.assertEqual([f['source_path'] for f in tools.facts.values()], [
            '/balance_minor','/reserve_minor','/risk_level','/nested/amount_minor',
            '/category_expense_minor/餐饮','/category_expense_minor/购物~1服务~0'])
        for fact in tools.facts.values():
            original, currency = resolve_pointer(tools.evidence['e-fixture']['data'], fact['source_path'])
            self.assertEqual(original, fact['value'])
            self.assertIs(type(original), type(fact['value']))
            self.assertIs(type(original), int)
            self.assertEqual(fact['currency'], currency if fact['unit']=='minor_currency' else None)
            self.assertEqual(fact['id'], 'f'+hashlib.sha256(('e-fixture'+fact['path']).encode()).hexdigest()[:20])
        self.assertEqual(tools.facts['f480176f0da0c7f4308ab']['path'], 'budget/balance_minor')
        self.assertEqual(tools.facts['f480176f0da0c7f4308ab']['field'], 'balance_minor')
        self.assertEqual(tools.facts['f480176f0da0c7f4308ab']['label'], '已结算余额')
        self.assertEqual(tools.facts['f480176f0da0c7f4308ab']['name'], '日常账户')

    def test_json_pointer_escapes_nested_keys_and_preserves_list_indices(self):
        data = {'currency':'USD','section~/':{'currency':'EUR',
            'values_minor':{'x/~':456},'items/~':[{'name':'明细~/','amount_minor':234}]}}
        tools = facts_for(data, 'escaped')
        facts = list(tools.facts.values())
        self.assertEqual([(f['id'],f['source_path']) for f in facts], [
            ('fa6a3925403147c104eb2','/section~0~1/values_minor/x~1~0'),
            ('f6d86e40918e6b04d2003','/section~0~1/items~1~0/0/amount_minor')])
        self.assertEqual(facts[0]['field'], 'x/~')
        self.assertEqual(facts[0]['path'], '查询记录/values_minor/x/~')
        for fact in facts:
            original, currency = resolve_pointer(tools.evidence['e-fixture']['data'], fact['source_path'])
            self.assertEqual(original, fact['value'])
            self.assertIs(type(original), type(fact['value']))
            self.assertEqual(currency, fact['currency'])
        roots = facts_for([{'currency':'JPY','amount_minor':123}], 'root_list')
        root_fact = next(iter(roots.facts.values()))
        self.assertEqual(root_fact['source_path'], '/0/amount_minor')
        self.assertEqual(root_fact['currency'], 'JPY')

    def test_statement_detail_labels_and_legacy_paths_are_unchanged(self):
        data = {'currency':'CNY','total_minor':200,'transaction_count':2,
            'items':[{'merchant':'模拟房租','currency':'CNY','amount_minor':200,'transaction_count':1}]}
        tools = facts_for(data, 'statement_details')
        facts = list(tools.facts.values())
        self.assertEqual([f['id'] for f in facts], ['ffa9951114ec0c86c80ca',
            'fcf206c6cb26acc180fcf','f53b4cfc8eadfe9ecfd85','fb20b45042c632d1cf155'])
        self.assertEqual([f['path'] for f in facts], ['statement_details/total_minor',
            'statement_details/transaction_count','statement_details/items/0/amount_minor',
            'statement_details/items/0/transaction_count'])
        self.assertEqual([f['label'] for f in facts], ['合计','笔数','金额','笔数'])
        self.assertEqual([f['source_path'] for f in facts], ['/total_minor',
            '/transaction_count','/items/0/amount_minor','/items/0/transaction_count'])
        self.assertEqual(facts[2]['name'], '房租')

    def test_existing_type_filter_and_opening_balance_exclusion_are_preserved(self):
        data = {'opening_balance_minor':100,'balance_minor':True,'float_minor':1.25,
            'risk_level':'2','transaction_count':False,'unregistered_integer':77}
        self.assertEqual(facts_for(data, 'budget').facts, {})

    def test_metadata_is_dropped_by_primary_and_reviewer_cloud_projection(self):
        data = {'currency':'CNY','accounts':[{'name':'日常账户','currency':'CNY','balance_minor':12345}],
            'profile':{'risk_level':2}}
        facts = list(facts_for(data, 'account_summary').facts.values())
        old_facts = [{k:v for k,v in f.items() if k not in LOCAL_KEYS} for f in facts]
        enriched = deepcopy(facts)
        for fact in enriched:
            for key in LOCAL_KEYS:
                fact[key] = 'LOCAL_ONLY_METADATA_MARKER'
        boundary = CloudDataBoundary(task='account')
        plain = boundary.tool_result('account_summary', {'evidence_id':'e-fixture','data':data,'facts':old_facts})
        projected = boundary.tool_result('account_summary', {'evidence_id':'e-fixture','data':data,'facts':enriched})
        self.assertEqual(projected, plain)
        self.assertTrue(projected['facts'])
        self.assertTrue(all(not (LOCAL_KEYS & f.keys()) for f in projected['facts']))
        base_packet = {'question':'账户余额是多少','evidence':[{'id':'e-fixture',
            'tool':'account_summary','data':data,'source':'simulation'}],
            'draft':{'text':'已核对账户。'}}
        old_packet = boundary.review_packet({**base_packet,'monetary_evidence':old_facts})
        new_packet = boundary.review_packet({**base_packet,'monetary_evidence':enriched})
        self.assertEqual(new_packet, old_packet)
        self.assertTrue(new_packet['monetary_evidence'])
        self.assertTrue(all(not (LOCAL_KEYS & f.keys()) for f in new_packet['monetary_evidence']))
        self.assertNotIn('LOCAL_ONLY_METADATA_MARKER', json.dumps([projected,new_packet]))

    def test_metadata_does_not_expand_cloud_fact_or_amount_allowlists(self):
        data = {'totals_minor':{'expense':30000},
            'transactions':[{'merchant':'私人商户原文','amount_minor':1234}]}
        facts = list(facts_for(data, 'monthly_statement').facts.values())
        boundary = CloudDataBoundary(task='statements')
        output = boundary.tool_result('monthly_statement', {'evidence_id':'e-fixture','data':data,'facts':facts})
        self.assertEqual([f['value'] for f in output['facts']], [30000])
        packet = boundary.review_packet({'question':'月消费是多少','evidence':[{
            'id':'e-fixture','tool':'monthly_statement','data':data,'source':'imported'}],
            'monetary_evidence':facts,'draft':{'text':'已核对消费。'}})
        self.assertEqual([f['value'] for f in packet['monetary_evidence']], [30000])
        self.assertNotIn('私人商户原文', json.dumps([output,packet],ensure_ascii=False))

    def test_preexisting_fact_id_collision_behavior_is_not_changed(self):
        data = {'items':[{'detail':{'amount_minor':100}},{'detail':{'amount_minor':200}}]}
        tools = facts_for(data, 'budget')
        # Legacy display paths can collide for unnamed nested rows. Preserve
        # the existing identity/last-record behavior; do not invent new IDs.
        self.assertEqual(len(tools.facts), 1)
        fact = next(iter(tools.facts.values()))
        self.assertEqual(fact['value'], 200)
        self.assertEqual(fact['source_path'], '/items/1/detail/amount_minor')
        self.assertEqual(fact['path'], '查询记录/detail/amount_minor')


if __name__ == '__main__':
    unittest.main()
