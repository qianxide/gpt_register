"""Upgrade the complete UPI integration on server 204, preserving credentials."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import deploy_sms_pool as release


def preserved_settings_hash():
    db_path = release.APP / "data/external_api.sqlite3"
    if not db_path.exists():
        rows = []
    else:
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT name,payload FROM settings ORDER BY name").fetchall()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def verify_upi_runtime():
    assert preserved_settings_hash() == os.environ["UPI_RELEASE_SETTINGS_HASH"], "Saved integration credentials changed"
    result = subprocess.run([sys.executable, "-c", """
from common.external_api import get_external_service
from common.external_clients import UpiClient
from web.routes.external_api import router
service = get_external_service()
settings = service.settings()['upi']
assert settings['payment_platform'] in {'none','foarge','masi','seashore'}
assert settings['seashore_api_base'] == 'https://seashore.lol/api/publisher'
assert callable(UpiClient.create_batch) and callable(UpiClient.cdk_fuse)
assert len(router.routes) == 15
print('upi_complete: 4 exclusive payment platforms; 15 local routes; batch/fusion clients ready')
"""], cwd=release.APP, check=True, capture_output=True, text=True, timeout=20)
    print(result.stdout.strip())


def check_local_submissions_idle():
    db_path = release.APP / "data/external_api.sqlite3"
    if not db_path.exists():
        return
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        pending_upi = db.execute("SELECT COUNT(*) FROM upi_jobs WHERE job_id='' AND status IN ('queued','submitting','cancelling')").fetchone()[0]
        pending_scan = db.execute("SELECT COUNT(*) FROM scan_submissions WHERE status='submitting'").fetchone()[0]
    if pending_upi or pending_scan:
        raise RuntimeError("External submissions active; wait for local create requests to finish")


if __name__ == "__main__":
    if sys.argv[1:] != ["45.205.31.204"]:
        raise SystemExit("This release is approved only for server 204")
    release.ALLOWED_HOSTS = {"45.205.31.204"}
    release.PACKAGE = Path("/tmp/gpt-register-fp-upi-complete-20260920.tar.gz")
    release.BASELINE = {
        "common/external_api.py": "a81f9eec795003d89e235b162c8f1cdea5b6b25af08136b3e60a550774c16e1f",
        "common/external_clients.py": "4d78ca3a809c01fe7cc1b00c61004968ac4124e2717ba6939a6c63a4dc08f94d",
        "web/routes/external_api.py": "f763364e04e252e2545fd15eb0f276e15d1572b0c882e0413287cd80d6f1e38b",
        "web_app.py": "b0654ede3e18e3352ce6bcd29c51f7449e19ca5c5c23b65e54d56fd268f7cd3d",
        "common/panel_ops.py": "9ede3f9cb5e9f8a7e26f4ba3bdc3a3a96147b42b088253d5e62331e5e72978ed",
    }
    release.NEW_MODULES = []
    release.BACKUP_PREFIX = "upi-complete"
    release.POST_DEPLOY_CHECK = verify_upi_runtime
    release.ADDITIONAL_IDLE_CHECK = check_local_submissions_idle
    os.environ["UPI_RELEASE_SETTINGS_HASH"] = preserved_settings_hash()
    release.main()
