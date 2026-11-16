#!/usr/bin/env bash
set -Eeuo pipefail

APP=/opt/gpt-register-fp-test
PACKAGE=/tmp/mailcom-proxy-failover-20260920.tgz
EXPECTED_SHA256=7fad599ae9bf606550e1ce17541130a7664b032ab86c8634de5087a2ec77a924
PANEL_SERVICE=gpt-register-fp-test.service
MAILCODE_SERVICE=mailcode-service.service
WITH_MAILCODE=${1:-0}
STAGE=$(mktemp -d /tmp/mailcom-proxy-pool.XXXXXX)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$APP/backups/mailcom-proxy-pool-$STAMP"
BACKUP_IMAGE="gpt-register-mailcode:backup-$STAMP"
CANDIDATE_IMAGE="gpt-register-mailcode:proxy-pool-$STAMP"
CHANGES_STARTED=0

cleanup() {
  rm -rf -- "$STAGE"
}

rollback() {
  local status=$?
  trap - ERR
  set +e
  if [[ "$CHANGES_STARTED" == 1 ]]; then
    systemctl stop "$PANEL_SERVICE"
    if [[ "$WITH_MAILCODE" == 1 ]]; then
      systemctl stop "$MAILCODE_SERVICE"
    fi
    tar -xzf "$BACKUP/source-before.tgz" -C "$APP"
    if [[ "$WITH_MAILCODE" == 1 ]]; then
      docker tag "$BACKUP_IMAGE" gpt-register-mailcode:local
      systemctl start "$MAILCODE_SERVICE"
    fi
    systemctl start "$PANEL_SERVICE"
    echo "rollback=restored backup=$BACKUP" >&2
  fi
  cleanup
  exit "$status"
}

trap cleanup EXIT
trap rollback ERR

[[ -n "${PANEL_PASSWORD_B64:-}" ]]
[[ "$(sha256sum "$PACKAGE" | awk '{print $1}')" == "$EXPECTED_SHA256" ]]
tar -xzf "$PACKAGE" -C "$STAGE"
test -f "$STAGE/mail/mailcom.py"
test -f "$STAGE/web/routes/mailcom.py"
test -f "$STAGE/web/static/vue/index.html"
test -f "$STAGE/mailcode-service/server.mjs"
"$APP/.venv/bin/python" -m py_compile "$STAGE/mail/mailcom.py" "$STAGE/web/routes/mailcom.py"
if [[ "$WITH_MAILCODE" == 1 ]]; then
  NODE_BIN="$APP/runtime/node-v22.23.2-linux-x64/bin/node"
  [[ -x "$NODE_BIN" ]] || NODE_BIN=node
  "$NODE_BIN" --check "$STAGE/mailcode-service/server.mjs"
  "$NODE_BIN" --check "$STAGE/mailcode-service/store.mjs"
fi

panel_check() {
  VERIFY_PROXY_FIELDS=${1:-0} "$APP/.venv/bin/python" - <<'PY'
import base64
import json
import os
import requests

s = requests.Session()
s.trust_env = False
password = base64.b64decode(os.environ["PANEL_PASSWORD_B64"]).decode()
r = s.post("http://127.0.0.1:8790/api/auth/login", json={"username": "admin", "password": password}, timeout=15)
r.raise_for_status()
for path in (
    "/api/status", "/api/progress", "/api/codex-oauth/status",
    "/api/accounts/rebind/progress", "/api/accounts/reauth/progress",
    "/api/accounts/refresh-at/progress", "/api/accounts/trial-eligibility/progress",
    "/api/accounts/health/progress",
):
    value = s.get("http://127.0.0.1:8790" + path, timeout=20)
    value.raise_for_status()
    payload = value.json()
    if path == "/api/status":
        payload = payload.get("job") or {}
    if payload.get("running") or payload.get("status") in ("running", "stopping"):
        raise RuntimeError("active task: " + path)
settings = s.get("http://127.0.0.1:8790/api/mailcom/settings", timeout=40)
settings.raise_for_status()
payload = settings.json()
if os.environ.get("VERIFY_PROXY_FIELDS") == "1":
    for key in ("proxy_pool_enabled", "proxy_pool_count", "proxy_pool_assigned", "proxy_pool_items"):
        if key not in payload:
            raise RuntimeError("missing proxy field: " + key)
print(json.dumps({
    "panel": "ok",
    "mailcom_accounts": len(payload.get("accounts") or []),
    "proxy_pool_enabled": payload.get("proxy_pool_enabled"),
    "proxy_pool_count": payload.get("proxy_pool_count"),
    "proxy_pool_assigned": payload.get("proxy_pool_assigned"),
}, ensure_ascii=False))
PY
}

