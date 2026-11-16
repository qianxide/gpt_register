#!/usr/bin/env bash
set -Eeuo pipefail

APP=/opt/gpt-register-fp-test
PANEL=gpt-register-fp-test.service
MAILCODE=mailcode-service.service
PKG="${1:?package required}"
EXPECTED_SHA="${2:?sha256 required}"
[[ "$PKG" == /tmp/mailcom-inventory-capacity-20260920.tgz ]]
[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{64}$ ]]
[[ "$(sha256sum "$PKG" | cut -d ' ' -f 1)" == "$EXPECTED_SHA" ]]
read -r PANEL_PASSWORD_B64
[[ -n "$PANEL_PASSWORD_B64" ]]

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$APP/backups/mailcom-inventory-capacity-$STAMP-$$"
STAGE=$(mktemp -d /tmp/mailcom-inventory-capacity.XXXXXX)
OLD_IMAGE="gpt-register-mailcode:backup-inventory-capacity-$STAMP-$$"
NEW_IMAGE="gpt-register-mailcode:inventory-capacity-$STAMP-$$"
STOPPED=0
CHANGED=0
BACKUP_READY=0
HAD_WEB_CLIENT=0

on_exit() {
  local status=$?
  trap - EXIT
  set +e
  if [[ "$status" -ne 0 && "$STOPPED" == 1 ]]; then
    systemctl stop "$PANEL" >/dev/null 2>&1
    systemctl stop "$MAILCODE" >/dev/null 2>&1
    if [[ "$CHANGED" == 1 && "$BACKUP_READY" == 1 ]]; then
      if [[ -d "$BACKUP/vue-before" ]]; then
        [[ ! -e "$APP/web/static/vue" ]] || mv "$APP/web/static/vue" "$BACKUP/vue-failed"
        mv "$BACKUP/vue-before" "$APP/web/static/vue"
      fi
      tar -xzf "$BACKUP/source-before.tgz" -C "$APP" --no-same-owner
      if [[ "$HAD_WEB_CLIENT" == 0 ]]; then
        rm -f -- "$APP/mailcode-service/web-alias-client.mjs"
      fi
      tar -xzf "$BACKUP/data-config-before.tgz" -C "$APP"
      tar -xzf "$BACKUP/mailcode-data-before.tgz" -C /var/lib
      tar -xzf "$BACKUP/mailcode-env-before.tgz" -C /etc/gc-mailcode
      docker tag "$OLD_IMAGE" gpt-register-mailcode:local
    fi
    systemctl start "$MAILCODE"
    systemctl start "$PANEL"
    echo "rollback=attempted backup=$BACKUP" >&2
  fi
  if [[ "$STAGE" == /tmp/mailcom-inventory-capacity.* ]]; then
    rm -rf -- "$STAGE"
  fi
  exit "$status"
}
trap on_exit EXIT

tar -xzf "$PKG" -C "$STAGE" --no-same-owner
FILES=(
  common/email_rebind_jobs.py
  mail/__init__.py mail/mailcom.py web/routes/mailcom.py web/routes/accounts_rebind.py
  mailcode-service/Dockerfile mailcode-service/package.json
  mailcode-service/package-lock.json mailcode-service/server.mjs
  mailcode-service/mailbox.mjs mailcode-service/store.mjs
  mailcode-service/web-alias-client.mjs
)
for file in "${FILES[@]}"; do
  test -s "$STAGE/$file"
