#!/usr/bin/env bash
set -Eeuo pipefail

APP="${APP:-/opt/gpt-register-fp-test}"
PKG="${PKG:-/tmp/gpt-register-fp-mailcom-bulk-20260918.tar.gz}"
SERVICE="${SERVICE:-gpt-register-fp-test.service}"
BACKUP_PREFIX="${BACKUP_PREFIX:-codex-before-mailcom-bulk}"

cd "$APP"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$APP/backups/$BACKUP_PREFIX-$STAMP"
DEPLOY_OK=0

rollback() {
  status=$?
  if [ "$DEPLOY_OK" -ne 1 ]; then
    if [ -f "$BACKUP/source-before.tar.gz" ]; then
      tar -xzf "$BACKUP/source-before.tar.gz" -C "$APP" --no-same-owner
    fi
    systemctl start "$SERVICE" >/dev/null 2>&1 || true
    echo "rollback=attempted status=$status"
  fi
}

trap rollback EXIT
test -s "$PKG"
mkdir -p "$BACKUP"

if [ -d "$APP/data" ]; then
  find "$APP/data" -type f -printf '%P\t%s\n' | sort > "$BACKUP/data-manifest.before"
else
  : > "$BACKUP/data-manifest.before"
fi
sha256sum "$APP/config.yaml" 2>/dev/null | awk '{print $1}' > "$BACKUP/config.sha256.before" || true
tar -czf "$BACKUP/source-before.tar.gz" -C "$APP" mail/mailcom.py web/routes/mailcom.py web/static/vue

systemctl stop "$SERVICE"
tar -xzf "$PKG" -C "$APP" --no-same-owner
"$APP/.venv/bin/python" -m compileall -q -f "$APP/mail/mailcom.py" "$APP/web/routes/mailcom.py"
"$APP/.venv/bin/python" -c 'from web.routes.mailcom import MailcomBulkImportRequest; from mail.mailcom import MailcomApiProvider; print("mailcom-bulk-import=ok")'

if [ -d "$APP/data" ]; then
  find "$APP/data" -type f -printf '%P\t%s\n' | sort > "$BACKUP/data-manifest.after"
else
  : > "$BACKUP/data-manifest.after"
fi
sha256sum "$APP/config.yaml" 2>/dev/null | awk '{print $1}' > "$BACKUP/config.sha256.after" || true
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
echo "health_code=$health_code"
test "$health_code" = "200"
sha256sum "$APP/mail/mailcom.py" "$APP/web/routes/mailcom.py"
echo "backup=$BACKUP"
DEPLOY_OK=1
echo "deploy=ok"
