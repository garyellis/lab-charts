"""Pick the chart tests a set of changed files, an explicit chart list or `--all` calls for."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from chart_manager.api.v1alpha1.chart_lifecycle import DEFAULT_PROFILE
from chart_manager.plumbing.errors import CapabilityUnavailableError, ChartManagerError, SpecError
from chart_manager.shared.charts.chart_tests import ChartTestCatalog
from chart_manager.shared.charts.lifecycle import require_chart_test_profile
from chart_manager.shared.workspace import RepositoryWorkspace


class ReasonCode(StrEnum):
    """The rule that selected a chart test."""

    CHART_CHANGE = "chart-change"
    DECLARED_DEPENDENT_TEST = "declared-dependent-test"
    CLUSTER_SAFETY_FANOUT = "cluster-safety-fanout"


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
    catalog = ChartTestCatalog(workspace.root, charts_dir=workspace.spec.charts_dir)
    if charts:
        return Selection(_explicit(charts, catalog))
    enabled = catalog.enabled_names()
    profiles = {chart: _default_profile(chart, catalog) for chart in enabled}
    if changes is None:
        return Selection(tuple(SelectedTest(chart, profiles[chart]) for chart in enabled))
    reasons, errors = _reasons_for(changes, profiles, catalog, workspace)
    tests = tuple(
        SelectedTest(chart, profile, tuple(found))
        for (chart, profile), found in sorted(reasons.items())
    )
    return Selection(tests, tuple(errors))


def _default_profile(chart: str, catalog: ChartTestCatalog) -> str:
    """`DEFAULT_PROFILE` when the chart has it, else its first profile in sorted order."""
    profiles = catalog.get(chart).spec.profiles
    if not profiles:
        raise SpecError(f"chart '{chart}' has enabled cluster tests but declares no profiles")
    if DEFAULT_PROFILE in profiles:
        return DEFAULT_PROFILE
    return sorted(profiles)[0]


def _explicit(charts: Sequence[str], catalog: ChartTestCatalog) -> tuple[SelectedTest, ...]:
    """The named charts at their default profiles, or every bad name in one error."""
    requested = sorted(set(charts))
    known = set(catalog.repository.list_names())
    unknown = [chart for chart in requested if chart not in known]
    unavailable: list[str] = []
    selected: list[SelectedTest] = []
    for chart in requested:
        if chart in unknown:
            continue
        try:
            selected.append(SelectedTest(chart, _default_profile(chart, catalog)))
        except CapabilityUnavailableError:
            unavailable.append(chart)
    if unknown or unavailable:
        details = []
        if unknown:
            details.append(f"unknown chart(s): {', '.join(unknown)}")
        if unavailable:
            details.append(f"chart(s) without enabled cluster tests: {', '.join(unavailable)}")
        raise SpecError("invalid cluster-test chart request: " + "; ".join(details))
    return tuple(selected)


def _reasons_for(
    changes: Sequence[str],
    profiles: dict[str, str],
    catalog: ChartTestCatalog,
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

    matches = workspace.matching_chart_test_patterns
    fanout = [(path, pattern) for path in paths for pattern in matches(path)]
    for name, profile in profiles.items():
        for path, pattern in fanout:
            detail = _fanout_detail(pattern, workspace)
            add((name, profile), Reason(ReasonCode.CLUSTER_SAFETY_FANOUT, path, detail))

    for path in paths:
        chart = workspace.chart_name_from_repo_path(path)
        if chart is None or chart not in profiles:
            continue
        detail = f"changed file belongs to enabled cluster-test chart {chart}"
        add((chart, profiles[chart]), Reason(ReasonCode.CHART_CHANGE, path, detail))
        for reference in catalog.get(chart).spec.dependent_tests:
            target = f"{reference.chart}:{reference.profile}"
            try:
                require_chart_test_profile(catalog.get(reference.chart).spec, reference.profile)
            except ChartManagerError as exc:
                errors.append(f"{chart} dependentTests {target}: {exc}")
                continue
            detail = f"{chart} declares dependent test {target}"
            add(
                (reference.chart, reference.profile),
                Reason(ReasonCode.DECLARED_DEPENDENT_TEST, path, detail),
            )
    return selected, errors


def _fanout_detail(pattern: str, workspace: RepositoryWorkspace) -> str:
    """Why a chart-test fanout pattern selects every chart test."""
    for chart in workspace.spec.chart_test.shared_charts:
        if pattern == workspace.repo_chart_path(chart).as_posix():
            return f"{chart} is a shared runtime prerequisite across cluster tests"
    if pattern not in workspace.spec.fanout.chart_test and pattern not in {
        workspace.spec.local_cluster.as_posix(),
        workspace.marker.relative_to(workspace.root).as_posix(),
    }:
        return f"{pattern} is a LocalCluster bootstrap prerequisite used by every cluster test"
    return f"workspace cluster-test fanout matched {pattern}"
