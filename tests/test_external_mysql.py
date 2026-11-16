"""Opt-in MySQL wiring for the external API and pipeline stores."""

from contextlib import contextmanager
from types import SimpleNamespace

from common import external_api, external_pipeline


def test_external_service_uses_mysql_only_for_the_default_path(tmp_path, monkeypatch):
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    monkeypatch.setattr(external_api.ExternalAPIService, "_init", lambda self: None)

    default_path = external_api.Path(external_api.ROOT) / "data/external_api.sqlite3"
    default = external_api.ExternalAPIService(default_path)
    custom = external_api.ExternalAPIService(tmp_path / "external_api.sqlite3")

    assert default._mysql is True
    assert custom._mysql is False


def test_mysql_external_db_preserves_parameter_binding(monkeypatch):
    calls = []

    class Connection:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            return self

    @contextmanager
    def fake_connection(kind):
        assert kind == "external_api"
        yield Connection()

    monkeypatch.setattr(external_api, "mysql_connection", fake_connection)
    monkeypatch.setattr(external_api.ExternalAPIService, "_init", lambda self: None)
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    service = external_api.ExternalAPIService(external_api.Path(external_api.ROOT) / "data/external_api.sqlite3")

    with service._db(True) as db:
        db.execute("SELECT * FROM settings WHERE name=?", ("scan",))

    assert calls == [("SELECT * FROM settings WHERE name=%s", ("scan",))]


def test_external_mysql_schema_contains_durable_pipeline_keys(monkeypatch):
    ddl = {}

    def fake_ensure(kind, statements):
        ddl[kind] = "\n".join(statements)

    @contextmanager
    def fake_connection(kind):
        class Connection:
            def execute(self, sql, params=()):
                return None
        yield Connection()

    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    monkeypatch.setattr(external_api, "ensure_schema", fake_ensure)
    monkeypatch.setattr(external_api, "mysql_connection", fake_connection)
    service = external_api.ExternalAPIService(external_api.Path(external_api.ROOT) / "data/external_api.sqlite3")

    assert "PRIMARY KEY" in ddl["external_api"]
    assert "utf8mb4_bin" in ddl["external_api"]
    assert "scan_submissions" in ddl["external_api"]
    assert service._mysql is True


def test_pipeline_mysql_schema_preserves_request_and_account_uniqueness(monkeypatch):
    ddl = {}
    monkeypatch.setattr(external_pipeline, "ensure_schema",
                        lambda kind, statements: ddl.setdefault(kind, "\n".join(statements)))

    class Connection:
        def execute(self, sql, params=()):
            assert "PRAGMA" not in sql
            return self

        def fetchall(self):
            return []

    @contextmanager
    def db(write=False):
        yield Connection()

    service = SimpleNamespace(_mysql=True, _db=db)
    external_pipeline.PipelineManager(service, lambda group_id: {}, lambda keys: {})

    schema = ddl["external_pipeline"]
    assert "request_id VARCHAR(80) COLLATE utf8mb4_bin NOT NULL UNIQUE" in schema
    assert "UNIQUE KEY pipeline_task_account (task_id, account_key)" in schema
