"""宿主安装的历史信息源；没有 HTTP 安装/恢复/续签入口。"""
from contextlib import contextmanager
from copy import deepcopy
import threading
from .catalog_information import (CatalogInformationSource, CatalogInformationObserver,
    validate_snapshot_shape, fail)

class CatalogInformationHost:
    def __init__(self):
        self._lock=threading.RLock(); self._selected={}; self._issued={}; self._history_authority=None
    def bind_history_authority(self, authority):
        if not callable(getattr(authority,'bind_catalog_source',None)):fail('CATALOG_HISTORY_AUTHORITY_REQUIRED')
        with self._lock:
            previous=self._history_authority
            if previous is not None and (getattr(previous,'g',None)is not getattr(authority,'g',None)
                    or getattr(previous,'store',None)is not getattr(authority,'store',None)):
                fail('CATALOG_HISTORY_AUTHORITY_CHANGED')
            if previous is None:self._history_authority=authority
            selected=list(self._selected.values())
        # 私库登记按bank→source锁序，不能持host锁等待bank。
        for source in selected:authority.bind_catalog_source(source)
    def install(self, workspace, source):
        if type(workspace) is not str or not workspace or type(source) is not CatalogInformationSource:fail()
        with self._lock:authority=self._history_authority
        if authority is not None:authority.bind_catalog_source(source)
        with self._lock:
            if self._history_authority is not authority:fail('CATALOG_HISTORY_AUTHORITY_CHANGED')
            previous=self._selected.get(workspace)
            self._selected[workspace]=source
        # 不在 host 锁内等待 source 锁；发布持 source 锁时仍需回查 host 选源。
        if previous is not None and previous is not source:previous.revoke(selection_only=True)
    def remove(self, workspace):
        with self._lock:
            source=self._selected.pop(workspace,None)
        if source is not None:source.revoke()
    def pin(self, *, request_id, workspace, privacy_revision, personal_revision, public_only):
        if type(public_only) is not bool:fail()
        if not public_only:return None
        if (type(request_id) is not str or not request_id or type(workspace) is not str or not workspace
            or type(privacy_revision) is not int or type(personal_revision) is not int):fail()
        with self._lock:source=self._selected.get(workspace)
        if source is None:return None
        if type(source) is not CatalogInformationSource:fail()
        binding=(request_id,workspace,privacy_revision,personal_revision)
        observer=source.pin(trusted_request_binding=binding)
        if type(observer) is not CatalogInformationObserver:fail()
        snapshot=observer.snapshot()
        with self._lock:
            if self._selected.get(workspace) is not source:fail('CATALOG_INFORMATION_HOST_BINDING_REQUIRED')
            if len(self._issued)>=256:self._issued.pop(next(iter(self._issued)))
            self._issued[snapshot['source_handle']]=(source,observer,deepcopy(snapshot),binding)
            return observer
    def lookup(self, snapshot, *, workspace, privacy_revision, personal_revision, request_id=None):
        snapshot=validate_snapshot_shape(snapshot)
        if (type(workspace) is not str or not workspace or type(privacy_revision) is not int
            or type(personal_revision) is not int or (request_id is not None and type(request_id) is not str)):fail()
        with self._lock:
            entry=self._issued.get(snapshot['source_handle'])
            if entry is None:fail('CATALOG_INFORMATION_HOST_BINDING_REQUIRED')
            source,observer,issued,binding=entry
            if (type(source) is not CatalogInformationSource or type(observer) is not CatalogInformationObserver
                or self._selected.get(workspace) is not source or snapshot!=issued
                or (workspace,privacy_revision,personal_revision)!=binding[1:]
                or (request_id is not None and request_id!=binding[0])):fail('CATALOG_INFORMATION_HOST_BINDING_REQUIRED')
        observer.assert_fresh()
        return observer
    @contextmanager
    def guard(self, snapshot, *, workspace, privacy_revision, personal_revision, request_id=None):
        if snapshot is None:
            yield None
            return
        # 全程 host → source 固定锁序，选源不能在发布中更换。
        with self._lock:
            observer=self.lookup(snapshot,workspace=workspace,privacy_revision=privacy_revision,
                personal_revision=personal_revision,request_id=request_id)
            with observer.publication_guard() as checkpoint:
                self.lookup(snapshot,workspace=workspace,privacy_revision=privacy_revision,
                    personal_revision=personal_revision,request_id=request_id)
                yield checkpoint


def response_snapshot(response):
    if type(response) is not dict:fail()
    preparation=response.get('knowledge_preparation')
    if preparation is None:return None
    if type(preparation) is not dict:fail()
    value=preparation.get('catalog_information')
    return validate_snapshot_shape(value) if value is not None else None
