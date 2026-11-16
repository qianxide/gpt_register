#!/usr/bin/env bash
set -Eeuo pipefail

# Pin the host runtime independently of the mailcode container and system Node.
APP=/opt/gpt-register-fp-test
APP_SERVICE=gpt-register-fp-test.service
NODE_VERSION=22.23.2
NODE_ARCHIVE="node-v${NODE_VERSION}-linux-x64.tar.xz"
NODE_SHA256=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
RUNTIME_DIR="$APP/runtime/node-v${NODE_VERSION}-linux-x64"
DROPIN_DIR="/etc/systemd/system/$APP_SERVICE.d"
DROPIN="$DROPIN_DIR/20-node-runtime.conf"
WORKER="$APP/register/sentinel_vm/runtime_worker.js"
CHECK_PATH="$RUNTIME_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"

test "$(id -u)" = 0
test "$(uname -m)" = x86_64
test -f "$WORKER"
test -x "$APP/.venv/bin/python"
exec 9>/run/lock/gpt-register-node-repair.lock
flock -n 9

STAGE=$(mktemp -d /tmp/gpt-register-node-XXXXXX)
curl -fsS --connect-timeout 10 --max-time 180 --retry 2 \
  "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_ARCHIVE}" -o "$STAGE/$NODE_ARCHIVE"
printf '%s  %s\n' "$NODE_SHA256" "$STAGE/$NODE_ARCHIVE" | sha256sum -c -
tar -xJf "$STAGE/$NODE_ARCHIVE" -C "$STAGE"
"$STAGE/node-v${NODE_VERSION}-linux-x64/bin/node" --version
"$STAGE/node-v${NODE_VERSION}-linux-x64/bin/node" --check "$WORKER"

# Inspect the persisted task state without creating or modifying the database.
"$APP/.venv/bin/python" - "$APP" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1]) / 'data/task_history.sqlite3'
if db.exists():
    connection = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT value FROM task_meta WHERE name=?', ('last_job',)).fetchone()
        job = json.loads(row[0]) if row else {}
    finally:
        connection.close()
    if job.get('running'):
        raise SystemExit('Registration task is running; retry when it has finished.')
print('registration_idle=true')
PY

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$APP/backups/node-runtime-$STAMP"
mkdir -p "$BACKUP" "$APP/runtime" "$DROPIN_DIR"
chmod 700 "$BACKUP"
if [ -e "$RUNTIME_DIR" ]; then
  test -x "$RUNTIME_DIR/bin/node"
  cmp "$STAGE/node-v${NODE_VERSION}-linux-x64/bin/node" "$RUNTIME_DIR/bin/node"
else
  mv "$STAGE/node-v${NODE_VERSION}-linux-x64" "$RUNTIME_DIR"
fi

HAD_DROPIN=0
if [ -f "$DROPIN" ]; then
  cp -a "$DROPIN" "$BACKUP/node-runtime.conf.before"
  HAD_DROPIN=1
fi
systemctl cat "$APP_SERVICE" > "$BACKUP/app-service.before"
sha256sum "$APP/config.yaml" > "$BACKUP/config.sha256.before"
sha256sum "$WORKER" "$APP/register/protocol_sentinel.py" > "$BACKUP/registration-code.sha256.before"
MAIL_PID=$(systemctl show mailcode-service.service -p MainPID --value)
CHANGED=0
SUCCESS=0

rollback() {
  code=$?
  if [ "$SUCCESS" != 1 ] && [ "$CHANGED" = 1 ]; then
    if [ "$HAD_DROPIN" = 1 ]; then
      cp -a "$BACKUP/node-runtime.conf.before" "$DROPIN"
    elif [ -f "$DROPIN" ]; then
      mv "$DROPIN" "$BACKUP/node-runtime.conf.failed"
    fi
    systemctl daemon-reload
    systemctl restart "$APP_SERVICE" || true
    echo "rollback=performed exit=$code"
  fi
}
trap rollback EXIT

CHANGED=1
systemctl stop "$APP_SERVICE"
tar -czf "$BACKUP/data-config.before.tar.gz" -C "$APP" data config.yaml
printf '[Service]\nEnvironment="PATH=%s"\nExecStartPre=%s/bin/node --check %s\n' \
  "$CHECK_PATH" "$RUNTIME_DIR" "$WORKER" > "$DROPIN"
chmod 644 "$DROPIN"
systemctl daemon-reload
systemctl start "$APP_SERVICE"

app_code=000
for _ in $(seq 1 30); do
  app_code=$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/health || true)
  [ "$app_code" = 200 ] && break
  sleep 1
done
test "$app_code" = 200
systemctl is-active --quiet "$APP_SERVICE"

# Verify the PATH inherited by the actual service, rather than the SSH shell.
"$APP/.venv/bin/python" - "$APP" "$APP_SERVICE" "$RUNTIME_DIR" <<'PY'
import json
import shutil
import subprocess
import sys
from pathlib import Path

app, service, runtime = sys.argv[1:]
pid = subprocess.check_output(['systemctl', 'show', service, '-p', 'MainPID', '--value'], text=True).strip()
environment = dict(part.split('=', 1) for part in Path('/proc', pid, 'environ').read_bytes().decode().split('\0') if '=' in part)
resolved = shutil.which('node', path=environment.get('PATH'))
assert resolved == str(Path(runtime) / 'bin/node'), resolved
version = subprocess.check_output(['node', '--version'], env=environment, text=True).strip()
worker = Path(app) / 'register/sentinel_vm/runtime_worker.js'
subprocess.run(['node', '--check', str(worker)], env=environment, check=True, timeout=15)
# Starting server mode with closed stdin exercises module loading and startup
# without submitting account operations or calling external authentication APIs.
result = subprocess.run(['node', str(worker), '--server'], input='', env=environment, cwd=app, text=True, capture_output=True, timeout=15)
if result.returncode:
    raise RuntimeError('Worker startup failed: ' + result.stderr[:1000])
print(json.dumps({'service_node': resolved, 'node_version': version, 'worker_syntax': 'passed', 'worker_startup': 'passed'}))
PY

sha256sum -c "$BACKUP/config.sha256.before"
sha256sum -c "$BACKUP/registration-code.sha256.before"
test "$(systemctl show mailcode-service.service -p MainPID --value)" = "$MAIL_PID"
mail_code=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:18787/api/health)
test "$mail_code" = 200
echo "app_health=$app_code mailcode_health=$mail_code"
echo "backup=$BACKUP"
SUCCESS=1
echo 'node_runtime_repair=ok'
