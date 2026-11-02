"""聊天文档的有界本地文字提取。仅读取原件，不执行宏、公式或外部关系。"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import posixpath
import re
import signal
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

if __name__ == '__main__':
    # 隔离worker不把项目目录加入sys.path，不加载应用/业务模块。
    class BankError(Exception):
        def __init__(self, code, message, status):
            self.code, self.status = code, status
            super().__init__(message)
else:
    from simulated_bank import BankError

MAX_DOCUMENT = 10 * 1024 * 1024
MAX_TEXT_CHARS = 120_000
MAX_ZIP_BYTES = 40 * 1024 * 1024
MAX_ZIP_PART = 8 * 1024 * 1024
MAX_ZIP_MEMBERS = 1024
MAX_XML_NODES = 300_000
MAX_PDF_PAGES = 200
PDF_TIMEOUT_SECONDS = 12
DOCUMENT_MIMES = {
    'application/pdf': 'pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
    'text/plain': 'txt',
    'text/markdown': 'md',
    'text/csv': 'csv',
}
DOCUMENT_FIELDS = {'file_name', 'document_format', 'text_chars', 'extraction_status', 'extraction_warning'}
_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_P = '{http://schemas.openxmlformats.org/package/2006/relationships}'
_CT = '{http://schemas.openxmlformats.org/package/2006/content-types}'


def reject(code='CHAT_DOCUMENT_INVALID', status=415):
    messages = {
        'CHAT_DOCUMENT_INVALID': '文档真实类型、文件扩展名或内容不受支持。',
        'CHAT_DOCUMENT_EMPTY': '文件为空或没有可读取的文字。',
        'CHAT_DOCUMENT_TOO_LARGE': '文档超过允许的大小、页数、解压体积或文字数量，请拆分后上传。',
        'CHAT_DOCUMENT_PARSE_TIMEOUT': '文档本地解析超时，请简化或拆分后上传。',
        'CHAT_DOCUMENT_PARSER_UNAVAILABLE': '本机缺少 PDF 文字解析依赖，暂时无法处理 PDF。',
    }
    raise BankError(code, messages[code], status)


def normalized_file_name(value, document_format):
    if (type(value) is not str or not 1 <= len(value) <= 180 or value != value.strip()
            or any(unicodedata.category(c).startswith('C') for c in value)
            or '/' in value or '\\' in value or value in ('.', '..')
            or PurePosixPath(value).suffix.lower() != '.' + document_format):
        reject()
    return value


def valid_document_fields(value):
    """Descriptor 中只含提取状态，正文永远由受限内部读取取得。"""
    try:
        fmt = DOCUMENT_MIMES[value['mime_type']]
        normalized_file_name(value['file_name'], fmt)
        status, count, warning = value['extraction_status'], value['text_chars'], value['extraction_warning']
        return (value['document_format'] == fmt and type(count) is int and 0 <= count <= MAX_TEXT_CHARS
                and ((status == 'text' and count > 0 and warning is None)
                     or (status == 'no_text' and count == 0 and type(warning) is str and 0 < len(warning) <= 200)))
    except (KeyError, TypeError, BankError):
        return False


class _Text:
    def __init__(self):
        self.parts, self.count = [], 0

    def add(self, text):
        # 只移除不可见控制字符；保留换行、制表符和用户原文中的标点。
        if len(text) > MAX_TEXT_CHARS:
            reject('CHAT_DOCUMENT_TOO_LARGE', 413)
        text = ''.join(c for c in text if c in '\n\t' or not unicodedata.category(c).startswith('C')).strip()
        if not text:
            return
        self.count += len(text) + bool(self.parts)
        if self.count > MAX_TEXT_CHARS:
            reject('CHAT_DOCUMENT_TOO_LARGE', 413)
        self.parts.append(text)

    def result(self):
        return '\n'.join(self.parts)


def _xml(raw):
    # ElementTree 不需要 DTD；显式拒绝任何 DTD/实体声明，包括 UTF-16 编码。
    probe = raw.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in probe or b'<!ENTITY' in probe:
        reject()
    root = ET.fromstring(raw)
    pending, count = [(root, 0)], 0
    while pending:
        element, depth = pending.pop()
        count += 1
        if depth > 128 or count > MAX_XML_NODES:
            reject('CHAT_DOCUMENT_TOO_LARGE', 413)
        pending.extend((child, depth + 1) for child in element)
    return root


class _Package:
    def __init__(self, raw):
        if not raw.startswith(b'PK\x03\x04'):
            reject()
        self.zip = zipfile.ZipFile(io.BytesIO(raw))
        entries = self.zip.infolist()
        if len(entries) > MAX_ZIP_MEMBERS or sum(x.file_size for x in entries) > MAX_ZIP_BYTES:
            reject('CHAT_DOCUMENT_TOO_LARGE', 413)
        self.names = set()
        for item in entries:
            name = item.filename
            if (name in self.names or name.startswith('/') or '\\' in name or '..' in PurePosixPath(name).parts
                    or item.flag_bits & 1 or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or (item.external_attr >> 16) & 0o170000 == 0o120000):
                reject()
            if (item.file_size > MAX_ZIP_PART
                    or item.file_size > max(1, item.compress_size) * 150):
                reject('CHAT_DOCUMENT_TOO_LARGE', 413)
            if re.search(r'(^|/)vbaproject\.|(^|/)activex/', name, re.I):
                reject()
            self.names.add(name)
        if '[Content_Types].xml' not in self.names or '_rels/.rels' not in self.names:
            reject()
        self.types = self.xml('[Content_Types].xml')
        if self.types.tag != _CT + 'Types' or any('macroenabled' in str(x.attrib).lower() or 'vbaproject' in str(x.attrib).lower()
                                                 for x in self.types):
            reject()

    def xml(self, name):
        if name not in self.names:
            reject()
        raw = self.zip.read(name)  # ZIP CRC 校验及已验证的解包体积上限。
        if len(raw) > MAX_ZIP_PART:
            reject('CHAT_DOCUMENT_TOO_LARGE', 413)
        return _xml(raw)

    def relationships(self, name, base):
        result = {}
        for rel in self.xml(name):
            if rel.tag != _P + 'Relationship' or rel.attrib.get('TargetMode') == 'External':
                continue  # 不解析、更不获取外部链接。
            target = rel.attrib.get('Target', '')
            if not target or '\\' in target or '#' in target or '?' in target or ':' in target:
                reject()
            target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(base, target))
            if target.startswith('../') or target not in self.names or rel.attrib.get('Id') in result:
                reject()
            result[rel.attrib.get('Id')] = (target, rel.attrib.get('Type', ''))
        return result

    def require_main(self, main, content_type):
        types = [x.attrib.get('ContentType') for x in self.types
                 if x.tag == _CT + 'Override' and x.attrib.get('PartName') == '/' + main]
        relationships = self.relationships('_rels/.rels', '')
        if types != [content_type] or not any(target == main and kind == _R[1:-1] + '/officeDocument'
                                               for target, kind in relationships.values()):
            reject()


def _ooxml(raw, fmt):
    package, output = _Package(raw), _Text()
    try:
        if fmt == 'docx':
            package.require_main('word/document.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml')
            root = package.xml('word/document.xml')
            if root.tag != _W + 'document':
                reject()
            for paragraph in root.iter(_W + 'p'):
                parts = []
                for element in paragraph.iter():
                    if element.tag == _W + 't':
                        parts.append(element.text or '')
                    elif element.tag == _W + 'tab':
                        parts.append('\t')
                    elif element.tag in (_W + 'br', _W + 'cr'):
                        parts.append('\n')
                output.add(''.join(parts))
        elif fmt == 'xlsx':
            package.require_main('xl/workbook.xml', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
            workbook = package.xml('xl/workbook.xml')
            if workbook.tag != _S + 'workbook':
                reject()
            rels = package.relationships('xl/_rels/workbook.xml.rels', 'xl')
            shared = []
            if 'xl/sharedStrings.xml' in package.names:
                strings = package.xml('xl/sharedStrings.xml')
                if strings.tag != _S + 'sst':
                    reject()
                for item in strings.findall(_S + 'si'):
                    shared.append(''.join(x.text or '' for x in item.iter(_S + 't')))
            sheets = list(workbook.iter(_S + 'sheet'))
            if len(sheets) > 200:
                reject('CHAT_DOCUMENT_TOO_LARGE', 413)
            seen_parts = set()
            for sheet in sheets:
                relation = rels.get(sheet.attrib.get(_R + 'id'))
                if not relation or relation[1] != _R[1:-1] + '/worksheet' or relation[0] in seen_parts:
                    reject()
                seen_parts.add(relation[0])
                output.add('工作表：' + sheet.attrib.get('name', ''))
                root = package.xml(relation[0])
                if root.tag != _S + 'worksheet':
                    reject()
                sheet_data = root.findall(_S + 'sheetData')
                if len(sheet_data) != 1:
                    reject()
                for row in sheet_data[0].findall(_S + 'row'):
                    cells, row_chars = [], 0
                    for cell in row.findall(_S + 'c'):
                        kind, value = cell.attrib.get('t'), cell.find(_S + 'v')
                        text = value.text or '' if value is not None else ''
                        if kind == 's':
                            if not text.isdigit() or int(text) >= len(shared):
                                reject()
                            text = shared[int(text)]
                        elif kind == 'inlineStr':
                            text = ''.join(x.text or '' for x in cell.iter(_S + 't'))
                        elif kind == 'b' and text in ('0', '1'):
                            text = 'TRUE' if text == '1' else 'FALSE'
                        if len(text) > MAX_TEXT_CHARS:
                            reject('CHAT_DOCUMENT_TOO_LARGE', 413)
                        # 公式不求值；只有文件自带的缓存才可读，并明确标记。
                        if cell.find(_S + 'f') is not None:
                            text = '[公式缓存] ' + text if text else '[公式未计算]'
                        if text:
                            address = cell.attrib.get('r', '')
                            if not re.fullmatch('[A-Z]{1,3}[1-9][0-9]{0,6}', address):
                                reject()
                            row_chars += len(address) + 1 + len(text) + bool(cells)
                            if row_chars + output.count + bool(output.parts) > MAX_TEXT_CHARS:
                                reject('CHAT_DOCUMENT_TOO_LARGE', 413)
                            cells.append(address + '=' + text)
                    output.add('\t'.join(cells))
        else:
            package.require_main('ppt/presentation.xml', 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml')
            root = package.xml('ppt/presentation.xml')
            ns = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
            if root.tag != ns + 'presentation':
                reject()
            rels = package.relationships('ppt/_rels/presentation.xml.rels', 'ppt')
            slides = list(root.iter(ns + 'sldId'))
            if len(slides) > 200:
                reject('CHAT_DOCUMENT_TOO_LARGE', 413)
            seen_parts = set()
            for number, slide in enumerate(slides, 1):
                relation = rels.get(slide.attrib.get(_R + 'id'))
                if not relation or relation[1] != _R[1:-1] + '/slide' or relation[0] in seen_parts:
                    reject()
                seen_parts.add(relation[0])
                content = package.xml(relation[0])
                if content.tag != ns + 'sld':
                    reject()
                labelled = False
                for paragraph in content.iter(_A + 'p'):
                    text = ''.join(x.text or '' for x in paragraph.iter(_A + 't'))
                    if text.strip():
                        if not labelled:
                            output.add('幻灯片 ' + str(number))
                            labelled = True
                        output.add(text)
        return output.result()
    finally:
        package.zip.close()


def _plain(raw):
    # UTF-8/UTF-16 BOM及常见中文GB18030；拒绝二进制伪装和HTML/XML载荷。
    if raw.startswith((b'PK\x03\x04', b'%PDF-', b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'\xd0\xcf\x11\xe0', b'\x7fELF')):
        reject()
    encodings = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030')
    text = None
    for encoding in encodings:
        try:
            text = raw.decode(encoding, errors='strict')
            break
        except UnicodeError:
            pass
    if text is None or any(ord(c) < 32 and c not in '\t\n\r' for c in text):
        reject()
    if re.match(r'\s*(?:<\?xml|<!doctype|<html(?:\s|>))', text, re.I):
        reject()
    output = _Text()
    output.add(text.replace('\r\n', '\n').replace('\r', '\n'))
    if not output.result():
        reject('CHAT_DOCUMENT_EMPTY', 422)
    return output.result()


def _pdf_worker(raw):
    # 进程内设置CPU/内存边界；父进程另有真实墙钟超时并终止整个进程组。
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        if sys.platform.startswith('linux'):
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        elif hasattr(resource, 'RLIMIT_DATA'):
            resource.setrlimit(resource.RLIMIT_DATA, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except (ImportError, OSError, ValueError):
        pass
    try:
        import pdfplumber
    except ImportError:
        return {'error': 'CHAT_DOCUMENT_PARSER_UNAVAILABLE', 'status': 503}
    try:
        output = _Text()
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            # 不接纳加密PDF，不尝试口令、JavaScript或外部资源。
            if pdf.doc.encryption:
                reject()
            pages = pdf.pages
            if not pages:
                reject('CHAT_DOCUMENT_EMPTY', 422)
            if len(pages) > MAX_PDF_PAGES:
                reject('CHAT_DOCUMENT_TOO_LARGE', 413)
            for number, page in enumerate(pages, 1):
                text = page.extract_text() or ''
                if text.strip():
                    output.add('第 ' + str(number) + ' 页')
                    output.add(text)
                page.close()
        return {'text': output.result()}
    except BankError as error:
        return {'error': error.code, 'status': error.status}
    except Exception:
        return {'error': 'CHAT_DOCUMENT_INVALID', 'status': 415}


def _pdf(raw):
    if not re.match(rb'%PDF-1\.[0-9]|%PDF-2\.0', raw[:8]) or b'%%EOF' not in raw[-2048:]:
        reject()
    process = subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), '--extract-pdf'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True, env={'PATH': os.defpath, 'PYTHONIOENCODING': 'utf-8'})
    try:
        data, _ = process.communicate(raw, timeout=PDF_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=.3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        reject('CHAT_DOCUMENT_PARSE_TIMEOUT', 422)
    if process.returncode or len(data) > MAX_TEXT_CHARS * 12 + 1024:
        reject()
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        reject()
    if type(result) is not dict:
        reject()
    if 'error' in result:
        reject(result['error'], result['status'])
    if set(result) != {'text'} or type(result['text']) is not str or len(result['text']) > MAX_TEXT_CHARS:
        reject()
    return result['text']


def inspect_document(raw, mime_type, file_name):
    if type(raw) is not bytes or not raw:
        reject('CHAT_DOCUMENT_EMPTY', 422)
    if len(raw) > MAX_DOCUMENT:
        reject('CHAT_DOCUMENT_TOO_LARGE', 413)
    fmt = DOCUMENT_MIMES.get(mime_type)
    if fmt is None:
        reject()
    file_name = normalized_file_name(file_name, fmt)
    try:
        text = _pdf(raw) if fmt == 'pdf' else _ooxml(raw, fmt) if fmt in ('docx', 'xlsx', 'pptx') else _plain(raw)
    except BankError:
        raise
    except Exception:
        reject()
    warning = None if text else ('PDF 没有可提取的文字，可能是扫描件；当前未执行 OCR，请提供文字版。'
                                  if fmt == 'pdf' else '文档没有可提取的文字，图片、图表及嵌入对象未识别。')
    metadata = {'kind': 'document', 'mime_type': mime_type, 'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(), 'duration_ms': None, 'audio_format': None,
                'file_name': file_name, 'document_format': fmt, 'text_chars': len(text),
                'extraction_status': 'text' if text else 'no_text', 'extraction_warning': warning}
    return metadata, text


if __name__ == '__main__' and sys.argv[1:] == ['--extract-pdf']:
    # 用绝对脚本路径和 -I 启动，不加载项目业务模块。
    print(json.dumps(_pdf_worker(sys.stdin.buffer.read(MAX_DOCUMENT + 1)), ensure_ascii=True))
