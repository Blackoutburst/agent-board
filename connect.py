#!/usr/bin/env python3
"""Pair an agent installation and bridge MCP over stdio without token env setup.

Download this file from your board, inspect it, then run python3 board-connect.py.
Only the bridge's JSON-RPC responses go to stdout in --stdio mode.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
import platform
from pathlib import Path
import re
import secrets
import stat
import sys
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request


def protect_path(path, directory=False):
    if os.name == 'nt':
        user = getpass.getuser()
        domain = os.environ.get('USERDOMAIN', '')
        principal = (domain+'\\' if domain else '')+user
        permission = '(OI)(CI)F' if directory else 'F'
        result = subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',principal+':'+permission],
                                capture_output=True,text=True)
        if result.returncode:
            raise RuntimeError('Could not restrict the local credential permissions. Nothing was installed.')
    else:
        os.chmod(path, 0o700 if directory else 0o600)


def private_write(path, contents):
    path = Path(path)
    payload = contents if isinstance(contents, bytes) else contents.encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.'+path.name+'-')
    try:
        # Binary output avoids Windows newline translation. Backups may supply
        # original bytes; generated text is encoded exactly once as UTF-8.
        with os.fdopen(fd, 'wb') as handle:
            protect_path(Path(temporary))
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def request(url, payload, token=None):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    if token:
        headers['Authorization'] = 'Bearer '+token
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        # Do not follow redirects with a pairing credential or bearer token.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as response:
            data = response.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Board returned HTTP {error.code}; check the address, pairing code or device revocation.') from None
    except (urllib.error.URLError, ValueError):
        raise RuntimeError('Could not connect to the board or read its JSON response.') from None


def validate_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise ValueError('Use the board address without credentials, a query, or a fragment.')
    if parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1')):
        raise ValueError('The board requires HTTPS; HTTP is supported only for local tests.')
    return url.rstrip('/')


def rpc(config, method, params, request_id=1):
    result = request(config['mcp_url'], {'jsonrpc':'2.0','id':request_id,'method':method,'params':params}, config['token'])
    if not result or 'error' in result:
        raise RuntimeError('The MCP request was rejected. Check your device and owner permissions.')
    return result['result']


def write_codex(path, installed, directory, client):
    path = Path(path).expanduser()
    original_bytes = path.read_bytes() if path.exists() else b''
    original = original_bytes.decode('utf-8')
    # Remove exactly this server's table and nested subtables, preserving all other configuration.
    lines = original.splitlines(keepends=True)
    kept, skipping = [], False
    for line in lines:
        if line.lstrip().startswith('['):
            heading = line.strip()
            skipping = bool(re.match(r'^\[mcp_servers\.(?:blackoutburst_board|"blackoutburst_board")(?:\.|\])', heading))
        if not skipping:
            kept.append(line)
    args = [str(installed), '--stdio', '--client', client, '--config-dir', str(directory)]
    block = '\n[mcp_servers.blackoutburst_board]\ncommand = '+json.dumps(sys.executable)+'\nargs = '+json.dumps(args)+'\n'
    if original:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        private_write(path.with_name(path.name+'.before-board-'+stamp), original_bytes)
    private_write(path, ''.join(kept).rstrip()+'\n'+block)


def write_claude(path, installed, directory, client):
    path = Path(path).expanduser()
    original_bytes = path.read_bytes() if path.exists() else b''
    original = original_bytes.decode('utf-8')
    data = json.loads(original) if original else {}
    data.setdefault('mcpServers', {})['blackoutburst_board'] = {'type':'stdio','command':sys.executable,
        'args':[str(installed),'--stdio','--client',client,'--config-dir',str(directory)]}
    if original:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        private_write(path.with_name(path.name+'.before-board-'+stamp), original_bytes)
    private_write(path, json.dumps(data, indent=2)+'\n')


def confirm_registration(result, config, session_id):
    """Never forward a call under an old profile after rejected registration."""
    try:
        if not isinstance(result, dict) or result.get('isError'):
            raise ValueError
        data = result.get('structuredContent')
        if data is None:
            data = json.loads(result['content'][0]['text'])
        if isinstance(data, dict) and set(data) == {'result'}:
            data = data['result']
        identity, session = data['identity'], data['session']
        if (not identity['id'] or identity['id'] != session['agent_id']
                or identity['owner_id'] != session['owner_id']
                or identity['device_id'] != session['device_id']
                or identity['identity_key'] != config['identity_key']
                or identity['agent_kind'] != config['client'] or session['id'] != session_id):
            raise ValueError
    except (ValueError, TypeError, KeyError, IndexError):
        raise RuntimeError('Agent registration was not confirmed. Reconnect using a new session for the selected profile.') from None


def bridge(config):
    registered = set()
    # MCP stdio is UTF-8, independently of the process locale (for example
    # Windows CP1252). Keep text-only streams usable for embedded callers.
    input_stream = getattr(sys.stdin, 'buffer', sys.stdin)
    for line in input_stream:
        message = None
        registering = None
        try:
            if isinstance(line, bytes):
                line = line.decode('utf-8')
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            if message.get('method') == 'tools/call':
                params = message.get('params', {})
                arguments = params.setdefault('arguments', {})
                session_id = arguments.get('session_id')
                if params.get('name') == 'register_agent':
                    registered.discard(session_id)
                    registering = session_id
                    arguments.update(agent_kind=config['client'], identity_key=config['identity_key'], name=config['name'])
                    os_skill = {'darwin':'macos'}.get(platform.system().lower(),platform.system().lower())
                    arguments['skills'] = list(dict.fromkeys(arguments.get('skills', [])+[os_skill]))
                elif session_id and session_id not in registered:
                    registration = rpc(config, 'tools/call', {'name':'register_agent','arguments':{
                        'session_id':session_id,'label':arguments.get('label', config['name']+' chat'),
                        'agent_kind':config['client'],'identity_key':config['identity_key'],'name':config['name'],
                        'skills':[{'darwin':'macos'}.get(platform.system().lower(),platform.system().lower())]}})
                    confirm_registration(registration, config, session_id)
                    registered.add(session_id)
            response = request(config['mcp_url'], message, config['token'])
            if registering is not None:
                confirm_registration(response.get('result') if isinstance(response, dict) else None, config, registering)
                registered.add(registering)
            if response is not None and 'id' in message:
                print(json.dumps(response), flush=True)
        except Exception as error:
            if isinstance(message, dict) and 'id' in message:
                # Error text contains no headers, credentials or request bodies.
                print(json.dumps({'jsonrpc':'2.0','id':message['id'],'error':{
                    'code':-32000,'message':str(error) if isinstance(error, RuntimeError) else 'Local board connection failed'}}), flush=True)


def select_agent(connection_path, identity_key):
    """Select a human-bound profile without copying an account or device token."""
    if (not isinstance(identity_key, str) or not identity_key.strip() or len(identity_key) > 200
            or any(ord(c) < 32 or ord(c) == 127 for c in identity_key)):
        raise ValueError('Use the exact agent key shown by the board.')
    if connection_path.is_symlink():
        raise RuntimeError('The existing connection must be a private regular file.')
    descriptor = os.open(connection_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, encoding='utf-8') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536
                or (os.name != 'nt' and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600))):
            raise RuntimeError('The existing connection must be a private regular file.')
        original = source.read(65537)
        if len(original.encode('utf-8')) > 65536:
            raise RuntimeError('The existing connection exceeds the size limit.')
    config = json.loads(original)
    if config.get('client') not in ('codex', 'claude-code', 'other'):
        raise RuntimeError('The existing connection is invalid.')
    endpoint = config.get('mcp_url', '')
    if not endpoint.endswith('/mcp') or validate_url(endpoint[:-4]) + '/mcp' != endpoint:
        raise RuntimeError('The existing MCP address is invalid.')
    selected = {**config, 'identity_key': identity_key}
    rpc(selected, 'initialize', {'protocolVersion':'2025-03-26','capabilities':{},
        'clientInfo':{'name':'board-profile-selection','version':'1'}})
    response = rpc(selected, 'tools/call', {'name':'register_agent','arguments':{
        'session_id':'profile-selection-'+secrets.token_hex(16),'label':'Profile connection verification',
        'agent_kind':config['client'],'identity_key':identity_key,'skills':[],'autonomous':False}})
    try:
        if not isinstance(response, dict) or response.get('isError'):
            raise ValueError
        data = response.get('structuredContent')
        if data is None:
            data = json.loads(response['content'][0]['text'])
        if isinstance(data, dict) and set(data) == {'result'}:
            data = data['result']
        profile, identity, session = data['profile'], data['identity'], data['session']
        if (not profile.get('id') or identity['id'] != profile['id'] or session['agent_id'] != profile['id']
                or identity['owner_id'] != profile['owner_id'] or session['owner_id'] != profile['owner_id']
                or identity['device_id'] != session['device_id'] or identity['agent_kind'] != config['client']
                or not isinstance(profile.get('name'), str) or not profile['name'].strip()):
            raise ValueError
    except (ValueError, TypeError, KeyError, IndexError):
        raise RuntimeError('Profile selection was not confirmed. Check its binding, permissions and status in the board.') from None
    selected['name'] = profile['name']
    # A failed verification above leaves the original connection unchanged.
    if connection_path.is_symlink() or connection_path.read_text(encoding='utf-8') != original:
        raise RuntimeError('The connection changed during verification. Retry after inspecting the current configuration.')
    # The client runs this installed copy, not necessarily the freshly downloaded
    # selector. Upgrade its registration checks before publishing the new key.
    installed = connection_path.parent / 'connect.py'
    if Path(__file__).resolve() != installed.resolve():
        private_write(installed, Path(__file__).read_text(encoding='utf-8'))
    private_write(connection_path, json.dumps(selected, indent=2)+'\n')
    return {'profile_id': profile['id'], 'name': profile['name']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='https://board.blackoutburst.com')
    parser.add_argument('--client', choices=['codex','claude-code','other'], default='codex')
    parser.add_argument('--name', help='Persistent display name, for example Codex · Work Mac')
    parser.add_argument('--select-agent-key', help='Select an existing human-bound agent using this connection; no new pairing.')
    parser.add_argument('--config-dir', type=Path, default=Path.home()/'.config'/'blackoutburst-board')
    parser.add_argument('--codex-config', type=Path, default=Path.home()/'.codex'/'config.toml')
    parser.add_argument('--claude-config', type=Path, default=Path.home()/'.claude.json')
    parser.add_argument('--stdio', action='store_true')
    args = parser.parse_args()
    directory = args.config_dir.expanduser().resolve()
    connection_path = directory/(args.client+'.json')
    if args.select_agent_key is not None:
        if args.stdio:
            raise ValueError('Select the profile before starting the stdio bridge.')
        select_agent(connection_path, args.select_agent_key)
        print('Agent profile selected. Restart the client and use a new chat session; existing work is not reassigned.')
        return
    if args.stdio:
        try:
            config = json.loads(connection_path.read_text(encoding='utf-8'))
            bridge(config)
        except Exception:
            print('Board connection is unavailable. Run the connector to pair this agent.', file=sys.stderr)
            raise SystemExit(1)
        return
    url = validate_url(args.url)
    print('In the board, open Connect agent, name this computer, and generate a pairing code.')
    code = getpass.getpass('One-time pairing code (hidden): ').strip().upper()
    paired = request(url+'/mcp/connect/exchange', {'code':code})
    expected_mcp = url+'/mcp'
    if paired.get('mcp_url') != expected_mcp:
        raise RuntimeError('The board returned a different MCP address; pairing was not installed.')
    old = json.loads(connection_path.read_text(encoding='utf-8')) if connection_path.exists() else {}
    title = {'codex':'Codex','claude-code':'Claude Code','other':'Agent'}[args.client]
    config = {**paired,'client':args.client,'identity_key':old.get('identity_key') or secrets.token_hex(16),
        'name':args.name or old.get('name') or title+' · '+paired['label']}
    rpc(config, 'initialize', {'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'board-connector','version':'2'}})
    tools = rpc(config, 'tools/list', {})['tools']
    if not {'register_agent','list_projects','get_work','claim_next_ticket'}.issubset({tool['name'] for tool in tools}):
        raise RuntimeError('This board does not provide the required work coordination tools.')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    protect_path(directory, directory=True)
    private_write(connection_path, json.dumps(config, indent=2)+'\n')
    installed = directory/'connect.py'
    if Path(__file__).resolve() != installed:
        private_write(installed, Path(__file__).read_text(encoding='utf-8'))
    if args.client == 'codex':
        write_codex(args.codex_config, installed, directory, args.client)
    elif args.client == 'claude-code':
        write_claude(args.claude_config, installed, directory, args.client)
    print(f"Connected {config['name']}. Verified {len(tools)} MCP tools.")
    print('Restart your agent client, then ask it to register this chat and read your project work.')
    print('The credential stays in a private local file; the client configuration contains no token or tool allowlist.')


if __name__ == '__main__':
    main()
