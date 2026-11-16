#!/usr/bin/env bash
set -Eeuo pipefail

APP=/opt/gpt-register-fp-test
PANEL=gpt-register-fp-test.service
PKG="${1:?package required}"
EXPECTED_SHA="${2:?sha256 required}"
MODE="${3:?docker or systemd required}"
TARGET="${4:?target required}"
[[ "$PKG" == /tmp/mailcom-inventory-capacity-20260920.tgz ]]
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{64}$ ]]
[[ "$(sha256sum "$PKG" | cut -d ' ' -f 1)" == "$EXPECTED_SHA" ]]
case "$TARGET:$MODE" in
  45.205.31.204:docker|45.205.31.227:docker|45.205.31.253:docker|45.205.31.123:docker|45.205.31.69:systemd) ;;
  *) echo "target/runtime mismatch" >&2; exit 2 ;;
esac
read -r PANEL_PASSWORD_B64
[[ -n "$PANEL_PASSWORD_B64" ]]

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$APP/backups/mailcom-inventory-capacity-$STAMP-$$"
STAGE=$(mktemp -d /tmp/mailcom-inventory-capacity-five.XXXXXX)
NEW_IMAGE="gc-mailcode:unconfirmed-recovery-$STAMP-$$"
OLD_IMAGE="gc-mailcode:backup-unconfirmed-recovery-$STAMP-$$"
OLD_CONTAINER="gc-mailcode-before-$STAMP-$$"
GC_SERVICE=gc-mailcode.service
GC_RUNTIME=/opt/gc-20260824/release/mailcode-service
GC_ENV=/opt/gc-20260824/shared/mailcode.env
GC_UNIT=/etc/systemd/system/gc-mailcode.service
GC_NODE=/opt/gc-20260824/runtime/node-v22.23.2-linux-x64/bin/node
GC_NPM=/opt/gc-20260824/runtime/node-v22.23.2-linux-x64/bin/npm
STOPPED=0
CHANGED=0
BACKUP_READY=0
HAD_APP_WEB_CLIENT=0

restore_mailcode_data() {
  if [[ -f "$BACKUP/mailcode-data-before.tgz" ]]; then
    [[ ! -e /var/lib/gc-mailcode ]] || mv /var/lib/gc-mailcode "$BACKUP/mailcode-data-failed"
    tar -xzf "$BACKUP/mailcode-data-before.tgz" -C /var/lib
  fi
}

rollback() {
  local status=$?
  trap - EXIT
  set +e
  if [[ "$status" -ne 0 && "$STOPPED" == 1 ]]; then
    systemctl stop "$PANEL" >/dev/null 2>&1
    if [[ "$MODE" == docker ]]; then
      docker rm -f gc-mailcode >/dev/null 2>&1
      restore_mailcode_data
      if docker container inspect "$OLD_CONTAINER" >/dev/null 2>&1; then
        docker rename "$OLD_CONTAINER" gc-mailcode
        docker start gc-mailcode >/dev/null
      fi
    else
      systemctl stop "$GC_SERVICE" >/dev/null 2>&1
      restore_mailcode_data
      if [[ -f "$BACKUP/mailcode-runtime-before.tgz" ]]; then
        [[ ! -e "$GC_RUNTIME" ]] || mv "$GC_RUNTIME" "$BACKUP/mailcode-runtime-failed"
        tar -xzf "$BACKUP/mailcode-runtime-before.tgz" -C /opt/gc-20260824/release
      fi
      systemctl start "$GC_SERVICE" >/dev/null 2>&1
    fi
    if [[ "$CHANGED" == 1 && "$BACKUP_READY" == 1 ]]; then
      [[ ! -e "$APP/web/static/vue" ]] || mv "$APP/web/static/vue" "$BACKUP/vue-failed"
      [[ ! -d "$BACKUP/vue-before" ]] || mv "$BACKUP/vue-before" "$APP/web/static/vue"
      tar -xzf "$BACKUP/source-before.tgz" -C "$APP" --no-same-owner
      [[ "$HAD_APP_WEB_CLIENT" == 1 ]] || rm -f -- "$APP/mailcode-service/web-alias-client.mjs"
      tar -xzf "$BACKUP/data-config-before.tgz" -C "$APP"
    fi
    systemctl start "$PANEL" >/dev/null 2>&1
    echo "rollback=attempted backup=$BACKUP" >&2
  fi
  [[ "$STAGE" == /tmp/mailcom-inventory-capacity-five.* ]] && rm -rf -- "$STAGE"
  exit "$status"
}
trap rollback EXIT

