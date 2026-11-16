#!/usr/bin/env bash
set -Eeuo pipefail

APP="${APP:-/opt/gpt-register-fp-test}"
PKG="${PKG:-/tmp/gpt-register-fp-v8-otp-retry.tar.gz}"
SERVICE="${SERVICE:-gpt-register-fp-test.service}"
BACKUP_PREFIX="${BACKUP_PREFIX:-codex-before-otp-retry}"
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
systemctl daemon-reload
systemctl stop "$SERVICE"
tar -czf "$BACKUP/source-before.tar.gz" \
  -C "$APP" \
  --exclude='./data' \
  --exclude='./.venv' \
  --exclude='./backups' \
  --exclude='./__pycache__' \
  --exclude='*/__pycache__' \
  .

if [ -f "$APP/config.yaml" ]; then
  cp -p "$APP/config.yaml" "$BACKUP/config.yaml"
fi
if [ -d "$APP/data" ]; then
  find "$APP/data" -type f -printf '%P\t%s\n' | sort > "$BACKUP/data-manifest.before"
else
  : > "$BACKUP/data-manifest.before"
fi
sha256sum "$APP/config.yaml" 2>/dev/null | awk '{print $1}' > "$BACKUP/config.sha256.before" || true

tar -xzf "$PKG" -C "$APP" --strip-components=1 --no-same-owner
"$APP/.venv/bin/python" -m compileall -q -f "$APP/mail" "$APP/web" "$APP/web_app.py"
"$APP/.venv/bin/python" -c "from mail.wdmail import is_wdmail_url, wdmail_json_url, normalize_wdmail_inbox; import mail; print('mail-adapter-import=ok')"

sha256sum "$APP/config.yaml" 2>/dev/null | awk '{print $1}' > "$BACKUP/config.sha256.after" || true
if [ -d "$APP/data" ]; then
  find "$APP/data" -type f -printf '%P\t%s\n' | sort > "$BACKUP/data-manifest.after"
else
  : > "$BACKUP/data-manifest.after"
fi
if cmp -s "$BACKUP/config.sha256.before" "$BACKUP/config.sha256.after"; then
  echo "config=unchanged"
else
  echo "config=changed"
fi
if cmp -s "$BACKUP/data-manifest.before" "$BACKUP/data-manifest.after"; then
  echo "data-manifest=unchanged"
else
  echo "data-manifest=changed"
fi

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

for file in mail/__init__.py mail/parsing.py mail/wdmail.py web/routes/mailbox.py; do
  sha256sum "$APP/$file"
done
echo "backup=$BACKUP"
DEPLOY_OK=1
echo "deploy=ok"
