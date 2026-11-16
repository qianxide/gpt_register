#!/usr/bin/env bash
set -Eeuo pipefail

APP="${APP:-/opt/gpt-register-fp-test}"
PKG="${PKG:-/tmp/gpt-register-fp-wdmail-20260915.tar.gz}"
SERVICE="${SERVICE:-gpt-register-fp-test.service}"
BACKUP_PREFIX="${BACKUP_PREFIX:-codex-before-wdmail}"

cd "$APP"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$APP/backups/$BACKUP_PREFIX-$STAMP"
DEPLOY_OK=0

rollback() {
  status=$?
  if [ "$DEPLOY_OK" -ne 1 ]; then
    if [ -f "$BACKUP/backend-before.tar.gz" ]; then
      tar -xzf "$BACKUP/backend-before.tar.gz" -C "$APP" --no-same-owner
    fi
    if [ -f "$BACKUP/web-static-before.tar.gz" ]; then
      tar -xzf "$BACKUP/web-static-before.tar.gz" -C "$APP" --no-same-owner
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
tar -czf "$BACKUP/backend-before.tar.gz" -C "$APP" mail/__init__.py mail/wdmail.py
tar -czf "$BACKUP/web-static-before.tar.gz" -C "$APP" web/static/vue

systemctl stop "$SERVICE"
tar -xzf "$PKG" -C "$APP" --no-same-owner
"$APP/.venv/bin/python" -m compileall -q -f "$APP/mail" "$APP/web" "$APP/web_app.py"
"$APP/.venv/bin/python" -c 'from mail.parsing import parse_icloud_credentials; from mail.wdmail import wdmail_json_url; wd=parse_icloud_credentials("pettier_remedy.9x@icloud.com----https://api.wdmail.top/m?f=html&e=pettier_remedy.9x@icloud.com")[0]; assert wd["fetch_url"].startswith("https://api.wdmail.top/m?"); assert "f=json" in wdmail_json_url(wd["fetch_url"]); hao=parse_icloud_credentials(r"fleet_online_12\@icloud.com----[https://icloud-api.haofanzi.net/api/mailboxes/latest?email_address=fleet_online_12@icloud.com&access_key=TOKEN](https://icloud-api.haofanzi.net/api/mailboxes/latest?email_address=fleet_online_12@icloud.com\&access_key=TOKEN)")[0]; assert hao["fetch_url"].endswith("access_key=TOKEN"); print("direct-mail-adapters=ok")'

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
sha256sum "$APP/mail/__init__.py" "$APP/mail/wdmail.py"
echo "backup=$BACKUP"
DEPLOY_OK=1
echo "deploy=ok"
