"""Real loopback TLS, synthetic RPCs only; no board, credentials or model."""
import http.server
import http.client
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from blackoutburst_agents.board_reader import BoardReader,BoardReadError,MAX_RESPONSE_BYTES


class Server(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def get_request(self):
        result=super().get_request();self.connections+=1
        if self.handshake_delay:time.sleep(self.handshake_delay)
        result[0].do_handshake()
        return result
    def handle_error(self,*_):pass


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def setup(self):
        super().setup();self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
    def log_message(self,*_):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append((body,self.headers.get('Authorization'),self.headers.get('Mcp-Session-Id')))
        behavior=self.server.behavior
        if behavior=='lost':
            self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();self.close_connection=True;return
        payload=json.dumps({'jsonrpc':'2.0','id':body['id'],'result':{'synthetic':True}}).encode()
        if behavior=='slow_headers':
            try:
                self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ');self.wfile.flush()
                for _ in range(20):
                    time.sleep(0.025);self.wfile.write(b'a');self.wfile.flush()
            except OSError:pass
            self.close_connection=True;return
        self.send_response(302 if behavior=='redirect' else 200)
        self.send_header('Content-Type','application/json')
        size=len(payload)+20 if behavior=='partial' else MAX_RESPONSE_BYTES+1 if behavior=='oversize' else len(payload)
        self.send_header('Content-Length',str(size))
        self.send_header('Mcp-Session-Id','synthetic-transport-session')
        if behavior=='redirect':self.send_header('Location','https://invalid.example/mcp')
        if behavior=='close':self.send_header('Connection','close');self.close_connection=True
        self.end_headers()
        if behavior=='oversize':return
        try:
            if behavior=='slow_body':
                for byte in payload:
                    self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(0.025)
            else:self.wfile.write(payload);self.wfile.flush()
        except OSError:pass
        if behavior in ('partial','silent_close'):
            self.close_connection=True


class PersistentTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='synthetic-mcp-tls-')
        cls.directory=Path(cls.temp.name);cls.cert=cls.directory/'cert.pem';key=cls.directory/'key.pem'
        config=cls.directory/'openssl.cnf'
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:true\n')
        executable=shutil.which('openssl')
        if executable is None:raise RuntimeError('local_tls_fixture_requires_openssl')
        subprocess.run([executable,'req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',str(key),'-out',str(cls.cert),'-config',str(config)],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        cls.server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(str(cls.cert),str(key))

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def setUp(self):
        self.server=Server(('127.0.0.1',0),Handler)
        self.server.connections=0;self.server.requests=[];self.server.behavior='normal'
        self.server.handshake_delay=0
        self.server.socket=self.server_context.wrap_socket(self.server.socket,server_side=True,
            do_handshake_on_connect=False)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.endpoint='https://127.0.0.1:'+str(self.server.server_port)+'/mcp'
        self.context=ssl.create_default_context(cafile=str(self.cert));self.readers=[]

    def stop(self):
        for reader in self.readers:reader.close()
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

    def reader(self,timeout=1,trusted=True):
        config={'mcp_url':self.endpoint,'token':'SYNTHETIC_TEST_TOKEN','client':'codex','identity_key':'fixture','name':'Fixture'}
        if trusted:
            with patch('blackoutburst_agents.board_reader._tls_context',return_value=self.context):
                reader=BoardReader(config,'synthetic-session',timeout)
        else:reader=BoardReader(config,'synthetic-session',timeout)
        self.readers.append(reader);return reader

    def rpc(self,reader,index=1):return reader._rpc('synthetic/read',{'index':index})

    def failure(self,reader,code):
        with self.assertRaises(BoardReadError) as error:self.rpc(reader)
        self.assertEqual(error.exception.code,code)
        self.assertNotIn('SYNTHETIC_TEST_TOKEN',str(error.exception))

    def test_tls_connection_reused_with_same_rpc_auth_and_session_headers(self):
        reader=self.reader()
        for index in range(5):self.assertEqual(self.rpc(reader,index),{'synthetic':True})
        self.assertEqual(self.server.connections,1)
        self.assertEqual(len(self.server.requests),5)
        self.assertEqual([r[0]['id'] for r in self.server.requests],list(range(1,6)))
        self.assertTrue(all(r[1]=='Bearer SYNTHETIC_TEST_TOKEN' for r in self.server.requests))
        self.assertIsNone(self.server.requests[0][2])
        self.assertTrue(all(r[2]=='synthetic-transport-session' for r in self.server.requests[1:]))

    def test_rejected_tls_certificate_receives_no_http_request(self):
        self.failure(self.reader(trusted=False),'connection_failed')
        self.assertEqual(self.server.requests,[])

    def test_slow_tls_handshake_expires_without_sending_a_post(self):
        self.server.handshake_delay=0.15
        self.failure(self.reader(timeout=0.05),'timeout')
        self.assertEqual(self.server.requests,[])

    def test_late_connect_return_checks_deadline_before_first_request_bytes(self):
        original=http.client.HTTPSConnection.connect
        def late_connect(connection):
            original(connection)
            time.sleep(0.075)
        with patch.object(http.client.HTTPSConnection,'connect',late_connect):
            self.failure(self.reader(timeout=0.05),'timeout')
        self.assertEqual(self.server.requests,[])

    def test_owner_closed_during_connect_closes_late_socket_without_post(self):
        reader=self.reader();original=http.client.HTTPSConnection.connect
        attached=[]
        def closed_during_connect(connection):
            reader.close()
            original(connection)
            attached.append(connection.sock)
        with patch.object(http.client.HTTPSConnection,'connect',closed_during_connect):
            self.failure(reader,'connection_failed')
        self.assertEqual(self.server.requests,[])
        self.assertEqual(len(attached),1)
        self.assertEqual(attached[0].fileno(),-1)
        self.assertIsNone(reader._opener._connection)
        self.failure(reader,'connection_closed')

    def test_lost_response_is_not_replayed_and_next_explicit_call_reconnects(self):
        reader=self.reader();self.server.behavior='lost'
        self.failure(reader,'connection_failed')
        self.assertEqual(len(self.server.requests),1)
        self.server.behavior='normal';self.rpc(reader,2)
        self.assertEqual(len(self.server.requests),2);self.assertEqual(self.server.connections,2)
        self.assertEqual([r[0]['params']['index'] for r in self.server.requests],[1,2])

    def test_silently_closed_keepalive_is_not_retried(self):
        reader=self.reader();self.server.behavior='silent_close';self.rpc(reader)
        time.sleep(0.025);self.server.behavior='normal'
        self.failure(reader,'connection_failed')
        self.assertEqual(len(self.server.requests),1)
        self.rpc(reader,3)
        self.assertEqual(len(self.server.requests),2);self.assertEqual(self.server.connections,2)

    def test_connection_close_header_allows_only_next_explicit_request(self):
        reader=self.reader();self.server.behavior='close'
        self.rpc(reader);self.assertEqual(len(self.server.requests),1)
        self.rpc(reader,2)
        self.assertEqual(len(self.server.requests),2);self.assertEqual(self.server.connections,2)

    def test_partial_body_discards_connection_without_accepting_valid_json_prefix(self):
        reader=self.reader();self.server.behavior='partial'
        self.failure(reader,'connection_failed');self.assertEqual(len(self.server.requests),1)
        self.server.behavior='normal';self.rpc(reader,2)
        self.assertEqual(self.server.connections,2)

    def test_redirect_is_not_followed_and_oversize_is_bounded(self):
        reader=self.reader();self.server.behavior='redirect';self.failure(reader,'redirect_blocked')
        self.assertEqual(len(self.server.requests),1)
        self.server.behavior='oversize';self.failure(reader,'response_too_large')
        self.assertEqual(len(self.server.requests),2);self.assertEqual(self.server.connections,2)

    def test_slow_headers_and_body_obey_whole_rpc_deadline(self):
        for behavior in ('slow_headers','slow_body'):
            self.server.behavior=behavior;reader=self.reader(timeout=0.08)
            start=time.monotonic();self.failure(reader,'timeout')
            self.assertLess(time.monotonic()-start,0.5)

    def test_reader_close_is_explicit_idempotent_and_never_reopens(self):
        reader=self.reader();self.rpc(reader);reader.close();reader.close()
        self.failure(reader,'connection_closed');self.assertEqual(len(self.server.requests),1)

    def test_benchmark_same_rpc_count_reports_measured_times_and_connections(self):
        count=12
        start=time.perf_counter()
        for index in range(count):
            with self.reader() as reader:self.rpc(reader,index)
        fresh=time.perf_counter()-start;fresh_connections=self.server.connections
        start=time.perf_counter()
        with self.reader() as reader:
            for index in range(count):self.rpc(reader,index)
        reused=time.perf_counter()-start
        self.assertEqual(fresh_connections,count)
        self.assertEqual(self.server.connections-fresh_connections,1)
        self.assertEqual(len(self.server.requests),2*count)
        self.assertEqual([r[0]['params'] for r in self.server.requests[:count]],
                         [r[0]['params'] for r in self.server.requests[count:]])
        print(json.dumps({'synthetic_loopback_tls':True,'rpc_count_each':count,
            'fresh_connections':fresh_connections,'reused_connections':self.server.connections-fresh_connections,
            'fresh_seconds':round(fresh,6),'persistent_seconds':round(reused,6),'automatic_retries':0}))
