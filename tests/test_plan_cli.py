"""The `plan` surface -- one selection command with output projections.

`tests/test_ci_matrix_cli.py` and `tests/test_ci_impact_cli.py` already cover
everything `plan` inherited from the three `ci` commands it replaced, reaching
it through `conftest._COMMAND_PATHS` under their original spellings, and
`tests/test_cli_aliases.py` proves those spellings still behave identically.

This module covers only what is genuinely new in P1.6: the combinations that
did not exist while the capability was split across three commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from chart_manager.cli import plan as plan_cli
from chart_manager.services.lifecycle import (
    SCHEMA_VERSION,
    ClusterTestImpact,
    ImpactReason,
    ImpactReasonCode,
    LifecycleImpact,
    ValidationImpact,
)

from .conftest import cli


def _reason(code: ImpactReasonCode = ImpactReasonCode.CHART_CHANGE) -> ImpactReason:
    return ImpactReason(
        code=code,
        changed_file=Path("charts/grafana/values-dev.yaml"),
        detail="changed file belongs to grafana",
    )


def _impact(
    *,
    spec_errors: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> LifecycleImpact:
    """An impact document with one validation case and one cluster test."""
    return LifecycleImpact(
        changed_files=(Path("charts/grafana/values-dev.yaml"),),
        validation=(
            ValidationImpact(
                chart="grafana",
                environment="dev",
                release="grafana",
                namespace="lab-dev",
                reasons=(_reason(ImpactReasonCode.VALIDATION_TRIGGER),),
            ),
        ),
        cluster_tests=(ClusterTestImpact("grafana", "minimal", (_reason(),)),),
        spec_errors=spec_errors,
        warnings=warnings,
    )


def _wire_impact(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[list[str]]:
    """Replace the impact service, recording the paths it was handed."""
    seen: list[list[str]] = []
    monkeypatch.setattr(
        plan_cli,
        "_impact_service",
        lambda root: SimpleNamespace(analyze=lambda paths: seen.append(paths) or result),
    )
    return seen


class _CiService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def matrix(self, selection: Any) -> tuple[ClusterTestImpact, ...]:
        self.calls.append(("matrix", selection))
        return (ClusterTestImpact("from-git", "minimal", ()),)

    def directly_changed_charts(self, changed_files: Path) -> list[str]:
        self.calls.append(("publish", changed_files))
        return ["alpha", "zeta"]


def _wire_ci(monkeypatch: pytest.MonkeyPatch, service: _CiService) -> None:
    monkeypatch.setattr(
        plan_cli,
        "_container",
        lambda: SimpleNamespace(ci_service=lambda _root: service),
    )


# --------------------------------------------------------------------------
# the combination the split commands could not express
# --------------------------------------------------------------------------


def test_github_matrix_can_be_built_from_an_explicit_changed_file_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`plan --changed-files F -o github` -- the design doc's CI invocation.

    `ci cluster-test-matrix` could only diff against `--base`; `ci impact`
    could take explicit paths but only ever rendered reasons. Feeding CI a
    matrix computed from the changed-file list the workflow already has is
    the capability that only exists once the two are one command.

    Routed through the impact service, not `CiService.matrix`: explicit paths
    are the question that service answers, and its `cluster_tests` are the
    same values the matrix is shaped from.
    """
    service = _CiService()
    _wire_ci(monkeypatch, service)
    seen = _wire_impact(monkeypatch, _impact())

    result = cli("plan", "--changed-file", "charts/grafana/values-dev.yaml", "-o", "github")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"include": [{"chart": "grafana", "profile": "minimal"}]}
    assert seen == [["charts/grafana/values-dev.yaml"]]
    assert service.calls == [], "explicit paths must not reach the git-diff selector"


