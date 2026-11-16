#!/usr/bin/env bash
set -Eeuo pipefail

APP="${APP:-/opt/gpt-register-fp-test}"
PKG="${PKG:-/tmp/gpt-register-fp-rebind-deferred-mailcom-20260918.tar.gz}"
SERVICE="${SERVICE:-gpt-register-fp-test.service}"
BACKUP_PREFIX="${BACKUP_PREFIX:-codex-before-rebind-deferred-mailcom}"

cd "$APP"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$APP/backups/$BACKUP_PREFIX-$STAMP"
DEPLOY_OK=0

data_manifest() {
  if [ -d "$APP/data" ]; then
    find "$APP/data" -type f -printf '%P\t%s\n' | sort
  fi
}

config_hash() {
  if [ -f "$APP/config.yaml" ]; then
    sha256sum "$APP/config.yaml" | awk '{print $1}'
  else
    printf 'missing\n'
  fi
}

rollback() {
  status=$?
  if [ "$DEPLOY_OK" -ne 1 ]; then
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    if [ -f "$BACKUP/source-before.tar.gz" ]; then
      tar -xzf "$BACKUP/source-before.tar.gz" -C "$APP" --no-same-owner
    fi
    systemctl start "$SERVICE" >/dev/null 2>&1 || true
    echo "rollback=attempted status=$status"
  fi
}

trap rollback EXIT
test -s "$PKG"
test -f "$APP/web/routes/accounts_rebind.py"
test -d "$APP/web/static/vue"
mkdir -p "$BACKUP"
data_manifest > "$BACKUP/data-manifest.before"
config_hash > "$BACKUP/config.sha256.before"
tar -czf "$BACKUP/source-before.tar.gz" -C "$APP" web/routes/accounts_rebind.py web/static/vue

systemctl stop "$SERVICE"
tar -xzf "$PKG" -C "$APP" --no-same-owner
"$APP/.venv/bin/python" -m py_compile "$APP/web/routes/accounts_rebind.py"

data_manifest > "$BACKUP/data-manifest.after"
config_hash > "$BACKUP/config.sha256.after"
cmp -s "$BACKUP/data-manifest.before" "$BACKUP/data-manifest.after"
cmp -s "$BACKUP/config.sha256.before" "$BACKUP/config.sha256.after"

systemctl start "$SERVICE"
health_code=000
for n in $(seq 1 45); do
  if ! systemctl is-active --quiet "$SERVICE"; then
    sleep 1
    continue
  fi
  health_code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/health || true)
  if [ "$health_code" = "200" ]; then
    break
  fi
  sleep 1
done
systemctl is-active --quiet "$SERVICE"
test "$health_code" = "200"
echo "health_code=$health_code"
sha256sum "$APP/web/routes/accounts_rebind.py"
echo "backup=$BACKUP"
DEPLOY_OK=1
echo "deploy=ok"