tar -xzf "$PKG" -C "$STAGE" --no-same-owner
FILES=(
  common/email_rebind_jobs.py
  mail/__init__.py mail/mailcom.py web/routes/mailcom.py web/routes/accounts_rebind.py
  mailcode-service/Dockerfile mailcode-service/package.json mailcode-service/package-lock.json
  mailcode-service/server.mjs mailcode-service/mailbox.mjs mailcode-service/store.mjs
  mailcode-service/web-alias-client.mjs
)
for file in "${FILES[@]}"; do test -s "$STAGE/$file"; done
test -s "$STAGE/web/static/vue/index.html"
test -s "$STAGE/web/static/vue/index.html"
grep -qF 'web-alias-client.mjs' "$STAGE/mailcode-service/Dockerfile"
grep -qF 'skip_domain_check' "$STAGE/mailcode-service/server.mjs"
grep -qF 'skip_domain_check' "$STAGE/web/routes/accounts_rebind.py"
grep -qF 'le=50' "$STAGE/web/routes/accounts_rebind.py"
grep -qF 'action="reconcile alias"' "$STAGE/mail/mailcom.py"
grep -qF 'request.once("aborted"' "$STAGE/mailcode-service/server.mjs"
grep -qF 'response.once("close"' "$STAGE/mailcode-service/server.mjs"
grep -qF 'MAILCOM_ALIAS_OPERATION_TIMEOUT_MS' "$STAGE/mailcode-service/server.mjs"
grep -qF 'boundedOperation(' "$STAGE/mailcode-service/server.mjs"
grep -qF 'createAliasInConfiguredDomain' "$STAGE/mailcode-service/web-alias-client.mjs"
grep -qF 'method: "POST"' "$STAGE/mailcode-service/web-alias-client.mjs"
grep -RqlF '保存邮箱并直接分配' "$STAGE/web/static/vue/assets"
grep -RqlF 'dr.com' "$STAGE/web/static/vue/assets"
grep -RqlF '1 到 50' "$STAGE/web/static/vue/assets"
grep -RqlF '邮箱查询并发' "$STAGE/web/static/vue/assets"
"$APP/.venv/bin/python" -m py_compile \
  "$STAGE/common/email_rebind_jobs.py" \
  "$STAGE/mail/__init__.py" "$STAGE/mail/mailcom.py" \
  "$STAGE/web/routes/mailcom.py" "$STAGE/web/routes/accounts_rebind.py"

panel_probe() {
  PANEL_PASSWORD_B64="$PANEL_PASSWORD_B64" "$APP/.venv/bin/python" - <<'PY'
import base64, json, os, pathlib, requests, sqlite3
s = requests.Session(); s.trust_env = False
password = base64.b64decode(os.environ['PANEL_PASSWORD_B64']).decode()
r = s.post('http://127.0.0.1:8790/api/auth/login', json={'username':'admin','password':password}, timeout=15)
r.raise_for_status()
paths = ('/api/status','/api/progress','/api/accounts/rebind/progress',
         '/api/accounts/reauth/progress','/api/accounts/refresh-at/progress',
         '/api/accounts/trial-eligibility/progress','/api/accounts/health/progress',
         '/api/codex-oauth/status')
for path in paths:
    response = s.get('http://127.0.0.1:8790' + path, timeout=20); response.raise_for_status()
    body = response.json(); items = [body, body.get('job') or {}, *(body.get('items') or [])]
    if any(isinstance(x, dict) and (x.get('running') or x.get('status') in ('queued','running','stopping')) for x in items):
        raise RuntimeError('active_task:' + path)
external = s.get('http://127.0.0.1:8790/api/external/pipeline/status?page_size=100', timeout=20)
if external.status_code == 200:
    if any(x.get('status') in ('queued','running','stopping') for x in (external.json().get('items') or []) if isinstance(x, dict)):
        raise RuntimeError('active_task:external_pipeline')
elif external.status_code != 404:
    external.raise_for_status()
db = pathlib.Path('/opt/gpt-register-fp-test/data/external_api.sqlite3')
if db.is_file():
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=10) as conn:
        active = conn.execute("SELECT COUNT(*) FROM pipeline_tasks WHERE status IN ('queued','running','stopping')").fetchone()[0]
    if active: raise RuntimeError('active_task:external_pipeline_db')
settings = s.get('http://127.0.0.1:8790/api/mailcom/settings?include_usage=0', timeout=30)
settings.raise_for_status(); body = settings.json()
print(json.dumps({'accounts':len(body.get('accounts') or []),
 'proxy_enabled':body.get('proxy_pool_enabled'),'proxy_count':body.get('proxy_pool_count'),
 'proxy_assigned':body.get('proxy_pool_assigned')}, sort_keys=True, separators=(',',':')))
