"""Deploy the SMS pool to the five approved panels; leave server 154 untouched."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import requests


ALLOWED_HOSTS = {"45.205.31.123", "45.205.31.204", "45.205.31.227", "45.205.31.253", "45.205.31.69"}
APP = Path("/opt/gpt-register-fp-test")
SERVICE = "gpt-register-fp-test.service"
PACKAGE = Path("/tmp/gpt-register-fp-sms-pool-20260919.tar.gz")
BASELINE = {
    "web_app.py": "479f90b00f2405e808d4658a3739103f80544373c0da6ef2860146374e18d65a",
    "codex_oauth/sms.py": "062bdd7e9f5ccc077e7eaba6721a1cbd849b992856a8805f1f6233ba372dce1a",
    "codex_oauth/import_jobs.py": "38116577c4b626c2d665811468546cc3f3d636fdc25387368e95ab9a921bb5ed",
    "web/routes/codex_oauth.py": "13d2a9c9775c7711502560a54cbb37b56189f701a55ec5769d8260bebb4b1264",
    "common/panel_ops.py": "5a4fee8c9cc2eab03f553cdd529381b87273e356d9e5af0110abc408371d7acd",
}
NEW_MODULES = ["common/sms_number_pool.py", "codex_oauth/imported_sms.py", "web/routes/sms_pool.py"]
BACKUP_PREFIX = "sms-pool"
POST_DEPLOY_CHECK = None
ADDITIONAL_IDLE_CHECK = None


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ALLOWED_HOSTS:
        raise SystemExit("Target is outside this release's five-server allowlist")
    target = sys.argv[1]
    password = base64.b64decode(os.environ.pop("SMS_POOL_PANEL_PASSWORD_B64")).decode()
    session = requests.Session()
    session.trust_env = False

    def api(path):
        response = session.get("http://127.0.0.1:8790" + path, timeout=20)
        response.raise_for_status()
        return response.json()

    def login():
        session.post("http://127.0.0.1:8790/api/auth/login", json={"username": "admin", "password": password}, timeout=15).raise_for_status()

    def idle():
        for path in ("/api/status", "/api/progress", "/api/codex-oauth/status", "/api/accounts/rebind/progress",
                     "/api/accounts/reauth/progress", "/api/accounts/refresh-at/progress",
                     "/api/accounts/trial-eligibility/progress", "/api/accounts/health/progress"):
            value = api(path)
            if path == "/api/status":
                value = value.get("job") or {}
            if value.get("running") or value.get("status") in ("running", "stopping"):
                raise RuntimeError("Task active; retry deployment after it finishes: " + path)
        if ADDITIONAL_IDLE_CHECK is not None:
            ADDITIONAL_IDLE_CHECK()

    login()
    idle()
    with tempfile.TemporaryDirectory(prefix="codex-sms-pool-") as stage_name:
        stage = Path(stage_name)
        with tarfile.open(PACKAGE) as archive:
            for member in archive:
                dest = (stage / member.name).resolve()
                if not dest.is_relative_to(stage) or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise RuntimeError("Unexpected package member")
                archive.extract(member, stage)
        sources = [*BASELINE, *NEW_MODULES]
        for name in sources:
            if not (stage / name).is_file():
                raise RuntimeError("Missing packaged module: " + name)
            py_compile.compile(str(stage / name), doraise=True)
        for name, expected in BASELINE.items():
            if digest(APP / name) not in (expected, digest(stage / name)):
                raise RuntimeError("Server has separate code changes: " + name)

        before_config = digest(APP / "config.yaml")
        before_sms = api("/api/sms-config")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = APP / "backups" / (BACKUP_PREFIX + "-" + stamp)
        backup.mkdir(parents=True, mode=0o700)
        backup_source = backup / "source"
        backup_source.mkdir()
        targets = [name for name in sources if (APP / name).exists()]
        for name in [*targets, "web/static/vue/index.html", "web/static/vue/new.html"]:
            if (APP / name).exists():
                dest = backup_source / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(APP / name, dest)
        (backup / "config.sha256").write_text(before_config)

        idle()
        stopped = False
        try:
            checked("systemctl", "stop", SERVICE)
            stopped = True
            checked("tar", "-czf", str(backup / "data-config.tar.gz"), "-C", str(APP), "data", "config.yaml")
            for name in sources:
                dest = APP / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stage / name, dest)
                dest.chmod(0o644)
            shutil.copytree(stage / "web/static/vue", APP / "web/static/vue", dirs_exist_ok=True)
            assert digest(APP / "config.yaml") == before_config
            checked("systemctl", "start", SERVICE)
            for _ in range(30):
                try:
                    if api("/api/health").get("ok"):
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Panel health check timed out")
            login()
            result = api("/api/sms-pool?page_size=10")
            assert result["max_uses"] == 3 and result["cooldown_seconds"] == 3600
            assert api("/api/sms-config") == before_sms, "Saved SMS config changed"
            assert digest(APP / "config.yaml") == before_config
            response = session.get("http://127.0.0.1:18787/api/health", timeout=10)
            response.raise_for_status()
            for name in sources:
                assert digest(APP / name) == digest(stage / name)
            assert digest(APP / "web/static/vue/index.html") == digest(stage / "web/static/vue/index.html")
            if POST_DEPLOY_CHECK is not None:
                POST_DEPLOY_CHECK()
            print(f"{target} deploy=ok panel=200 mailcode=200 pool={result['summary']} config=preserved")
            print("backup=" + str(backup))
        except BaseException:
            if stopped:
                checked("systemctl", "stop", SERVICE)
                shutil.copytree(backup_source, APP, dirs_exist_ok=True)
                checked("systemctl", "start", SERVICE)
                print("rollback=restored_previous_source", flush=True)
            raise


if __name__ == "__main__":
    main()
