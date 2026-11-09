"""本地文档解析与隔离私库测试；不使用真实账号、不调用模型或网络。"""
from contextlib import closing
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote
import uuid
import zipfile

from simulated_bank import BankError
from base_runtime import chat_documents as documents
from base_runtime.chat_media import ChatMediaService, inspect_bytes, validate_descriptor
import test_chat_media as media_fixture


MIME = {fmt: mime for mime, fmt in documents.DOCUMENT_MIMES.items()}
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'


def package(fmt, *, text='文档中的真实文字', extra=None):
    main, kind = {'docx': ('word/document.xml', 'wordprocessingml.document'),
                  'xlsx': ('xl/workbook.xml', 'spreadsheetml.sheet'),
                  'pptx': ('ppt/presentation.xml', 'presentationml.presentation')}[fmt]
    content = f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/{main}" ContentType="application/vnd.openxmlformats-officedocument.{kind}.main+xml"/></Types>'
    parts = {'[Content_Types].xml': content,
             '_rels/.rels': f'<Relationships xmlns="{PKG}"><Relationship Id="r1" Type="{REL}/officeDocument" Target="{main}"/></Relationships>'}
    if fmt == 'docx':
        parts[main] = f'<w:document xmlns:w="{documents._W[1:-1]}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格值</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
    elif fmt == 'xlsx':
        parts[main] = f'<workbook xmlns="{documents._S[1:-1]}" xmlns:r="{REL}"><sheets><sheet name="数据" sheetId="1" r:id="sheet1"/></sheets></workbook>'
        parts['xl/_rels/workbook.xml.rels'] = f'<Relationships xmlns="{PKG}"><Relationship Id="sheet1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        parts['xl/sharedStrings.xml'] = f'<sst xmlns="{documents._S[1:-1]}"><si><t>{text}</t></si></sst>'
        parts['xl/worksheets/sheet1.xml'] = f'<worksheet xmlns="{documents._S[1:-1]}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><f>SUM(A2:A3)</f><v>3</v></c><c r="D1"><f>HYPERLINK(&quot;https://invalid.local&quot;)</f></c></row></sheetData></worksheet>'
    else:
        ns = 'http://schemas.openxmlformats.org/presentationml/2006/main'
        parts[main] = f'<p:presentation xmlns:p="{ns}" xmlns:r="{REL}"><p:sldIdLst><p:sldId id="256" r:id="s1"/></p:sldIdLst></p:presentation>'
        parts['ppt/_rels/presentation.xml.rels'] = f'<Relationships xmlns="{PKG}"><Relationship Id="s1" Type="{REL}/slide" Target="slides/slide1.xml"/></Relationships>'
        parts['ppt/slides/slide1.xml'] = f'<p:sld xmlns:p="{ns}" xmlns:a="{documents._A[1:-1]}"><p:cSld><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:cSld></p:sld>'
    parts.update(extra or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return out.getvalue()


def pdf_bytes(text='sample document', pages=1):
    # 合法最小PDF，使用内置字体，无需测试依赖或磁盘原件。
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'']
    kids = []
    for number in range(pages):
        page_id, stream_id = 3 + number * 2, 4 + number * 2
        kids.append(f'{page_id} 0 R')
        stream = (f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET' if text else '').encode()
        objects.extend([f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents {stream_id} 0 R >>'.encode(),
                        f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream'])
    objects[1] = f'<< /Type /Pages /Count {pages} /Kids [{" ".join(kids)}] >>'.encode()
    result, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f'{number} 0 obj\n'.encode() + value + b'\nendobj\n')
    xref = len(result)
    result.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for at in offsets[1:]:
        result.extend(f'{at:010d} 00000 n \n'.encode())
    result.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(result)


class DocumentParserTests(unittest.TestCase):
    def deny(self, callback, code=None):
        with self.assertRaises(BankError) as caught:
            callback()
        if code:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_text_markdown_csv_utf8_utf16_and_chinese_encoding(self):
        for fmt, raw in (('txt', '个人资料\r\n保留原始字节'.encode()), ('md', '# 标题\n\n正文'.encode()),
                         ('csv', '日期,金额\n2026-10-27,100'.encode()), ('txt', '中文文字'.encode('utf-16')),
                         ('txt', '中文旧编码'.encode('gb18030'))):
            with self.subTest(fmt=fmt, raw=raw[:10]):
                metadata, text = documents.inspect_document(raw, MIME[fmt], '原始附件.' + fmt)
                self.assertEqual(metadata['kind'], 'document')
                self.assertEqual(metadata['text_chars'], len(text))
                self.assertEqual(metadata['sha256'], hashlib.sha256(raw).hexdigest())
                self.assertTrue(documents.valid_document_fields(metadata))
                self.assertNotIn('extracted_text', metadata)

    def test_ooxml_document_table_workbook_positions_and_slide_text(self):
        for fmt in ('docx', 'xlsx', 'pptx'):
            with self.subTest(fmt=fmt):
                metadata, text = documents.inspect_document(package(fmt), MIME[fmt], '附件.' + fmt)
                self.assertIn('文档中的真实文字', text)
                self.assertEqual(metadata['extraction_status'], 'text')
                if fmt == 'docx':
                    self.assertIn('表格值', text)
                if fmt == 'xlsx':
                    self.assertIn('工作表：数据', text)
                    self.assertIn('A1=文档中的真实文字', text)
                    self.assertIn('C1=[公式缓存] 3', text)
                    self.assertIn('D1=[公式未计算]', text)
                    self.assertNotIn('invalid.local', text)

    def test_real_pdf_text_scan_warning_and_page_limit(self):
        metadata, text = documents.inspect_document(pdf_bytes(), MIME['pdf'], '附件.pdf')
        self.assertIn('sample document', text)
        self.assertEqual(metadata['extraction_status'], 'text')
        metadata, text = documents.inspect_document(pdf_bytes(''), MIME['pdf'], '扫描.pdf')
        self.assertEqual(text, '')
        self.assertEqual(metadata['extraction_status'], 'no_text')
        self.assertIn('未执行 OCR', metadata['extraction_warning'])
        self.deny(lambda: documents.inspect_document(pdf_bytes(pages=201), MIME['pdf'], '过长.pdf'), 'CHAT_DOCUMENT_TOO_LARGE')

    def test_empty_bad_extension_mime_disguise_and_binary_are_rejected(self):
        for raw, mime, name in ((b'', MIME['txt'], '空.txt'), (b' \r\n\t', MIME['txt'], '空.txt'),
                                (b'abc', MIME['txt'], 'file.pdf'), (b'abc', MIME['txt'], '../file.txt'),
                                (b'abc', MIME['txt'], 'bad\n.txt'), (b'abc', MIME['txt'], 'bad.exe'),
                                (package('docx'), MIME['txt'], 'fake.txt'), (package('docx'), MIME['xlsx'], 'fake.xlsx'),
                                (b'\x00binary\x01', MIME['txt'], 'fake.txt'), (b'<html>abc</html>', MIME['txt'], 'fake.txt'),
                                (b'%PDF-1.7\nnot actually a PDF\n%%EOF', MIME['pdf'], 'fake.pdf')):
            with self.subTest(name=name, mime=mime):
                self.deny(lambda: documents.inspect_document(raw, mime, name))

    def test_document_and_text_character_limits_are_rejections(self):
        self.deny(lambda: documents.inspect_document(b'a' * (documents.MAX_DOCUMENT + 1), MIME['txt'], 'large.txt'), 'CHAT_DOCUMENT_TOO_LARGE')
        self.deny(lambda: documents.inspect_document(b'a' * (documents.MAX_TEXT_CHARS + 1), MIME['txt'], 'long.txt'), 'CHAT_DOCUMENT_TOO_LARGE')
        raw = package('docx', text='字符' * documents.MAX_TEXT_CHARS)
        self.deny(lambda: documents.inspect_document(raw, MIME['docx'], 'long.docx'), 'CHAT_DOCUMENT_TOO_LARGE')
        # 同一个共享字符串在一行复用时，也要在拼接整行前拦截展开体积。
        shared = ''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(500))
        cells = ''.join(f'<c r="{chr(65+i)}1" t="s"><v>0</v></c>' for i in range(10))
        sheet = f'<worksheet xmlns="{documents._S[1:-1]}"><sheetData><row>{cells}</row></sheetData></worksheet>'
        raw = package('xlsx', text=shared, extra={'xl/worksheets/sheet1.xml': sheet})
        lengths, original = [], documents._Text.add
        def counted(output, text):
            lengths.append(len(text))
            return original(output, text)
        with patch.object(documents._Text, 'add', counted):
            self.deny(lambda: documents.inspect_document(raw, MIME['xlsx'], 'expanded.xlsx'), 'CHAT_DOCUMENT_TOO_LARGE')
        self.assertLessEqual(max(lengths), documents.MAX_TEXT_CHARS)

    def test_zip_bomb_entity_macro_duplicate_traversal_and_encryption_are_rejected(self):
        for extras in ({'word/huge.xml': 'a' * 100_000}, {'../escape.xml': '<x/>'},
                       {'word/vbaProject.bin': b'macro'}, {'word/activeX/activeX1.xml': '<x/>'},
                       {'word/document.xml': '<!DOCTYPE a [<!ENTITY b "boom">]><a>&b;</a>'},
                       {'[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'}):
            with self.subTest(extras=list(extras)):
                self.deny(lambda: documents.inspect_document(package('docx', extra=extras), MIME['docx'], 'unsafe.docx'))
        raw = bytearray(package('docx'))
        central = raw.find(b'PK\x01\x02')
        raw[central + 8] |= 1
        self.deny(lambda: documents.inspect_document(bytes(raw), MIME['docx'], 'encrypted.docx'))
        out = io.BytesIO(package('docx'))
        with zipfile.ZipFile(out, 'a') as archive:
            with self.assertWarns(UserWarning):
                archive.writestr('word/document.xml', '<duplicate/>')
        self.deny(lambda: documents.inspect_document(out.getvalue(), MIME['docx'], 'duplicate.docx'))

    def test_pdf_timeout_terminates_isolated_process_group(self):
        # 使用真实睡眠子进程验证墙钟超时会杀进程，无模型或网络。
        real_popen = subprocess.Popen
        spawned = []
        def sleeping(*args, **kwargs):
            process = real_popen([os.sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            spawned.append(process)
            return process
        start = time.monotonic()
        with patch.object(documents, 'PDF_TIMEOUT_SECONDS', .1), patch.object(documents.subprocess, 'Popen', sleeping):
            self.deny(lambda: documents.inspect_document(pdf_bytes(), MIME['pdf'], 'timeout.pdf'), 'CHAT_DOCUMENT_PARSE_TIMEOUT')
        self.assertLess(time.monotonic() - start, 3)
        self.assertTrue(all(process.poll() is not None for process in spawned))


class DocumentStorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = media_fixture.ChatMediaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.media, self.g, self.w = self.f.media, self.f.g, self.f.w

    def upload(self, *, key=None, name='私有资料.txt'):
        raw = 'private-document-sentinel\n可复核原件'.encode()
        with self.f.scope() as db:
            result = self.media.put_user(self.w, raw, MIME['txt'], key or uuid.uuid4().hex, bank_db=db,
                                        expected_privacy=0, expected_epoch=0, file_name=name)
        return raw, result['media']

    def test_encrypted_original_idempotent_descriptor_and_bound_current_turn_text(self):
        key = uuid.uuid4().hex
        raw, item = self.upload(key=key)
        self.assertEqual(item, self.upload(key=key)[1])
        self.assertEqual(validate_descriptor(item), item)
        self.assertNotIn('extracted_text', item)
        self.f.deny('CHAT_MEDIA_NOT_FOUND', lambda: self.f.read(item['media_id']))
        rid, archived = self.f.archive([item['media_id']])
        self.assertEqual(archived['attachments'][0]['file_name'], '私有资料.txt')
        self.assertEqual(self.f.read(item['media_id'])[0], raw)
        with self.f.scope() as db:
            result = self.media.read_document_text(self.w, item['media_id'], request_id=rid, bank_db=db)
            self.assertIn('private-document-sentinel', result['extracted_text'])
            self.f.deny('CHAT_MEDIA_NOT_FOUND', lambda: self.media.read_document_text(self.w, item['media_id'], request_id='other-turn', bank_db=db))
        with closing(self.g.personal_data.connect()) as db:
            row = db.execute('SELECT payload_cipher,metadata_cipher FROM chat_media_objects').fetchone()
            self.assertNotIn(b'private-document-sentinel', bytes(row[0]))
            self.assertNotIn('私有资料', row[1])
        self.f.deny('CHAT_MEDIA_UPLOAD_KEY_CONFLICT', lambda: self.upload(key=key, name='另一个文件.txt'))

    def test_cross_owner_persona_revocation_and_pending_expiry_still_apply(self):
        raw, item = self.upload()
        rid, _ = self.f.archive([item['media_id']])
        other = self.f.fixture.select('grain_farmer', subject='welcome-' + uuid.uuid4().hex)
        with self.f.scope(other) as db:
            self.f.deny('CHAT_MEDIA_NOT_FOUND', lambda: self.media.read_document_text(other['workspace_id'], item['media_id'], request_id=rid, bank_db=db))
        _, pending = self.upload()
        with closing(self.g.personal_data.connect()) as db, db:
            db.execute('UPDATE chat_media_objects SET expires_at=0 WHERE media_id=?', (pending['media_id'],))
        self.f.deny('CHAT_MEDIA_NOT_FOUND', lambda: self.f.archive([pending['media_id']]))
        self.g.experience_provider.revoke_subject(self.f.fixture.owner, self.f.fixture.subject)
        self.f.deny('SESSION_CHANGED', lambda: self.f.read(item['media_id']))

    def test_binary_http_document_filename_download_and_context_limits(self):
        responses, headers = [], Message()
        raw = 'HTTP文档内容'.encode()
        for name, value in {'Host': '127.0.0.1:8765', 'Cookie': 'qz_bank_user=' + self.f.token,
                            'X-QZ-CSRF': self.g.csrf(self.f.token), 'Content-Type': MIME['txt'],
                            'Content-Length': str(len(raw)), 'X-Navren-Expected-Workspace': self.w,
                            'X-Navren-Privacy-Revision': '0', 'X-Navren-Archive-Epoch': '0',
                            'X-Navren-Upload-Key': uuid.uuid4().hex, 'X-Navren-File-Name': quote('财务资料.txt')}.items():
            headers[name] = value
        handler = SimpleNamespace(path='/api/finance/chat-media', headers=headers, server=SimpleNamespace(server_port=8765),
                                  client_address=('127.0.0.1', 0), rfile=io.BytesIO(raw),
                                  _finance_json=lambda *args: responses.append(args))
        self.media.upload_http(handler)
        self.assertEqual(responses[-1][0], 201)
        item = responses[-1][1]['media']
        self.assertEqual(item['file_name'], '财务资料.txt')
        self.f.archive([item['media_id']])
        captured = []
        handler.path += '/' + item['media_id']
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: captured.append(('status', status))
        handler.send_header = lambda *args: captured.append(args)
        handler.end_headers = lambda: None
        self.media.read_http(handler, item['media_id'])
        self.assertEqual(handler.wfile.getvalue(), raw)
        self.assertIn(('Content-Disposition', "attachment; filename*=UTF-8''" + quote('财务资料.txt', safe='')), captured)
        handler.path = '/api/finance/chat-media/context'
        self.media.context_http(handler)
        self.assertEqual(responses[-1][1]['limits']['document_text_chars'], documents.MAX_TEXT_CHARS)
        headers.replace_header('X-Navren-File-Name', quote('../资料.txt'))
        handler.path = '/api/finance/chat-media'
        handler.rfile = io.BytesIO(raw)
        self.f.deny('CHAT_DOCUMENT_INVALID', lambda: self.media.upload_http(handler), 415)


if __name__ == '__main__':
    unittest.main(verbosity=2)
