"""`test.run()`: provision, bootstrap, then each chart's plan, fail-fast with diagnostics."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands import test
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError, MissingToolError
from chart_manager.shared.cluster.progress import ProgressEvent
from chart_manager.shared.settings import Settings
from chart_manager.shared.workspace import load_repository_workspace as load_workspace
from tests.conftest import FakeCommandRunner, MakeChart, argv_prefix, plain_argv

LOCAL_CLUSTER = """\
apiVersion: chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster: {config: kind-config.yaml}
  bootstrap:
    releases:
      - type: local
        name: cni
        chart: charts/cni
        namespace: kube-system
        values: []
        timeout: 2m
        readiness: {nodesReady: true}
"""


@pytest.fixture
def repo(chart_root: Path, make_chart: MakeChart) -> Path:
    (chart_root / "kind-config.yaml").write_text("kind: Cluster\n")
    cni = chart_root / "charts" / "cni"
    cni.mkdir(parents=True)
    (cni / "Chart.yaml").write_text("apiVersion: v2\nname: cni\nversion: 1.0.0\n")
    config = chart_root / ".chart-manager" / "local-cluster.yaml"
    config.write_text(LOCAL_CLUSTER)
    make_chart("db", profiles={"minimal": {"namespace": "data"}})
    make_chart(
        "app",
        profiles={
            "minimal": {
                "namespace": "apps",
                "helmTest": True,
                "requires": [{"chart": "db", "profile": "minimal"}],
            }
        },
    )
    return chart_root


def _runner(*clusters: str) -> FakeCommandRunner:
    return (
        FakeCommandRunner()
        .respond(argv_prefix("kind", "get", "clusters"), stdout="\n".join(clusters))
        .respond(argv_prefix("kubectl", "get", "--raw=/readyz"), stdout="ok")
    )


def _run(repo: Path, runner: FakeCommandRunner, **request: object) -> test.ChartTestOutcome:
    return test.run(
        test.ChartTestRequest(chart="app", cluster_name="lab", **request),  # type: ignore[arg-type]
        workspace=load_workspace(repo),
        runner=runner,
        settings=Settings(),
    )


def _steps(runner: FakeCommandRunner) -> list[tuple[str, ...]]:
    """Cluster-changing calls, in order: create, namespaces, installs, tests, node wait."""
    keep = (
        ("kind", "create"),
        ("kubectl", "create", "namespace"),
        ("helm", "upgrade"),
        ("helm", "test"),
        ("kubectl", "wait", "--for=condition=Ready"),
    )
    return [
        cmd
        for cmd in map(plain_argv, runner.calls)
        if any(cmd[: len(prefix)] == prefix for prefix in keep)
    ]


def test_run_provisions_bootstraps_then_installs_requires_first_and_tests_the_target(
    repo: Path,
) -> None:
    runner = _runner()

    outcome = _run(repo, runner)

    assert outcome.ok
    steps = [step[:4] for step in _steps(runner)]
    assert steps == [
        ("kind", "create", "cluster", "--name"),
        ("helm", "upgrade", "--install", "cni"),
        ("kubectl", "wait", "--for=condition=Ready", "nodes"),
        ("kubectl", "create", "namespace", "data"),
        ("helm", "upgrade", "--install", "db"),
        ("helm", "test", "db", "--namespace"),
        ("kubectl", "create", "namespace", "apps"),
        ("helm", "upgrade", "--install", "app"),
        ("helm", "test", "app", "--namespace"),
    ]
    assert [(a.kind, a.verdict) for a in outcome.actions][-1] == ("helm-test", "PASS")


def test_a_failed_install_fails_the_outcome_with_diagnostics_and_skips_the_rest(
    repo: Path,
) -> None:
    runner = (
        _runner()
        .respond(argv_prefix("helm", "upgrade", "--install", "db"), returncode=1, stderr="db broke")
        .respond(argv_prefix("kubectl", "get", "pods", "-n", "data"), stdout="db-0 Pending")
    )

    outcome = _run(repo, runner)

    assert not outcome.ok
    assert outcome.failed is not None
    assert outcome.failed.kind == "install"
    assert "db broke" in (outcome.failed.detail or "")
    assert "db-0 Pending" in outcome.diagnostics
    assert not any(step[:4] == ("helm", "upgrade", "--install", "app") for step in _steps(runner))
    assert {a.verdict for a in outcome.actions[-3:]} == {"SKIP"}


def test_a_failed_helm_test_is_a_failed_action_not_an_exception(repo: Path) -> None:
    runner = _runner().respond(argv_prefix("helm", "test", "app"), returncode=1, stderr="probe failed")

    outcome = _run(repo, runner)

    assert outcome.failed is not None
    assert outcome.failed.kind == "helm-test"
    assert "probe failed" in (outcome.failed.detail or "") or "exited 1" in (
        outcome.failed.detail or ""
    )


def test_a_missing_helm_binary_is_raised_as_itself(repo: Path) -> None:
    def helm_missing(argv: tuple[str, ...]) -> bool:
        if plain_argv(argv)[:2] == ("helm", "upgrade"):
            raise MissingToolError("helm not found")
        return False

    runner = _runner().respond(helm_missing)

    with pytest.raises(MissingToolError):
        _run(repo, runner)


def test_skip_requires_on_an_existing_cluster_verifies_instead_of_installing(
    repo: Path,
) -> None:
    listing = (
        '[{"name": "cni", "namespace": "kube-system", "revision": "1", "status": "deployed"},'
        ' {"name": "db", "namespace": "data", "revision": "1", "status": "failed"}]'
    )
    runner = _runner("lab").respond(
        argv_prefix("helm", "list", "-o", "json", "-A", "--all"), stdout=listing
    )

    outcome = _run(repo, runner, skip_requires=True)

    assert outcome.ok
    installs = [s[3] for s in _steps(runner) if s[:2] == ("helm", "upgrade")]
    assert installs == ["app"]


def test_skip_requires_names_a_missing_requirement_before_installing_anything(
    repo: Path,
) -> None:
    listing = '[{"name": "cni", "namespace": "kube-system", "revision": "1", "status": "deployed"}]'
    runner = _runner("lab").respond(
        argv_prefix("helm", "list", "-o", "json", "-A", "--all"), stdout=listing
    )

    with pytest.raises(ChartManagerError, match="app requires db:minimal, not installed in data"):
        _run(repo, runner, skip_requires=True)

    assert not any(s[:2] == ("helm", "upgrade") for s in _steps(runner))


def test_a_pre_install_hook_runs_after_its_namespace_and_before_its_install(
    repo: Path, make_chart: MakeChart
) -> None:
    make_chart(
        "app",
        profiles={"minimal": {"namespace": "apps", "hooks": {"preInstall": ["true", "pre"]}}},
    )
    runner = _runner()

    _run(repo, runner)

    calls = [plain_argv(argv) for argv in runner.calls]
    namespace = calls.index(("kubectl", "create", "namespace", "apps"))
    hook = calls.index(("true", "pre"))
    install = next(
        i for i, c in enumerate(calls) if c[:4] == ("helm", "upgrade", "--install", "app")
    )
    assert namespace < hook < install
    record = next(r for r in runner.records if r.args == ("true", "pre"))
    assert record.env is not None
    assert record.env["CHART_MANAGER_NAMESPACE"] == "apps"
    assert record.env["CHART_MANAGER_KUBE_CONTEXT"] == "kind-lab"


def test_teardown_runs_cleanup_hooks_then_deletes_the_cluster(
    repo: Path, make_chart: MakeChart
) -> None:
    make_chart(
        "app",
        profiles={"minimal": {"namespace": "apps", "hooks": {"cleanup": ["true", "bye"]}}},
    )
    runner = _runner("lab")

    outcome = test.teardown(
        test.TeardownRequest(chart="app", cluster_name="lab"),
        workspace=load_workspace(repo),
        runner=runner,
        settings=Settings(),
    )

    assert outcome.ok and outcome.cluster_deleted
    calls = [plain_argv(argv) for argv in runner.calls]
    assert calls.index(("true", "bye")) < calls.index(
        ("kind", "delete", "cluster", "--name", "lab")
    )


def test_plan_lists_namespace_install_and_helm_test_per_chart_requires_first(repo: Path) -> None:
    plan = test.plan(test.ChartTestRequest(chart="app"), workspace=load_workspace(repo))

    assert [(a.target.chart, a.kind.value) for a in plan.actions] == [
        ("db", "namespace-ensure"),
        ("db", "install"),
        ("db", "helm-test"),
        ("app", "namespace-ensure"),
        ("app", "install"),
        ("app", "helm-test"),
    ]


def test_no_ensure_cluster_attaches_without_creating_anything(repo: Path) -> None:
    runner = _runner()

    outcome = _run(repo, runner, ensure_cluster=False)

    assert outcome.ok
    assert not any(plain_argv(argv)[:2] in {("kind", "create"), ("kind", "get")} for argv in runner.calls)
    install = next(argv for argv in runner.calls if plain_argv(argv)[:2] == ("helm", "upgrade"))
    assert install[install.index("--kube-context") + 1] == "kind-lab"


def test_skip_requires_on_a_new_cluster_installs_requirements_but_tests_only_the_target(
    repo: Path,
) -> None:
    runner = _runner()

    outcome = _run(repo, runner, skip_requires=True)

    assert outcome.ok
    steps = _steps(runner)
    assert [s[3] for s in steps if s[:2] == ("helm", "upgrade")] == ["cni", "db", "app"]
    assert [s[2] for s in steps if s[:2] == ("helm", "test")] == ["app"]


def test_lint_runs_before_each_charts_install_and_fails_it(repo: Path) -> None:
    runner = _runner().respond(argv_prefix("helm", "lint", str(repo / "charts" / "app")), returncode=1)

    outcome = _run(repo, runner, lint=True)

    assert outcome.failed is not None and outcome.failed.kind == "helm-lint"
    installs = [s[3] for s in _steps(runner) if s[:2] == ("helm", "upgrade")]
    assert installs == ["cni", "db"]


def test_a_failed_pre_install_hook_stops_before_the_install(
    repo: Path, make_chart: MakeChart
) -> None:
    make_chart(
        "app",
        profiles={"minimal": {"namespace": "apps", "hooks": {"preInstall": ["true", "pre"]}}},
    )
    runner = _runner().respond(("true", "pre"), returncode=3, stderr="no token")

    outcome = _run(repo, runner)

    assert outcome.failed is not None and outcome.failed.kind == "hook-pre-install"
    assert not any(s[:4] == ("helm", "upgrade", "--install", "app") for s in _steps(runner))


def test_cleanup_hooks_are_left_for_teardown(repo: Path, make_chart: MakeChart) -> None:
    make_chart(
        "app",
        profiles={"minimal": {"namespace": "apps", "hooks": {"cleanup": ["true", "bye"]}}},
    )
    runner = _runner()

    outcome = _run(repo, runner)

    assert outcome.ok
    assert ("true", "bye") not in runner.calls
    assert outcome.actions[-1].kind == "hook-cleanup"
    assert outcome.actions[-1].verdict == "SKIP"


def _teardown(repo: Path, runner: FakeCommandRunner, **request: object) -> test.TeardownOutcome:
    return test.teardown(
        test.TeardownRequest(chart="app", cluster_name="lab", **request),  # type: ignore[arg-type]
        workspace=load_workspace(repo),
        runner=runner,
        settings=Settings(),
    )


@pytest.fixture
def with_cleanups(repo: Path, make_chart: MakeChart) -> Path:
    make_chart(
        "db",
        profiles={"minimal": {"namespace": "data", "hooks": {"cleanup": ["true", "db"]}}},
    )
    make_chart(
        "app",
        profiles={
            "minimal": {
                "namespace": "apps",
                "requires": [{"chart": "db", "profile": "minimal"}],
                "hooks": {"cleanup": ["true", "app"]},
            }
        },
    )
    return repo


def test_teardown_cleans_up_in_reverse_install_order_past_a_failure(with_cleanups: Path) -> None:
    runner = _runner("lab").respond(("true", "app"), returncode=1, stderr="gone")

    outcome = _teardown(with_cleanups, runner)

    hooks = [argv for argv in runner.calls if argv[0] == "true"]
    assert hooks == [("true", "app"), ("true", "db")]
    assert [c.verdict for c in outcome.cleanups] == ["FAIL", "PASS"]
    assert outcome.cluster_deleted and not outcome.ok


def test_teardown_keep_cluster_runs_cleanups_without_deleting(with_cleanups: Path) -> None:
    runner = _runner("lab")

    outcome = _teardown(with_cleanups, runner, keep_cluster=True)

    assert outcome.ok and not outcome.cluster_deleted
    assert not any(plain_argv(argv)[:2] == ("kind", "delete") for argv in runner.calls)


def test_teardown_of_a_missing_cluster_still_runs_cleanups(with_cleanups: Path) -> None:
    runner = _runner()

    outcome = _teardown(with_cleanups, runner)

    assert [c.verdict for c in outcome.cleanups] == ["PASS", "PASS"]
    assert not outcome.cluster_deleted
    record = next(r for r in runner.records if r.args == ("true", "app"))
    assert record.env is not None and record.env["CHART_MANAGER_KUBE_CONTEXT"] == ""


def test_teardown_reports_a_failed_delete(with_cleanups: Path) -> None:
    runner = _runner("lab").respond(argv_prefix("kind", "delete"), returncode=1, stderr="busy")

    outcome = _teardown(with_cleanups, runner)

    assert outcome.delete_error is not None and "busy" in outcome.delete_error
    assert not outcome.ok


def test_a_failed_bootstrap_release_is_raised_as_a_tool_error_after_its_diagnostics(
    repo: Path,
) -> None:
    runner = (
        _runner()
        .respond(argv_prefix("helm", "upgrade", "--install", "cni"), returncode=1, stderr="no cni")
        .respond(argv_prefix("kubectl", "get", "pods", "-n", "kube-system"), stdout="cni-0 Pending")
    )
    events: list[ProgressEvent] = []

    with pytest.raises(ExternalCommandError, match="no cni"):
        test.run(
            test.ChartTestRequest(chart="app", cluster_name="lab"),
            workspace=load_workspace(repo),
            runner=runner,
            settings=Settings(),
            progress=events.append,
        )

    assert any("cni-0 Pending" in e.message for e in events)
    assert not any(s[:4] == ("helm", "upgrade", "--install", "db") for s in _steps(runner))
