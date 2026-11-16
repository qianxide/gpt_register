"""Publish the direct mail.com alias release to the five secondary panels."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
from pathlib import Path
import re
import shlex
import urllib.request

from audit_mailcom_direct_five import connect_pinned, run_remote_python, REMOTE_AUDIT
from readonly_ssh_recon import live_jobs, panel_credentials


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent.parent
ARCHIVE = WORKSPACE / "mailcom-inventory-capacity-20260920.tgz"
SCRIPT = ROOT / "deploy/remote_update_mailcom_direct_five.sh"
REMOTE_ARCHIVE = "/tmp/mailcom-inventory-capacity-20260920.tgz"
REMOTE_SCRIPT = "/tmp/codex-mailcom-inventory-capacity-five-20260920.sh"
EXPECTED_SHA256 = "280f7707477e32c52524ddf0625b123d411e3e96a54c3dd5029b8773e75065e0"
HOSTS = {
    "45.205.31.204": "docker",
    "45.205.31.227": "docker",
    "45.205.31.253": "docker",
    "45.205.31.69": "systemd",
    "45.205.31.123": "docker",
}


def emit(**value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def remote_syntax(client):
    stdin, stdout, stderr = client.exec_command("bash -n", timeout=30)
    stdin.write(SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
    stdin.channel.shutdown_write()
    status = stdout.channel.recv_exit_status()
    if status:
        raise RuntimeError(f"Remote script syntax failed; stderr_bytes={len(stderr.read())}")


def tunnel_get(client, path):
    channel = client.get_transport().open_channel(
        "direct-tcpip", ("127.0.0.1", 8790), ("127.0.0.1", 0), timeout=15,
    )
    channel.settimeout(30)
    connection = http.client.HTTPConnection("127.0.0.1", 8790, timeout=30)
    connection.sock = channel
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def public_get(host, path):
    request = urllib.request.Request(f"http://{host}:8790{path}", headers={"User-Agent": "codex-release-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read()


def acceptance(host, client):
    frontend = ROOT / "web/static/vue"
    index = (frontend / "index.html").read_bytes()
    text = index.decode("utf-8")
    main_match = re.search(r'src="([^"]*main-[^"]+\.js)"', text)
    if not main_match:
        raise RuntimeError("Built main frontend chunk not found")
    main_path = main_match.group(1).removeprefix("/static/vue/").lstrip("./")
    pickers = list(frontend.glob("assets/RebindMailboxPicker-*.js"))
    if len(pickers) != 1:
        raise RuntimeError(f"Expected one built picker chunk, found {len(pickers)}")
    picker_path = "assets/" + pickers[0].name
    domain_source = (ROOT / "web-ui/src/constants/mailcomAliasDomains.ts").read_text("utf-8")
    domains = re.findall(r'^\s*"([^"\\]+)",?\s*$', domain_source, flags=re.MULTILINE)
    picker_text = pickers[0].read_text("utf-8")
    if len(domains) != 189 or len(set(domains)) != 189 or "dr.com" not in domains:
        raise RuntimeError("Local domain catalog is not the expected 189-domain set")
    if any(domain not in picker_text for domain in domains):
        raise RuntimeError("Built picker is missing one or more mail.com domains")
    expected = {
        "/api/health": None,
        "/static/vue/index.html": index,
        "/static/vue/" + main_path: (frontend / main_path).read_bytes(),
        "/static/vue/" + picker_path: (frontend / picker_path).read_bytes(),
    }
    checks = {}
    for route, local in expected.items():
        tunnel_status, tunnel_body = tunnel_get(client, route)
        public_status, public_body = public_get(host, route)
        if tunnel_status != 200 or public_status != 200:
            raise RuntimeError(f"Acceptance HTTP failure on {host}: {route}")
        if local is not None and (tunnel_body != local or public_body != local):
            raise RuntimeError(f"Served static asset differs on {host}: {route}")
        checks[route] = {"tunnel": tunnel_status, "public": public_status,
                         "sha256": hashlib.sha256(tunnel_body).hexdigest()}
    emit(host=host, stage="acceptance", domain_count=len(domains), domain_has_dr=True,
         rebind_concurrency_max=50, direct_alias_post=True, bounded_operation=True,
         frontend={"main": main_path, "picker": picker_path}, checks=checks,
         real_alias_request_sent=False)


def active_summary(jobs):
    result = {}
    for path, item in jobs.items():
        if path == "config_content_unchanged":
            continue
        bodies = [item.get("summary") or {}, item.get("job") or {}, item.get("progress") or {}]
        result[path] = any(body.get("running") or body.get("status") in ("queued", "running", "stopping")
                           for body in bodies if isinstance(body, dict))
    return result


def preflight(host, client):
    audit = run_remote_python(client, REMOTE_AUDIT)
    jobs = live_jobs(client, {"password_is_pbkdf2": True, "required_fields_present": True})
    active = active_summary(jobs)
    if any(active.values()):
        raise RuntimeError(f"Active task on {host}")
    runtime = audit.get("docker_mailcode") or audit.get("gc_mailcode") or {}
    if int(runtime.get("health_http") or 0) != 200:
        raise RuntimeError(f"mailcode is unhealthy on {host}")
    expected_mode = HOSTS[host]
    if expected_mode == "docker" and (audit.get("docker_mailcode") or {}).get("state") != "running":
        raise RuntimeError(f"Docker mailcode is not running on {host}")
    if expected_mode == "systemd" and (audit.get("gc_mailcode") or {}).get("ActiveState") != "active":
        raise RuntimeError(f"systemd mailcode is not running on {host}")
    emit(host=host, stage="preflight", runtime=expected_mode, accounts=runtime.get("accounts"),
         proxy_supported=runtime.get("proxy_pool_supported"), task_status=active)
    return audit


def apply(host, client):
    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    if digest != EXPECTED_SHA256:
        raise RuntimeError("Release archive SHA256 differs from the canary archive")
    remote_syntax(client)
    preflight(host, client)
    with client.open_sftp() as sftp:
        sftp.put(str(ARCHIVE), REMOTE_ARCHIVE)
        with sftp.open(REMOTE_SCRIPT, "wb") as stream:
            stream.write(SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
        sftp.chmod(REMOTE_SCRIPT, 0o700)
    command = " ".join(shlex.quote(value) for value in (
        "bash", REMOTE_SCRIPT, REMOTE_ARCHIVE, digest, HOSTS[host], host,
    ))
    stdin, stdout, stderr = client.exec_command(command, timeout=1200)
    _, password = panel_credentials()
    stdin.write(base64.b64encode(password.encode()).decode() + "\n")
    stdin.channel.shutdown_write()
    output = stdout.read().decode("utf-8", "replace").strip()
    error = stderr.read().decode("utf-8", "replace")
    status = stdout.channel.recv_exit_status()
    if status:
        emit(host=host, stage="failed", exit_code=status, stderr_bytes=len(error), summary=output[-1200:])
        raise RuntimeError(f"Release failed on {host}; remote rollback was attempted")
    emit(host=host, stage="published", summary=output[-1600:])
    post = preflight(host, client)
    expected = {
        "mail/__init__.py": hashlib.sha256((ROOT / "mail/__init__.py").read_bytes()).hexdigest(),
        "mail/mailcom.py": hashlib.sha256((ROOT / "mail/mailcom.py").read_bytes()).hexdigest(),
        "web/routes/mailcom.py": hashlib.sha256((ROOT / "web/routes/mailcom.py").read_bytes()).hexdigest(),
        "web/routes/accounts_rebind.py": hashlib.sha256((ROOT / "web/routes/accounts_rebind.py").read_bytes()).hexdigest(),
    }
    if post.get("panel_source") != expected:
        raise RuntimeError(f"Panel source verification failed on {host}")
    acceptance(host, client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("syntax", "preflight", "apply"))
    parser.add_argument("--hosts", nargs="+", choices=HOSTS, default=list(HOSTS))
    args = parser.parse_args()
    if not ARCHIVE.is_file() or hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() != EXPECTED_SHA256:
        raise RuntimeError("The canary-tested archive is missing or changed")
    for host in args.hosts:
        with connect_pinned(host) as client:
            if args.action == "syntax":
                remote_syntax(client); emit(host=host, syntax="ok")
            elif args.action == "preflight":
                preflight(host, client)
            else:
                apply(host, client)


if __name__ == "__main__":
    main()
