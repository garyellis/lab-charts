from pathlib import Path
from types import SimpleNamespace

from chart_manager.cli import schemas as schemas_cli

from .conftest import cli


def test_sync_forwards_only_explicit_pin_update(monkeypatch):
    calls = []

    def sync(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            generation_published=True,
            lock=SimpleNamespace(generation="sha256:" + "a" * 64),
            generation_path=Path("/cache/repos"),
        )

    monkeypatch.setattr(schemas_cli, "_make_service", lambda: SimpleNamespace(sync=sync))
    assert cli("schemas", "sync").exit_code == 0
    result = cli("schemas", "sync", "--update")
    assert result.exit_code == 0
    assert calls == [{"update": False}, {"update": True}]
    assert "schema generation" in result.stdout


def test_sync_help_has_no_inventory_refresh_or_render_workers():
    result = cli("schemas", "sync", "--help")
    assert result.exit_code == 0
    for option in ("--refresh", "--workers", "--offline"):
        assert option not in result.output
    assert "--update" in result.output
