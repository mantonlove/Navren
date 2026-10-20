"""用户录入收款联系人：仅使用新建临时账本，不执行资金操作。"""
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from finance_gateway import COOKIE, FinanceGateway
from simulated_bank import BankError, SimulatedBank
from simulated_bank.service import digest


FORM = {'recipient_name': '测试收款人', 'recipient_account': '06222021000000000123',
        'recipient_bank': '测试开户银行'}


class RemittanceContactsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'bank.sqlite3'
        self.now = [1800000000.0]
        self.env = patch.dict(os.environ, {
            'NAVREN_KNOWLEDGE_DB': str(Path(self.directory.name) / 'knowledge.sqlite3'),
            'NAVREN_FINANCIAL_DB': '', 'NAVREN_PUBLIC_SYNC_ENABLED': 'false'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.g = FinanceGateway(self.path)
        self.g.bank.clock = lambda: self.now[0]
        self.a = self.g.bank.create_workspace()
        self.b = self.g.bank.create_workspace()
        self.w = self.a['workspace_id']

    def create(self, **changes):
        return self.g.remittance_contacts(self.w, {**FORM, **changes}, action='create')['contact']

    def assert_error(self, code, call, status=None):
        with self.assertRaises(BankError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def financial_state(self):
        with closing(self.g.bank.connect()) as db:
            tables = [row['name'] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name<>'navren_remittance_contacts' ORDER BY name")]
            return {table: sorted(json.dumps(dict(row), sort_keys=True, ensure_ascii=False)
                                  for row in db.execute('SELECT * FROM "' + table + '"'))
                    for table in tables}

    def test_empty_response_has_user_entered_source_and_workspace_binding(self):
        response = self.g.remittance_contacts(self.w)
        self.assertEqual(response, {'environment': 'simulation', 'source': 'user_entered',
                                    'workspace_id': self.w, 'contacts': []})

    def test_create_edit_restart_read_and_delete_preserve_full_account(self):
        contact = self.create()
        self.assertEqual(contact['recipient_account'], FORM['recipient_account'])
        self.assertEqual(contact['recipient_branch'], '')
        self.assertEqual(contact['alias'], '')
        self.assertEqual(contact['masked_account'], '*' * 16 + '0123')
        self.assertEqual(contact['created_at'], self.now[0])
        self.now[0] += 10
        body = {**FORM, 'recipient_name': '测试收款人改名', 'recipient_branch': '测试支行', 'alias': '同学'}
        updated = self.g.remittance_contacts(self.w, body, contact['id'], 'update')['contact']
        self.assertEqual(updated['id'], contact['id'])
        self.assertEqual(updated['created_at'], contact['created_at'])
        self.assertEqual(updated['updated_at'], self.now[0])
        reopened = FinanceGateway(self.path)
        self.assertEqual(reopened.remittance_contacts(self.w)['contacts'], [updated])
        # 新表初始化可重复执行，旧联系人和金融档案继续保留。
        SimulatedBank(self.path)
        self.assertEqual(reopened.remittance_contacts(self.w)['contacts'], [updated])
        deleted = reopened.remittance_contacts(self.w, {}, contact['id'], 'delete')
        self.assertTrue(deleted['deleted'])
        self.assertEqual(deleted['id'], contact['id'])
        self.assertEqual(reopened.remittance_contacts(self.w)['contacts'], [])

    def test_outer_spaces_normalized_and_account_leading_zero_retained(self):
        contact = self.create(recipient_name='  测试姓名  ', recipient_account='  ' + FORM['recipient_account'] + '  ',
                              recipient_bank='  测试银行  ', recipient_branch='  ', alias='  家人  ')
        self.assertEqual(contact['recipient_name'], '测试姓名')
        self.assertEqual(contact['recipient_account'], FORM['recipient_account'])
        self.assertEqual(contact['recipient_bank'], '测试银行')
        self.assertEqual(contact['recipient_branch'], '')
        self.assertEqual(contact['alias'], '家人')

    def test_identical_retry_is_idempotent_and_conflicting_duplicate_rejected(self):
        first = self.create(alias='家人')
        self.assertEqual(self.create(alias='家人'), first)
        self.assert_error('CONTACT_EXISTS', lambda: self.create(alias='另一个别名'), 409)
        second = self.create(recipient_account='62220210000000009999')
        self.assert_error('CONTACT_EXISTS', lambda: self.g.remittance_contacts(
            self.w, FORM, second['id'], 'update'), 409)
        self.assertEqual(len(self.g.remittance_contacts(self.w)['contacts']), 2)

    def test_same_account_in_other_workspace_is_independent(self):
        first = self.create()
        other = self.g.remittance_contacts(self.b['workspace_id'], FORM, action='create')['contact']
        self.assertNotEqual(first['id'], other['id'])
        self.assertEqual(self.g.remittance_contacts(self.w)['contacts'], [first])
        self.assertEqual(self.g.remittance_contacts(self.b['workspace_id'])['contacts'], [other])
        for action, payload in [('update', FORM), ('delete', {})]:
            self.assert_error('CONTACT_NOT_FOUND', lambda: self.g.remittance_contacts(
                self.b['workspace_id'], payload, first['id'], action), 404)
        self.assertEqual(self.g.remittance_contacts(self.w)['contacts'], [first])

    def test_missing_and_unknown_fields_rejected_without_writes(self):
        for missing in FORM:
            with self.subTest(missing=missing):
                self.assert_error('INVALID_FIELDS', lambda: self.g.remittance_contacts(
                    self.w, {key: value for key, value in FORM.items() if key != missing}, action='create'))
        for key in ('user_id', 'workspace_id', 'verified', 'account_id', 'amount_minor', 'permission'):
            with self.subTest(field=key):
                self.assert_error('INVALID_FIELDS', lambda: self.create(**{key: 'injected'}))
        self.assertEqual(self.g.remittance_contacts(self.w)['contacts'], [])

    def test_invalid_account_and_field_values_rejected_without_writes(self):
        for account in ('123456789', '1' * 33, '0' * 10, '12345abcde', '12345 67890', '１２３４５６７８９０', 1234567890):
            with self.subTest(account=account):
                code = 'INVALID_CONTACT_FIELD' if not isinstance(account, str) or len(account) > 32 else 'INVALID_CONTACT_ACCOUNT'
                self.assert_error(code, lambda: self.create(recipient_account=account))
        for changes in ({'recipient_name': ''}, {'recipient_name': ' '}, {'recipient_bank': ''},
                        {'recipient_name': '名' * 65}, {'recipient_bank': '行' * 101},
                        {'recipient_branch': '支' * 101}, {'alias': '别' * 65}, {'alias': None},
                        {'recipient_name': '姓名\n'}, {'recipient_branch': '\x00'}, {'alias': '\x7f'}):
            with self.subTest(fields=changes):
                self.assert_error('INVALID_CONTACT_FIELD', lambda: self.create(**changes))
        self.assertEqual(self.g.remittance_contacts(self.w)['contacts'], [])

    def test_delete_unknown_fields_and_nonexistent_id_rejected(self):
        contact = self.create()
        self.assert_error('INVALID_FIELDS', lambda: self.g.remittance_contacts(
            self.w, {'confirmed': True}, contact['id'], 'delete'))
        for contact_id in ('f' * 32, 'not-a-contact'):
            self.assert_error('CONTACT_NOT_FOUND', lambda: self.g.remittance_contacts(
                self.w, {}, contact_id, 'delete'), 404)
        self.assertEqual(self.g.remittance_contacts(self.w)['contacts'], [contact])

    def test_all_other_tables_unchanged_by_contact_crud_and_invalid_attempts(self):
        before = self.financial_state()
        contact = self.create()
        self.g.remittance_contacts(self.w)
        self.g.remittance_contacts(self.w, {**FORM, 'alias': '备注'}, contact['id'], 'update')
        self.assert_error('INVALID_FIELDS', lambda: self.create(verified=True))
        self.g.remittance_contacts(self.w, {}, contact['id'], 'delete')
        self.assertEqual(self.financial_state(), before)

    def test_revoked_token_rechecked_inside_write_transaction(self):
        token = self.a['session_token']
        with self.g.demo_sessions.request(token, self.w):
            with closing(self.g.bank.connect()) as db, db:
                db.execute('INSERT INTO demo_sessions VALUES(?,?,?,?,?,1)',
                           (digest(token), self.w, 'legacy', 0, self.now[0]))
            self.assert_error('SESSION_CHANGED', lambda: self.create(), 409)
        self.assertEqual(self.g.remittance_contacts(self.w)['contacts'], [])

    def test_new_contacts_do_not_create_aa_users_or_bank_beneficiaries(self):
        before = self.financial_state()
        contact = self.create()
        self.assertNotIn('user_id', contact)
        self.assertNotIn('verified', contact)
        self.assert_error('AA_IDENTITY_REQUIRED', lambda: self.g.payment_contacts(self.w), 409)
        self.assertEqual(self.financial_state(), before)

    def test_real_http_crud_authentication_csrf_scope_and_revoked_session(self):
        from server import NavrenHTTPServer, ProxyHandler
        # 原生产处理器，但新建临时库、随机端口；不启动模型、行情或公开数据同步。
        server = NavrenHTTPServer(('127.0.0.1', 0), ProxyHandler)
        server.finance = self.g
        with patch.object(ProxyHandler, 'log_message', lambda *args: None):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            def call(path, method='GET', body=None, session=None, csrf=True):
                session = self.a if session is None else session
                headers = {'Content-Type': 'application/json'}
                if session:
                    headers['Cookie'] = COOKIE + '=' + session['session_token']
                    if csrf:
                        headers['X-QZ-CSRF'] = self.g.csrf(session['session_token'])
                request = urllib.request.Request(base + path, method=method, headers=headers,
                    data=json.dumps(body or {}).encode() if method == 'POST' else None)
                try:
                    result = urllib.request.urlopen(request, timeout=5)
                except urllib.error.HTTPError as error:
                    result = error
                with result:
                    return result.status, json.loads(result.read())
            route = '/api/finance/remittance-contacts'
            before = self.financial_state()
            try:
                self.assertEqual(call(route, 'POST', FORM, session={})[0], 401)
                self.assertEqual(call(route, 'POST', FORM, csrf=False)[0], 403)
                self.assertEqual(call(route + '?workspace_id=other')[0], 400)
                self.assertEqual(call(route + '?workspace_id=', 'POST', FORM)[0], 400)
                status, response = call(route, 'POST', FORM)
                self.assertEqual(status, 200, response)
                contact = response['contact']
                self.assertEqual(response['workspace_id'], self.w)
                self.assertEqual(call(route)[1]['contacts'], [contact])
                self.assertEqual(call(route, session=self.b)[1]['contacts'], [])
                target = route + '/' + contact['id']
                self.assertEqual(call(target + '/update', 'POST', FORM, session=self.b)[0], 404)
                self.assertEqual(call(target + '/delete', 'POST', {}, session=self.b)[0], 404)
                self.assertEqual(call(target + '/update', 'POST', {**FORM, 'alias': '朋友'})[1]['contact']['alias'], '朋友')
                self.assertEqual(call(target + '/delete', 'POST', {})[0], 200)
                self.assertEqual(call(route)[1]['contacts'], [])
                self.assertEqual(self.financial_state(), before)
                self.assertEqual(call('/api/finance/session/logout', 'POST', {})[0], 200)
                self.assertEqual(call(route, 'POST', FORM)[0], 409)
                self.assertEqual(self.g.remittance_contacts(self.w)['contacts'], [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


if __name__ == '__main__':
    unittest.main()
