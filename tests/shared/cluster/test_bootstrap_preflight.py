"""`bootstrap.preflight()`: resolve and lint bootstrap charts before the cluster is touched."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError, SpecError
from chart_manager.shared.cluster import bootstrap
from chart_manager.shared.cluster.bootstrap import ExternallySatisfiedLifecycle


def _cluster(releases: list[dict[str, object]]) -> LocalCluster:
    return LocalCluster.model_validate(
        {
            "apiVersion": "chartmanager.io/v1alpha1",
            "kind": "LocalCluster",
            "metadata": {"name": "default"},
            "spec": {
                "cluster": {"config": "kind-config.yaml"},
                "bootstrap": {"releases": releases},
            },
        }
    )


class _Helm:
    timeout = None

    def __init__(self, *, fail_lint: bool = False) -> None:
        self.fail_lint = fail_lint
        self.dependencies: list[Path] = []
        self.lints: list[tuple[Path, list[Path]]] = []

    def dependency_update(self, chart: Path, *, timeout: float) -> None:
        self.dependencies.append(chart)

    def lint(self, chart: Path, values: list[Path] | None = None) -> None:
        self.lints.append((chart, values or []))
        if self.fail_lint:
            raise ExternalCommandError("lint failed")


def test_raw_local_and_oci_releases_never_claim_managed_lifecycle_identity(
    tmp_path: Path,
) -> None:
    (tmp_path / "charts/network").mkdir(parents=True)
    cluster = _cluster(
        [
            {
                "type": "local",
                "name": "network",
                "chart": "charts/network",
                "namespace": "kube-system",
                "values": [],
                "timeout": "5m",
            },
            {
                "type": "oci",
                "name": "network",
                "chart": "oci://registry.example.test/charts/network",
                "version": "1.2.3",
                "namespace": "monitoring",
                "values": [],
                "timeout": "5m",
            },
        ]
    )

    assert bootstrap.preflight(cluster, root=tmp_path) == frozenset()


def test_preflight_resolves_bootstrap_lifecycle_identities(tmp_path: Path) -> None:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (chart / "chart-lifecycle.yaml").write_text(
        """
apiVersion: chartmanager.io/v1alpha1
kind: ChartLifecycle
metadata: {name: network}
spec:
  chartTest:
    profiles:
      minimal:
        namespace: kube-system
        values: []
""".lstrip(),
        encoding="utf-8",
    )
    cluster = _cluster(
        [
            {
                "type": "lifecycle",
                "chart": "charts/network",
                "profile": "minimal",
            }
        ]
    )

    identities = bootstrap.preflight(cluster, root=tmp_path)

    assert identities == frozenset(
        {
            ExternallySatisfiedLifecycle(
                chart_path=chart.resolve(),
                chart="network",
                profile="minimal",
                namespace="kube-system",
            )
        }
    )


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        # `preflight` publishes ownership as an identity that includes the
        # namespace and excludes by exact identity, so a bootstrap-owned chart
        # must author its namespace and is rejected at load without one.
        pytest.param(
            "      minimal: {values: []}\n",
            r"profiles\.minimal\.namespace",
            id="no-namespace",
        ),
        # Bootstrap installs outside the compiled plan, so hooks would never run.
        pytest.param(
            "      minimal:\n"
            "        namespace: kube-system\n"
            "        values: []\n"
            "        hooks: {preInstall: [./scripts/credential]}\n",
            r"bootstrap chart network:minimal declares chart-test hooks",
            id="declares-hooks",
        ),
    ],
)
def test_preflight_rejects_a_lifecycle_profile_bootstrap_cannot_own(
    tmp_path: Path, profile: str, message: str
) -> None:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (chart / "chart-lifecycle.yaml").write_text(
        "apiVersion: chartmanager.io/v1alpha1\n"
        "kind: ChartLifecycle\n"
        "metadata: {name: network}\n"
        "spec:\n"
        "  chartTest:\n"
        "    profiles:\n" + profile,
        encoding="utf-8",
    )
    cluster = _cluster([{"type": "lifecycle", "chart": "charts/network", "profile": "minimal"}])

    with pytest.raises(SpecError, match=message):
        bootstrap.preflight(cluster, root=tmp_path)


def test_a_lifecycle_release_pointing_at_a_foreign_chart_is_rejected(
    tmp_path: Path,
) -> None:
    """The path-identity check that makes a per-release catalog safe.

    `charts/network/Chart.yaml` declaring someone else's name means the
    catalog resolves that name to the other tree, and bootstrap would install
    a chart the LocalCluster never named. Shared with `local up`
    through `lifecycle_install_plan`, so this covers both.
    """
    chart = tmp_path / "charts/network"
    other = tmp_path / "charts/other"
    for path, name in ((chart, "other"), (other, "other")):
        path.mkdir(parents=True)
        (path / "Chart.yaml").write_text(
            f"apiVersion: v2\nname: {name}\nversion: 1.0.0\n",
            encoding="utf-8",
        )
    (other / "chart-lifecycle.yaml").write_text(
        "apiVersion: chartmanager.io/v1alpha1\n"
        "kind: ChartLifecycle\n"
        "metadata: {name: other}\n"
        "spec:\n"
        "  chartTest:\n"
        "    profiles:\n"
        "      minimal: {namespace: kube-system, values: []}\n",
        encoding="utf-8",
    )
    cluster = _cluster(
        [{"type": "lifecycle", "chart": "charts/network", "profile": "minimal"}]
    )

    with pytest.raises(ChartManagerError, match="bootstrap chart 'other' does not match"):
        bootstrap.preflight(cluster, root=tmp_path)


def test_bootstrap_lint_failure_prevents_any_install(tmp_path: Path) -> None:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n"
        "dependencies: [{name: cni, version: 1.0.0, repository: 'https://example.test'}]\n",
        encoding="utf-8",
    )
    (chart / "chart-lifecycle.yaml").write_text(
        """
apiVersion: chartmanager.io/v1alpha1
kind: ChartLifecycle
metadata: {name: network}
spec:
  chartTest:
    profiles:
      minimal: {namespace: kube-system, values: []}
""".lstrip(),
        encoding="utf-8",
    )
    cluster = _cluster(
        [{"type": "lifecycle", "chart": "charts/network", "profile": "minimal"}]
    )
    helm = _Helm(fail_lint=True)

    with pytest.raises(ExternalCommandError, match="lint failed"):
        bootstrap.preflight(cluster, root=tmp_path, helm=helm)  # type: ignore[arg-type]

    assert len(helm.lints) == 1
    assert helm.dependencies == [chart]


def test_preflight_resolves_every_release_before_linting_any(
    tmp_path: Path,
) -> None:
    chart = tmp_path / "charts/network"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: network\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (chart / "chart-lifecycle.yaml").write_text(
        """
apiVersion: chartmanager.io/v1alpha1
kind: ChartLifecycle
metadata: {name: network}
spec:
  chartTest:
    profiles:
      minimal: {namespace: kube-system, values: []}
""".lstrip(),
        encoding="utf-8",
    )
    cluster = _cluster(
        [
            {"type": "lifecycle", "chart": "charts/network", "profile": "minimal"},
            {"type": "lifecycle", "chart": "charts/network", "profile": "missing"},
        ]
    )
    helm = _Helm()

    with pytest.raises(ChartManagerError, match="unknown profile 'missing'"):
        bootstrap.preflight(cluster, root=tmp_path, helm=helm)  # type: ignore[arg-type]

    assert helm.lints == []
