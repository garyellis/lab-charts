"""Pick the chart tests a set of changed files, an explicit chart list or `--all` calls for."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from chart_manager.api.v1alpha1.chart_lifecycle import DEFAULT_PROFILE, ChartTestSpec
from chart_manager.api.v1alpha1.releases import LifecycleRelease, LocalChartRelease
from chart_manager.plumbing.errors import CapabilityUnavailableError, ChartManagerError, SpecError
from chart_manager.shared.charts.chart import chart_names, load_chart
from chart_manager.shared.charts.lifecycle import (
    CapabilityStatus,
    chart_test_status,
    require_chart_test,
    require_chart_test_profile,
)
from chart_manager.shared.cluster.local_cluster import load_cluster
from chart_manager.shared.workspace import WORKSPACE_FILE, RepositoryWorkspace, pattern_matches


class ReasonCode(StrEnum):
    """The rule that selected a chart test."""

    CHART_CHANGE = "chart-change"
    DECLARED_DEPENDENT_TEST = "declared-dependent-test"
    CHART_TEST_FANOUT = "chart-test-fanout"


@dataclass(frozen=True)
class Reason:
    """One changed file and the rule by which it selected a chart test."""

    code: ReasonCode
    changed_file: Path
    detail: str


@dataclass(frozen=True)
class SelectedTest:
    """One chart test to run: a chart at one profile, and why changes selected it."""

    chart: str
    profile: str
    reasons: tuple[Reason, ...] = ()


@dataclass(frozen=True)
class Selection:
    """The selected chart tests, and the `dependentTests` entries that name no chart test."""

    tests: tuple[SelectedTest, ...]
    spec_errors: tuple[str, ...] = ()


def select(
    changes: Sequence[str] | None,
    *,
    workspace: RepositoryWorkspace,
    charts: Sequence[str] = (),
) -> Selection:
    """Exactly `charts` when given; else every enabled chart test, or those `changes` touch.

    Each chart runs at its default profile. With `changes`, a changed file selects:
    - every enabled chart test, when it matches the workspace's chart-test fanout;
    - its chart's test, and the tests that chart's `dependentTests` names.

    Raises `SpecError` when `charts` names an unknown chart or one without enabled chart tests.
    """
    charts_dir = workspace.charts_root
    if charts:
        return Selection(_explicit(charts, charts_dir))
    enabled = _enabled_names(charts_dir)
    profiles = {chart: _default_profile(chart, charts_dir) for chart in enabled}
    if changes is None:
        return Selection(tuple(SelectedTest(chart, profiles[chart]) for chart in enabled))
    reasons, errors = _reasons_for(changes, profiles, charts_dir, workspace)
    tests = tuple(
        SelectedTest(chart, profile, tuple(found))
        for (chart, profile), found in sorted(reasons.items())
    )
    return Selection(tests, tuple(errors))


def _chart_test(charts_dir: Path, chart: str) -> ChartTestSpec:
    """The chart's enabled chart-test section; raises when it has none."""
    return require_chart_test(load_chart(charts_dir / chart).lifecycle, chart_name=chart)


def _enabled_names(charts_dir: Path) -> list[str]:
    """Charts whose chart tests are enabled; a malformed chart fails rather than shrinking CI."""
    return [
        name
        for name in chart_names(charts_dir)
        if chart_test_status(load_chart(charts_dir / name).lifecycle) is CapabilityStatus.ENABLED
    ]


def _default_profile(chart: str, charts_dir: Path) -> str:
    """`DEFAULT_PROFILE` when the chart has it, else its first profile in sorted order."""
    profiles = _chart_test(charts_dir, chart).profiles
    if not profiles:
        raise SpecError(f"chart '{chart}' has enabled chart tests but declares no profiles")
    if DEFAULT_PROFILE in profiles:
        return DEFAULT_PROFILE
    return sorted(profiles)[0]


