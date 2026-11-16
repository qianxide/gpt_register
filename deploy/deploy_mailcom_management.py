"""Publish the mail.com management update to the five explicitly selected hosts.

Uses the existing SSH credential document; never prints or uploads credentials.
The excluded 154 server is intentionally absent from the allowlist.
"""
from __future__ import annotations

import argparse
import hashlib
from http.cookies import SimpleCookie
import http.client
import json
from pathlib import Path
import shlex
import tarfile

import paramiko
from readonly_ssh_recon import ALLOWED, ROOT, WORKSPACE, connect, credentials, panel_credentials

RELEASE = "gpt-register-mailcom-management-20260919-v2.tar.gz"
SOURCES = ("mail/mailcom.py", "web/routes/mailcom.py", "web-ui/src/components/MailcomRebindSettings.vue")
SCRIPT = ROOT / "deploy/remote_update_mailcom_management.sh"
KEYS = {
    "45.205.31.123": "6d09fc8311f0fbd602182a62cb276c3427b0507cb5206a6013c932e0c5f2c8c3",
    "45.205.31.227": "d253d3c8f638ab36aaab74c5743ae18a501210e319d480405b582d630208d1c7",
    "45.205.31.204": "89c48fee7ba72a60e3e0806d7c410a83df29536e81feaf36c2b865ac71c22bd3",
    "45.205.31.253": "8834a8031e572adbdb5c06bc8f13e4cf747442442d037d5cf1c3e3449b05021a",
    "45.205.31.69": "378c9077267b49f0daec2c6d79885ed45bdab371f612d444d574d60f10cac3d3",
}


def emit(host, **fields):
    print(json.dumps({"host": host, **fields}, ensure_ascii=True), flush=True)


def package():
    path = WORKSPACE / RELEASE
    with tarfile.open(path, "w:gz") as archive:
        for name in SOURCES:
            archive.add(ROOT / name, arcname=name)
        archive.add(ROOT / "web/static/vue", arcname="web/static/vue")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


class PinnedKeyPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        if hashlib.sha256(key.asbytes()).hexdigest() != KEYS[hostname]:
            raise RuntimeError("Host key differs from verified fingerprint")


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
        data = response.read()
        return response.status, response.getheader("Set-Cookie", ""), data
    finally:
        conn.close()


def login(client):
    username, password = panel_credentials()
    status, header, _ = request(client, "POST", "/api/auth/login", {"username": username, "password": password})
    if status != 200:
        raise RuntimeError(f"Panel login returned {status}")
    cookies = SimpleCookie()
    cookies.load(header)
    return "; ".join(f"{key}={m.value}" for key, m in cookies.items())


def assert_idle(client, cookie):
    for path in (
        "/api/status", "/api/progress", "/api/accounts/reauth/progress",
        "/api/accounts/rebind/progress", "/api/accounts/refresh-at/progress",
        "/api/accounts/trial-eligibility/progress", "/api/codex-oauth/status",
    ):
        status, _, raw = request(client, "GET", path, cookie=cookie)
        if status != 200:
            raise RuntimeError(f"Job preflight returned {status} for {path}")
        body = json.loads(raw)
        items = [body, body.get("job") or {}, *(body.get("items") or [])]
        if any(isinstance(item, dict) and (
            item.get("running") or item.get("status") in ("queued", "running", "stopping")
        ) for item in items):
            raise RuntimeError(f"Active work found at {path}; update deferred")


def verify(client, cookie, sftp):
    for name in (*SOURCES, "web/static/vue/index.html"):
        with sftp.open("/opt/gpt-register-fp-test/" + name, "rb") as fp:
            remote_hash = hashlib.sha256(fp.read()).hexdigest()
        assert remote_hash == hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), name
    # Invalid input verifies deployed handlers without selecting any real mailbox.
    for path in ("/api/mailcom/aliases/batch", "/api/mailcom/accounts/batch"):
        code, _, _ = request(client, "DELETE", path, {"emails": [], "concurrency": 3}, cookie)
        assert code == 422, (path, code)
        code, _, raw = request(client, "DELETE", path, {"emails": [], "concurrency": 11}, cookie)
        assert code == 422
        errors = json.loads(raw).get("detail", [])
        assert any(item.get("loc") == ["body", "concurrency"] for item in errors), path
    status, _, html = request(client, "GET", "/static/vue/index.html", cookie=cookie)
    assert status == 200
    assert hashlib.sha256(html).digest() == hashlib.sha256((ROOT / "web/static/vue/index.html").read_bytes()).digest()
    return {"files_match": True, "batch_remove_route": True, "delete_concurrency": True, "empty_requests": "422"}


def deploy(host, archive, checksum, verify_only=False):
    assert host in ALLOWED and host in KEYS
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedKeyPolicy())
    user, password = credentials()[host]
    try:
        for index, route in enumerate(("socks", "direct")):
            try:
                client.connect(host, username=user, password=password, sock=connect(host, 22, route, 12),
                               look_for_keys=False, allow_agent=False, banner_timeout=12, auth_timeout=12)
                break
            except Exception:
                client.close()
                if index == 1:
                    raise
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(PinnedKeyPolicy())
                emit(host, stage="retry_connection", route="direct")
        cookie = login(client)
        if verify_only:
            with client.open_sftp() as sftp:
                emit(host, stage="verified", **verify(client, cookie, sftp))
            return True
        assert_idle(client, cookie)
        emit(host, stage="idle_checked")
        with client.open_sftp() as sftp:
            # The mailcode runtime already supports per-mailbox locks/bulk alias deletion.
            # This release changes only the Python scheduler and UI.
            with sftp.open("/opt/gpt-register-fp-test/mailcode-service/server.mjs", "rb") as fp:
                assert hashlib.sha256(fp.read()).digest() == hashlib.sha256((ROOT / "mailcode-service/server.mjs").read_bytes()).digest()
            remote_pkg = "/tmp/" + RELEASE
            sftp.put(str(archive), remote_pkg)
            remote_script = "/tmp/codex-mailcom-management-update-20260919.sh"
            with sftp.open(remote_script, "wb") as fp:
                fp.write(SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
            sftp.chmod(remote_script, 0o700)
            emit(host, stage="uploaded")
            command = "bash " + " ".join(map(shlex.quote, (remote_script, remote_pkg, checksum)))
            _, stdout, stderr = client.exec_command(command, timeout=180)
            output = stdout.read().decode()
            errors = stderr.read().decode()
            status = stdout.channel.recv_exit_status()
            emit(host, stage="update", exit_code=status, summary=output.strip(),
                 errors=errors[-1000:] if status else "")
            if status:
                raise RuntimeError("Remote update failed; see rollback summary")
            result = verify(client, cookie, sftp)
            emit(host, stage="verified", **result)
            return True
    except Exception as error:
        emit(host, stage="failed", error=str(error)[:500])
        return False
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", nargs="+", choices=ALLOWED, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    archive, checksum = (None, "") if args.verify_only else package()
    if archive:
        emit("local", archive=str(archive), sha256=checksum)
    # Each service update has its own backup and verification.
    results = [deploy(host, archive, checksum, args.verify_only) for host in dict.fromkeys(args.hosts)]
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
