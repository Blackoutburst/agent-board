"""Protocol bytes and configuration stay UTF-8 under legacy host codecs."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'connect.py'
spec = importlib.util.spec_from_file_location('connector_unicode', SCRIPT)
connector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connector)


@pytest.mark.parametrize('host_codec', ['cp1252', 'latin-1', 'ascii'])
def test_bridge_reads_utf8_protocol_bytes_independently_of_host_codec(monkeypatch, capsys, host_codec):
    body = 'Été déjà publié — l’œuvre 🪟 日本語'
    message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'post_message', 'arguments': {'body': body}}}
    protocol_bytes = (json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8')
    stdin = io.TextIOWrapper(io.BytesIO(protocol_bytes), encoding=host_codec)
    seen = []

    def request(url, payload, token):
        seen.append(payload)
        return {'jsonrpc': '2.0', 'id': payload['id'], 'result': payload['params']['arguments']}

    monkeypatch.setattr(connector.sys, 'stdin', stdin)
    monkeypatch.setattr(connector, 'request', request)
    connector.bridge({'mcp_url': 'https://board.test/mcp', 'token': 'synthetic-token'})
    assert seen[0]['params']['arguments']['body'] == body
    assert json.loads(capsys.readouterr().out)['result']['body'] == body


def test_connector_preserves_utf8_files_under_non_utf8_locale(tmp_path):
    # A subprocess controls Python's actual default file codec; mocking locale
    # accessors does not change the codec selected internally by open().
    code = r'''
import importlib.util, json, locale, os
from pathlib import Path
spec = importlib.util.spec_from_file_location('connector', os.environ['BOARD_CONNECTOR_SCRIPT'])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
root = Path(os.environ['BOARD_TEST_ROOT'])
original = 'title = "caf\u00e9"\n[mcp_servers.other]\ncommand="kept"\n'
path = root / 'config.toml'; path.write_bytes(original.encode('utf-8'))
module.write_codex(path, root / 'connect.py', root, 'codex')
assert 'caf\u00e9'.encode('utf-8') in path.read_bytes()
backup = next(root.glob('config.toml.before-board-*'))
assert backup.read_bytes() == original.encode('utf-8')
text = 'Fran\u00e7ais \u2014 \u65e5\u672c\u8a9e'
module.private_write(root / 'unicode.txt', text)
assert (root / 'unicode.txt').read_bytes() == text.encode('utf-8')
print(json.dumps({'codec': locale.getpreferredencoding(False), 'preserved': True}))
'''
    result = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', code],
        env={**os.environ, 'LC_ALL': 'C', 'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0',
             'PYTHONIOENCODING': 'ascii', 'BOARD_CONNECTOR_SCRIPT': str(SCRIPT),
             'BOARD_TEST_ROOT': str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['preserved']


def test_stdio_process_round_trips_unicode_through_http_under_cp1252(tmp_path):
    text = 'Été — l’œuvre 🪟 日本語'
    observed = []

    class Echo(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            observed.append(message['params']['clientInfo']['name'])
            body = json.dumps({'jsonrpc': '2.0', 'id': message['id'],
                               'result': {'text': text}}, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Echo)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        config = {'mcp_url': f'http://127.0.0.1:{server.server_port}/mcp',
                  'token': 'synthetic-local-token', 'name': text, 'client': 'codex'}
        (tmp_path / 'codex.json').write_bytes(json.dumps(config, ensure_ascii=False).encode('utf-8'))
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                   'params': {'clientInfo': {'name': text}}}
        result = subprocess.run([sys.executable, str(SCRIPT), '--stdio', '--config-dir', str(tmp_path)],
            input=(json.dumps(message, ensure_ascii=False) + '\n').encode('utf-8'),
            env={**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'},
            capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode('ascii', errors='replace')
        assert observed == [text]
        assert json.loads(result.stdout)['result']['text'] == text
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