PY
}

mailcode_probe() {
  RUNTIME_MODE="$MODE" "$APP/.venv/bin/python" - <<'PY'
import collections, hashlib, json, os, subprocess, requests, urllib.parse
mode = os.environ['RUNTIME_MODE']
if mode == 'docker':
    raw = subprocess.run(['docker','inspect','gc-mailcode'], check=True, capture_output=True, text=True).stdout
    env = dict(x.split('=',1) for x in json.loads(raw)[0]['Config'].get('Env',[]) if '=' in x)
else:
    pid = subprocess.run(['systemctl','show','gc-mailcode.service','-p','MainPID','--value'], check=True,
                         capture_output=True, text=True).stdout.strip()
    env = dict(x.decode().split('=',1) for x in open('/proc/'+pid+'/environ','rb').read().split(b'\0') if b'=' in x)
key = env['MAILCODE_ADMIN_KEY']
s = requests.Session(); s.trust_env = False; headers={'x-api-key':key}
a = s.get('http://127.0.0.1:18787/api/v1/accounts', headers=headers, timeout=30); a.raise_for_status()
accounts = a.json().get('accounts') or []
identity = sorted(str(x.get('email') or '').strip().lower() for x in accounts)
account_alias_counts = sorted((str(x.get('email') or '').strip().lower(), int(x.get('aliasCount') or 0)) for x in accounts)
aliases = []
for account in accounts:
    response = s.get('http://127.0.0.1:18787/api/v1/aliases', headers=headers,
                     params={'account_email': account.get('email')}, timeout=30)
    response.raise_for_status()
    aliases.extend(response.json().get('items') or [])
alias_mapping = sorted((str(x.get('address') or '').strip().lower(),
                        str(x.get('primaryEmail') or '').strip().lower(),
                        str(x.get('status') or 'unknown')) for x in aliases)
alias_statuses = dict(sorted(collections.Counter(x[2] for x in alias_mapping).items()))
p = s.get('http://127.0.0.1:18787/api/v1/proxy-pool', headers=headers, timeout=30)
if p.status_code == 404:
    supported=False; pool={}
else:
    p.raise_for_status(); supported=True; pool=p.json().get('proxy_pool') or {}
sha = lambda value: hashlib.sha256(json.dumps(value,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
print(json.dumps({'accounts':len(accounts),'email_set_sha256':sha(identity),
 'account_alias_counts_sha256':sha(account_alias_counts),
 'alias_mapping_sha256':sha(alias_mapping),'alias_statuses':alias_statuses,
 'alias_total':len(alias_mapping),
 'proxy_supported':supported,'proxy_enabled':bool(pool.get('enabled')),
 'proxy_count':int(pool.get('count') or 0),'proxy_assigned':int(pool.get('assigned') or 0)},
 sort_keys=True,separators=(',',':')))
PY
}

compare_mailcode() {
  BEFORE="$1" AFTER="$2" "$APP/.venv/bin/python" - <<'PY'
import json, os
before=json.loads(os.environ['BEFORE']); after=json.loads(os.environ['AFTER'])
for key in ('accounts','email_set_sha256','account_alias_counts_sha256',
            'alias_mapping_sha256','alias_statuses','alias_total'):
    if before[key] != after[key]: raise RuntimeError('mailcode identity changed: '+key)
if not after['proxy_supported']: raise RuntimeError('proxy endpoint missing after release')
if before['proxy_supported']:
    for key in ('proxy_enabled','proxy_count','proxy_assigned'):
        if before[key] != after[key]: raise RuntimeError('proxy state changed: '+key)
elif after['proxy_enabled'] or after['proxy_count'] or after['proxy_assigned']:
    raise RuntimeError('new proxy settings were not empty defaults')
PY
}

PRE_PANEL=$(panel_probe)
PRE_MAILCODE=$(mailcode_probe)
systemctl is-active --quiet "$PANEL"
mkdir -p -m 700 "$BACKUP"

if [[ "$MODE" == docker ]]; then
  docker container inspect gc-mailcode >"$BACKUP/container-inspect.json"
  chmod 0600 "$BACKUP/container-inspect.json"
  "$APP/.venv/bin/python" - "$BACKUP/container-inspect.json" "$BACKUP/container.env" <<'PY'
import json, os, pathlib, sys
item=json.loads(pathlib.Path(sys.argv[1]).read_text())[0]; host=item.get('HostConfig') or {}; mounts=item.get('Mounts') or []
if (item.get('State') or {}).get('Status') != 'running': raise RuntimeError('mailcode container not running')
if host.get('NetworkMode') != 'host': raise RuntimeError('unexpected network mode')
if (host.get('RestartPolicy') or {}).get('Name') != 'unless-stopped': raise RuntimeError('unexpected restart policy')
expected=[m for m in mounts if m.get('Source')=='/var/lib/gc-mailcode' and m.get('Destination')=='/var/lib/mailcode' and m.get('RW')]
if len(expected)!=1: raise RuntimeError('unexpected data mount')
values=(item.get('Config') or {}).get('Env') or []
if any('\n' in x or '\r' in x for x in values): raise RuntimeError('unsupported multiline environment')
pathlib.Path(sys.argv[2]).write_text('\n'.join(values)+'\n'); os.chmod(sys.argv[2],0o600)
PY
  OLD_ID=$(docker image inspect --format '{{.Id}}' "$(docker inspect --format '{{.Config.Image}}' gc-mailcode)")
  docker tag "$OLD_ID" "$OLD_IMAGE"
  docker build --pull=false -t "$NEW_IMAGE" "$STAGE/mailcode-service" >"$BACKUP/docker-build.log" 2>&1
  docker run --rm --entrypoint node "$NEW_IMAGE" --check server.mjs
  docker run --rm --entrypoint node "$NEW_IMAGE" --check web-alias-client.mjs
  docker run --rm --entrypoint sh "$NEW_IMAGE" -c \
    'test -s /app/web-alias-client.mjs && grep -qF '\''request.once("aborted"'\'' /app/server.mjs && grep -qF '\''response.once("close"'\'' /app/server.mjs && grep -qF MAILCOM_ALIAS_OPERATION_TIMEOUT_MS /app/server.mjs && grep -qF '\''boundedOperation('\'' /app/server.mjs && grep -qF createAliasInConfiguredDomain /app/web-alias-client.mjs && grep -qF '\''method: "POST"'\'' /app/web-alias-client.mjs'
else
  systemctl is-active --quiet "$GC_SERVICE"
  test -x "$GC_NODE"; test -x "$GC_NPM"; test -f "$GC_ENV"; test -f "$GC_UNIT"
  cmp -s "$STAGE/mailcode-service/mailbox.mjs" "$GC_RUNTIME/mailbox.mjs"
  cp -a "$GC_RUNTIME"/*.test.mjs "$STAGE/mailcode-service/"
  (cd "$STAGE/mailcode-service" && "$GC_NPM" ci --omit=dev --ignore-scripts >"$BACKUP/npm-ci.log" 2>&1)
  "$GC_NODE" --check "$STAGE/mailcode-service/server.mjs"
  "$GC_NODE" --check "$STAGE/mailcode-service/web-alias-client.mjs"
  (cd "$STAGE/mailcode-service" && "$GC_NODE" --test ./*.test.mjs >"$BACKUP/node-tests.log" 2>&1)
  cp -a "$GC_ENV" "$BACKUP/mailcode.env.before"; chmod 0600 "$BACKUP/mailcode.env.before"
  cp -a "$GC_UNIT" "$BACKUP/gc-mailcode.service.before"
  tar -czf "$BACKUP/mailcode-runtime-before.tgz" -C /opt/gc-20260824/release mailcode-service
  tar -tzf "$BACKUP/mailcode-runtime-before.tgz" >/dev/null
fi

# Verified live backups exist before any service is stopped.
tar -czf "$BACKUP/data-config-before.tgz" -C "$APP" data config.yaml
tar -czf "$BACKUP/mailcode-data-live-before.tgz" -C /var/lib gc-mailcode
tar -tzf "$BACKUP/data-config-before.tgz" >/dev/null
tar -tzf "$BACKUP/mailcode-data-live-before.tgz" >/dev/null

# Builds and backups can take time, so verify the idle window and state again.
[[ "$(panel_probe)" == "$PRE_PANEL" ]]
[[ "$(mailcode_probe)" == "$PRE_MAILCODE" ]]
SOURCE_FILES=(common/email_rebind_jobs.py mail/__init__.py mail/mailcom.py web/routes/mailcom.py web/routes/accounts_rebind.py)
for file in Dockerfile package.json package-lock.json server.mjs mailbox.mjs store.mjs; do
  [[ ! -f "$APP/mailcode-service/$file" ]] || SOURCE_FILES+=("mailcode-service/$file")
done
if [[ -f "$APP/mailcode-service/web-alias-client.mjs" ]]; then
  HAD_APP_WEB_CLIENT=1; SOURCE_FILES+=(mailcode-service/web-alias-client.mjs)
fi
tar -czf "$BACKUP/source-before.tgz" -C "$APP" "${SOURCE_FILES[@]}"
tar -tzf "$BACKUP/source-before.tgz" >/dev/null
cp -a "$STAGE/web/static/vue" "$BACKUP/vue-candidate"

STOPPED=1
systemctl stop "$PANEL"
if [[ "$MODE" == docker ]]; then
  docker stop gc-mailcode >/dev/null
  docker rename gc-mailcode "$OLD_CONTAINER"
else
  systemctl stop "$GC_SERVICE"
fi
tar -czf "$BACKUP/mailcode-data-before.tgz" -C /var/lib gc-mailcode
tar -tzf "$BACKUP/mailcode-data-before.tgz" >/dev/null
BACKUP_READY=1
CHANGED=1

for file in "${FILES[@]}"; do install -D -m 0644 "$STAGE/$file" "$APP/$file"; done
mv "$APP/web/static/vue" "$BACKUP/vue-before"
mv "$BACKUP/vue-candidate" "$APP/web/static/vue"

if [[ "$MODE" == docker ]]; then
  docker run -d --name gc-mailcode --network host --restart unless-stopped \
    --env-file "$BACKUP/container.env" --mount type=bind,src=/var/lib/gc-mailcode,dst=/var/lib/mailcode \
    "$NEW_IMAGE" >/dev/null
else
  install -o gc-mailcode -g gc-mailcode -m 0644 "$STAGE/mailcode-service/package.json" "$GC_RUNTIME/package.json"
  install -o gc-mailcode -g gc-mailcode -m 0644 "$STAGE/mailcode-service/package-lock.json" "$GC_RUNTIME/package-lock.json"
  install -o gc-mailcode -g gc-mailcode -m 0644 "$STAGE/mailcode-service/server.mjs" "$GC_RUNTIME/server.mjs"
  install -o gc-mailcode -g gc-mailcode -m 0644 "$STAGE/mailcode-service/store.mjs" "$GC_RUNTIME/store.mjs"
  install -o gc-mailcode -g gc-mailcode -m 0644 "$STAGE/mailcode-service/web-alias-client.mjs" "$GC_RUNTIME/web-alias-client.mjs"
  mv "$GC_RUNTIME/node_modules" "$BACKUP/node_modules-before-live"
  mv "$STAGE/mailcode-service/node_modules" "$GC_RUNTIME/node_modules"
  chown -R gc-mailcode:gc-mailcode "$GC_RUNTIME/node_modules"
  systemctl start "$GC_SERVICE"
fi

for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 http://127.0.0.1:18787/api/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 10 http://127.0.0.1:18787/api/health >/dev/null
POST_MAILCODE=$(mailcode_probe)
compare_mailcode "$PRE_MAILCODE" "$POST_MAILCODE"

systemctl start "$PANEL"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 http://127.0.0.1:8790/api/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 10 http://127.0.0.1:8790/api/health >/dev/null
systemctl is-active --quiet "$PANEL"
[[ "$(panel_probe)" == "$PRE_PANEL" ]]
for file in "${FILES[@]}"; do cmp -s "$STAGE/$file" "$APP/$file"; done
diff -qr "$STAGE/web/static/vue" "$APP/web/static/vue" >/dev/null
if [[ "$MODE" == systemd ]]; then
  systemctl is-active --quiet "$GC_SERVICE"
  for file in package.json package-lock.json server.mjs store.mjs web-alias-client.mjs; do
    cmp -s "$STAGE/mailcode-service/$file" "$GC_RUNTIME/$file"
  done
  cmp -s "$STAGE/mailcode-service/mailbox.mjs" "$GC_RUNTIME/mailbox.mjs"
else
  docker container inspect gc-mailcode >/dev/null
  docker run --rm --entrypoint sh "$NEW_IMAGE" -c \
    'test -s /app/web-alias-client.mjs && grep -qF skip_domain_check /app/server.mjs && grep -qF '\''request.once("aborted"'\'' /app/server.mjs && grep -qF '\''response.once("close"'\'' /app/server.mjs && grep -qF MAILCOM_ALIAS_OPERATION_TIMEOUT_MS /app/server.mjs && grep -qF '\''boundedOperation('\'' /app/server.mjs && grep -qF createAliasInConfiguredDomain /app/web-alias-client.mjs && grep -qF '\''method: "POST"'\'' /app/web-alias-client.mjs'
fi

CHANGED=0
STOPPED=0
echo "deploy=ok target=$TARGET runtime=$MODE panel_health=200 mailcode_health=200 panel_counts=$PRE_PANEL mailcode_fingerprint=$POST_MAILCODE backup=$BACKUP"
