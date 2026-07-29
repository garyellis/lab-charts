"""Machine-facing CI cluster-test matrix command."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from chart_manager.cli import main
from chart_manager.services.lifecycle import ClusterTestImpact


class _CiService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cluster_test_matrix(self, base: str) -> tuple[ClusterTestImpact, ...]:
        self.calls.append(("diff", base))
        return (
            ClusterTestImpact("consumer", "full", ()),
            ClusterTestImpact("source", "minimal", ()),
        )

    def all_cluster_test_matrix(self) -> tuple[ClusterTestImpact, ...]:
        self.calls.append(("all", None))
        return (ClusterTestImpact("source", "minimal", ()),)

    def explicit_cluster_test_matrix(
        self,
        charts: list[str],
    ) -> tuple[ClusterTestImpact, ...]:
        self.calls.append(("list", charts))
        return tuple(ClusterTestImpact(chart, "minimal", ()) for chart in charts)

    def directly_changed_charts(self, changed_files: object) -> list[str]:
        self.calls.append(("publish", changed_files))
        return ["alpha", "zeta"]


def _wire(monkeypatch: pytest.MonkeyPatch, service: _CiService) -> None:
    monkeypatch.setattr(
        main,
        "_container",
        lambda: SimpleNamespace(ci_service=lambda _root: service),
    )


def test_diff_matrix_emits_exact_chart_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)

    result = CliRunner().invoke(
        main.app,
        ["ci", "cluster-test-matrix", "--base", "merge-base"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "include": [
            {"chart": "consumer", "profile": "full"},
            {"chart": "source", "profile": "minimal"},
        ]
    }
    assert service.calls == [("diff", "merge-base")]


def test_all_and_explicit_matrix_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)
    runner = CliRunner()

    all_result = runner.invoke(main.app, ["ci", "cluster-test-matrix", "--all"])
    explicit_result = runner.invoke(
        main.app,
        [
            "ci",
            "cluster-test-matrix",
            "--chart",
            "beta",
            "--chart",
            "alpha",
        ],
    )

    assert json.loads(all_result.stdout)["include"] == [
        {"chart": "source", "profile": "minimal"}
    ]
    assert json.loads(explicit_result.stdout)["include"] == [
        {"chart": "beta", "profile": "minimal"},
        {"chart": "alpha", "profile": "minimal"},
    ]
    assert service.calls == [
        ("all", None),
        ("list", ["beta", "alpha"]),
    ]


def test_matrix_rejects_conflicting_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)

    result = CliRunner().invoke(
        main.app,
        ["ci", "cluster-test-matrix", "--all", "--chart", "alpha"],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, main.ChartManagerError)
    assert service.calls == []


def test_publish_charts_emits_newline_list_from_explicit_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)

    result = CliRunner().invoke(
        main.app,
        ["ci", "publish-charts", "--changed-files", "changed.txt"],
    )

    assert result.exit_code == 0
    assert result.stdout == "alpha\nzeta\n"
    assert service.calls == [("publish", main.Path("changed.txt"))]
