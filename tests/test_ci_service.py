"""`CiService`: publish selection and the Git diff it hands to lifecycle impact."""

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import ExternalCommandError, SpecError
from chart_manager.services.ci import CiService
from chart_manager.services.lifecycle import LifecycleImpact

from .conftest import MakeChart, workspace_for


def _service(root: Path) -> CiService:
    return CiService(
        workspace=workspace_for(root, fanout={"chartTest": ["kind-config.yaml"]})
    )


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
