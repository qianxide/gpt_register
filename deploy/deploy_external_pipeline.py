"""Build and publish the complete external pipeline to server 204 only."""
from __future__ import annotations

import argparse
import hashlib
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import shlex
import tarfile

import paramiko

from readonly_ssh_recon import connect, credentials, panel_credentials


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent.parent
HOST = "45.205.31.204"
HOST_KEY = "89c48fee7ba72a60e3e0806d7c410a83df29536e81feaf36c2b865ac71c22bd3"
RELEASE = "gpt-register-fp-external-pipeline-20260920.tar.gz"
SCRIPT = ROOT / "deploy/remote_update_external_pipeline.sh"
SOURCES = (
    "common/external_clients.py",
    "common/external_api.py",
    "common/external_pipeline.py",
    "web/routes/external_api.py",
)


class PinnedKeyPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        if hostname == HOST and hashlib.sha256(key.asbytes()).hexdigest() != HOST_KEY:
            raise RuntimeError("204 SSH host key differs from the verified fingerprint")


def package() -> tuple[Path, str]:
    path = WORKSPACE / RELEASE
    with tarfile.open(path, "w:gz") as archive:
        for name in SOURCES:
            archive.add(ROOT / name, arcname=name)
        archive.add(ROOT / "web/static/vue", arcname="web/static/vue")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def request(client, method: str, path: str, body=None, cookie: str = ""):
    channel = client.get_transport().open_channel(
        "direct-tcpip", ("127.0.0.1", 8790), ("127.0.0.1", 0), timeout=15
    )
    channel.settimeout(30)
    conn = http.client.HTTPConnection("127.0.0.1", 8790, timeout=30)
    conn.sock = channel
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        return response.status, response.getheader("Set-Cookie", ""), response.read()
    finally:
        conn.close()


def login(client) -> str:
    username, password = panel_credentials()
    status, header, _ = request(client, "POST", "/api/auth/login", {"username": username, "password": password})
    if status != 200:
        raise RuntimeError(f"panel login returned {status}")
    cookies = SimpleCookie()
    cookies.load(header)
    if "gpt_reg_session" not in cookies:
        raise RuntimeError("panel session cookie missing")
    return "gpt_reg_session=" + cookies["gpt_reg_session"].value


def verify(client, cookie: str, sftp) -> dict[str, object]:
    for name in SOURCES + ("web/static/vue/index.html", "web/static/vue/new.html"):
        with sftp.open("/opt/gpt-register-fp-test/" + name, "rb") as remote:
            remote_hash = hashlib.sha256(remote.read()).hexdigest()
        local_hash = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if remote_hash != local_hash:
            raise RuntimeError("remote file hash mismatch: " + name)

    checks = {}
    for path in ("/api/health", "/api/external/pipeline/status"):
        status, _, raw = request(client, "GET", path, cookie=cookie)
        checks[path] = status
        if status != 200:
            raise RuntimeError(f"verification returned {status}: {path}")
        json.loads(raw)
    return {
        "files_match": True,
        "health_http": checks["/api/health"],
        "pipeline_status_http": checks["/api/external/pipeline/status"],
    }


def deploy(archive: Path, checksum: str) -> None:
    user, password = credentials()[HOST]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedKeyPolicy())
    try:
        sock = connect(HOST, 22, "direct", 20)
        client.connect(HOST, port=22, username=user, password=password, sock=sock,
                       look_for_keys=False, allow_agent=False, timeout=20,
                       banner_timeout=20, auth_timeout=20)
        cookie = login(client)
        with client.open_sftp() as sftp:
            remote_package = "/tmp/" + RELEASE
            remote_script = "/tmp/codex-external-pipeline-update-20260920.sh"
            sftp.put(str(archive), remote_package)
            with sftp.open(remote_script, "wb") as remote:
                remote.write(SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
            sftp.chmod(remote_script, 0o700)
        command = "bash " + " ".join(map(shlex.quote, (remote_script, remote_package, checksum)))
        stdin, stdout, stderr = client.exec_command(command, timeout=240)
        stdin.write(panel_credentials()[1] + "\n")
        stdin.flush()
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8", "replace").strip()
        errors = stderr.read().decode("utf-8", "replace").strip()
        status = stdout.channel.recv_exit_status()
        if output:
            print(output)
        if status:
            if errors:
                print(errors[-2000:])
            raise RuntimeError(f"remote deployment failed with exit code {status}")
        with client.open_sftp() as sftp:
            print(json.dumps(verify(client, cookie, sftp), ensure_ascii=False))
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=(HOST,), default=HOST)
    args = parser.parse_args()
    archive, checksum = package()
    print(json.dumps({"host": args.host, "archive": str(archive), "sha256": checksum}, ensure_ascii=False))
    deploy(archive, checksum)


if __name__ == "__main__":
    main()
