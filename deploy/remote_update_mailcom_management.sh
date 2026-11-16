#!/usr/bin/env bash
set -Eeuo pipefail

APP=/opt/gpt-register-fp-test
SERVICE=gpt-register-fp-test.service
PKG="${1:?release archive required}"
EXPECTED_SHA="${2:?release checksum required}"
case "$PKG" in /tmp/gpt-register-mailcom-management-*.tar.gz) ;; *) exit 2;; esac
test -d "$APP/data"
test -f "$APP/config.yaml"
test "$(sha256sum "$PKG" | cut -d ' ' -f 1)" = "$EXPECTED_SHA"
cd "$APP"

BACKUP="$APP/backups/mailcom-management-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$BACKUP"
DEPLOY_OK=0
STOPPED=0
rollback() {
  status=$?
  if [ "$DEPLOY_OK" -ne 1 ] && [ "$STOPPED" -eq 1 ]; then
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    if [ -s "$BACKUP/source.tar.gz" ]; then
      tar -xzf "$BACKUP/source.tar.gz" -C "$APP" --no-same-owner
    fi
    systemctl start "$SERVICE" >/dev/null 2>&1 || true
    echo "rollback=attempted status=$status backup=$BACKUP"
  fi
}
trap rollback EXIT

# Back up only the application files this release will replace.
paths=(mail/mailcom.py web/routes/mailcom.py web/static/vue)
if [ -f "$APP/web-ui/src/components/MailcomRebindSettings.vue" ]; then
  paths+=(web-ui/src/components/MailcomRebindSettings.vue)
fi
tar -czf "$BACKUP/source.tar.gz" -C "$APP" "${paths[@]}"
systemctl stop "$SERVICE"
STOPPED=1
sha256sum config.yaml > "$BACKUP/config.before"
find data -type f -printf '%P\t%s\n' | sort > "$BACKUP/data.before"
tar -xzf "$PKG" -C "$APP" --no-same-owner
"$APP/.venv/bin/python" -m py_compile mail/mailcom.py web/routes/mailcom.py
"$APP/.venv/bin/python" -B -c 'from web.routes.mailcom import MailcomAliasBatchDeleteRequest, MailcomAccountBatchRemoveRequest; assert MailcomAliasBatchDeleteRequest(emails=["fixture@example.test"]).concurrency == 3; assert MailcomAccountBatchRemoveRequest(emails=["fixture@example.test"]).concurrency == 3'
sha256sum config.yaml > "$BACKUP/config.after"
find data -type f -printf '%P\t%s\n' | sort > "$BACKUP/data.after"
cmp -s "$BACKUP/config.before" "$BACKUP/config.after"
cmp -s "$BACKUP/data.before" "$BACKUP/data.after"
systemctl start "$SERVICE"
health=000
for _ in $(seq 1 30); do
  health=$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/health || true)
  [ "$health" = 200 ] && break
  sleep 1
done
test "$health" = 200
systemctl is-active --quiet "$SERVICE"
test "$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:18787/api/health)" = 200
DEPLOY_OK=1
echo "deploy=ok app_health=200 mailcode_health=200 config=unchanged data=unchanged backup=$BACKUP"
