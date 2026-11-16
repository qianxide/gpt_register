"""Read-only mail.com runtime audit for the five secondary panels."""
from __future__ import annotations

import hashlib
import json
import sys

import paramiko

from readonly_ssh_recon import ALLOWED, connect, credentials


HOST_KEY_SHA256 = {
    "45.205.31.204": "89c48fee7ba72a60e3e0806d7c410a83df29536e81feaf36c2b865ac71c22bd3",
    "45.205.31.227": "d253d3c8f638ab36aaab74c5743ae18a501210e319d480405b582d630208d1c7",
    "45.205.31.253": "8834a8031e572adbdb5c06bc8f13e4cf747442442d037d5cf1c3e3449b05021a",
    "45.205.31.69": "378c9077267b49f0daec2c6d79885ed45bdab371f612d444d574d60f10cac3d3",
    "45.205.31.123": "6d09fc8311f0fbd602182a62cb276c3427b0507cb5206a6013c932e0c5f2c8c3",
}


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        actual = hashlib.sha256(key.asbytes()).hexdigest()
        if actual != HOST_KEY_SHA256.get(hostname):
            raise paramiko.BadHostKeyException(hostname, key, None)
        client.get_host_keys().add(hostname, key.get_name(), key)


REMOTE_AUDIT = r'''
import hashlib, json, os, pathlib, subprocess, urllib.request, urllib.error, yaml
from urllib.parse import urlsplit

APP = pathlib.Path('/opt/gpt-register-fp-test')
cfg = yaml.safe_load((APP / 'config.yaml').read_text()) or {}
result = {'panel_source': {
    name: hashlib.sha256((APP / name).read_bytes()).hexdigest()
    for name in ('mail/__init__.py', 'mail/mailcom.py', 'web/routes/mailcom.py', 'web/routes/accounts_rebind.py')
}}
providers = [p for p in (cfg.get('mail') or {}).get('providers') or []
             if isinstance(p, dict) and p.get('type') == 'mailcom_api']
summary = []
for p in providers:
    parsed = urlsplit(str(p.get('api_base') or ''))
    summary.append({'enabled': bool(p.get('enable')), 'rebind_only': bool(p.get('rebind_only')),
                    'endpoint_host': parsed.hostname, 'endpoint_port': parsed.port,
                    'accounts_configured': len([s for s in str(p.get('accounts') or '').splitlines() if s.strip()])})

def service(name):
    result = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveState', '-p', 'MainPID',
                             '-p', 'WorkingDirectory', '-p', 'FragmentPath', '-p', 'EnvironmentFiles',
                             '-p', 'ExecStart', '-p', 'User', '-p', 'Group'],
                            capture_output=True, text=True, timeout=10)
    return dict(s.split('=', 1) for s in result.stdout.splitlines() if '=' in s)

node = service('gc-mailcode.service')
result.update(panel_mailcom=summary, gc_mailcode=node)

def counts(key):
    def get(label, path, auth=True, optional=False):
        req = urllib.request.Request('http://127.0.0.1:18787' + path,
                                     headers={'x-api-key': key.decode()} if auth else {})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response), response.status
        except urllib.error.HTTPError as exc:
            if optional and exc.code == 404:
                return {}, exc.code
            raise RuntimeError(f'{label} HTTP {exc.code}') from None
    payload, _ = get('accounts', '/api/v1/accounts')
    proxy_response, proxy_status = get('proxy-pool', '/api/v1/proxy-pool', optional=True)
    proxy = proxy_response.get('proxy_pool') or {}
    _, health = get('health', '/api/health', False)
    accounts = payload.get('accounts') or []
    account_fields = sorted({field for item in accounts if isinstance(item, dict) for field in item})
    return {'accounts': len(accounts), 'account_fields': account_fields,
            'alias_total': sum(int(item.get('aliasCount') or 0) for item in accounts if isinstance(item, dict)),
            'proxy_pool_supported': proxy_status == 200,
            'proxy_pool_enabled': proxy.get('enabled'),
            'proxy_pool_count': proxy.get('count'), 'proxy_pool_assigned': proxy.get('assigned'),
            'health_http': health}

if node.get('ActiveState') == 'active' and node.get('MainPID', '0').isdigit() and int(node['MainPID']):
    pid = node['MainPID']
    process = pathlib.Path('/proc') / pid
    result['gc_mailcode']['running_script'] = next(
        (s for s in (process / 'cmdline').read_bytes().split(b'\0') if s.endswith(b'/server.mjs')), b'').decode()
    env = dict(item.split(b'=', 1) for item in (process / 'environ').read_bytes().split(b'\0') if b'=' in item)
    key = env.get(b'MAILCODE_ADMIN_KEY', b'')
    result['gc_mailcode']['admin_key_present'] = bool(key)
    data_dir = env.get(b'MAILCODE_DATA_DIR', b'').decode()
    result['gc_mailcode']['data_dir'] = data_dir
    if key:
        result['gc_mailcode'].update(counts(key))
    source = pathlib.Path('/opt/gc-20260824/release/mailcode-service')
    result['gc_mailcode']['source_files'] = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ('package.json', 'package-lock.json', 'server.mjs', 'mailbox.mjs', 'store.mjs', 'web-alias-client.mjs')
        if (source / name).is_file()}
    node_bin = '/opt/gc-20260824/runtime/node-v22.23.2-linux-x64/bin/node'
    dependency = subprocess.run([node_bin, '-e', "import('undici')"], cwd=source,
                                capture_output=True, text=True, timeout=15)
    result['gc_mailcode']['undici_import_ok'] = dependency.returncode == 0
    result['gc_mailcode']['test_files'] = sorted(p.name for p in source.glob('*.test.mjs'))
else:
    inspected = subprocess.run(['docker', 'inspect', 'gc-mailcode'], capture_output=True, text=True, timeout=15)
    if inspected.returncode == 0:
        container = json.loads(inspected.stdout)[0]
        config = container.get('Config') or {}
        host = container.get('HostConfig') or {}
        env = dict(item.split('=', 1) for item in config.get('Env') or [] if '=' in item)
        key = env.get('MAILCODE_ADMIN_KEY', '').encode()
        result['docker_mailcode'] = {
            'state': (container.get('State') or {}).get('Status'),
            'image_ref': config.get('Image'), 'image_id': container.get('Image'),
            'network_mode': host.get('NetworkMode'), 'port_bindings': host.get('PortBindings'),
            'restart_policy': host.get('RestartPolicy'), 'mounts': [
                {'Type': m.get('Type'), 'Source': m.get('Source'), 'Destination': m.get('Destination')}
                for m in container.get('Mounts') or []],
            'env_names': sorted(env), 'admin_key_present': bool(key),
            'labels': sorted((config.get('Labels') or {}).keys()),
        }
        if key:
            result['docker_mailcode'].update(counts(key))
        source = APP / 'mailcode-service'
        result['docker_mailcode']['source_files'] = {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ('Dockerfile', 'package.json', 'package-lock.json', 'server.mjs',
                         'mailbox.mjs', 'store.mjs', 'web-alias-client.mjs')
            if (source / name).is_file()}
print(json.dumps(result))
'''


def connect_pinned(host):
    username, password = credentials()[host]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHostKey())
    client.connect(host, username=username, password=password, sock=connect(host, 22, 'direct', 20),
                   look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
    return client


def run_remote_python(client, source):
    stdin, stdout, stderr = client.exec_command('/opt/gpt-register-fp-test/.venv/bin/python -B -', timeout=90)
    stdin.write(source)
    stdin.channel.shutdown_write()
    output = stdout.read().decode('utf-8', 'replace')
    status = stdout.channel.recv_exit_status()
    if status:
        tail = stderr.read().decode('utf-8', 'replace').splitlines()[-1:]
        raise RuntimeError(f'Remote audit failed (exit {status}, error={tail})')
    return json.loads(output)


if __name__ == '__main__':
    hosts = sys.argv[1:] or ['45.205.31.69']
    if any(host not in ALLOWED for host in hosts):
        raise SystemExit('Host outside five-server allowlist')
    for host in hosts:
        with connect_pinned(host) as client:
            print(json.dumps({'host': host, 'audit': run_remote_python(client, REMOTE_AUDIT)}, ensure_ascii=False))
