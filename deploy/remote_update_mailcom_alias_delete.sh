#!/usr/bin/env bash
set -Eeuo pipefail

APP="${APP:-/opt/gpt-register-fp-test}"
PKG="${PKG:-/tmp/gpt-register-fp-mailcom-alias-delete-20260919.tar.gz}"
APP_SERVICE="${APP_SERVICE:-gpt-register-fp-test.service}"
MAILCODE_MODE="${MAILCODE_MODE:-none}"
MAILCODE_SERVICE="${MAILCODE_SERVICE:-}"
MAILCODE_SOURCE_DIR="${MAILCODE_SOURCE_DIR:-}"
MAILCODE_CONTAINER="${MAILCODE_CONTAINER:-}"
MAILCODE_IMAGE="${MAILCODE_IMAGE:-}"
MAILCODE_BUILD_IMAGE="${MAILCODE_BUILD_IMAGE:-0}"
BACKUP_PREFIX="${BACKUP_PREFIX:-codex-before-mailcom-alias-delete}"

cd "$APP"
test -s "$PKG"
test -d "$APP/data"
mkdir -p "$APP/backups"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$APP/backups/$BACKUP_PREFIX-$STAMP"
TMP_DIR="/tmp/codex-mailcom-alias-delete-$STAMP-$$"
mkdir -p "$BACKUP" "$TMP_DIR"
DEPLOY_OK=0
APP_WAS_ACTIVE=0
MAIL_WAS_ACTIVE=0
CONTAINER_WAS_RUNNING=0
OLD_CONTAINER_IMAGE=""
MAILCODE_OWNER=""
MAILCODE_MODE_BITS="644"

data_manifest() {
  find "$APP/data" -type f -printf '%P\t%s\n' | sort
}

config_hash() {
  if [ -f "$APP/config.yaml" ]; then
    sha256sum "$APP/config.yaml" | awk '{print $1}'
  else
    printf 'missing\n'
  fi
}

node_bin() {
  if [ -x /usr/bin/node ]; then
    printf '/usr/bin/node\n'
  else
    command -v node
  fi
}

syntax_check() {
  file="$1"
  if [ "$MAILCODE_BUILD_IMAGE" = "1" ] && [ -n "$MAILCODE_IMAGE" ] && command -v docker >/dev/null 2>&1; then
    check_dir=$(dirname "$file")
    check_name=$(basename "$file")
    docker run --rm -v "$check_dir:/codex-check:ro" "$MAILCODE_IMAGE" node --check "/codex-check/$check_name"
  else
    "$(node_bin)" --check "$file"
  fi
}

rollback() {
  status=$?
  if [ "$DEPLOY_OK" -ne 1 ]; then
    systemctl stop "$APP_SERVICE" >/dev/null 2>&1 || true

    if [ -f "$BACKUP/source-before.tar.gz" ]; then
      tar -xzf "$BACKUP/source-before.tar.gz" -C "$APP" --no-same-owner >/dev/null 2>&1 || true
    fi

    if [ "$MAILCODE_MODE" = "source" ] && [ -n "$MAILCODE_SOURCE_DIR" ]; then
      if [ -f "$BACKUP/mailcode-server.mjs" ]; then
        install -m "$MAILCODE_MODE_BITS" "$BACKUP/mailcode-server.mjs" "$MAILCODE_SOURCE_DIR/server.mjs" || true
        if [ -n "$MAILCODE_OWNER" ]; then
          chown "$MAILCODE_OWNER" "$MAILCODE_SOURCE_DIR/server.mjs" || true
        fi
      fi
      if [ -n "$MAILCODE_SERVICE" ]; then
        systemctl restart "$MAILCODE_SERVICE" >/dev/null 2>&1 || true
      fi
    elif [ "$MAILCODE_MODE" = "docker" ] && [ -n "$MAILCODE_CONTAINER" ]; then
      if [ -f "$BACKUP/mailcode-server.mjs" ]; then
        docker cp "$BACKUP/mailcode-server.mjs" "$MAILCODE_CONTAINER:/app/server.mjs" >/dev/null 2>&1 || true
      fi
      if [ -n "$OLD_CONTAINER_IMAGE" ] && [ -n "$MAILCODE_IMAGE" ]; then
        docker tag "$OLD_CONTAINER_IMAGE" "$MAILCODE_IMAGE" >/dev/null 2>&1 || true
      fi
      if [ "$CONTAINER_WAS_RUNNING" -eq 1 ]; then
        docker restart "$MAILCODE_CONTAINER" >/dev/null 2>&1 || true
      fi
    fi

    if [ "$APP_WAS_ACTIVE" -eq 1 ]; then
      systemctl start "$APP_SERVICE" >/dev/null 2>&1 || true
    fi
    echo "rollback=attempted status=$status"
  fi
}

trap rollback EXIT

if systemctl is-active --quiet "$APP_SERVICE"; then APP_WAS_ACTIVE=1; fi

