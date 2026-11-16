from common import relay_paths


def test_default_relay_auths_dir_falls_back_to_project_storage(monkeypatch, tmp_path):
    external = tmp_path / "mounted" / "auths"
    local = tmp_path / "project" / "auths"
    monkeypatch.setattr(relay_paths, "EXTERNAL_RELAY_AUTHS_DIR", external)
    monkeypatch.setattr(relay_paths, "LOCAL_RELAY_AUTHS_DIR", local)

    assert relay_paths.default_relay_auths_dir() == local


def test_default_relay_auths_dir_prefers_existing_mount(monkeypatch, tmp_path):
    external = tmp_path / "mounted" / "auths"
    external.mkdir(parents=True)
    local = tmp_path / "project" / "auths"
    monkeypatch.setattr(relay_paths, "EXTERNAL_RELAY_AUTHS_DIR", external)
    monkeypatch.setattr(relay_paths, "LOCAL_RELAY_AUTHS_DIR", local)

    assert relay_paths.default_relay_auths_dir() == external
