"""固定公开端点读取；调用有总截止，阻塞读取占用的线程数有界。"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import threading
import time
import urllib.request

from .store import KnowledgeError

_READERS=ThreadPoolExecutor(max_workers=2,thread_name_prefix='public-source')
_SLOTS=threading.BoundedSemaphore(2)


def _remaining(deadline):
    remaining=deadline-time.monotonic()
    if remaining<=0:raise KnowledgeError('deadline_exceeded')
    return remaining


def _read(provider,source_id,url,deadline,max_bytes):
    class RegisteredRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            provider.validate_source_url(source_id,newurl)
            req.timeout=_remaining(deadline)
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    request=urllib.request.Request(url,headers={'User-Agent':'Navren/1.0 registered-public-evidence','Accept':'application/json,text/html,text/plain'})
    with urllib.request.build_opener(RegisteredRedirect()).open(request,timeout=_remaining(deadline)) as response:
        provider.validate_source_url(source_id,response.geturl())
        parts=[];size=0
        while True:
            remaining=_remaining(deadline)
            # 标准 urllib HTTPSResponse 的底层 socket；每次读取仅使用剩余预算。
            raw=getattr(getattr(response,'fp',None),'raw',None)
            sock=getattr(raw,'_sock',None)
            if sock is not None:sock.settimeout(remaining)
            block=response.read1(min(65536,max_bytes-size+1))
            _remaining(deadline)
            if not block:break
            size+=len(block)
            if size>max_bytes:raise KnowledgeError('response_too_large')
            parts.append(block)
        if not parts:raise KnowledgeError('empty_source_response')
        return b''.join(parts)


def fetch_registered(provider,source_id,url, *, timeout=8,max_bytes=2*1024*1024):
    source=provider.validate_source_url(source_id,url)
    if not source['enabled'] or not source['read_allowed'] or source['paused']:
        raise KnowledgeError('source_permission_missing')
    timeout=max(.01,min(float(timeout),15));deadline=time.monotonic()+timeout
    if type(max_bytes) is not int or not 1<=max_bytes<=16*1024*1024:
        raise KnowledgeError('invalid_byte_limit')
    if not _SLOTS.acquire(blocking=False):raise KnowledgeError('source_reader_busy')
    try:future=_READERS.submit(_read,provider,source_id,url,deadline,max_bytes)
    except BaseException:
        _SLOTS.release();raise
    future.add_done_callback(lambda _: _SLOTS.release())
    try:return future.result(timeout=_remaining(deadline))
    except TimeoutError:
        future.cancel()
        raise KnowledgeError('deadline_exceeded') from None
    # DNS 无法可靠强制取消。迟到读取只占已有的至多两个槽，结果不会回传。