def _explicit(charts: Sequence[str], charts_dir: Path) -> tuple[SelectedTest, ...]:
    """The named charts at their default profiles, or every bad name in one error."""
    requested = sorted(set(charts))
    known = set(chart_names(charts_dir))
    unknown = [chart for chart in requested if chart not in known]
    unavailable: list[str] = []
    selected: list[SelectedTest] = []
    for chart in requested:
        if chart in unknown:
            continue
        try:
            selected.append(SelectedTest(chart, _default_profile(chart, charts_dir)))
        except CapabilityUnavailableError:
            unavailable.append(chart)
    if unknown or unavailable:
        details = []
        if unknown:
            details.append(f"unknown chart(s): {', '.join(unknown)}")
        if unavailable:
            details.append(f"chart(s) without enabled chart tests: {', '.join(unavailable)}")
        raise SpecError("invalid chart-test request: " + "; ".join(details))
    return tuple(selected)


def _reasons_for(
    changes: Sequence[str],
    profiles: dict[str, str],
    charts_dir: Path,
    workspace: RepositoryWorkspace,
) -> tuple[dict[tuple[str, str], list[Reason]], list[str]]:
    """Why `changes` select each (chart, profile); errors from `dependentTests` entries."""
    paths = sorted({Path(raw) for raw in changes if raw}, key=Path.as_posix)
    selected: dict[tuple[str, str], list[Reason]] = {}
    errors: list[str] = []

    def add(key: tuple[str, str], reason: Reason) -> None:
        found = selected.setdefault(key, [])
        if reason not in found:
            found.append(reason)

    patterns = _fanout_patterns(workspace)
    fanout = [
        (path, pattern) for path in paths for pattern in patterns if pattern_matches(pattern, path)
    ]
    for name, profile in profiles.items():
        for path, pattern in fanout:
            detail = _fanout_detail(pattern, workspace)
            add((name, profile), Reason(ReasonCode.CHART_TEST_FANOUT, path, detail))

    for path in paths:
        chart = workspace.chart_name_from_repo_path(path)
        if chart is None or chart not in profiles:
            continue
        detail = f"changed file belongs to {chart}, which has chart tests enabled"
        add((chart, profiles[chart]), Reason(ReasonCode.CHART_CHANGE, path, detail))
        for reference in _chart_test(charts_dir, chart).dependent_tests:
            target = f"{reference.chart}:{reference.profile}"
            try:
                require_chart_test_profile(
                    _chart_test(charts_dir, reference.chart), reference.profile
                )
            except ChartManagerError as exc:
                errors.append(f"{chart} dependentTests {target}: {exc}")
                continue
            detail = f"{chart} declares dependent test {target}"
            add(
                (reference.chart, reference.profile),
                Reason(ReasonCode.DECLARED_DEPENDENT_TEST, path, detail),
            )
    return selected, errors


def _fanout_patterns(workspace: RepositoryWorkspace) -> tuple[str, ...]:
    """Patterns whose changes select every chart test.

    These are the authored fanout, the workspace files, the shared charts, and the LocalCluster's
    kind config and local bootstrap charts. A malformed LocalCluster fails selection.
    """
    implicit = [workspace.spec.local_cluster.as_posix(), WORKSPACE_FILE.as_posix()]
    implicit.extend(
        workspace.repo_chart_path(name).as_posix()
        for name in workspace.spec.chart_test.shared_charts
    )
    if workspace.local_cluster_path.is_file():
        cluster = load_cluster(workspace)
        implicit.append(cluster.spec.cluster.config.as_posix())
        implicit.extend(
            release.chart.as_posix()
            for release in cluster.spec.bootstrap.releases
            if isinstance(release, (LifecycleRelease, LocalChartRelease))
        )
    return tuple(sorted({*workspace.spec.fanout.chart_test, *implicit}))


def _fanout_detail(pattern: str, workspace: RepositoryWorkspace) -> str:
    """Why a chart-test fanout pattern selects every chart test."""
    for chart in workspace.spec.chart_test.shared_charts:
        if pattern == workspace.repo_chart_path(chart).as_posix():
            return f"{chart} is a shared chart used by every chart test"
    if pattern not in workspace.spec.fanout.chart_test and pattern not in {
        workspace.spec.local_cluster.as_posix(),
        workspace.marker.relative_to(workspace.root).as_posix(),
    }:
        return f"{pattern} is part of the LocalCluster every chart test runs on"
    return f"workspace chart-test fanout matched {pattern}"
