"""CI chart selection uses enabled cluster-test capabilities, not directories."""

from pathlib import Path

import pytest

from chart_manager.services.ci import CiService


def test_changed_charts_filters_to_enabled_cluster_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CiService(tmp_path, helm=object(), kubectl=object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        service.git,
        "changed_charts",
        lambda _base: ["enabled", "unmanaged", "disabled"],
    )
    monkeypatch.setattr(service.cluster_tests, "enabled_names", lambda: ["enabled"])

    assert service.changed_charts("main") == ["enabled"]


def test_cluster_test_charts_returns_the_enabled_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CiService(tmp_path, helm=object(), kubectl=object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        service.cluster_tests,
        "enabled_names",
        lambda: ["alloy", "grafana"],
    )

    assert service.cluster_test_charts() == ["alloy", "grafana"]
