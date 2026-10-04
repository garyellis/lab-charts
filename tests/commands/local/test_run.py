"""`local up/reset/down/status`: converge a chart or stack onto the persistent kind cluster."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.commands import local
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.shared.charts.chart import ResolvedChartTarget
from chart_manager.shared.cluster.progress import ProgressEvent
from chart_manager.shared.settings import Settings
from chart_manager.shared.workspace import load_repository_workspace
from tests.conftest import FakeCommandRunner, MakeChart
from tests.shared.cluster.test_converge import _cmd, _is

LOCAL_CLUSTER = """\
apiVersion: chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster:
    config: kind-config.yaml
    hooks: {preProvision: ["true", pre]}
  bootstrap:
    releases:
      - type: local
        name: cni
        chart: charts/cni
        namespace: kube-system
        values: []
        timeout: 2m
"""


@pytest.fixture
def repo(chart_root: Path, make_chart: MakeChart) -> Path:
    (chart_root / "kind-config.yaml").write_text("kind: Cluster\n")
    cni = chart_root / "charts" / "cni"
    cni.mkdir(parents=True)
    (cni / "Chart.yaml").write_text("apiVersion: v2\nname: cni\nversion: 1.0.0\n")
    (chart_root / ".chart-manager" / "local-cluster.yaml").write_text(LOCAL_CLUSTER)
    make_chart("db", profiles={"minimal": {"namespace": "data"}})
    make_chart(
        "app",
        profiles={
            "minimal": {"namespace": "apps", "requires": [{"chart": "db", "profile": "minimal"}]}
        },
    )
    return chart_root


def _runner(*clusters: str) -> FakeCommandRunner:
    return (
        FakeCommandRunner()
        .respond(_is("kind", "get", "clusters"), stdout="\n".join(clusters))
        .respond(_is("kubectl", "get", "--raw=/readyz"), stdout="ok")
        .respond(_is("kubectl", "get", "virtualservices"), returncode=1)
    )


def _target(repo: Path) -> ResolvedChartTarget:
    return ResolvedChartTarget(name="app", path=(repo / "charts" / "app").resolve())


def _installs(runner: FakeCommandRunner) -> list[str]:
    return [_cmd(a)[3] for a in runner.calls if _cmd(a)[:3] == ("helm", "upgrade", "--install")]


def _up(repo: Path, runner: FakeCommandRunner, **options: object) -> local.DevelopmentClusterResult:
    return local.up(
        _target(repo),
        workspace=load_repository_workspace(repo),
        runner=runner,
        settings=Settings(),
        **options,  # type: ignore[arg-type]
    )


def test_up_provisions_bootstraps_and_converges_the_chart_after_its_requirements(
    repo: Path,
) -> None:
    runner = _runner()

    result = _up(repo, runner)

    assert result.ok
    assert _installs(runner) == ["cni", "db", "app"]
    assert [(o.chart, o.namespace) for o in result.applied] == [
        ("cni", "kube-system"),
        ("db", "data"),
        ("app", "apps"),
    ]
    assert _cmd(next(a for a in runner.calls if _cmd(a)[:2] == ("kind", "create")))[:3] == (
        "kind",
        "create",
        "cluster",
    )


def test_up_records_a_failed_release_and_keeps_converging(repo: Path) -> None:
    runner = _runner().respond(_is("helm", "upgrade", "--install", "db"), returncode=1)

    result = _up(repo, runner)

    assert not result.ok
    assert [(f.chart, f.namespace) for f in result.failed] == [("db", "data")]
    assert "app" in _installs(runner)


def test_skip_installed_skips_deployed_and_failed_releases(repo: Path) -> None:
    listing = (
        '[{"name": "db", "namespace": "data", "revision": "1", "status": "failed"},'
        ' {"name": "app", "namespace": "apps", "revision": "1", "status": "pending-install"}]'
    )
    runner = _runner("lab").respond(
        _is("helm", "list", "-o", "json", "-A", "--all"), stdout=listing
    )

    result = _up(repo, runner, skip_installed=True)

    assert _installs(runner) == ["cni", "app"]
    assert [o.chart for o in result.no_change] == ["db"]


def test_reset_runs_the_pre_hook_once_then_deletes_and_recreates_the_cluster(repo: Path) -> None:
    runner = _runner("chart-manager")

    local.reset(
        _target(repo),
        workspace=load_repository_workspace(repo),
        runner=runner,
        settings=Settings(),
        run_hooks=True,
    )

    calls = [_cmd(a) for a in runner.calls]
    assert calls.count(("true", "pre")) == 1
    pre = calls.index(("true", "pre"))
    delete = calls.index(("kind", "delete", "cluster", "--name", "chart-manager"))
    assert pre < delete
    assert _installs(runner) == ["cni", "db", "app"]


def test_down_stops_the_running_nodes(repo: Path) -> None:
    runner = FakeCommandRunner().respond(
        lambda argv: argv[:2] == ("docker", "ps"), stdout="chart-manager-control-plane\n"
    )

    result = local.down(runner=runner, settings=Settings())

    assert result.changed
    assert ("docker", "stop", "chart-manager-control-plane") in runner.calls


def test_status_of_an_absent_cluster_says_so_and_asks_nothing_else(repo: Path) -> None:
    runner = _runner()

    status = local.status(
        workspace=load_repository_workspace(repo), runner=runner, settings=Settings()
    )

    assert not status.exists
    assert all(_cmd(a)[0] == "kind" for a in runner.calls)


def test_status_lists_releases_sorted_by_namespace_and_name(repo: Path) -> None:
    listing = (
        '[{"name": "web", "namespace": "z", "revision": "2", "status": "deployed"},'
        ' {"name": "db", "namespace": "a", "revision": "1", "status": "failed"}]'
    )
    runner = _runner("chart-manager").respond(_is("helm", "list"), stdout=listing)

    status = local.status(
        workspace=load_repository_workspace(repo), runner=runner, settings=Settings()
    )

    assert status.exists and status.context == "kind-chart-manager"
    assert [(r.namespace, r.name, r.status) for r in status.releases] == [
        ("a", "db", "failed"),
        ("z", "web", "deployed"),
    ]


def test_plan_lists_bootstrap_and_target_releases_and_touches_nothing(repo: Path) -> None:
    plan = local.plan(_target(repo), workspace=load_repository_workspace(repo), profile=None)

    assert [(e.chart, e.source) for e in plan.entries] == [
        ("db", "target"),
        ("app", "target"),
    ]
    assert plan.provisioning_hooks == (("preProvision", ("true", "pre")),)


def test_plan_fails_on_an_unresolvable_profile_like_the_real_run(repo: Path) -> None:
    with pytest.raises(ChartManagerError):
        local.plan(_target(repo), workspace=load_repository_workspace(repo), profile="missing")


def test_plan_warns_that_local_up_does_not_run_cluster_test_hooks(
    repo: Path, make_chart: MakeChart
) -> None:
    make_chart(
        "app",
        profiles={"minimal": {"namespace": "apps", "hooks": {"preInstall": ["true", "x"]}}},
    )
    events: list[ProgressEvent] = []

    local.plan(
        _target(repo),
        workspace=load_repository_workspace(repo),
        profile=None,
        progress=events.append,
    )

    assert [e.message for e in events if e.severity == "warn"] == [
        "local up does not run cluster-test hooks declared by app:minimal"
    ]


def test_plan_leaves_out_a_requirement_bootstrap_installs(repo: Path) -> None:
    config = repo / ".chart-manager" / "local-cluster.yaml"
    config.write_text(
        config.read_text().replace(
            "      - type: local\n        name: cni",
            "      - type: lifecycle\n        chart: charts/db\n        profile: minimal\n"
            "      - type: local\n        name: cni",
        )
    )

    plan = local.plan(_target(repo), workspace=load_repository_workspace(repo), profile=None)

    assert [(e.chart, e.source) for e in plan.entries] == [("db", "bootstrap"), ("app", "target")]


def test_status_records_a_failed_release_listing_instead_of_raising(repo: Path) -> None:
    runner = _runner("chart-manager").respond(
        _is("helm", "list"), returncode=1, stderr="unreachable"
    )

    status = local.status(
        workspace=load_repository_workspace(repo), runner=runner, settings=Settings()
    )

    assert status.exists
    assert status.releases_error is not None and "unreachable" in status.releases_error


def test_status_survives_a_repository_with_no_local_cluster(chart_root: Path) -> None:
    status = local.status(
        workspace=load_repository_workspace(chart_root),
        runner=_runner("chart-manager"),
        settings=Settings(),
    )

    assert status.exists
    assert status.drift.error is None


def test_a_failed_bootstrap_release_stops_up_after_printing_its_diagnostics(repo: Path) -> None:
    runner = (
        _runner()
        .respond(_is("helm", "upgrade", "--install", "cni"), returncode=1, stderr="no cni")
        .respond(_is("kubectl", "get", "pods", "-n", "kube-system"), stdout="cni-0 Pending")
    )
    events: list[ProgressEvent] = []

    with pytest.raises(ExternalCommandError, match="no cni"):
        _up(repo, runner, progress=events.append)

    assert any("cni-0 Pending" in e.message for e in events)
    assert _installs(runner) == ["cni"]
