"""CLI contract for the eager schema synchronization surface."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from chart_manager.cli import schemas as schemas_cli

from .conftest import cli


class _Service:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def sync(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(
            rows=3,
            required=12,
            generated=4,
            local=1,
            sync=SimpleNamespace(
                generation_published=True,
                lock=SimpleNamespace(generation="sha256:" + "a" * 64),
                generation_path=Path("/cache/lab-charts/aaa"),
            ),
        )


def test_sync_forwards_update_offline_and_workers(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = _Service()
    monkeypatch.setattr(schemas_cli, "_make_service", lambda: service)

    result = cli("schemas", "sync", "--update", "--offline", "--workers", "2")

    assert result.exit_code == 0
    assert service.calls == [{"update": True, "offline": True, "workers": 2}]
    assert "schema generation sha256:" in result.stdout


def test_sync_inherits_offline_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = _Service()
    monkeypatch.setenv("CHART_MANAGER_OFFLINE", "1")
    monkeypatch.setattr(schemas_cli, "_make_service", lambda: service)

    result = cli("schemas", "sync")

    assert result.exit_code == 0
    assert service.calls[0]["offline"] is True


def test_explicit_online_overrides_offline_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = _Service()
    monkeypatch.setenv("CHART_MANAGER_OFFLINE", "1")
    monkeypatch.setattr(schemas_cli, "_make_service", lambda: service)

    result = cli("schemas", "sync", "--online")

    assert result.exit_code == 0
    assert service.calls[0]["offline"] is False


def test_schemas_help_lists_sync() -> None:
    result = cli("schemas", "--help")

    assert result.exit_code == 0
    assert "sync" in result.stdout
