#!/usr/bin/env bash
set -Eeuo pipefail

APP=/opt/gpt-register-fp-test
SERVICE=gpt-register-fp-test.service
PKG="${1:?release archive required}"
EXPECTED_SHA="${2:?release checksum required}"
case "$PKG" in
  /tmp/gpt-register-fp-external-pipeline-20260920.tar.gz) ;;
  *) echo "unexpected release archive" >&2; exit 2 ;;
esac
test -d "$APP/data"
test -f "$APP/config.yaml"
test "$(sha256sum "$PKG" | cut -d ' ' -f 1)" = "$EXPECTED_SHA"

# The password arrives over stdin, so it never appears in the remote command
# line or in deployment output.
IFS= read -r PANEL_PASSWORD
export PANEL_PASSWORD

"$APP/.venv/bin/python" - "$APP" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

import requests

app = Path(sys.argv[1])
session = requests.Session()
session.trust_env = False
response = session.post(
    "http://127.0.0.1:8790/api/auth/login",
    json={"username": "admin", "password": os.environ.get("PANEL_PASSWORD", "")},
    timeout=15,
)
response.raise_for_status()

paths = (
    "/api/status", "/api/progress", "/api/codex-oauth/status",
    "/api/accounts/rebind/progress", "/api/accounts/reauth/progress",
    "/api/accounts/refresh-at/progress", "/api/accounts/trial-eligibility/progress",
    "/api/accounts/health/progress",
)
for path in paths:
    item = session.get("http://127.0.0.1:8790" + path, timeout=20).json()
    candidates = [item]
    if isinstance(item, dict):
        candidates.extend(value for key, value in item.items() if key in {"job", "progress"})
    if any(isinstance(value, dict) and (
        value.get("running") or value.get("status") in {"queued", "running", "stopping"}
    ) for value in candidates):
        raise SystemExit("active panel task: " + path)

db_path = app / "data/external_api.sqlite3"
if db_path.exists():
    with sqlite3.connect(db_path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "upi_jobs" in tables:
            count = db.execute(
                "SELECT COUNT(*) FROM upi_jobs WHERE status IN ('queued','submitting','running','cancelling')"
            ).fetchone()[0]
            if count:
                raise SystemExit("external UPI jobs are active")
        if "scan_submissions" in tables:
            count = db.execute(
                "SELECT COUNT(*) FROM scan_submissions WHERE status='submitting'"
            ).fetchone()[0]
            if count:
                raise SystemExit("external scan submissions are active")
        if "pipeline_tasks" in tables:
            count = db.execute(
                "SELECT COUNT(*) FROM pipeline_tasks WHERE status IN ('queued','running','stopping')"
            ).fetchone()[0]
            if count:
                raise SystemExit("external pipeline task is active")
PY

BACKUP="$APP/backups/external-pipeline-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$BACKUP"
DEPLOY_OK=0
STOPPED=0
rollback() {
  status=$?
  if [ "$DEPLOY_OK" -ne 1 ] && [ "$STOPPED" -eq 1 ]; then
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    tar -xzf "$BACKUP/source.tar.gz" -C "$APP" --no-same-owner >/dev/null 2>&1 || true
    systemctl start "$SERVICE" >/dev/null 2>&1 || true
    echo "rollback=attempted status=$status backup=$BACKUP"
  fi
}
trap rollback EXIT

backup_paths=(
  common/external_clients.py
  common/external_api.py
  web/routes/external_api.py
  web/static/vue
)
if [ -f "$APP/common/external_pipeline.py" ]; then
  backup_paths+=(common/external_pipeline.py)
fi
tar -czf "$BACKUP/source.tar.gz" -C "$APP" "${backup_paths[@]}"
tar -czf "$BACKUP/data-config.tar.gz" -C "$APP" data config.yaml
sha256sum "$APP/config.yaml" > "$BACKUP/config.before"

SETTINGS_BEFORE=$("$APP/.venv/bin/python" - "$APP/data/external_api.sqlite3" <<'PY'
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
rows = []
if path.exists():
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT name,payload FROM settings ORDER BY name").fetchall()
print(hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest())
PY
)

systemctl stop "$SERVICE"
STOPPED=1
rm -rf "$APP/web/static/vue"
tar -xzf "$PKG" -C "$APP" --no-same-owner

"$APP/.venv/bin/python" -m py_compile \
  "$APP/common/external_clients.py" "$APP/common/external_api.py" \
  "$APP/common/external_pipeline.py" "$APP/web/routes/external_api.py"

systemctl start "$SERVICE"
health=000
for _ in $(seq 1 45); do
  health=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/health || true)
  [ "$health" = 200 ] && break
  sleep 1
done
test "$health" = 200
systemctl is-active --quiet "$SERVICE"

"$APP/.venv/bin/python" - "$APP" "$SETTINGS_BEFORE" <<'PY'
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

app = Path(sys.argv[1])
before = sys.argv[2]
sys.path.insert(0, str(app))
from common.external_api import get_external_service
from common.external_pipeline import PipelineManager
from web.routes.external_api import router

service = get_external_service()
settings = service.settings()
assert settings["scan"]["base_url"] == "https://scan.fishmail.vip"
assert settings["upi"]["base_url"] == "https://upi.43-255-159-106.sslip.io"
assert isinstance(service._pipeline(), PipelineManager)
assert len(router.routes) == 20
db_path = app / "data/external_api.sqlite3"
with sqlite3.connect(db_path) as db:
    rows = db.execute("SELECT name,payload FROM settings ORDER BY name").fetchall()
after = hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
assert after == before, "external credentials changed"
print("panel=200 external_routes=20 pipeline=ready credentials=preserved")
PY

sha256sum "$APP/config.yaml" > "$BACKUP/config.after"
cmp -s "$BACKUP/config.before" "$BACKUP/config.after"
DEPLOY_OK=1
echo "deploy=ok backup=$BACKUP"
