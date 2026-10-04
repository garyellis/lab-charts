"""Machine-facing `plan` projections: `-o github` and `--for publish`.

`.github/workflows/ci.yaml` captures both into shell variables, so their
stdout is a wire contract: `-o github` is the GitHub Actions matrix JSON and
`--for publish` is a newline-delimited chart list, not JSON.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from chart_manager.cli import plan as plan_cli
from chart_manager.commands import test
from chart_manager.services.ci import MatrixSelection

from .conftest import cli


class _CiService:
    """Stands in for `CiService` at `plan._container()`, recording what it was asked.

    `test.select()` and `CiService.matrix` own which charts are chosen; these
    tests only pin that the CLI hands over its flags as one `MatrixSelection`
    and prints the answer in the matrix shape.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def matrix(self, selection: MatrixSelection) -> tuple[test.SelectedTest, ...]:
        self.calls.append(("matrix", selection))
        return (test.SelectedTest("consumer", "full"), test.SelectedTest("source", "minimal"))

    def directly_changed_charts(self, changed_files: object) -> list[str]:
        self.calls.append(("publish", changed_files))
        return ["alpha", "zeta"]


def _wire(monkeypatch: pytest.MonkeyPatch, service: _CiService) -> None:
    monkeypatch.setattr(
        plan_cli,
        "_container",
        lambda: SimpleNamespace(ci_service=lambda _root: service),
    )


def test_diff_matrix_emits_exact_chart_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)

    result = cli("plan", "-o", "github", "--base", "merge-base")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "include": [
            {"chart": "consumer", "profile": "full"},
            {"chart": "source", "profile": "minimal"},
        ]
    }
    assert service.calls == [("matrix", MatrixSelection(base="merge-base"))]


def test_all_and_explicit_flags_reach_the_service_as_one_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)

    all_result = cli("plan", "-o", "github", "--all")
    explicit_result = cli("plan", "-o", "github", "--chart", "beta", "--chart", "alpha")

    assert all_result.exit_code == explicit_result.exit_code == 0
    assert service.calls == [
        ("matrix", MatrixSelection(all_charts=True)),
        ("matrix", MatrixSelection(charts=("beta", "alpha"))),
    ]


def test_matrix_rejects_conflicting_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)

    result = cli("plan", "-o", "github", "--all", "--chart", "alpha")

    assert result.exit_code == 1
    assert isinstance(result.exception, plan_cli.ChartManagerError)
    assert service.calls == []


def test_publish_charts_emits_newline_list_from_explicit_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)

    # `-o table` names the projection under test. The output default is
    # `auto`, which resolves to json off a terminal -- which is what
    # CliRunner is, and what CI is. This is the exact contract
    # `.github/workflows/ci.yaml` depends on: it captures this stdout and
    # reads it with `while IFS= read -r chart`, so it passes `-o table` too.
    result = cli("plan", "--for", "publish", "-o", "table", "--changed-files", "changed.txt")

    assert result.exit_code == 0
    assert result.stdout == "alpha\nzeta\n"
    assert service.calls == [("publish", plan_cli.Path("changed.txt"))]
