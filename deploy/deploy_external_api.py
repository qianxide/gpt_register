"""Publish the complete external API pipeline to server 204 only."""
import os
from pathlib import Path
import subprocess
import sys

import deploy_sms_pool as release


def verify_external_runtime():
    result = subprocess.run([sys.executable, "-c", """
from common.external_api import get_external_service
from common.external_pipeline import PipelineManager
from web.routes.external_api import router
service = get_external_service()
settings = service.settings()
assert settings['scan']['base_url'] == 'https://scan.fishmail.vip'
assert settings['upi']['base_url'] == 'https://upi.43-255-159-106.sslip.io'
assert isinstance(service._pipeline(), PipelineManager)
assert len(router.routes) == 20
print('external_api: both providers loaded, 20 routes, group pipeline ready')
"""], cwd=release.APP, check=True, capture_output=True, text=True, timeout=20)
    print(result.stdout.strip())


if __name__ == "__main__":
    if sys.argv[1:] != ["45.205.31.204"]:
        raise SystemExit("This release is approved only for server 204")
    release.ALLOWED_HOSTS = {"45.205.31.204"}
    release.PACKAGE = Path("/tmp/gpt-register-fp-external-pipeline-20260920.tar.gz")
    release.BASELINE = {
        "web_app.py": "b0654ede3e18e3352ce6bcd29c51f7449e19ca5c5c23b65e54d56fd268f7cd3d",
        "common/panel_ops.py": "9ede3f9cb5e9f8a7e26f4ba3bdc3a3a96147b42b088253d5e62331e5e72978ed",
        "common/external_clients.py": "4d78ca3a809c01fe7cc1b00c61004968ac4124e2717ba6939a6c63a4dc08f94d",
        "common/external_api.py": "a81f9eec795003d89e235b162c8f1cdea5b6b25af08136b3e60a550774c16e1f",
        "web/routes/external_api.py": "f763364e04e252e2545fd15eb0f276e15d1572b0c882e0413287cd80d6f1e38b",
    }
    release.NEW_MODULES = ["common/external_pipeline.py"]
    release.BACKUP_PREFIX = "external-pipeline"
    release.POST_DEPLOY_CHECK = verify_external_runtime
    release.main()