def test_github_matrix_still_uses_the_matrix_selector_without_explicit_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other branch of the same choice, so the test above cannot pass alone."""
    service = _CiService()
    _wire_ci(monkeypatch, service)
    monkeypatch.setattr(
        plan_cli,
        "_impact_service",
        lambda root: pytest.fail("the impact service must not be used for --base"),
    )

    result = cli("plan", "--base", "merge-base", "-o", "github")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"include": [{"chart": "from-git", "profile": "minimal"}]}
    assert [name for name, _ in service.calls] == ["matrix"]


# --------------------------------------------------------------------------
# --for narrows the human projection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "present", "absent"),
    [
        ("validate", "Validation:", "Cluster tests:"),
        ("test", "Cluster tests:", "Validation:"),
    ],
)
def test_for_narrows_the_table_to_one_kind_of_work(
    monkeypatch: pytest.MonkeyPatch, kind: str, present: str, absent: str
) -> None:
    _wire_impact(monkeypatch, _impact(warnings=("unmatched chart change README.md",)))

    result = cli(
        "plan", "--changed-file", "charts/grafana/values-dev.yaml", "--for", kind,
        "-o", "table",
    )

    assert result.exit_code == 0
    assert present in result.stdout
    assert absent not in result.stdout
    # Warnings survive every narrowing: they are usually the answer to
    # "why is nothing selected?", which a narrowed view is most often asked.
    assert "unmatched chart change README.md" in result.stdout


def test_for_all_is_the_default_and_shows_both_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_impact(monkeypatch, _impact())

    result = cli("plan", "--changed-file", "charts/grafana/values-dev.yaml", "-o", "table")

    assert result.exit_code == 0
    assert "Validation:" in result.stdout
    assert "Cluster tests:" in result.stdout


def test_json_emits_the_whole_document_even_when_for_narrows_the_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--for` is a view concern; the wire document is owned by `services/`.

    Deleting a key from the payload here would fork a versioned contract in
    the surface, so the machine projection stays complete and `--for` only
    narrows the human one. Pinned because the opposite is the obvious guess.
    """
    _wire_impact(monkeypatch, _impact())

    result = cli("plan", "--changed-file", "charts/grafana/values-dev.yaml", "--for", "validate", "-o", "json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert [
        (entry["chart"], entry["profile"]) for entry in payload["cluster_test_matrix"]
    ] == [("grafana", "minimal")]


# --------------------------------------------------------------------------
# --for publish keeps its own engine
# --------------------------------------------------------------------------


def test_publish_plan_is_direct_ownership_and_never_the_impact_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Publishing must not inherit lifecycle fanout, so it uses its own selector."""
    service = _CiService()
    _wire_ci(monkeypatch, service)
    monkeypatch.setattr(
        plan_cli,
        "_impact_service",
        lambda root: pytest.fail("publish selection must not use lifecycle impact"),
    )
    changed = tmp_path / "changed.txt"
    changed.write_text("charts/alpha/values.yaml\n", encoding="utf-8")

    result = cli("plan", "--for", "publish", "--changed-files", str(changed), "-o", "table")

    assert result.exit_code == 0
    assert result.stdout == "alpha\nzeta\n"
    assert service.calls == [("publish", changed)]


@pytest.mark.parametrize(
    ("output", "load"),
    [("json", json.loads), ("yaml", yaml.safe_load)],
)
def test_publish_plan_machine_projections_are_a_bare_list_of_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: str, load: Any
) -> None:
    """No envelope: a list of chart names has no schema to version."""
    _wire_ci(monkeypatch, _CiService())
    changed = tmp_path / "changed.txt"
    changed.write_text("charts/alpha/values.yaml\n", encoding="utf-8")

    result = cli("plan", "--for", "publish", "--changed-files", str(changed), "-o", output)

    assert result.exit_code == 0
    assert load(result.stdout) == ["alpha", "zeta"]


def test_publish_plan_requires_an_explicit_changed_file_list() -> None:
    """`--changed-file` is not enough: the selector reads the file itself."""
    result = cli("plan", "--for", "publish", "--changed-file", "charts/alpha/values.yaml")

    assert result.exit_code == 2
    assert "--changed-files" in result.stderr


# --------------------------------------------------------------------------
# combinations that have no answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["validate", "publish"])
def test_github_output_rejects_a_work_kind_it_cannot_project(kind: str) -> None:
    """`-o github` is the cluster-test matrix; there is no validate/publish matrix.

    Rejected at the surface rather than silently emitting the test matrix,
    which would hand a workflow a matrix for work it did not ask for.
    """
    result = cli("plan", "--all", "--for", kind, "-o", "github")

    assert result.exit_code == 2
    assert "--for" in result.stderr


def test_all_and_chart_remain_mutually_exclusive() -> None:
    """Inherited from `ci cluster-test-matrix`; classification is the surface's job."""
    result = cli("plan", "--all", "--chart", "alpha", "-o", "github")

    assert result.exit_code == 1
    assert isinstance(result.exception, plan_cli.ChartManagerError)


def test_table_output_still_requires_an_explicit_change_source() -> None:
    """`--base`/`--all` select a matrix, not an impact document.

    A limitation carried over unchanged: only the lifecycle impact service
    produces the reasons `-o table` renders, and it takes explicit paths.
    """
    result = cli("plan", "--base", "merge-base", "-o", "table")

    assert result.exit_code == 2
    assert "--changed-files / --changed-file" in result.stderr