done
test -s "$STAGE/web/static/vue/index.html"
grep -qF 'web-alias-client.mjs' "$STAGE/mailcode-service/Dockerfile"
grep -qF 'skip_domain_check' "$STAGE/mailcode-service/server.mjs"
grep -qF 'skip_domain_check' "$STAGE/web/routes/accounts_rebind.py"
grep -qF 'le=50' "$STAGE/web/routes/accounts_rebind.py"
grep -qF 'status in {"pending", "unconfirmed"}' "$STAGE/mail/mailcom.py"
grep -qF 'action="reconcile alias"' "$STAGE/mail/mailcom.py"
grep -qF '"skip_domain_check": True' "$STAGE/mail/mailcom.py"
grep -qF 'cancel_cleanup' "$STAGE/common/email_rebind_jobs.py"
grep -qF 'stop_event.is_set()' "$STAGE/common/email_rebind_jobs.py"
grep -qF '_rebind_cancel_cleanup' "$STAGE/web/routes/accounts_rebind.py"
grep -qF 'create_kwargs["stop_check"]' "$STAGE/web/routes/accounts_rebind.py"
grep -qF 'raise RuntimeError("cancelled")' "$STAGE/mail/__init__.py"
grep -qF '_stop_requested' "$STAGE/mail/mailcom.py"
grep -qF '_allocation_slot(stop_check)' "$STAGE/mail/mailcom.py"
grep -qF '_ACCOUNT_EXHAUSTED' "$STAGE/mail/mailcom.py"
grep -qF '_ACCOUNT_ALLOCATION_CONDITION' "$STAGE/mail/mailcom.py"
grep -qF 'alias_usage_summaries' "$STAGE/mail/mailcom.py"
grep -qF '全部主邮箱别名已满' "$STAGE/mail/mailcom.py"
grep -qF 'fetch_alias_inventory' "$STAGE/web/routes/mailcom.py"
grep -qF '"usage_source": "local_inventory"' "$STAGE/web/routes/mailcom.py"
grep -qF 'request.once("aborted"' "$STAGE/mailcode-service/server.mjs"
grep -qF 'response.once("close"' "$STAGE/mailcode-service/server.mjs"
grep -qF 'MAILCOM_ALIAS_OPERATION_TIMEOUT_MS' "$STAGE/mailcode-service/server.mjs"
grep -qF 'boundedOperation(' "$STAGE/mailcode-service/server.mjs"
grep -qF 'aliasInventory()' "$STAGE/mailcode-service/server.mjs"
grep -qF '/api/v1/alias-inventory' "$STAGE/mailcode-service/server.mjs"
grep -qF 'createAliasInConfiguredDomain' "$STAGE/mailcode-service/web-alias-client.mjs"
grep -qF 'method: "POST"' "$STAGE/mailcode-service/web-alias-client.mjs"
grep -RqlF '保存邮箱并直接分配' "$STAGE/web/static/vue/assets"
grep -RqlF 'dr.com' "$STAGE/web/static/vue/assets"
grep -RqlF '1 到 50' "$STAGE/web/static/vue/assets"
grep -RqlF '邮箱查询并发' "$STAGE/web/static/vue/assets"
grep -RqlF 'quarantined_alias_count' "$STAGE/web/static/vue/assets"
grep -RqlF '隔离' "$STAGE/web/static/vue/assets"
grep -RqlF 'Math.min(50' "$STAGE/web/static/vue/assets/RebindView-"*.js
"$APP/.venv/bin/python" -m py_compile \
  "$STAGE/common/email_rebind_jobs.py" \
  "$STAGE/mail/__init__.py" "$STAGE/mail/mailcom.py" \
  "$STAGE/web/routes/mailcom.py" "$STAGE/web/routes/accounts_rebind.py"
NODE_BIN="$APP/runtime/node-v22.23.2-linux-x64/bin/node"
test -x "$NODE_BIN"
"$NODE_BIN" --check "$STAGE/mailcode-service/server.mjs"
"$NODE_BIN" --check "$STAGE/mailcode-service/web-alias-client.mjs"

panel_probe() {
  PANEL_PASSWORD_B64="$PANEL_PASSWORD_B64" "$APP/.venv/bin/python" - <<'PY'
import base64, json, os, requests
s = requests.Session()
s.trust_env = False
password = base64.b64decode(os.environ['PANEL_PASSWORD_B64']).decode()
r = s.post('http://127.0.0.1:8790/api/auth/login', json={'username': 'admin', 'password': password}, timeout=15)
r.raise_for_status()
for path in (
    '/api/status', '/api/progress', '/api/accounts/rebind/progress',
    '/api/accounts/reauth/progress', '/api/accounts/refresh-at/progress',
    '/api/accounts/trial-eligibility/progress', '/api/accounts/health/progress',
    '/api/codex-oauth/status',
):
    payload = s.get('http://127.0.0.1:8790' + path, timeout=20)
    payload.raise_for_status()
    body = payload.json()
    items = [body, body.get('job') or {}, *(body.get('items') or [])]
    if any(isinstance(x, dict) and (x.get('running') or x.get('status') in ('queued', 'running', 'stopping')) for x in items):
        raise RuntimeError('active_task:' + path)
settings = s.get('http://127.0.0.1:8790/api/mailcom/settings?include_usage=0', timeout=90)
settings.raise_for_status()
body = settings.json()
print(json.dumps({'accounts': len(body.get('accounts') or []),
                  'proxy_enabled': body.get('proxy_pool_enabled'),
                  'proxy_count': body.get('proxy_pool_count'),
                  'proxy_assigned': body.get('proxy_pool_assigned')}, sort_keys=True))
PY
}

mailcode_probe() {
  (
    set -a
    # shellcheck disable=SC1091
    source /etc/gc-mailcode/mailcode.env
    set +a
    "$APP/.venv/bin/python" - <<'PY'
import collections, hashlib, json, os, requests
s = requests.Session()
s.trust_env = False
headers = {'x-api-key': os.environ['MAILCODE_ADMIN_KEY']}
a = s.get('http://127.0.0.1:18787/api/v1/accounts', headers=headers, timeout=30)
a.raise_for_status()
p = s.get('http://127.0.0.1:18787/api/v1/proxy-pool', headers=headers, timeout=30)
p.raise_for_status()
accounts = a.json().get('accounts') or []
aliases = []
for account in accounts:
    response = s.get('http://127.0.0.1:18787/api/v1/aliases',
                     headers=headers, params={'account_email': account.get('email')}, timeout=30)
    response.raise_for_status()
    aliases.extend(response.json().get('items') or [])
statuses = collections.Counter(str(item.get('status') or 'unknown') for item in aliases)
account_alias_counts = '\n'.join(sorted(
    '\t'.join((str(account.get('email') or '').lower(), str(int(account.get('aliasCount') or 0))))
    for account in accounts
))
mapping = '\n'.join(sorted(
    '\t'.join((str(item.get('address') or '').lower(),
               str(item.get('primaryEmail') or '').lower(),
               str(item.get('status') or 'unknown')))
    for item in aliases
))
pool = p.json().get('proxy_pool') or {}
print(json.dumps({'accounts': len(accounts),
                  'proxy_enabled': bool(pool.get('enabled')),
                  'proxy_count': int(pool.get('count') or 0),
                  'proxy_assigned': int(pool.get('assigned') or 0),
                  'alias_total': len(aliases),
                  'alias_statuses': dict(sorted(statuses.items())),
                  'account_alias_counts_sha256': hashlib.sha256(account_alias_counts.encode()).hexdigest(),
                  'alias_mapping_sha256': hashlib.sha256(mapping.encode()).hexdigest()}, sort_keys=True))
PY
  )
}

