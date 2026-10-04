"""`chart test` plan compilation: action order, inputs, digests and hooks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.commands.test.models import ActionKind, LifecycleAction, LifecyclePlan
from chart_manager.commands.test.plan import compile_chart_test
from chart_manager.commands.test.wire import plan_to_dict
from chart_manager.plumbing.errors import (
    ChartManagerError,
    DependencyCycleError,
    SpecError,
)
from chart_manager.shared.charts.chart_tests import ChartTestCatalog
from chart_manager.shared.charts.install_plan import DependencyResolver
from tests.conftest import MakeChart

CHARTS_DIR = Path("charts")


def _compile(root: Path, chart: str, profile: str, **options: object) -> LifecyclePlan:
    catalog = ChartTestCatalog(root, charts_dir=CHARTS_DIR)
    return compile_chart_test(
        chart,
        profile,
        root=root.resolve(),
        catalog=catalog,
        resolver=DependencyResolver(catalog.get),
        **options,  # type: ignore[arg-type]
    )


def _by_id(plan: LifecyclePlan, action_id: str) -> LifecycleAction:
    return next(action for action in plan.actions if action.action_id == action_id)


def _requires(*refs: str) -> dict[str, object]:
    parsed = []
    for ref in refs:
        chart, _, profile = ref.partition(":")
        parsed.append({"chart": chart, "profile": profile or "minimal"})
    return {"requires": parsed}


def test_cluster_test_compiles_dependency_first_actions_and_effective_inputs(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart(
        "base",
        profiles={
            "minimal": {
                "namespace": "operators",
                "timeout": "7m",
                "values": ["values.yaml"],
            }
        },
    )
    make_chart(
        "app",
        profiles={
            "full": {
                **_requires("base"),
                "namespace": "workloads",
                "timeout": "20m",
                "values": ["values.yaml", "values-full.yaml"],
            }
        },
    )

    plan = _compile(chart_root, "app", "full")

    assert [action.target.chart for action in plan.actions] == [
        *(["base"] * 3),
        *(["app"] * 3),
    ]
    app_install = next(
        action
        for action in plan.actions
        if action.target.chart == "app" and action.kind is ActionKind.INSTALL
    )
    assert app_install.target.namespace == "workloads"
    assert app_install.timeout == "20m"
    assert [path.name for path in app_install.values] == [
        "values.yaml",
        "values-full.yaml",
    ]
    assert app_install.metadata == ()
    assert all(action.metadata == () for action in plan.actions)


def test_cluster_test_namespace_override_wins_over_authored_profile(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app", profiles={"minimal": {"namespace": "authored"}})

    plan = _compile(
        chart_root,
        "app",
        "minimal",
        namespace_override="requested",
    )

    assert {
        action.target.namespace for action in plan.actions if action.target.namespace is not None
    } == {"requested"}


def test_cluster_test_namespace_override_does_not_relocate_authored_dependency(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("base", profiles={"minimal": {"namespace": "foundation"}})
    make_chart(
        "app",
        profiles={
            "minimal": {
                "namespace": "authored-app",
                "requires": [{"chart": "base", "profile": "minimal"}],
            }
        },
    )

    plan = _compile(
        chart_root,
        "app",
        "minimal",
        namespace_override="requested-app",
    )

    namespaces = {
        action.target.chart: action.target.namespace
        for action in plan.actions
        if action.kind is ActionKind.INSTALL
    }
    assert namespaces == {"base": "foundation", "app": "requested-app"}


def test_cluster_test_without_helm_test_ends_at_its_install(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app", profiles={"minimal": {"helmTest": False}})

    plan = _compile(chart_root, "app", "minimal")

    assert [action.kind for action in plan.actions] == [
        ActionKind.NAMESPACE_ENSURE,
        ActionKind.INSTALL,
    ]


def test_cluster_test_lint_runs_between_the_namespace_and_the_install(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app")

    plan = _compile(chart_root, "app", "minimal", lint=True)

    assert [action.kind for action in plan.actions] == [
        ActionKind.NAMESPACE_ENSURE,
        ActionKind.HELM_LINT,
        ActionKind.INSTALL,
        ActionKind.HELM_TEST,
    ]
    lint = next(action for action in plan.actions if action.kind is ActionKind.HELM_LINT)
    assert {path.name for path in lint.values} == {"values.yaml"}


def test_plan_projection_is_deterministic_and_json_serializable(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app")

    first = plan_to_dict(_compile(chart_root, "app", "minimal"))
    second = plan_to_dict(_compile(chart_root, "app", "minimal"))

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["chart"] == "app"
    assert first["profile"] == "minimal"
    assert first["actions"][0]["action_id"].startswith("chart-test.app.minimal.")
    assert first["actions"][0]["target"]["chart"] == "app"
    assert "edges" not in first


def test_generated_dependency_contents_do_not_change_compiled_input_digest(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    before = _compile(chart_root, "app", "minimal")

    generated = chart / "charts"
    generated.mkdir()
    (generated / "dependency-1.2.3.tgz").write_bytes(b"downloaded later")
    after = _compile(chart_root, "app", "minimal")

    assert [action.input_digest for action in before.actions] == [
        action.input_digest for action in after.actions
    ]

    templates = chart / "templates"
    templates.mkdir()
    (templates / "deployment.yaml").write_text("kind: Deployment\n")
    source_changed = _compile(chart_root, "app", "minimal")

    assert [action.input_digest for action in after.actions] != [
        action.input_digest for action in source_changed.actions
    ]

    (chart / "Chart.lock").write_text("dependencies: []\n")
    lock_changed = _compile(chart_root, "app", "minimal")
    assert [action.input_digest for action in source_changed.actions] != [
        action.input_digest for action in lock_changed.actions
    ]


def test_digest_rejects_value_symlink_that_escapes_repository_root(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    chart = make_chart("app")
    outside = chart_root.parent / "outside-values.yaml"
    outside.write_text("{}\n")
    values = chart / "values.yaml"
    values.unlink()
    values.symlink_to(outside)

    with pytest.raises(SpecError, match="digest input escapes repository root"):
        _compile(chart_root, "app", "minimal")


def test_compile_rejects_a_requires_cycle(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    """A `requires` cycle fails at compile time, not silently mid-install.

    This and the two tests below are what remains of the deleted `lifecycle
    doctor` command. Doctor checked the whole repository up front; the
    compiler checks the chart:profile actually being compiled. Since every
    execution path (`charts test`, `local up`) compiles before
    it mutates anything, a broken reference on a chart anyone exercises still
    fails loudly -- see `DependencyResolver.install_plan`.
    """
    make_chart("a", profiles={"minimal": _requires("b")})
    make_chart("b", profiles={"minimal": _requires("a")})

    with pytest.raises(DependencyCycleError, match="dependency cycle detected"):
        _compile(chart_root, "a", "minimal")


def test_compile_rejects_an_unknown_chart_reference(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("a", profiles={"minimal": _requires("missing")})

    with pytest.raises(ChartManagerError):
        _compile(chart_root, "a", "minimal")


def test_compile_rejects_an_unknown_profile_reference(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("base")
    make_chart("a", profiles={"minimal": _requires("base:nope")})

    with pytest.raises(SpecError, match="unknown profile 'nope'"):
        _compile(chart_root, "a", "minimal")


def test_compile_accepts_a_valid_requires_graph(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("base")
    make_chart("app", profiles={"minimal": _requires("base")})

    plan = _compile(chart_root, "app", "minimal")

    assert [action.target.chart for action in plan.actions].count("base") >= 1


# --- chart-test hooks ------------------------------------------------------


def _script(root: Path, relative: str, body: str = "#!/bin/sh\n") -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return relative


def _hooks(root: Path, chart: str, *phases: str) -> dict[str, object]:
    return {
        "hooks": {phase: [_script(root, f"scripts/{chart}-{phase}"), chart] for phase in phases}
    }


_ALL_PHASES = ("preInstall", "postInstall", "cleanup")


def _kinds(plan: LifecyclePlan) -> list[tuple[str, ActionKind]]:
    return [(action.target.chart, action.kind) for action in plan.actions]


def test_hooks_wrap_install_and_cleanups_form_a_reverse_install_order_tail(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("base", profiles={"minimal": _hooks(chart_root, "base", *_ALL_PHASES)})
    make_chart(
        "app",
        profiles={"minimal": {**_requires("base"), **_hooks(chart_root, "app", *_ALL_PHASES)}},
    )

    plan = _compile(chart_root, "app", "minimal", lint=True)

    body = [
        ActionKind.NAMESPACE_ENSURE,
        ActionKind.HELM_LINT,
        ActionKind.HOOK_PRE_INSTALL,
        ActionKind.INSTALL,
        ActionKind.HOOK_POST_INSTALL,
        ActionKind.HELM_TEST,
    ]
    assert _kinds(plan) == [
        *(("base", kind) for kind in body),
        *(("app", kind) for kind in body),
        ("app", ActionKind.HOOK_CLEANUP),
        ("base", ActionKind.HOOK_CLEANUP),
    ]
    pre = _by_id(plan, "chart-test.app.minimal.hook-pre-install")
    assert pre.command == ("scripts/app-preInstall", "app")
    assert pre.values == ()
    assert pre.target.namespace == "default"
    assert pre.timeout == "10m"  # the profile's timeout bounds its hooks too
    assert plan_to_dict(plan)["actions"][2]["command"] == ["scripts/base-preInstall", "base"]


def test_undeclared_hooks_compile_no_hook_actions(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart("app", profiles={"minimal": _hooks(chart_root, "app", "postInstall")})
    make_chart("plain")

    with_post = _compile(chart_root, "app", "minimal")
    plain = _compile(chart_root, "plain", "minimal")

    assert [kind for _chart, kind in _kinds(with_post)] == [
        ActionKind.NAMESPACE_ENSURE,
        ActionKind.INSTALL,
        ActionKind.HOOK_POST_INSTALL,
        ActionKind.HELM_TEST,
    ]
    assert all(action.command == () for action in plain.actions)
    assert all(payload["command"] == [] for payload in plan_to_dict(plain)["actions"])


def test_dependency_installed_under_its_own_profile_carries_its_own_hooks(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    make_chart(
        "base",
        profiles={
            "minimal": {},
            "secured": _hooks(chart_root, "base", "preInstall", "cleanup"),
        },
    )
    make_chart("app", profiles={"minimal": _requires("base:secured")})

    plan = _compile(chart_root, "app", "minimal")

    hooks = [(action.action_id, action.command) for action in plan.actions if action.command]
    assert hooks == [
        ("chart-test.base.secured.hook-pre-install", ("scripts/base-preInstall", "base")),
        ("chart-test.base.secured.hook-cleanup", ("scripts/base-cleanup", "base")),
    ]
    assert plan.actions[-1].kind is ActionKind.HOOK_CLEANUP


def test_hook_digest_covers_argv_and_repo_script_content(
    chart_root: Path,
    make_chart: MakeChart,
) -> None:
    script = _script(chart_root, "scripts/prepare", "#!/bin/sh\necho one\n")

    def pre_digest(argv: list[str]) -> str:
        make_chart("app", profiles={"minimal": {"hooks": {"preInstall": argv}}})
        plan = _compile(chart_root, "app", "minimal")
        return _by_id(plan, "chart-test.app.minimal.hook-pre-install").input_digest

    original = pre_digest([script, "--flag"])
    assert pre_digest([script, "--flag"]) == original
    assert pre_digest([script, "--other"]) != original

    edited_before = pre_digest([script, "--flag"])
    _script(chart_root, "scripts/prepare", "#!/bin/sh\necho two\n")
    assert pre_digest([script, "--flag"]) != edited_before


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

    with pytest.raises(SpecError, match=message) as excinfo:
        _compile(chart_root, "app", "minimal")
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

    plan = _compile(chart_root, "app", "minimal")

    assert _by_id(plan, "chart-test.app.minimal.hook-pre-install").command == (
        "mint-token",
        "-q",
    )
