"""Inspect, package and publish the mail.com local-inventory update on 154 only.

No remote changes occur with ``inspect`` or ``package``. ``apply`` requires the
locally tested archive and runs the remote script with automatic rollback.
Credentials are read into memory from the private deployment document.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import shlex
import tarfile
import time

import paramiko
import socks

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent.parent
HOST = "154.202.118.39"
RELEASE = "mailcom-inventory-capacity-20260920"
ARCHIVE = WORKSPACE / f"{RELEASE}.tgz"
REMOTE_ARCHIVE = f"/tmp/{RELEASE}.tgz"
REMOTE_SCRIPT = f"/tmp/codex-{RELEASE}.sh"
SCRIPT = ROOT / "deploy/remote_update_mailcom_direct.sh"
FILES = (
    "common/email_rebind_jobs.py",
    "mail/__init__.py",
    "mail/mailcom.py",
    "web/routes/mailcom.py",
    "web/routes/accounts_rebind.py",
    "mailcode-service/Dockerfile",
    "mailcode-service/package.json",
    "mailcode-service/package-lock.json",
    "mailcode-service/server.mjs",
    "mailcode-service/mailbox.mjs",
    "mailcode-service/store.mjs",
    "mailcode-service/web-alias-client.mjs",
)
ACTIVE_PATHS = (
    "/api/status",
    "/api/progress",
    "/api/accounts/rebind/progress",
    "/api/accounts/reauth/progress",
    "/api/accounts/refresh-at/progress",
    "/api/accounts/trial-eligibility/progress",
    "/api/accounts/health/progress",
    "/api/codex-oauth/status",
)


def emit(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


def credentials():
    document = (WORKSPACE / "多服务器部署说明文档.md").read_text(encoding="utf-8-sig")
    ssh_user = ssh_password = panel_user = panel_password = None
    for line in document.splitlines():
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 5 and cells[1] == HOST:
            ssh_user, ssh_password = cells[2], cells[3]
        if line.startswith("- 面板管理员："):
            panel_user = line.split("：", 1)[1].strip().strip("`")
        if line.startswith("- 面板密码："):
            panel_password = line.split("：", 1)[1].strip().strip("`")
    if not all((ssh_user, ssh_password, panel_user, panel_password)):
        raise RuntimeError("Private deployment credentials are incomplete")
    return ssh_user, ssh_password, panel_user, panel_password


def connection():
    user, password, _, _ = credentials()
    known_hosts = Path.home() / ".ssh/known_hosts"
    if not known_hosts.is_file():
        raise RuntimeError("Trusted SSH host key is missing")
    for route in ("direct", "socks"):
        client = paramiko.SSHClient()
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        sock = None
        try:
            if route == "socks":
                sock = socks.socksocket()
                sock.set_proxy(socks.SOCKS5, "127.0.0.1", 7897)
                sock.settimeout(12)
                sock.connect((HOST, 22))
            client.connect(HOST, username=user, password=password, sock=sock,
                           look_for_keys=False, allow_agent=False,
                           timeout=12, banner_timeout=12, auth_timeout=12)
            return client
        except (OSError, EOFError, paramiko.SSHException):
            client.close()
            if route == "socks":
                raise RuntimeError("SSH connection failed on both routes") from None
    raise RuntimeError("SSH connection failed")


def command(client, argv, timeout=30):
    _, stdout, stderr = client.exec_command(" ".join(shlex.quote(item) for item in argv), timeout=timeout)
    output = stdout.read().decode("utf-8", "replace").strip()
    error = stderr.read().decode("utf-8", "replace").strip()
    status = stdout.channel.recv_exit_status()
    if status:
        # Avoid echoing remote diagnostics that might contain sensitive values.
        raise RuntimeError(f"Remote command failed: {argv[0]} exit={status}; stderr_bytes={len(error)}")
    return output


def request(client, method, path, body=None, cookie=""):
    channel = client.get_transport().open_channel(
        "direct-tcpip", ("127.0.0.1", 8790), ("127.0.0.1", 0), timeout=15,
    )
    channel.settimeout(30)
    conn = http.client.HTTPConnection("127.0.0.1", 8790, timeout=30)
    conn.sock = channel
    try:
        conn.request(method, path, json.dumps(body) if body is not None else None,
                     {"Content-Type": "application/json", "Cookie": cookie})
        response = conn.getresponse()
        return response.status, response.getheader("Set-Cookie", ""), response.read()
    finally:
        conn.close()


def login(client):
    _, _, user, password = credentials()
    status, header, _ = request(client, "POST", "/api/auth/login", {"username": user, "password": password})
    if status != 200:
        raise RuntimeError(f"Panel login returned HTTP {status}")
    cookies = SimpleCookie()
    cookies.load(header)
    return "; ".join(f"{key}={item.value}" for key, item in cookies.items())


def check_idle(client, cookie):
    statuses = {}
    for path in ACTIVE_PATHS:
        status, _, raw = request(client, "GET", path, cookie=cookie)
        if status != 200:
            raise RuntimeError(f"Task status {path} returned HTTP {status}")
        body = json.loads(raw)
        items = [body, body.get("job") or {}, *(body.get("items") or [])]
        active = any(isinstance(item, dict) and (
            item.get("running") or item.get("status") in ("queued", "running", "stopping")
        ) for item in items)
        statuses[path] = "active" if active else "idle"
    return statuses


def inspect(client):
    cookie = login(client)
    states = check_idle(client, cookie)
    panel = command(client, ["systemctl", "is-active", "gpt-register-fp-test.service"])
    mailcode = command(client, ["systemctl", "is-active", "mailcode-service.service"])
    panel_health = command(client, ["curl", "-sS", "-o", "/dev/null", "--max-time", "10", "-w", "%{http_code}", "http://127.0.0.1:8790/api/health"])
    mailcode_health = command(client, ["curl", "-sS", "-o", "/dev/null", "--max-time", "10", "-w", "%{http_code}", "http://127.0.0.1:18787/api/health"])
    image_id = command(client, ["docker", "image", "inspect", "--format", "{{.Id}}", "gpt-register-mailcode:local"])
    hashes = command(client, ["sha256sum", *("/opt/gpt-register-fp-test/" + name for name in FILES if name != "mailcode-service/web-alias-client.mjs")])
    remote_hashes = {line.split()[1].removeprefix("/opt/gpt-register-fp-test/"): line.split()[0]
                     for line in hashes.splitlines()}
    settings_status, _, settings_raw = request(client, "GET", "/api/mailcom/settings?include_usage=0", cookie=cookie)
    if settings_status != 200:
        raise RuntimeError(f"mail.com settings returned HTTP {settings_status}")
    settings = json.loads(settings_raw)
    probe = r'''set -a
source /etc/gc-mailcode/mailcode.env
set +a
python3 - <<'PY'
import collections, hashlib, json, os, urllib.parse, urllib.request
base = "http://127.0.0.1:18787"
header = {"x-api-key": os.environ["MAILCODE_ADMIN_KEY"]}
def read(path):
    request = urllib.request.Request(base + path, headers=header)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)
accounts = read("/api/v1/accounts").get("accounts") or []
pool = read("/api/v1/proxy-pool").get("proxy_pool") or {}
aliases = []
for account in accounts:
    email = str(account.get("email") or "")
    query = urllib.parse.urlencode({"account_email": email})
    aliases.extend(read("/api/v1/aliases?" + query).get("items") or [])
statuses = collections.Counter(str(item.get("status") or "unknown") for item in aliases)
account_alias_counts = "\n".join(sorted(
    "\t".join((str(account.get("email") or "").lower(), str(int(account.get("aliasCount") or 0))))
    for account in accounts
))
mapping = "\n".join(sorted(
    "\t".join((str(item.get("address") or "").lower(),
                 str(item.get("primaryEmail") or "").lower(),
                 str(item.get("status") or "unknown")))
    for item in aliases
))
print(json.dumps({
    "accounts": len(accounts),
    "proxy_enabled": bool(pool.get("enabled")),
    "proxy_count": int(pool.get("count") or 0),
    "proxy_assigned": int(pool.get("assigned") or 0),
    "alias_total": len(aliases),
    "alias_statuses": dict(sorted(statuses.items())),
    "account_alias_counts_sha256": hashlib.sha256(account_alias_counts.encode()).hexdigest(),
    "alias_mapping_sha256": hashlib.sha256(mapping.encode()).hexdigest(),
}, sort_keys=True))
PY'''
    mailcode_counts = json.loads(command(client, ["bash", "-lc", probe], timeout=60))
    panel_counts = {"accounts": len(settings.get("accounts") or []),
                    "proxy_enabled": settings.get("proxy_pool_enabled"),
                    "proxy_count": settings.get("proxy_pool_count"),
                    "proxy_assigned": settings.get("proxy_pool_assigned")}
    baseline = WORKSPACE / "mailcom-proxy-failover-20260920.tgz"
    baseline_matches = {}
    if baseline.is_file():
        with tarfile.open(baseline) as archive:
            for name in remote_hashes:
                try:
                    stream = archive.extractfile(name)
                    if stream:
                        baseline_matches[name] = hashlib.sha256(stream.read()).hexdigest() == remote_hashes[name]
                except KeyError:
                    pass
    local_matches = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
                     for name, digest in remote_hashes.items()}
    emit(host=HOST, panel=panel, mailcode=mailcode,
         panel_health=panel_health, mailcode_health=mailcode_health,
         image_id=image_id, task_statuses=states, panel_counts=panel_counts,
         mailcode_counts=mailcode_counts, source_hashes=remote_hashes,
         baseline_matches=baseline_matches, local_matches=local_matches)
    return cookie, states


def package():
    for name in FILES:
        if not (ROOT / name).is_file():
            raise RuntimeError(f"Missing release input: {name}")
    if not (ROOT / "web/static/vue/index.html").is_file():
        raise RuntimeError("Frontend has not been built")
    domain_source = ROOT / "web-ui/src/constants/mailcomAliasDomains.ts"
    if not domain_source.is_file():
        raise RuntimeError("Static mail.com domain catalog is missing")
    domain_text = domain_source.read_text(encoding="utf-8")
    domains = re.findall(r'^\s*"([^"\\]+)",?\s*$', domain_text, flags=re.MULTILINE)
    if len(domains) != 189 or len(set(domains)) != 189 or "dr.com" not in domains:
        raise RuntimeError("Static mail.com domain catalog is not the expected 189-domain set")
    pickers = list((ROOT / "web/static/vue/assets").glob("RebindMailboxPicker-*.js"))
    if len(pickers) != 1:
        raise RuntimeError(f"Expected one built rebind picker chunk, found {len(pickers)}")
    picker_text = pickers[0].read_text(encoding="utf-8")
    missing_domains = [domain for domain in domains if domain not in picker_text]
    if missing_domains:
        raise RuntimeError(f"Built frontend is missing {len(missing_domains)} mail.com domains")
    for phrase in ("dr.com", "1 到 50", "邮箱查询并发"):
        if phrase not in picker_text:
            raise RuntimeError(f"Built frontend is missing release marker: {phrase}")
    if "le=50" not in (ROOT / "web/routes/accounts_rebind.py").read_text(encoding="utf-8"):
        raise RuntimeError("Backend rebind concurrency limit is not 50")
    frontend_concurrency_sources = (
        ROOT / "web-ui/src/new/stores/rebind.ts",
        ROOT / "web-ui/src/new/views/accounts/RebindView.vue",
        ROOT / "web-ui/src/components/RebindView.vue",
    )
    for source in frontend_concurrency_sources:
        source_text = source.read_text(encoding="utf-8")
        if "50" not in source_text or ("Math.min(50" not in source_text and 'max="50"' not in source_text):
            raise RuntimeError(f"Frontend rebind concurrency 50 marker missing: {source.name}")
    rebind_chunks = list((ROOT / "web/static/vue/assets").glob("RebindView-*.js"))
    if len(rebind_chunks) != 1 or "Math.min(50" not in rebind_chunks[0].read_text(encoding="utf-8"):
        raise RuntimeError("Built rebind chunk does not submit concurrency up to 50")
    mailcom_text = (ROOT / "mail/mailcom.py").read_text(encoding="utf-8")
    for marker in ('status in {"pending", "unconfirmed"}', 'action="reconcile alias"',
                   '"skip_domain_check": True', "_ACCOUNT_EXHAUSTED",
                   "_ACCOUNT_ALLOCATION_CONDITION", "alias_usage_summaries",
                   "全部主邮箱别名已满"):
        if marker not in mailcom_text:
            raise RuntimeError(f"Unconfirmed alias recovery marker missing: {marker}")
    fast_stop_sources = {
        "common/email_rebind_jobs.py": ("cancel_cleanup", "stop_event.is_set()"),
        "web/routes/accounts_rebind.py": ("_rebind_cancel_cleanup", 'create_kwargs["stop_check"]'),
        "mail/__init__.py": ("stop_check=None", 'raise RuntimeError("cancelled")'),
        "mail/mailcom.py": ("_stop_requested", "_allocation_slot(stop_check)"),
    }
    for name, markers in fast_stop_sources.items():
        source_text = (ROOT / name).read_text(encoding="utf-8")
        for marker in markers:
            if marker not in source_text:
                raise RuntimeError(f"Fast-stop marker missing from {name}: {marker}")
    server_text = (ROOT / "mailcode-service/server.mjs").read_text(encoding="utf-8")
    web_alias_text = (ROOT / "mailcode-service/web-alias-client.mjs").read_text(encoding="utf-8")
    for marker in ('request.once("aborted"', 'response.once("close"', "MAILCOM_ALIAS_OPERATION_TIMEOUT_MS"):
        if marker not in server_text:
            raise RuntimeError(f"Node disconnect/deadline marker missing: {marker}")
    for marker in ("aliasInventory()", 'path === "/api/v1/alias-inventory"'):
        if marker not in server_text:
            raise RuntimeError(f"Local inventory marker missing: {marker}")
    route_text = (ROOT / "web/routes/mailcom.py").read_text(encoding="utf-8")
    for marker in ("fetch_alias_inventory", "alias_usage_summaries", '"usage_source": "local_inventory"'):
        if marker not in route_text:
            raise RuntimeError(f"Fast settings inventory marker missing: {marker}")
    if "quarantined_alias_count" not in picker_text or "隔离" not in picker_text:
        raise RuntimeError("Built frontend is missing the quarantined alias counter")
    for marker in ("createAliasInConfiguredDomain", 'method: "POST"'):
        if marker not in web_alias_text:
            raise RuntimeError(f"Direct alias POST marker missing: {marker}")
    with tarfile.open(ARCHIVE, "w:gz") as archive:
        for name in FILES:
            archive.add(ROOT / name, arcname=name)
        archive.add(ROOT / "web/static/vue", arcname="web/static/vue")
    sha = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    emit(archive=str(ARCHIVE), sha256=sha, bytes=ARCHIVE.stat().st_size,
         domain_count=len(domains), domain_has_dr=True, rebind_concurrency_max=50,
         frontend_files=sum(1 for item in (ROOT / "web/static/vue").rglob("*") if item.is_file()),
         direct_alias_post=True, disconnect_deadline=True, unconfirmed_recovery=True,
         fast_stop=True, local_inventory=True, capacity_skip=True,
         account_claim_wait=True, quarantined_counter=True)
    return sha


def audit(client):
    with client.open_sftp() as sftp:
        with sftp.open("/opt/gpt-register-fp-test/mail/__init__.py", "rb") as fp:
            previous = fp.read().decode("utf-8")
    current = (ROOT / "mail/__init__.py").read_text(encoding="utf-8")
    diff = list(difflib.unified_diff(previous.splitlines(), current.splitlines(),
                                     fromfile="remote/mail/__init__.py",
                                     tofile="local/mail/__init__.py", lineterm=""))
    if len(diff) > 120:
        raise RuntimeError(f"Unexpectedly large mail/__init__.py diff: {len(diff)} lines")
    baseline = WORKSPACE / "rebind-preflight-fix-20260920.tgz"
    with tarfile.open(baseline) as archive:
        prior_rebind = archive.extractfile("web/routes/accounts_rebind.py").read()
    with client.open_sftp() as sftp:
        with sftp.open("/opt/gpt-register-fp-test/web/routes/accounts_rebind.py", "rb") as fp:
            remote_rebind = fp.read()
    emit(rebind_matches_previous_release=remote_rebind == prior_rebind,
         mail_init_changed_lines=len(diff))
    print("\n".join(diff), flush=True)


def check_remote_syntax(client):
    stdin, stdout, stderr = client.exec_command("bash -n", timeout=30)
    stdin.write(SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
    stdin.flush()
    stdin.channel.shutdown_write()
    status = stdout.channel.recv_exit_status()
    if status:
        raise RuntimeError(f"Remote bash syntax check failed; stderr_bytes={len(stderr.read())}")
    emit(remote_bash_syntax="ok")


def accept(client):
    cookie = login(client)
    states = check_idle(client, cookie)
    settings_started = time.monotonic()
    status, _, settings_raw = request(client, "GET", "/api/mailcom/settings?include_usage=0", cookie=cookie)
    settings_ms = round((time.monotonic() - settings_started) * 1000)
    if status != 200:
        raise RuntimeError(f"mail.com settings returned HTTP {status}")
    settings = json.loads(settings_raw)
    rows = settings.get("accounts") or []
    count_fields = (
        "alias_count", "bound_alias_count", "unbound_alias_count",
        "available_alias_count", "quarantined_alias_count",
    )
    if settings.get("usage_source") != "local_inventory" or not rows:
        raise RuntimeError("Fast settings did not use the local alias inventory")
    for row in rows:
        if any(not isinstance(row.get(field), int) or row.get(field) < 0 for field in count_fields):
            raise RuntimeError("Fast settings returned an incomplete alias counter")
        if row["bound_alias_count"] + row["unbound_alias_count"] != row["alias_count"]:
            raise RuntimeError("Fast settings alias counters do not add up")
        if row["available_alias_count"] + row["quarantined_alias_count"] != row["unbound_alias_count"]:
            raise RuntimeError("Fast settings unbound counters do not add up")
    counter_totals = {field: sum(row[field] for row in rows) for field in count_fields}
    inventory_script = r'''set -a
source /etc/gc-mailcode/mailcode.env
set +a
python3 - <<'PY'
import collections, hashlib, json, os, urllib.request
request = urllib.request.Request(
    "http://127.0.0.1:18787/api/v1/alias-inventory",
    headers={"x-api-key": os.environ["MAILCODE_ADMIN_KEY"]},
)
with urllib.request.urlopen(request, timeout=30) as response:
    body = json.load(response)
items = body.get("items") or []
allowed = {"address", "primaryEmail", "status", "hasKey"}
if any(set(item) != allowed for item in items):
    raise SystemExit("inventory_fields")
statuses = collections.Counter(str(item.get("status") or "unknown") for item in items)
mapping = "\n".join(sorted("\t".join((
    str(item.get("address") or "").lower(),
    str(item.get("primaryEmail") or "").lower(),
    str(item.get("status") or "unknown"),
    "1" if item.get("hasKey") is True else "0",
)) for item in items))
print(json.dumps({
    "total": len(items),
    "statuses": dict(sorted(statuses.items())),
    "mapping_sha256": hashlib.sha256(mapping.encode()).hexdigest(),
}, sort_keys=True))
PY'''
    inventory = json.loads(command(client, ["bash", "-lc", inventory_script], timeout=60))
    local_frontend = ROOT / "web/static/vue"
    main_match = re.search(r'src="([^"]*main-[^"]+\.js)"', (local_frontend / "index.html").read_text("utf-8"))
    if not main_match:
        raise RuntimeError("Built main frontend chunk not found")
    main_path = main_match.group(1).removeprefix("/static/vue/").lstrip("./")
    new_match = re.search(r'src="([^"]*new-[^"]+\.js)"', (local_frontend / "new.html").read_text("utf-8"))
    if not new_match:
        raise RuntimeError("Built new-console frontend chunk not found")
    new_path = new_match.group(1).removeprefix("/static/vue/").lstrip("./")
    picker = next(local_frontend.glob("assets/RebindMailboxPicker-*.js"))
    rebind = next(local_frontend.glob("assets/RebindView-*.js"))
    frontend = {}
    for relative in ("index.html", "new.html", main_path, new_path,
                     "assets/" + picker.name, "assets/" + rebind.name):
        status, _, body = request(client, "GET", "/static/vue/" + relative, cookie=cookie)
        if status != 200:
            raise RuntimeError(f"Static asset {relative} returned HTTP {status}")
        local = (local_frontend / relative).read_bytes()
        if body != local:
            raise RuntimeError(f"Served static asset differs: {relative}")
        frontend[relative] = hashlib.sha256(body).hexdigest()
    picker_text = picker.read_text("utf-8")
    rebind_text = rebind.read_text("utf-8")
    if "Math.min(50" not in rebind_text:
        raise RuntimeError("Served rebind chunk does not submit concurrency up to 50")
    domain_source = (ROOT / "web-ui/src/constants/mailcomAliasDomains.ts").read_text("utf-8")
    domains = re.findall(r'^\s*"([^"\\]+)",?\s*$', domain_source, flags=re.MULTILINE)
    if len(domains) != 189 or any(domain not in picker_text for domain in domains):
        raise RuntimeError("Served rebind picker does not contain the validated 189-domain catalog")
    for phrase in ("保存邮箱并直接分配", "换绑创建别名时才调用 mail.com", "dr.com", "1 到 50", "邮箱查询并发"):
        if phrase not in picker_text:
            raise RuntimeError(f"Expected UI text missing: {phrase}")
    with client.open_sftp() as sftp:
        source_matches = {}
        for name in FILES:
            with sftp.open("/opt/gpt-register-fp-test/" + name, "rb") as fp:
                source_matches[name] = hashlib.sha256(fp.read()).hexdigest() == hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    if not all(source_matches.values()):
        raise RuntimeError("One or more deployed source files differ")
    deployed_mailcom = (ROOT / "mail/mailcom.py").read_text(encoding="utf-8")
    for marker in ('status in {"pending", "unconfirmed"}', 'action="reconcile alias"',
                   '"skip_domain_check": True', "_ACCOUNT_EXHAUSTED",
                   "_ACCOUNT_ALLOCATION_CONDITION", "alias_usage_summaries",
                   "全部主邮箱别名已满"):
        if marker not in deployed_mailcom:
            raise RuntimeError(f"Unconfirmed alias recovery marker missing after deploy: {marker}")
    deployed_fast_stop = {
        "common/email_rebind_jobs.py": ("cancel_cleanup", "stop_event.is_set()"),
        "web/routes/accounts_rebind.py": ("_rebind_cancel_cleanup", 'create_kwargs["stop_check"]'),
        "mail/__init__.py": ("stop_check=None", 'raise RuntimeError("cancelled")'),
        "mail/mailcom.py": ("_stop_requested", "_allocation_slot(stop_check)"),
    }
    for name, markers in deployed_fast_stop.items():
        source_text = (ROOT / name).read_text(encoding="utf-8")
        for marker in markers:
            if marker not in source_text:
                raise RuntimeError(f"Fast-stop marker missing after deploy from {name}: {marker}")
    command(client, ["docker", "run", "--rm", "--entrypoint", "sh", "gpt-register-mailcode:local", "-c",
                     "test -s /app/web-alias-client.mjs && "
                     "grep -qF skip_domain_check /app/server.mjs && "
                     "grep -qF 'request.once(\"aborted\"' /app/server.mjs && "
                     "grep -qF 'response.once(\"close\"' /app/server.mjs && "
                     "grep -qF MAILCOM_ALIAS_OPERATION_TIMEOUT_MS /app/server.mjs && "
                     "grep -qF 'boundedOperation(' /app/server.mjs && "
                     "grep -qF 'aliasInventory()' /app/server.mjs && "
                     "grep -qF '/api/v1/alias-inventory' /app/server.mjs && "
                     "grep -qF createAliasInConfiguredDomain /app/web-alias-client.mjs && "
                     "grep -qF 'method: \"POST\"' /app/web-alias-client.mjs"], timeout=60)
    image_id = command(client, ["docker", "image", "inspect", "--format", "{{.Id}}", "gpt-register-mailcode:local"])
    emit(acceptance="ok", accounts=len(rows), settings_ms=settings_ms,
         proxy_enabled=settings.get("proxy_pool_enabled"), proxy_count=settings.get("proxy_pool_count"),
         proxy_assigned=settings.get("proxy_pool_assigned"), task_statuses=states,
         domain_count=len(domains), domain_has_dr="dr.com" in domains, rebind_concurrency_max=50,
         unconfirmed_recovery=True, fast_stop=True, usage_source=settings.get("usage_source"),
         alias_counter_totals=counter_totals, inventory=inventory,
         inventory_sdk_login=False, capacity_skip=True, account_claim_wait=True,
         image_id=image_id, frontend=frontend, source_matches=source_matches,
         real_alias_request_sent=False)


def apply(client, sha):
    cookie = login(client)
    states = check_idle(client, cookie)
    if "active" in states.values():
        raise RuntimeError("Deployment deferred: active panel task")
    with client.open_sftp() as sftp:
        sftp.put(str(ARCHIVE), REMOTE_ARCHIVE)
        with sftp.open(REMOTE_SCRIPT, "wb") as target:
            target.write(SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
        sftp.chmod(REMOTE_SCRIPT, 0o700)
    emit(stage="uploaded", sha256=sha)
    stdin, stdout, stderr = client.exec_command(
        " ".join(shlex.quote(arg) for arg in ("bash", REMOTE_SCRIPT, REMOTE_ARCHIVE, sha)),
        timeout=600,
    )
    ssh_user, ssh_password, panel_user, panel_password = credentials()
    del ssh_user, ssh_password, panel_user
    stdin.write(base64.b64encode(panel_password.encode()).decode() + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", "replace").strip()
    error = stderr.read().decode("utf-8", "replace").strip()
    status = stdout.channel.recv_exit_status()
    # Remote script only emits an allowlisted health/backup summary. Never print stderr.
    if status:
        emit(stage="failed", exit_code=status, stderr_bytes=len(error), backup_summary=output[-1000:])
        raise RuntimeError("Remote release failed; rollback attempted")
    emit(stage="published", summary=output[-1000:])
    inspect(client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "audit", "syntax", "package", "apply", "accept"))
    args = parser.parse_args()
    if args.action == "package":
        package()
        return
    with connection() as client:
        if args.action == "inspect":
            inspect(client)
        elif args.action == "audit":
            audit(client)
        elif args.action == "syntax":
            check_remote_syntax(client)
        elif args.action == "accept":
            accept(client)
        else:
            if not ARCHIVE.is_file():
                raise RuntimeError("Build and verify the package before apply")
            apply(client, hashlib.sha256(ARCHIVE.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
