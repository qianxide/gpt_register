#!/usr/bin/env bash
set -Eeuo pipefail

APP="${APP:-/opt/gpt-register-fp-test}"
PKG="${PKG:-/tmp/gpt-register-fp-full-20260918.tar.gz}"
APP_SERVICE="${APP_SERVICE:-gpt-register-fp-test.service}"
MAIL_SERVICE="${MAIL_SERVICE:-mailcode-service.service}"
MAIL_IMAGE="${MAIL_IMAGE:-gpt-register-mailcode:local}"
MAIL_ENV="${MAIL_ENV:-/etc/gc-mailcode/mailcode.env}"
MAIL_DATA="${MAIL_DATA:-/var/lib/gc-mailcode}"
BACKUP_ROOT="${BACKUP_ROOT:-$APP/backups}"

if [ "$(id -u)" -ne 0 ]; then
  echo "该安装脚本需要 root 权限" >&2
  exit 1
fi
test -s "$PKG"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-dev build-essential ca-certificates curl tar openssl

mkdir -p "$APP" "$BACKUP_ROOT" "$MAIL_DATA" "$(dirname "$MAIL_ENV")"
chmod 700 "$MAIL_DATA"

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_ROOT/full-install-before-$STAMP"
mkdir -p "$BACKUP"
if [ -f "$APP/config.yaml" ]; then
  cp -a "$APP/config.yaml" "$BACKUP/config.yaml.before"
fi
if [ -d "$APP/data" ]; then
  tar -czf "$BACKUP/data-before.tar.gz" -C "$APP" data
fi

tar -xzf "$PKG" -C "$APP" --no-same-owner
cp -f "$APP/config.example.yaml" "$APP/config.yaml"

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$APP/.venv/bin/python" -m pip install -r "$APP/requirements.txt"

: "${PANEL_PASSWORD:?Set PANEL_PASSWORD before running this installation script}"
export PANEL_PASSWORD

"$APP/.venv/bin/python" - "$APP/config.yaml" <<'PY'
from pathlib import Path
import os
import sys
import yaml

path = Path(sys.argv[1])
cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
mail = cfg.setdefault("mail", {})
mail["split_count"] = 1
providers = mail.setdefault("providers", [])
mailcom = next((item for item in providers if item.get("type") == "mailcom_api"), None)
if mailcom is None:
    mailcom = {"type": "mailcom_api"}
    providers.append(mailcom)
mailcom.update({
    "enable": True,
    "rebind_only": True,
    "label": "mail.com 域名邮箱",
    "api_base": "http://127.0.0.1:18787",
    "admin_key": "",
    "admin_key_env": "MAILCODE_ADMIN_KEY",
    "accounts": "",
    "import_concurrency": 3,
    "alias_domain": "random",
    "mailboxes": "",
})
web = cfg.setdefault("web", {})
web.update({
    "host": "0.0.0.0",
    "port": 8790,
    "username": "admin",
    "password": os.environ["PANEL_PASSWORD"],
})
path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
PY
chmod 600 "$APP/config.yaml"

if [ ! -s "$MAIL_ENV" ]; then
  MAILCODE_ADMIN_KEY=$(openssl rand -hex 32)
  umask 077
  printf 'MAILCODE_ADMIN_KEY=%s\nMAILCODE_HOST=127.0.0.1\nMAILCODE_PORT=18787\nMAILCODE_DATA_DIR=/var/lib/gc-mailcode\n' "$MAILCODE_ADMIN_KEY" > "$MAIL_ENV"
fi
chmod 600 "$MAIL_ENV"

docker build --pull -t "$MAIL_IMAGE" "$APP/mailcode-service"

cat > /etc/systemd/system/$MAIL_SERVICE <<EOF
[Unit]
Description=GPT Register mail.com mailcode service
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/docker rm -f gc-register-mailcode
ExecStart=/usr/bin/docker run --rm --name gc-register-mailcode --network host --env-file $MAIL_ENV -v $MAIL_DATA:/var/lib/gc-mailcode:rw $MAIL_IMAGE
ExecStop=/usr/bin/docker stop -t 10 gc-register-mailcode
Restart=always
RestartSec=5
TimeoutStartSec=180
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/$APP_SERVICE <<EOF
[Unit]
Description=GPT Register FP Web Panel
After=network-online.target $MAIL_SERVICE
Requires=$MAIL_SERVICE
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$MAIL_ENV
ExecStart=$APP/.venv/bin/python -u cli.py web --host 0.0.0.0 --port 8790
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$MAIL_SERVICE" "$APP_SERVICE" >/dev/null
systemctl restart "$MAIL_SERVICE"
systemctl restart "$APP_SERVICE"

mail_code=000
for _ in $(seq 1 45); do
  mail_code=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:18787/api/health || true)
  [ "$mail_code" = "200" ] && break
  sleep 1
done
app_code=000
for _ in $(seq 1 45); do
  app_code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8790/api/health || true)
  [ "$app_code" = "200" ] && break
  sleep 1
done

systemctl is-active --quiet "$MAIL_SERVICE"
systemctl is-active --quiet "$APP_SERVICE"
test "$mail_code" = "200"
test "$app_code" = "200"
echo "mailcode_health=$mail_code"
echo "app_health=$app_code"
echo "app=$APP"
echo "mailcode_data=$MAIL_DATA"
echo "backup=$BACKUP"
echo "install=ok"
