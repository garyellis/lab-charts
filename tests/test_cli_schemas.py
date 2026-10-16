from pathlib import Path
from types import SimpleNamespace

from chart_manager.commands.validate import cli as validate_cli

from .conftest import FakeCommandRunner, cli


def test_sync_updates_pins_only_when_asked(monkeypatch):
    calls = []

    def verb(name):
        def run(workspace, store, **kwargs):
            calls.append(name)
            return SimpleNamespace(
                published=True,
                lock=SimpleNamespace(generation="sha256:" + "a" * 64),
                generation_path=Path("/cache/repos"),
            )

        return run

    monkeypatch.setattr(
        validate_cli, "schema_lock", SimpleNamespace(sync=verb("sync"), update=verb("update"))
    )
    monkeypatch.setattr(validate_cli, "_container", lambda: SimpleNamespace(
        workspace=lambda: None,
        settings=SimpleNamespace(
            command_timeout=None, schema_cache_root=Path("/cache"), github_token=None
        ),
        command_runner=lambda: FakeCommandRunner(),
    ))
    assert cli("schemas", "sync").exit_code == 0
    result = cli("schemas", "sync", "--update")
    assert result.exit_code == 0
    assert calls == ["sync", "update"]
    assert "schema generation" in result.stdout


def test_sync_help_has_no_inventory_refresh_or_render_workers():
    result = cli("schemas", "sync", "--help")
    assert result.exit_code == 0
    for option in ("--refresh", "--workers", "--offline"):
        assert option not in result.output
    assert "--update" in result.output
