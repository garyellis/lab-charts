"""`chart test` plan compilation: each entry's steps, namespaces, hooks and warnings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chart_manager.commands.test.models import ChartTestRequest, LifecyclePlan, PlanError
from chart_manager.commands.test.plan import (
    EXTERNAL_BOOTSTRAP_WARNING_PREFIX,
    SKIPPED_REQUIRES_WARNING_PREFIX,
    CompiledPlan,
    Requires,
    SkippedRequirement,
    compile_plan,
)
from chart_manager.plumbing.errors import SpecError
from chart_manager.shared.cluster.bootstrap import ExternallySatisfiedLifecycle
from tests.conftest import MakeChart


def _compile(
    root: Path,
    request: ChartTestRequest,
    *,
    owned: tuple[str, ...],
    requires: Requires,
) -> CompiledPlan:
    identities = frozenset(
        ExternallySatisfiedLifecycle(
            (root / "charts" / chart).resolve(), chart, "minimal", "default"
        )
        for chart in owned
    )
    return compile_plan(
        request,
        root=root,
        charts_dir=root / "charts",
        bootstrap_owned=identities,
        requires=requires,
    )


def _steps(plan: LifecyclePlan) -> list[str]:
    return [f"{a.entry.chart.name}@{a.entry.namespace} {a.kind}" for a in plan.actions]


def _on(chart: str, *kinds: str, namespace: str = "default") -> list[str]:
    return [f"{chart}@{namespace} {kind}" for kind in kinds]


def _script(root: Path, relative: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return relative


@pytest.fixture
def charts(chart_root: Path, make_chart: MakeChart) -> Path:
    """base <- app <- web; app's dependent test is web, which has no helm test."""
    hook = _script(chart_root, "scripts/hook")
    make_chart("base", profiles={"minimal": {"hooks": {"cleanup": [hook, "base"]}}})
    phases = ("preInstall", "postInstall", "cleanup")
    make_chart(
        "app",
        profiles={
            "minimal": {
                "requires": [{"chart": "base", "profile": "minimal"}],
                "hooks": {phase: [hook, phase] for phase in phases},
            }
        },
        dependent_tests=[("web", "minimal")],
    )
    make_chart(
        "web",
        profiles={
            "minimal": {"requires": [{"chart": "app", "profile": "minimal"}], "helmTest": False}
        },
    )
    return chart_root


BASE = _on("base", "namespace-ensure", "install", "helm-test")
BASE_UNTESTED = _on("base", "namespace-ensure", "install")
APP_BODY = ("namespace-ensure", "hook-pre-install", "install", "hook-post-install", "helm-test")
APP = _on("app", *APP_BODY)
WEB = _on("web", "namespace-ensure", "install")
CLEANUPS = [*_on("app", "hook-cleanup"), *_on("base", "hook-cleanup")]
SKIPPED_BASE = SkippedRequirement("base", "minimal", "base", "default")


def _owned_warning(charts: str) -> str:
    return (
        EXTERNAL_BOOTSTRAP_WARNING_PREFIX
        + charts
        + "; environment-owned preparation/install actions were excluded from this executable plan"
    )


@pytest.mark.parametrize(
    ("request_fields", "owned", "requires", "steps", "warnings", "skipped"),
    [
        pytest.param({}, (), "install", [*BASE, *APP, *CLEANUPS], [], [], id="install"),
        pytest.param(
            {},
            (),
            "skip",
            [*APP, *_on("app", "hook-cleanup")],
            [SKIPPED_REQUIRES_WARNING_PREFIX + "base:minimal"],
            [SKIPPED_BASE],
            id="skip",
        ),
        pytest.param(
            {}, (), "install-untested", [*BASE_UNTESTED, *APP, *CLEANUPS], [], [], id="untested"
        ),
        pytest.param(
            {},
            ("base",),
            "install",
            [*APP, *_on("app", "hook-cleanup")],
            [_owned_warning("base")],
            [],
            id="bootstrap-owns-required",
        ),
        pytest.param(
            {},
            ("base",),
            "skip",
            [*APP, *_on("app", "hook-cleanup")],
            [_owned_warning("base")],
            [],
            id="bootstrap-owns-required-skip",
        ),
        pytest.param(
            {},
            ("app",),
            "install",
            [*BASE, *_on("app", "workload-ready", "helm-test"), *_on("base", "hook-cleanup")],
            [_owned_warning("app")],
            [],
            id="bootstrap-owns-selected",
        ),
        pytest.param(
            {"include_dependent_tests": True},
            (),
            "install-untested",
            [*BASE_UNTESTED, *APP, *WEB, *CLEANUPS],
            [],
            [],
            id="dependent-tests-untested",
        ),
        pytest.param(
            {"include_dependent_tests": True},
            (),
            "skip",
            [*APP, *WEB, *_on("app", "hook-cleanup")],
            [SKIPPED_REQUIRES_WARNING_PREFIX + "base:minimal"],
            [SKIPPED_BASE],
            id="dependent-tests-skip",
        ),
        pytest.param(
            {"namespace": "custom"},
            (),
            "install",
            [
                *BASE,
                *_on("app", *APP_BODY, namespace="custom"),
                *_on("app", "hook-cleanup", namespace="custom"),
                *_on("base", "hook-cleanup"),
            ],
            [],
            [],
            id="namespace-relocates-only-the-selected-chart",
        ),
        pytest.param(
            {"lint": True},
            (),
            "install",
            [
                *_on("base", "namespace-ensure", "helm-lint", "install", "helm-test"),
                *_on("app", "namespace-ensure", "helm-lint", *APP_BODY[1:]),
                *CLEANUPS,
            ],
            [],
            [],
            id="lint",
        ),
    ],
)
def test_compile_plan_decides_each_entrys_steps(
    charts: Path,
    request_fields: dict[str, Any],
    owned: tuple[str, ...],
    requires: Requires,
    steps: list[str],
    warnings: list[str],
    skipped: list[SkippedRequirement],
) -> None:
    request = ChartTestRequest(chart="app", profile="minimal", **request_fields)

    compiled = _compile(charts, request, owned=owned, requires=requires)

    assert _steps(compiled.plan) == steps
    assert list(compiled.plan.warnings) == warnings
    assert list(compiled.skipped) == skipped