panel_check 0

if [[ "$WITH_MAILCODE" == 1 ]]; then
  docker image inspect gpt-register-mailcode:local >/dev/null
  docker tag gpt-register-mailcode:local "$BACKUP_IMAGE"
  docker build --pull=false -t "$CANDIDATE_IMAGE" "$STAGE/mailcode-service"
fi

mkdir -p -m 700 "$BACKUP"
BACKUP_SOURCES=(mail/mailcom.py web/routes/mailcom.py web/static/vue)
if [[ "$WITH_MAILCODE" == 1 ]]; then
  BACKUP_SOURCES+=(
    mailcode-service/Dockerfile mailcode-service/package.json mailcode-service/package-lock.json \
    mailcode-service/server.mjs mailcode-service/mailbox.mjs mailcode-service/store.mjs
  )
fi
tar -czf "$BACKUP/source-before.tgz" -C "$APP" "${BACKUP_SOURCES[@]}"
tar -czf "$BACKUP/data-config-before.tgz" -C "$APP" data config.yaml
if [[ "$WITH_MAILCODE" == 1 ]]; then
  tar -czf "$BACKUP/mailcode-data-before.tgz" -C /var/lib gc-mailcode
fi

panel_check 0
CHANGES_STARTED=1
systemctl stop "$PANEL_SERVICE"
if [[ "$WITH_MAILCODE" == 1 ]]; then
  systemctl stop "$MAILCODE_SERVICE"
fi

install -m 0644 "$STAGE/mail/mailcom.py" "$APP/mail/mailcom.py"
install -m 0644 "$STAGE/web/routes/mailcom.py" "$APP/web/routes/mailcom.py"
cp -a "$STAGE/web/static/vue/." "$APP/web/static/vue/"

if [[ "$WITH_MAILCODE" == 1 ]]; then
  install -m 0644 "$STAGE/mailcode-service/Dockerfile" "$APP/mailcode-service/Dockerfile"
  install -m 0644 "$STAGE/mailcode-service/package.json" "$APP/mailcode-service/package.json"
  install -m 0644 "$STAGE/mailcode-service/package-lock.json" "$APP/mailcode-service/package-lock.json"
  install -m 0644 "$STAGE/mailcode-service/server.mjs" "$APP/mailcode-service/server.mjs"
  install -m 0644 "$STAGE/mailcode-service/mailbox.mjs" "$APP/mailcode-service/mailbox.mjs"
  install -m 0644 "$STAGE/mailcode-service/store.mjs" "$APP/mailcode-service/store.mjs"
  docker tag "$CANDIDATE_IMAGE" gpt-register-mailcode:local
  systemctl start "$MAILCODE_SERVICE"
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 5 http://127.0.0.1:18787/api/health >/dev/null; then break; fi
    sleep 1
  done
  curl -fsS --max-time 10 http://127.0.0.1:18787/api/health >/dev/null
fi

systemctl start "$PANEL_SERVICE"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 http://127.0.0.1:8790/api/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS --max-time 10 http://127.0.0.1:8790/api/health >/dev/null
panel_check 1

if [[ "$WITH_MAILCODE" == 1 ]]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/gc-mailcode/mailcode.env
  set +a
  ACCOUNT_COUNT=$(curl -fsS --max-time 15 -H "x-api-key: $MAILCODE_ADMIN_KEY" http://127.0.0.1:18787/api/v1/accounts \
    | "$APP/.venv/bin/python" -c 'import json,sys; print(len(json.load(sys.stdin).get("accounts") or []))')
  PROXY_STATE=$(curl -fsS --max-time 15 -H "x-api-key: $MAILCODE_ADMIN_KEY" http://127.0.0.1:18787/api/v1/proxy-pool \
    | "$APP/.venv/bin/python" -c 'import json,sys; x=json.load(sys.stdin).get("proxy_pool") or {}; enabled=bool(x.get("enabled")); count=int(x.get("count") or 0); assigned=int(x.get("assigned") or 0); print(f"enabled={str(enabled).lower()} count={count} assigned={assigned}")')
  echo "mailcode=ok accounts=$ACCOUNT_COUNT proxy_pool=$PROXY_STATE"
fi

sha256sum "$APP/mail/mailcom.py" "$APP/web/routes/mailcom.py" "$APP/web/static/vue/index.html"
CHANGES_STARTED=0
echo "deploy=ok backup=$BACKUP"
