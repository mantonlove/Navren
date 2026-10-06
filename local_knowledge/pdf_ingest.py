"""固定版本的本地PDF文字提取；仅接受人工核对的页内原文跨度，不产生金融解释。"""
from __future__ import annotations
from functools import lru_cache
from importlib import metadata
from io import BytesIO
from types import MappingProxyType

from .store import KnowledgeError,MAX_TEXT,packed,sha

PARSER_ID='pdfplumber-text/1'
SHAPE='pdfplumber-pages/1'
PDFPLUMBER_VERSION='0.11.9'
PDFMINER_VERSION='20251230'
EXTRACTOR_VERSION='pdfplumber/'+PDFPLUMBER_VERSION+';pdfminer.six/'+PDFMINER_VERSION
MAX_PAGES=400
PARAMETERS=MappingProxyType({'layout':False,'x_tolerance':3,'y_tolerance':3,'x_tolerance_ratio':None,'y_tolerance_ratio':None,
    'keep_blank_chars':False,'use_text_flow':False,'line_dir':'ttb','char_dir':'ltr',
    'split_at_punctuation':False,'expand_ligatures':True})
SEPARATOR='\n\n'


def _dependencies():
    try:
        if metadata.version('pdfplumber')!=PDFPLUMBER_VERSION or metadata.version('pdfminer.six')!=PDFMINER_VERSION:
            raise KnowledgeError('pdf_parser_version_mismatch')
        import pdfplumber
    except (ImportError,metadata.PackageNotFoundError):
        raise KnowledgeError('pdf_parser_unavailable') from None
    return pdfplumber


@lru_cache(maxsize=2)
def _extract_cached(raw):
    pdfplumber=_dependencies()
    try:
        with pdfplumber.open(BytesIO(raw),laparams=None,unicode_norm=None,repair=False,raise_unicode_errors=True) as pdf:
            if not 1<=len(pdf.pages)<=MAX_PAGES:raise KnowledgeError('pdf_page_limit')
            pages=[];parts=[];offset=0
            for number,page in enumerate(pdf.pages,1):
                text=page.extract_text(**PARAMETERS) or ''
                if any(ord(char)<32 and char not in '\n\r\t' for char in text):
                    raise KnowledgeError('pdf_text_control_character')
                end=offset+len(text)
                if end>MAX_TEXT:raise KnowledgeError('pdf_text_limit')
                pages.append((number,text,offset,end));parts.append(text)
                offset=end+len(SEPARATOR)
                page.close()
            text=SEPARATOR.join(parts)
            if not text.strip():raise KnowledgeError('pdf_text_unavailable')
            return text,tuple(pages)
    except KnowledgeError:raise
    except Exception:raise KnowledgeError('pdf_parse_failed') from None


def extract_pdf(raw_bytes):
    """原件完整bytes输入；确定性逐页提取并返回可审核的半开字符定位。"""
    if not isinstance(raw_bytes,bytes) or not raw_bytes.startswith(b'%PDF-') or not 1<=len(raw_bytes)<=16*1024*1024:
        raise KnowledgeError('invalid_pdf_original')
    _dependencies()  # 命中缓存也不允许变化后的解析依赖版本混入。
    text,pages=_extract_cached(raw_bytes)
    return {'schema_version':1,'text':text,'pages':[{'page':n,'text':page_text,'start_char':start,'end_char':end,
        'text_sha256':sha(page_text)} for n,page_text,start,end in pages],
        'parser_id':PARSER_ID,'shape':SHAPE,'extractor_version':EXTRACTOR_VERSION,
        'parameters':dict(PARAMETERS),'page_separator':SEPARATOR,'raw_sha256':sha(raw_bytes),'text_sha256':sha(text),
        'nontext_page_count':sum(not page_text.strip() for _,page_text,_,_ in pages)}


def validate_page_spans(spans,extracted):
    """每个引用必须在声明的单页完整范围内；不能跨页或修改提取原文。"""
    if not isinstance(spans,list) or not spans:raise KnowledgeError('pdf_reviewed_spans_required')
    pages={p['page']:p for p in extracted['pages']}
    for span in spans:
        if not isinstance(span,dict) or type(span.get('page')) is not int:raise KnowledgeError('pdf_page_locator_required')
        page=pages.get(span['page']);start,end=span.get('start_char'),span.get('end_char')
        if (page is None or type(start) is not int or type(end) is not int
                or not page['start_char']<=start<end<=page['end_char']):raise KnowledgeError('pdf_span_page_mismatch')
        if 'excerpt' in span and span['excerpt']!=extracted['text'][start:end]:raise KnowledgeError('quote_mismatch')


def stage_reviewed_pdf(provider,raw_bytes,reviewed_spans, *, source_id,source_url,title,reviewed_by,review_reference,
                       published_on=None,retrieved_on=None,effective_from=None,effective_to=None,
                       product_id=None,share_class_id=None,channel_id=None,document_key='document',data_on=None):
    """只保存候选；显式审核与发布由受信调用方完成，日期与身份不从PDF自动猜测。"""
    if not all(isinstance(v,str) and v.strip() for v in (reviewed_by,review_reference)):
        raise KnowledgeError('pdf_human_review_required')
    extracted=extract_pdf(raw_bytes);validate_page_spans(reviewed_spans,extracted)
    spans=[]
    for span in reviewed_spans:
        value=dict(span)
        value.setdefault('paragraph','pdf-page:'+str(span['page'])+':chars:'+str(span['start_char'])+'-'+str(span['end_char']))
        spans.append(value)
    version=provider.stage_document(source_id=source_id,source_url=source_url,title=title,text=extracted['text'],raw_bytes=raw_bytes,
        parser_id=PARSER_ID,shape=SHAPE,spans=spans,published_on=published_on,retrieved_on=retrieved_on,
        effective_from=effective_from,effective_to=effective_to,product_id=product_id,share_class_id=share_class_id,
        channel_id=channel_id,document_key=document_key,data_on=data_on)
    return {'version_id':version,'parser_id':PARSER_ID,'shape':SHAPE,'extractor_version':EXTRACTOR_VERSION,
        'raw_sha256':extracted['raw_sha256'],'text_sha256':extracted['text_sha256'],'page_count':len(extracted['pages']),
        'nontext_page_count':extracted['nontext_page_count'],'approved':False,'published':False,
        'verification_basis':packed({'method':'human_reviewed_pdf_span','reviewed_by':reviewed_by,
            'review_reference':review_reference,'extractor_version':EXTRACTOR_VERSION,'parameters':dict(PARAMETERS),
            'raw_sha256':extracted['raw_sha256'],'text_sha256':extracted['text_sha256']})}
