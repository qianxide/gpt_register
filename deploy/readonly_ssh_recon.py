"""Scoped read-only recon. Credentials stay in memory; JSON output is allowlisted.

Examples: python deploy/readonly_ssh_recon.py info
          python deploy/readonly_ssh_recon.py probe --route socks
Only the five explicit IPv4 targets and known ports 22/8790 are reachable here.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
from http.cookies import SimpleCookie
import ipaddress
import json
import logging
from pathlib import Path
import re
import socket
import time

import paramiko
import socks
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent.parent
ALLOWED = tuple('45.205.31.' + s for s in ('123', '227', '204', '253', '69'))
PROXY = ('127.0.0.1', 7897)
PROXY_CONFIG = Path('C:/Users/Administrator/AppData/Roaming/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml')
logging.getLogger('paramiko').setLevel(logging.CRITICAL)


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def credentials():
    text = (WORKSPACE / '多服务器部署说明文档.md').read_text(encoding='utf-8-sig')
    rows = {}
    for line in text.splitlines():
        cells = [s.strip().strip('`') for s in line.strip().strip('|').split('|')]
        if len(cells) >= 5 and cells[1] in ALLOWED:
            rows[cells[1]] = (cells[2], cells[3])
    if set(rows) != set(ALLOWED):
        raise ValueError('Credential table missing allowed targets')
    return rows


def panel_credentials():
    text = (WORKSPACE / '多服务器部署说明文档.md').read_text(encoding='utf-8-sig')
    values = {}
    for line in text.splitlines():
        match = re.match(r'^\s*-\s*(面板管理员|面板密码)[：:]\s*(.*?)\s*$', line)
        if match:
            values[match[1]] = match[2].strip('`')
    return values['面板管理员'], values['面板密码']


def connect(host, port, route, timeout=12):
    if host not in ALLOWED or port not in (22, 8790):
        raise ValueError('Target outside diagnostic allowlist')
    if route == 'socks':
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, *PROXY)
        sock.settimeout(timeout)
        sock.connect((host, port))
    elif route == 'http':
        sock = socket.create_connection(PROXY, timeout)
        sock.sendall(f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n'.encode())
        response = b''
        while b'\r\n\r\n' not in response and len(response) < 8192:
            block = sock.recv(1)
            if not block:
                raise ConnectionError('Proxy closed before CONNECT response')
            response += block
        if response.split(b'\r\n')[0].split()[1:2] != [b'200']:
            sock.close()
            raise ConnectionError('HTTP CONNECT rejected')
    else:
        sock = socket.create_connection((host, port), timeout)
    return sock


def probe(host, route):
    result = {'host': host, 'route': route}
    started = time.monotonic()
    stage = 'tcp_or_proxy_connect'
    sock = None
    try:
        sock = connect(host, 22, route)
        result.update(connect_ms=round(1000 * (time.monotonic() - started)), source=sock.getsockname())
        stage = 'ssh_banner'
        sock.sendall(b'SSH-2.0-ReadOnlyRecon\r\n')
        banner = b''
        while b'\n' not in banner and len(banner) < 1024:
            block = sock.recv(1)
            if not block:
                break
            banner += block
        result['ssh_banner'] = banner.decode('ascii', 'replace').strip()[:200] if banner.startswith(b'SSH-') else None
        result['received_bytes'] = len(banner)
    except Exception as exc:
        result.update(failed_stage=stage, error_type=type(exc).__name__, errno=getattr(exc, 'winerror', None) or getattr(exc, 'errno', None))
    finally:
        if sock:
            sock.close()
    result['elapsed_ms'] = round(1000 * (time.monotonic() - started))
    sock = None
    try:
        sock = connect(host, 8790, route)
        sock.sendall(f'GET /api/health HTTP/1.1\r\nHost: {host}:8790\r\nConnection: close\r\n\r\n'.encode())
        response = b''
        while b'\r\n\r\n' not in response and len(response) < 8192:
            block = sock.recv(1024)
            if not block:
                break
            response += block
        result['health_http'] = response.split(b'\r\n')[0].decode('ascii', 'replace')[:80]
    except Exception as exc:
        result['health_error_type'] = type(exc).__name__
    finally:
        if sock:
            sock.close()
    return result


def tree_hash(path):
    if not path.exists():
        return {'present': False}
    files = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
    entries = [(str(p.relative_to(path)) if path.is_dir() else p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]
    if path.is_file():
        return {'sha256': entries[0][1], 'bytes': path.stat().st_size}
    digest = hashlib.sha256('\n'.join(name.replace('\\', '/') + '\t' + h for name, h in entries).encode()).hexdigest()
    return {'files': len(entries), 'tree_sha256': digest}


def info():
    rows = credentials()
    emit({'credentials_loaded_for': list(rows), 'ssh_port': 22})
    config = yaml.safe_load(PROXY_CONFIG.read_text(encoding='utf-8-sig'))
    selected = {k: config.get(k) for k in ('mode', 'mixed-port', 'socks-port', 'port', 'external-controller', 'external-controller-pipe', 'interface-name', 'routing-mark')}
    selected['tun'] = {k: (config.get('tun') or {}).get(k) for k in ('enable', 'stack', 'auto-route', 'auto-detect-interface', 'strict-route', 'route-exclude-address')}
    selected['controller_secret_present'] = bool(config.get('secret'))
    selected['authentication_present'] = bool(config.get('authentication'))
    selected['groups'] = [{'name': p.get('name'), 'type': p.get('type')} for p in config.get('proxy-groups', [])]
    matches = []
    for rule in config.get('rules', []):
        parts = str(rule).split(',')
        kind = parts[0].upper()
        if kind in ('MATCH', 'FINAL') or (kind == 'DST-PORT' and parts[1] in ('22', '8790')) or (kind == 'PROCESS-NAME' and parts[1].lower() in ('python.exe', 'ssh.exe')):
            matches.append(rule)
        if kind == 'IP-CIDR' and any(ipaddress.ip_address(h) in ipaddress.ip_network(parts[1], strict=False) for h in ALLOWED):
            matches.append(rule)
    selected['relevant_rules'] = matches
    selected['rule_count'] = len(config.get('rules', []))
    emit({'proxy': selected})
    emit({'local_hashes': {name: tree_hash(ROOT / name) for name in ('mail/mailcom.py', 'web/routes/mailcom.py', 'mailcode-service/server.mjs', 'mailcode-service/static', 'mailcode-service/public', 'web/static/vue')}})


REMOTE_INSPECT = r'''
import collections, hashlib, json, os, pathlib, re, shlex, shutil, sqlite3, subprocess, time

def run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return p.stdout if p.returncode == 0 else ''
    except Exception:
        return ''

def hashes(path):
    path = pathlib.Path(path)
    if not path.exists():
        return {'present': False}
    if path.is_file():
        return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    entries = [(str(p.relative_to(path)), hashlib.sha256(p.read_bytes()).hexdigest())
               for p in sorted(path.rglob('*')) if p.is_file()]
    return {'files': len(entries), 'tree_sha256': hashlib.sha256('\n'.join(n + '\t' + h for n, h in entries).encode()).hexdigest()}

def safe_job(obj):
    if not isinstance(obj, dict):
        return {}
    fields = ('running', 'paused', 'stop_requested', 'status', 'total', 'done', 'completed',
              'success', 'failed', 'success_count', 'fail_count', 'skipped_count',
              'started_at', 'finished_at', 'updated_at', 'concurrency', 'workers')
    return {k: obj[k] for k in fields if k in obj and isinstance(obj[k], (str, int, float, bool, type(None)))}

report = {'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'services': [], 'processes': []}
units = [l.split()[0] for l in run(['systemctl', 'list-units', '--all', '--type=service', '--no-pager', '--no-legend', '--plain']).splitlines()
         if l.strip() and re.search(r'gpt-register|mailcode|icloud-privacy', l.split()[0])]
pids = set()
base = pathlib.Path('/opt/gpt-register-fp-test')
for unit in units:
    props = 'Id,LoadState,ActiveState,SubState,MainPID,WorkingDirectory,FragmentPath,DropInPaths,ControlGroup,ExecStart'
    raw = run(['systemctl', 'show', unit, '--property=' + props])
    data = dict(l.split('=', 1) for l in raw.splitlines() if '=' in l)
    start = data.pop('ExecStart', '')
    data['exec_path'] = re.findall(r'path=([^ ;]+)', start)
    data['script_paths'] = re.findall(r'(?<!\w)(/[A-Za-z0-9_./+-]+\.(?:mjs|js|py|sh))(?=[\s;}]|$)', start)
    report['services'].append(data)
    if unit.startswith('gpt-register') and data.get('WorkingDirectory'):
        base = pathlib.Path(data['WorkingDirectory'])
    if data.get('MainPID', '0').isdigit() and int(data.get('MainPID', '0')):
        pids.add(int(data['MainPID']))
    cg = pathlib.Path('/sys/fs/cgroup') / data.get('ControlGroup', '').lstrip('/')
    if data.get('ControlGroup') and cg.exists():
        for fp in list(cg.rglob('cgroup.procs'))[:50]:
            try:
                pids.update(int(n) for n in fp.read_text().split())
            except Exception:
                pass

mailcode_runtime = []
for pid in sorted(pids):
    try:
        proc = pathlib.Path('/proc') / str(pid)
        exe = os.readlink(proc / 'exe')
        if not re.search(r'python|node|docker|ipm-server', exe):
            continue
        cwd = os.readlink(proc / 'cwd')
        args = (proc / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0')
        scripts = [str((pathlib.Path(cwd) / a).resolve()) for a in args if re.fullmatch(r'[A-Za-z0-9_./+-]+\.(?:py|mjs|js)', a)]
        row = {'pid': pid, 'exe': exe, 'cwd': cwd, 'scripts': scripts}
        if pathlib.Path(exe).name == 'node':
            row['actual_node_version'] = run([exe, '--version']).strip()
        env = dict(item.split(b'=', 1) for item in (proc / 'environ').read_bytes().split(b'\0') if b'=' in item)
        if b'PATH' in env:
            node = shutil.which('node', path=env[b'PATH'].decode('utf-8', 'replace'))
            row['path_node'] = node
            if node:
                row['path_node_real'] = os.path.realpath(node)
                row['path_node_version'] = run([node, '--version']).strip()
        report['processes'].append(row)
        for script in scripts:
            if script.endswith('server.mjs') and 'mailcode' in script:
                mailcode_runtime.append({'source': 'running_process', 'path': script, 'hash': hashes(script)})
    except Exception:
        pass
report['project_dir'] = str(base)
report['hashes'] = {name: hashes(base / name) for name in (
    'mail/mailcom.py', 'web/routes/mailcom.py', 'mailcode-service/server.mjs',
    'mailcode-service/static', 'mailcode-service/public', 'web/static/vue',
    'web/static/vue/index.html', 'deploy/remote_update_mailcom_alias_delete.sh')}
comparison = {'checked_local_files': len(LOCAL_MANIFEST), 'matched': 0, 'missing': [], 'changed': []}
for name, expected in LOCAL_MANIFEST.items():
    actual = hashes(base / name)
    if not actual.get('sha256'):
        comparison['missing'].append(name)
    elif actual['sha256'] == expected:
        comparison['matched'] += 1
    else:
        comparison['changed'].append({'file': name, 'local_sha256': expected, 'remote_sha256': actual['sha256']})
comparison['additional_remote_files'] = report['hashes']['web/static/vue'].get('files', 0) - len(LOCAL_MANIFEST) + len(comparison['missing'])
report['current_local_static_comparison'] = comparison
node = shutil.which('node')
report['shell_node'] = {'path': node, 'version': run([node, '--version']).strip() if node else None}
report['containers'] = []
if shutil.which('docker'):
    for line in run(['docker', 'ps', '-a', '--format', '{{json .}}']).splitlines():
        try:
            brief = json.loads(line)
            if not re.search(r'mailcode|gpt-register', brief.get('Names', '') + ' ' + brief.get('Image', '')):
                continue
            detail = json.loads(run(['docker', 'inspect', brief['ID']]))[0]
            config, state = detail.get('Config', {}), detail.get('State', {})
            row = {k: brief.get(k) for k in ('ID', 'Names', 'Image', 'State', 'Status', 'Ports')}
            row.update(image_id=detail.get('Image'), pid=state.get('Pid'), working_dir=config.get('WorkingDir'),
                       executable=detail.get('Path'), mounts=[{k: m.get(k) for k in ('Type', 'Source', 'Destination', 'RW')} for m in detail.get('Mounts', [])])
            if state.get('Running'):
                procroot = pathlib.Path('/proc') / str(state['Pid']) / 'root'
                row['actual_process_exe'] = os.readlink(pathlib.Path('/proc') / str(state['Pid']) / 'exe')
                if pathlib.Path(row['actual_process_exe']).name == 'node':
                    row['actual_node_version'] = run(['docker', 'exec', brief['ID'], row['actual_process_exe'], '--version']).strip()
                work = config.get('WorkingDir') or '/'
                for arg in (detail.get('Args') or []):
                    if isinstance(arg, str) and re.fullmatch(r'[A-Za-z0-9_./+-]+\.mjs', arg):
                        path = pathlib.PurePosixPath(work) / arg
                        actual = procroot / str(path).lstrip('/')
                        if actual.is_file():
                            row.setdefault('running_scripts', []).append({'path': str(path), 'hash': hashes(actual)})
            report['containers'].append(row)
        except Exception:
            report['containers'].append({'inspection_error': True})
report['mailcode_runtime'] = mailcode_runtime
report['known_listeners'] = [l for l in run(['ss', '-lntp']).splitlines() if re.search(r':(?:22|8790|18787)\s', l)]
report['persisted_jobs'] = {}
db = base / 'data/task_history.sqlite3'
if db.is_file():
    try:
        # mode=ro honors the live WAL, unlike immutable=1; no app migration helpers.
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
            conn.execute('PRAGMA query_only=ON')
            meta = dict(conn.execute("SELECT name,value FROM task_meta WHERE name IN ('last_job','updated_at')"))
            report['persisted_jobs']['last_job'] = safe_job(json.loads(meta.get('last_job', '{}')))
            report['persisted_jobs']['updated_at'] = meta.get('updated_at')
            report['persisted_jobs']['card_status_counts'] = dict(conn.execute("SELECT json_extract(payload,'$.status'),count(*) FROM task_cards GROUP BY json_extract(payload,'$.status')"))
    except Exception as exc:
        report['persisted_jobs']['error_type'] = type(exc).__name__
elif (base / 'data/task_history.json').is_file():
    data = json.loads((base / 'data/task_history.json').read_text())
    report['persisted_jobs'] = {'last_job': safe_job(data.get('last_job')), 'updated_at': data.get('updated_at'),
        'card_status_counts': dict(collections.Counter(c.get('status') for c in data.get('cards', []) if isinstance(c, dict)))}
report['checkpoints'] = []
for fp in sorted((base / 'data/task_checkpoints').glob('*.json')):
    try:
        data = json.loads(fp.read_text())
        report['checkpoints'].append({'file': fp.name, 'summary': safe_job(data), 'mtime_epoch': fp.stat().st_mtime})
    except Exception:
        report['checkpoints'].append({'file': fp.name, 'read_error': True})
try:
    import yaml
    config_path = base / 'config.yaml'
    web = (yaml.safe_load(config_path.read_text()) or {}).get('web', {})
    report['panel_login_metadata'] = {'password_is_pbkdf2': str(web.get('password', '')).startswith('pbkdf2_sha256$'),
                                    'required_fields_present': all(bool(web.get(k)) for k in ('username','password','token')),
                                    'config_mtime_ns': config_path.stat().st_mtime_ns}
except Exception as exc:
    report['panel_login_metadata'] = {'error_type': type(exc).__name__}
print(json.dumps(report, ensure_ascii=True))
'''


def live_jobs(client, config_metadata):
    if not (config_metadata.get('password_is_pbkdf2') and config_metadata.get('required_fields_present')):
        return {'skipped': 'Login may upgrade or initialize server configuration'}
    username, password = panel_credentials()
    config_path = '/opt/gpt-register-fp-test/config.yaml'
    sftp = client.open_sftp()
    with sftp.open(config_path, 'rb') as fp:
        before = hashlib.sha256(fp.read()).digest()

    def request(path, payload=None, cookie=''):
        # Normal login/status API over the existing SSH connection, never public HTTP credentials.
        channel = client.get_transport().open_channel('direct-tcpip', ('127.0.0.1', 8790), ('127.0.0.1', 0), timeout=15)
        channel.settimeout(20)
        conn = http.client.HTTPConnection('127.0.0.1', 8790, timeout=20)
        conn.sock = channel
        headers = {'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        try:
            conn.request('POST' if payload is not None else 'GET', path,
                         body=json.dumps(payload) if payload is not None else None, headers=headers)
            response = conn.getresponse()
            data = response.read(4 * 1024 * 1024)
            result = json.loads(data) if data else {}
            return response.status, response.getheader('Set-Cookie', ''), result
        finally:
            conn.close()

    result = {}
    try:
        status, set_cookie, _ = request('/api/auth/login', {'username': username, 'password': password})
        if status != 200:
            return {'login_http_status': status}
        cookies = SimpleCookie()
        cookies.load(set_cookie)
        if 'gpt_reg_session' not in cookies:
            return {'login_http_status': status, 'session_cookie_present': False}
        cookie = 'gpt_reg_session=' + cookies['gpt_reg_session'].value
        fields = {'running', 'paused', 'stop_requested', 'status', 'total', 'done', 'completed', 'success', 'failed',
                  'success_count', 'fail_count', 'skipped_count', 'started_at', 'finished_at', 'concurrency', 'workers'}

        def summary(obj):
            if not isinstance(obj, dict):
                return {}
            return {k: v for k, v in obj.items() if k in fields and isinstance(v, (bool, str, int, float, type(None)))}

        for path in ('/api/status', '/api/progress', '/api/accounts/reauth/progress',
                     '/api/accounts/rebind/progress', '/api/accounts/refresh-at/progress',
                     '/api/accounts/trial-eligibility/progress'):
            try:
                status, _, body = request(path, cookie=cookie)
                row = {'http_status': status}
                if status == 200 and isinstance(body, dict):
                    row['summary'] = summary(body)
                    for key in ('job', 'progress'):
                        if key in body:
                            row[key] = summary(body[key])
                    items = body.get('items')
                    if isinstance(items, list):
                        counts = {}
                        for item in items:
                            value = item.get('status', 'unknown') if isinstance(item, dict) else 'unknown'
                            if value not in ('running', 'queued', 'stopping', 'success', 'failed', 'done', 'completed', 'cancelled', 'idle', 'error'):
                                value = 'other'
                            counts[value] = counts.get(value, 0) + 1
                        row['item_status_counts'] = counts
                result[path] = row
            except Exception as exc:
                result[path] = {'error_type': type(exc).__name__}
        with sftp.open(config_path, 'rb') as fp:
            result['config_content_unchanged'] = before == hashlib.sha256(fp.read()).digest()
        return result
    finally:
        sftp.close()


def inspect(host, route, include_live_jobs=False):
    username, password = credentials()[host]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    authenticated = False
    try:
        sock = connect(host, 22, route, 20)
        client.connect(host, port=22, username=username, password=password, sock=sock,
                       look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
        authenticated = True
        key = client.get_transport().get_remote_server_key()
        emit({'host': host, 'route': route, 'authenticated': True,
              'host_key_type': key.get_name(), 'host_key_sha256_hex': hashlib.sha256(key.asbytes()).hexdigest()})
        # Execute over stdin; no remote script, environment dump, or credential file.
        stdin, stdout, stderr = client.exec_command('/opt/gpt-register-fp-test/.venv/bin/python -B -', timeout=90)
        manifest = {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((ROOT / 'web/static/vue').rglob('*')) if p.is_file()}
        stdin.write('LOCAL_MANIFEST = ' + repr(manifest) + '\n' + REMOTE_INSPECT)
        stdin.flush()
        stdin.channel.shutdown_write()
        output = stdout.read().decode('utf-8', 'replace')
        exit_code = stdout.channel.recv_exit_status()
        if exit_code == 0:
            recon = json.loads(output)
            emit({'host': host, 'recon': recon})
            if include_live_jobs:
                emit({'host': host, 'live_jobs': live_jobs(client, recon.get('panel_login_metadata', {}))})
        else:
            emit({'host': host, 'remote_inspect_exit': exit_code, 'stderr_bytes': len(stderr.read())})
    except Exception as exc:
        emit({'host': host, 'route': route, 'authenticated': authenticated, 'error_type': type(exc).__name__})
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('info', 'probe', 'inspect'))
    parser.add_argument('--route', choices=('direct', 'socks', 'http'), default='direct')
    parser.add_argument('--hosts', nargs='+', choices=ALLOWED, default=list(ALLOWED))
    parser.add_argument('--live-jobs', action='store_true', help='Use normal panel login over SSH, then GET job summaries; skip if login could migrate config')
    args = parser.parse_args()
    if args.action == 'info':
        info()
    elif args.action == 'probe':
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(probe, host, args.route) for host in args.hosts]
            for future in concurrent.futures.as_completed(futures):
                emit(future.result())
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda host: inspect(host, args.route, args.live_jobs), args.hosts))


if __name__ == '__main__':
    main()