PRE_PANEL=$(panel_probe)
PRE_MAILCODE=$(mailcode_probe)
systemctl is-active --quiet "$PANEL"
systemctl is-active --quiet "$MAILCODE"
docker image inspect gpt-register-mailcode:local >/dev/null
mkdir -p -m 700 "$BACKUP"
docker tag gpt-register-mailcode:local "$OLD_IMAGE"
if ! docker build --pull=false -t "$NEW_IMAGE" "$STAGE/mailcode-service" >"$BACKUP/docker-build.log" 2>&1; then
  echo "docker_build=failed backup=$BACKUP" >&2
  exit 1
fi

# A long image build may outlive an idle window; check again immediately before stopping.
[[ "$(panel_probe)" == "$PRE_PANEL" ]]
[[ "$(mailcode_probe)" == "$PRE_MAILCODE" ]]
SOURCE_FILES=()
for file in "${FILES[@]}"; do
  if [[ "$file" == mailcode-service/web-alias-client.mjs ]]; then
    if [[ -f "$APP/$file" ]]; then
      HAD_WEB_CLIENT=1
      SOURCE_FILES+=("$file")
    fi
  else
    SOURCE_FILES+=("$file")
  fi
done
tar -czf "$BACKUP/source-before.tgz" -C "$APP" "${SOURCE_FILES[@]}"
cp -a "$STAGE/web/static/vue" "$BACKUP/vue-candidate"

STOPPED=1
systemctl stop "$PANEL"
systemctl stop "$MAILCODE"
tar -czf "$BACKUP/data-config-before.tgz" -C "$APP" data config.yaml
tar -czf "$BACKUP/mailcode-data-before.tgz" -C /var/lib gc-mailcode
tar -czf "$BACKUP/mailcode-env-before.tgz" -C /etc/gc-mailcode mailcode.env
tar -tzf "$BACKUP/data-config-before.tgz" >/dev/null
tar -tzf "$BACKUP/mailcode-data-before.tgz" >/dev/null
BACKUP_READY=1
CHANGED=1

for file in "${FILES[@]}"; do
  install -m 0644 "$STAGE/$file" "$APP/$file"
done
mv "$APP/web/static/vue" "$BACKUP/vue-before"
mv "$BACKUP/vue-candidate" "$APP/web/static/vue"
docker tag "$NEW_IMAGE" gpt-register-mailcode:local
docker run --rm --entrypoint sh gpt-register-mailcode:local -c \
  'test -s /app/web-alias-client.mjs && grep -qF '\''request.once("aborted"'\'' /app/server.mjs && grep -qF '\''response.once("close"'\'' /app/server.mjs && grep -qF MAILCOM_ALIAS_OPERATION_TIMEOUT_MS /app/server.mjs && grep -qF '\''boundedOperation('\'' /app/server.mjs && grep -qF '\''aliasInventory()'\'' /app/server.mjs && grep -qF '\''/api/v1/alias-inventory'\'' /app/server.mjs && grep -qF createAliasInConfiguredDomain /app/web-alias-client.mjs && grep -qF '\''method: "POST"'\'' /app/web-alias-client.mjs'
systemctl start "$MAILCODE"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 http://127.0.0.1:18787/api/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 10 http://127.0.0.1:18787/api/health >/dev/null
systemctl start "$PANEL"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 http://127.0.0.1:8790/api/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 10 http://127.0.0.1:8790/api/health >/dev/null
systemctl is-active --quiet "$PANEL"
systemctl is-active --quiet "$MAILCODE"
[[ "$(panel_probe)" == "$PRE_PANEL" ]]
[[ "$(mailcode_probe)" == "$PRE_MAILCODE" ]]
for file in "${FILES[@]}"; do
  cmp -s "$STAGE/$file" "$APP/$file"
done
diff -qr "$STAGE/web/static/vue" "$APP/web/static/vue" >/dev/null
CHANGED=0
STOPPED=0
echo "deploy=ok panel_health=200 mailcode_health=200 panel_counts=$PRE_PANEL mailcode_counts=$PRE_MAILCODE backup=$BACKUP"
