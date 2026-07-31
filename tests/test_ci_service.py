"""CI selection delegates Git diffs to the typed lifecycle impact service."""

from pathlib import Path

import pytest
import yaml

from chart_manager.plumbing.errors import ExternalCommandError, SpecError
from chart_manager.services.ci import CiService
from chart_manager.services.lifecycle import LifecycleImpact

from .conftest import MakeChart


def _service(root: Path) -> CiService:
    return CiService(root)


def _dependent_test(
    chart: Path,
    *,
    target: str,
    profile: str,
) -> None:
    path = chart / "chart-lifecycle.yaml"
    config = yaml.safe_load(path.read_text())
    config["spec"]["clusterTest"]["dependentTests"] = [
        {"chart": target, "profile": profile}
    ]
    path.write_text(yaml.safe_dump(config))


def test_directly_changed_charts_uses_only_explicit_file_ownership(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("alpha")
    make_chart("zeta")
    listing = chart_root / "changed.txt"
    listing.write_text(
        "\n".join(
            [
                "README.md",
                "charts/zeta/README.md",
                "charts/alpha/templates/deployment.yaml",
                "charts/zeta/values.yaml",
                "kind-config.yaml",
            ]
        )
    )

    assert _service(chart_root).directly_changed_charts(listing) == ["alpha", "zeta"]


def test_directly_changed_charts_skips_a_deleted_chart(chart_root: Path) -> None:
    listing = chart_root / "changed.txt"
    listing.write_text("charts/removed/Chart.yaml\n")

    assert _service(chart_root).directly_changed_charts(listing) == []


def test_directly_changed_charts_reports_unreadable_input(chart_root: Path) -> None:
    with pytest.raises(SpecError, match="cannot read changed-files input"):
        _service(chart_root).directly_changed_charts(chart_root / "missing.txt")


def test_cluster_test_matrix_preserves_declared_dependent_profile_and_reasons(
    chart_root: Path,
    make_chart: MakeChart,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_chart("source")
    make_chart("consumer", profiles={"minimal": {}, "full": {}})
    _dependent_test(source, target="consumer", profile="full")
    service = _service(chart_root)
    monkeypatch.setattr(
        service.git,
        "changed_files",
        lambda _base: ["charts/source/values.yaml"],
    )

    matrix = service.cluster_test_matrix("main")

    assert [(entry.chart, entry.profile) for entry in matrix] == [
        ("consumer", "full"),
        ("source", "minimal"),
    ]
    assert matrix[0].reasons[0].code.value == "declared-dependent-test"


def test_cluster_test_matrix_applies_repository_safety_fanout(
    chart_root: Path,
    make_chart: MakeChart,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_chart("alpha")
    make_chart("beta")
    service = _service(chart_root)
    monkeypatch.setattr(
        service.git,
        "changed_files",
        lambda _base: ["kind-config.yaml"],
    )

    matrix = service.cluster_test_matrix()

    assert [(entry.chart, entry.profile) for entry in matrix] == [
        ("alpha", "minimal"),
        ("beta", "minimal"),
    ]
    assert all(
        entry.reasons[0].code.value == "cluster-safety-fanout"
        for entry in matrix
    )


def test_lifecycle_impact_propagates_git_failure(
    chart_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(chart_root)

    def fail(_base: str) -> list[str]:
        raise ExternalCommandError("git diff failed")

    monkeypatch.setattr(service.git, "changed_files", fail)

    with pytest.raises(ExternalCommandError, match="git diff failed"):
        service.lifecycle_impact("missing-base")


def test_lifecycle_impact_fails_loudly_on_structured_spec_errors(
    chart_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(chart_root)
    monkeypatch.setattr(service.git, "changed_files", lambda _base: ["charts/bad/Chart.yaml"])
    monkeypatch.setattr(
        service.impact,
        "analyze",
        lambda _files: LifecycleImpact(
            changed_files=(Path("charts/bad/Chart.yaml"),),
            validation=(),
            cluster_tests=(),
            spec_errors=("bad: invalid ChartLifecycle resource",),
        ),
    )

    with pytest.raises(SpecError, match="invalid ChartLifecycle resource"):
        service.lifecycle_impact("main")


def test_all_matrix_uses_minimal_or_deterministic_profile_fallback(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("alpha", profiles={"smoke": {}, "full": {}})
    make_chart("beta", profiles={"minimal": {}, "full": {}})

    matrix = _service(chart_root).all_cluster_test_matrix()

    assert [(entry.chart, entry.profile) for entry in matrix] == [
        ("alpha", "full"),
        ("beta", "minimal"),
    ]


def test_explicit_matrix_rejects_unknown_and_unavailable_charts_together(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("enabled")
    disabled = make_chart("disabled")
    path = disabled / "chart-lifecycle.yaml"
    config = yaml.safe_load(path.read_text())
    config["spec"]["clusterTest"]["enabled"] = False
    path.write_text(yaml.safe_dump(config))

    with pytest.raises(SpecError) as caught:
        _service(chart_root).explicit_cluster_test_matrix(
            ["missing", "disabled", "enabled"]
        )

    assert "unknown chart(s): missing" in str(caught.value)
    assert "without enabled cluster tests: disabled" in str(caught.value)
