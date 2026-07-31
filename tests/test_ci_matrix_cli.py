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

from chart_manager.cli import main
from chart_manager.services.ci import MatrixSelection, _select_cluster_tests
from chart_manager.services.lifecycle import ClusterTestImpact

from .conftest import cli


class _CiService:
    """Stands in for `CiService` at `main._container()`.

    `matrix` delegates to the real `_select_cluster_tests` rather than
    re-running the `--all` > `--chart` > `--base` precedence here. A double
    that reimplements the dispatch under test would keep passing after the
    real one regressed, which is the whole failure mode these tests exist to
    catch -- the CLI used to own that if/elif chain.

    The recorded `calls` are what prove the delegation reached the right
    selector: this class only implements `ClusterTestMatrixSource`, so
    `matrix` cannot produce an answer without going through one of the three.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.selections: list[MatrixSelection] = []

    def matrix(self, selection: MatrixSelection) -> tuple[ClusterTestImpact, ...]:
        self.selections.append(selection)
        return _select_cluster_tests(self, selection)

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

    result = cli("plan", "-o", "github", "--base", "merge-base")

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

    all_result = cli("plan", "-o", "github", "--all")
    explicit_result = cli(
        "plan",
        "-o",
        "github",
        "--chart",
        "beta",
        "--chart",
        "alpha",
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


def test_cli_delegates_the_whole_selection_to_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The surface hands over intent, not a chosen selector.

    Without this, moving the `--all` > `--chart` > `--base` precedence back
    into `cli/main.py` would leave every other test in this file green -- the
    double implements all three selectors, so a CLI calling them directly
    still gets the right answer. This is the assertion that makes
    `CiService.matrix` the contract rather than a convenience.
    """
    service = _CiService()
    _wire(monkeypatch, service)

    result = cli("plan", "-o", "github", "--base", "merge-base")

    assert result.exit_code == 0
    assert service.selections == [
        MatrixSelection(base="merge-base", all_charts=False, charts=())
    ]


def test_matrix_rejects_conflicting_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _CiService()
    _wire(monkeypatch, service)

    result = cli("plan", "-o", "github", "--all", "--chart", "alpha")

    assert result.exit_code == 1
    assert isinstance(result.exception, main.ChartManagerError)
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
    assert service.calls == [("publish", main.Path("changed.txt"))]
