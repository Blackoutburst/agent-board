"""One bounded HTTP/1.1 connection per MCP reader, never a request retry.

The caller retains every RPC, header, deadline and authority decision. Only the
socket can survive a fully consumed response. A failed/partial exchange discards
it; a later explicit request may open a new connection without replaying bytes.
"""
from __future__ import annotations

import http.client
import io
import math
import threading
import time
import urllib.parse


def _remaining(deadline):
    value=deadline-time.monotonic()
    if value<=0:raise TimeoutError
    return value


class _DeadlineRaw(io.RawIOBase):
    """Refresh the remaining wall budget before each socket read, even headers."""
    def __init__(self,sock):
        super().__init__();self._sock=sock;self._file=sock.makefile('rb',buffering=0)
        self.deadline=None

    def readable(self):return True

    def readinto(self,buffer):
        self._sock.settimeout(_remaining(self.deadline))
        size=self._file.readinto(buffer)
        _remaining(self.deadline)
        return size

    def close(self):
        try:self._file.close()
        finally:super().close()


class _ResponseSocket:
    def __init__(self,sock,deadline):self.sock,self.deadline=sock,deadline
    def makefile(self,mode):
        if mode!='rb':raise ValueError('unsupported_response_stream')
        raw=_DeadlineRaw(self.sock);raw.deadline=self.deadline
        return io.BufferedReader(raw)


class _DeadlineConnection:
    def _require_active(self):
        owner=getattr(self,'_transport',None)
        if (getattr(self,'_invalidated',False) or not self.auto_open or owner is None
                or owner._closed or owner._connection is not self):
            # close() may have run while connect() had not attached its socket
            # yet. Close that late socket too; this object can never reopen.
            self.close()
            raise OSError('connection_invalidated')

    def connect(self):
        self._require_active()
        self.timeout=_remaining(self.exchange_deadline)
        try:
            super().connect()
            self._require_active()
            # A late connection must not send the first request bytes before
            # checking both owner validity and its remaining exchange budget.
            sock=self.sock
            if sock is not None:sock.settimeout(_remaining(self.exchange_deadline))
        except BaseException:
            self.close()
            raise

    def send(self,data):
        self._require_active()
        self.timeout=_remaining(self.exchange_deadline)
        if self.sock is None:self.connect()
        self._require_active()
        sock=self.sock
        if sock is not None:sock.settimeout(_remaining(self.exchange_deadline))
        super().send(data)
        _remaining(self.exchange_deadline)


class _HTTPConnection(_DeadlineConnection,http.client.HTTPConnection):pass
class _HTTPSConnection(_DeadlineConnection,http.client.HTTPSConnection):pass


class _Response:
    def __init__(self,transport,response,url):
        self.transport,self.response,self.url=transport,response,url
        self.headers,self.status=response.headers,response.status
        self.complete,self.closed=False,False

    @property
    def fp(self):return self.response.fp

    def geturl(self):return self.url

    def read1(self,size):
        data=self.response.read1(size)
        if not data and self.response.length not in (None,0):
            raise http.client.IncompleteRead(b'')
        if self.response.isclosed() or self.response.length==0:self.complete=True
        return data

    read=read1

    def __enter__(self):return self

    def __exit__(self,kind,*_):
        self.close(failed=kind is not None)

    def close(self,*,failed=False):
        if self.closed:return
        self.closed=True
        try:
            if failed or not self.complete or self.response.will_close:
                self.transport._drop()
        finally:
            try:self.response.close()
            finally:self.transport._lock.release()


class PersistentMCPTransport:
    """Synchronous, origin-pinned transport; no proxy, redirect or auto-replay."""
    def __init__(self,endpoint,context):
        parts=urllib.parse.urlsplit(endpoint)
        if (parts.path!='/mcp' or parts.query or parts.fragment or parts.username is not None
                or parts.password is not None or not parts.hostname
                or parts.scheme not in ('http','https')
                or parts.scheme=='http' and parts.hostname not in ('localhost','127.0.0.1','::1')):
            raise ValueError('invalid_transport_endpoint')
        self.endpoint,self.parts,self.context=endpoint,parts,context
        self._connection,self._closed=None,False
        self._lock=threading.Lock()

    def _drop(self):
        connection,self._connection=self._connection,None
        if connection is not None:
            connection._invalidated=True
            connection.auto_open=0
            connection.close()

    def discard(self):
        # A protocol failure may abort this owner's current channel, but never
        # waits behind another caller or replays that caller's request.
        self._drop()

    def close(self):
        # Closing an owner invalidates future calls; no other reader shares it.
        self._closed=True
        self._drop()

    def open(self,request,timeout):
        if (type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout<=0
                or request.full_url!=self.endpoint or request.get_method()!='POST'):
            raise ValueError('invalid_transport_request')
        deadline=time.monotonic()+timeout
        if not self._lock.acquire(timeout=timeout):raise TimeoutError
        response=None
        try:
            if self._closed:raise OSError('transport_closed')
            if self._connection is None:
                chosen=_HTTPSConnection if self.parts.scheme=='https' else _HTTPConnection
                options={'context':self.context} if self.parts.scheme=='https' else {}
                connection=chosen(self.parts.hostname,self.parts.port,
                    timeout=_remaining(deadline),**options)
                connection._transport=self
                self._connection=connection
            connection=self._connection;connection.exchange_deadline=deadline
            connection.response_class=lambda sock,**options:http.client.HTTPResponse(
                _ResponseSocket(sock,deadline),**options)
            connection.request('POST',self.parts.path,body=request.data,headers=dict(request.header_items()))
            response=connection.getresponse()
            _remaining(deadline)
            return _Response(self,response,self.endpoint)
        except BaseException:
            try:
                self._drop()
            finally:
                try:
                    if response is not None:response.close()
                finally:self._lock.release()
            raise
