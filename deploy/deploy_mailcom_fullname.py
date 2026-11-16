"""Full-name alias release, including SMS-pool catch-up on server 154."""
import json
from pathlib import Path
import subprocess
import sys

import deploy_sms_pool as release


def verify_alias_runtime():
    # Generate examples only. This check never creates aliases or consumes slots.
    result = subprocess.run([sys.executable, "-c", """
import json, re
import mail
from mail.mailcom import MailcomApiProvider, MAILCOM_ALIAS_GIVEN_NAMES, MAILCOM_ALIAS_SURNAMES
samples = []
for length in (8, 12, 13, 14, 17, 32):
    provider = MailcomApiProvider({'alias_name_mode':'name_digits','alias_name_length':length}, mail._make_config({}))
    try:
        address = provider._generated_alias_address('dr.com')
        local = address.split('@')[0]
        match = re.fullmatch(r'([a-z]+)([0-9]{5,})', local)
        assert match and len(local) == max(13, length)
        assert any(match[1] == first+last for first in MAILCOM_ALIAS_GIVEN_NAMES for last in MAILCOM_ALIAS_SURNAMES)
        if length in (14,17): samples.append(address)
    finally:
        provider.close()
print(json.dumps({'fullname_generation':'passed','samples':samples}))
"""], cwd=release.APP, check=True, capture_output=True, text=True, timeout=20)
    print(result.stdout.strip())


if __name__ == "__main__":
    baselines = json.loads(Path(__file__).with_name("mailcom_fullname_baselines.json").read_text())
    if len(sys.argv) != 2 or sys.argv[1] not in baselines:
        raise SystemExit("Specify a server from this release's six-server manifest")
    baseline = baselines[sys.argv[1]]
    release.ALLOWED_HOSTS = set(baselines)
    release.PACKAGE = Path("/tmp/gpt-register-fp-mailcom-fullname-20260919.tar.gz")
    release.BASELINE = {name:value for name,value in baseline.items() if value != "missing"}
    release.NEW_MODULES = [name for name,value in baseline.items() if value == "missing"]
    release.BACKUP_PREFIX = "mailcom-fullname"
    release.POST_DEPLOY_CHECK = verify_alias_runtime
    release.main()