def test_a_chart_resolving_to_two_namespaces_is_a_plan_error(charts: Path) -> None:
    request = ChartTestRequest(
        chart="app", profile="minimal", namespace="custom", include_dependent_tests=True
    )

    with pytest.raises(PlanError, match=r"app:minimal .*\(custom, default\)"):
        _compile(charts, request, owned=(), requires="install")


@pytest.mark.parametrize(
    ("chart_path", "profile", "namespace", "installs_base"),
    [
        ("charts/base", "minimal", "default", False),
        ("charts/base", "full", "default", True),
        ("charts/base", "minimal", "kube-system", True),
        ("elsewhere/base", "minimal", "default", True),
    ],
    ids=["exact", "other-profile", "other-namespace", "other-chart-path"],
)
def test_bootstrap_owns_a_requirement_only_under_its_exact_lifecycle_identity(
    chart_root: Path,
    make_chart: MakeChart,
    chart_path: str,
    profile: str,
    namespace: str,
    installs_base: bool,
) -> None:
    make_chart("base")
    make_chart("app", profiles={"minimal": {"requires": [{"chart": "base"}]}})
    identity = ExternallySatisfiedLifecycle(
        (chart_root / chart_path).resolve(), "base", profile, namespace
    )

    plan = compile_plan(
        ChartTestRequest(chart="app", profile="minimal"),
        root=chart_root,
        charts_dir=chart_root / "charts",
        bootstrap_owned=frozenset({identity}),
        requires="install",
    ).plan

    charts = {action.entry.chart.name for action in plan.actions}
    assert charts == ({"base", "app"} if installs_base else {"app"})


@pytest.mark.parametrize(
    ("executable", "message"),
    [
        pytest.param("/usr/bin/true", "relative", id="absolute"),
        pytest.param("scripts/../prepare", "without", id="parent-segment"),
        pytest.param("scripts/missing", "file does not exist", id="missing"),
        pytest.param("chart-manager-no-such-command", "not found on PATH", id="bare-not-on-path"),
    ],
)
def test_compile_rejects_an_unresolvable_hook_executable(
    chart_root: Path,
    make_chart: MakeChart,
    executable: str,
    message: str,
) -> None:
    _script(chart_root, "prepare")
    make_chart("app", profiles={"minimal": {"hooks": {"cleanup": [executable]}}})
    request = ChartTestRequest(chart="app", profile="minimal")

    with pytest.raises(SpecError, match=message) as excinfo:
        _compile(chart_root, request, owned=(), requires="install")
    assert "hooks.cleanup[0]" in str(excinfo.value)


def test_compile_accepts_a_bare_hook_executable_found_on_path(
    chart_root: Path,
    make_chart: MakeChart,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path_factory.mktemp("bin")
    _script(bin_dir, "mint-token")
    monkeypatch.setenv("PATH", str(bin_dir))
    make_chart("app", profiles={"minimal": {"hooks": {"preInstall": ["mint-token", "-q"]}}})
    request = ChartTestRequest(chart="app", profile="minimal")

    plan = _compile(chart_root, request, owned=(), requires="install").plan

    assert [a.command for a in plan.actions if a.command] == [("mint-token", "-q")]