case "$MAILCODE_MODE" in
  none)
    ;;
  source)
    test -n "$MAILCODE_SOURCE_DIR"
    test -f "$MAILCODE_SOURCE_DIR/server.mjs"
    if [ -n "$MAILCODE_SERVICE" ] && systemctl is-active --quiet "$MAILCODE_SERVICE"; then MAIL_WAS_ACTIVE=1; fi
    MAILCODE_OWNER=$(stat -c '%u:%g' "$MAILCODE_SOURCE_DIR/server.mjs")
    MAILCODE_MODE_BITS=$(stat -c '%a' "$MAILCODE_SOURCE_DIR/server.mjs")
    cp "$MAILCODE_SOURCE_DIR/server.mjs" "$BACKUP/mailcode-server.mjs"
    ;;
  docker)
    test -n "$MAILCODE_CONTAINER"
    test -n "$MAILCODE_IMAGE"
    test "$(docker inspect -f '{{.State.Running}}' "$MAILCODE_CONTAINER")" = true
    CONTAINER_WAS_RUNNING=1
    OLD_CONTAINER_IMAGE=$(docker inspect -f '{{.Image}}' "$MAILCODE_CONTAINER")
    docker cp "$MAILCODE_CONTAINER:/app/server.mjs" "$BACKUP/mailcode-server.mjs"
    ;;
  *)
    echo "unsupported MAILCODE_MODE=$MAILCODE_MODE" >&2
    exit 2
    ;;
esac

data_manifest > "$BACKUP/data-manifest.before"
config_hash > "$BACKUP/config.sha256.before"
paths=(mail/mailcom.py web/routes/mailcom.py web/static/vue)
if [ -f "$APP/mailcode-service/server.mjs" ]; then
  paths+=(mailcode-service/server.mjs)
fi
tar -czf "$BACKUP/source-before.tar.gz" -C "$APP" "${paths[@]}"

tar -xzf "$PKG" -C "$TMP_DIR" --no-same-owner
PATCH_SERVER="$TMP_DIR/mailcode-service/server.mjs"
test -s "$PATCH_SERVER"

systemctl stop "$APP_SERVICE"
if [ "$MAILCODE_MODE" = "source" ] && [ -n "$MAILCODE_SERVICE" ]; then
  systemctl stop "$MAILCODE_SERVICE"
fi

tar -xzf "$PKG" -C "$APP" --no-same-owner
"$APP/.venv/bin/python" -m py_compile "$APP/mail/mailcom.py" "$APP/web/routes/mailcom.py"
syntax_check "$APP/mailcode-service/server.mjs"

if [ "$MAILCODE_MODE" = "source" ]; then
  install -m "$MAILCODE_MODE_BITS" "$PATCH_SERVER" "$MAILCODE_SOURCE_DIR/server.mjs"
  chown "$MAILCODE_OWNER" "$MAILCODE_SOURCE_DIR/server.mjs"
  syntax_check "$MAILCODE_SOURCE_DIR/server.mjs"
  if [ "$MAILCODE_BUILD_IMAGE" = "1" ]; then
    test -f "$MAILCODE_SOURCE_DIR/Dockerfile"
    test -n "$MAILCODE_IMAGE"
    docker build -t "$MAILCODE_IMAGE" "$MAILCODE_SOURCE_DIR"
  fi
elif [ "$MAILCODE_MODE" = "docker" ]; then
  docker cp "$PATCH_SERVER" "$MAILCODE_CONTAINER:/app/server.mjs"
  docker commit "$MAILCODE_CONTAINER" "$MAILCODE_IMAGE" >/dev/null
  docker restart "$MAILCODE_CONTAINER" >/dev/null
fi

data_manifest > "$BACKUP/data-manifest.after"
config_hash > "$BACKUP/config.sha256.after"
cmp -s "$BACKUP/data-manifest.before" "$BACKUP/data-manifest.after"
cmp -s "$BACKUP/config.sha256.before" "$BACKUP/config.sha256.after"

if [ "$MAILCODE_MODE" = "source" ] && [ -n "$MAILCODE_SERVICE" ]; then
  systemctl start "$MAILCODE_SERVICE"
fi
systemctl start "$APP_SERVICE"

app_code=000
for _ in $(seq 1 45); do
  app_code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/health || true)
  [ "$app_code" = "200" ] && break
  sleep 1
done
systemctl is-active --quiet "$APP_SERVICE"
test "$app_code" = "200"

mail_code=000
for _ in $(seq 1 45); do
  mail_code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:18787/api/health || true)
  [ "$mail_code" = "200" ] && break
  sleep 1
done
test "$mail_code" = "200"

route_code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8790/api/mailcom/aliases?email=probe%40invalid.local' || true)
case "$route_code" in
  200|400|401|403|404|422|502)
    ;;
  *)
    echo "unexpected_alias_route_code=$route_code" >&2
    exit 1
    ;;
esac

echo "app_health=$app_code"
echo "mailcode_health=$mail_code"
echo "alias_route=$route_code"
echo "backup=$BACKUP"
DEPLOY_OK=1
echo "deploy=ok"
