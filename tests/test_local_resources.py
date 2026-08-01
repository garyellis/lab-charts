from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.api.local.v1alpha1 import (
    BootstrapLifecycleRelease,
    BootstrapLocalChartRelease,
    BootstrapOciChartRelease,
    LifecycleRelease,
    OciChartRelease,
)
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.local_resources import (
    LocalResourceLoader,
    LocalTargetResolver,
    ResolvedChartTarget,
    ResolvedStackTarget,
    load_local_stack,
)

from .conftest import REPO_ROOT


def _write(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _chart(root: Path, relative: str, *, name: str = "demo", lifecycle: bool = True) -> Path:
    path = root / relative
    path.mkdir(parents=True)
    _write(
        root,
        f"{relative}/Chart.yaml",
        f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n",
    )
    if lifecycle:
        _write(
            root,
            f"{relative}/chart-lifecycle.yaml",
            f"""
apiVersion: lifecycle.cmg.io/v1alpha1
kind: ChartLifecycle
metadata: {{name: {name}}}
spec:
  clusterTest:
    profiles:
      kind: {{}}
      minimal: {{}}
""",
        )
    return path


def test_repository_default_local_cluster_is_available_to_fresh_checkouts() -> None:
    cluster = LocalResourceLoader(REPO_ROOT).load_cluster()

    assert cluster.metadata.name == "default"
    assert (REPO_ROOT / cluster.spec.cluster.config).is_file()
    assert cluster.spec.bootstrap.releases


def test_conventional_local_cluster_loads_ordered_bootstrap_releases(tmp_path: Path) -> None:
    _write(tmp_path, "kind.yaml", "kind: Cluster\n")
    _chart(tmp_path, "charts/demo")
    _chart(tmp_path, "charts/raw", name="raw", lifecycle=False)
    _write(tmp_path, "values/raw.yaml", "{}\n")
    _write(tmp_path, "values/remote.yaml", "{}\n")
    _write(
        tmp_path,
        ".chart-manager/local-cluster.yaml",
        """
apiVersion: local.cmg.io/v1alpha1
kind: LocalCluster
metadata: {name: local}
spec:
  cluster:
    config: kind.yaml
  bootstrap:
    releases:
      - {type: lifecycle, chart: charts/demo, profile: kind}
      - type: local
        name: raw
        chart: charts/raw
        namespace: platform
        values: [values/raw.yaml]
        timeout: 5m
        runtimeValues:
          server.host: ${kind.controlPlaneHost}
          server.port: ${kind.controlPlanePort}
        readiness:
          nodesReady: true
          workloadsReady: {namespace: platform, timeout: 2m}
      - type: oci
        name: cilium
        chart: oci://quay.io/cilium/charts/cilium
        version: 1.18.5
        namespace: kube-system
        values: [values/remote.yaml]
        timeout: 10m
""",
    )

    releases = LocalResourceLoader(tmp_path).load_cluster().spec.bootstrap.releases

    assert [type(release) for release in releases] == [
        BootstrapLifecycleRelease,
        BootstrapLocalChartRelease,
        BootstrapOciChartRelease,
    ]
    assert [release.type for release in releases] == ["lifecycle", "local", "oci"]
    raw = releases[1]
    assert isinstance(raw, BootstrapLocalChartRelease)
    assert raw.runtime_values == {
        "server.host": "${kind.controlPlaneHost}",
        "server.port": "${kind.controlPlanePort}",
    }
    assert raw.readiness is not None
    assert raw.readiness.nodes_ready is True
    assert raw.readiness.workloads_ready is not None
    assert raw.readiness.workloads_ready.namespace == "platform"


def test_stack_only_accepts_lifecycle_and_pinned_oci_releases(tmp_path: Path) -> None:
    stack = _write(
        tmp_path,
        "stack.yaml",
        """
apiVersion: local.cmg.io/v1alpha1
kind: LocalStack
metadata: {name: observability}
spec:
  releases:
    - {type: lifecycle, chart: charts/demo, profile: minimal}
    - type: oci
      name: prometheus
      chart: oci://example.test/charts/prometheus
      digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      namespace: monitoring
      values: []
      timeout: 10m
""",
    )

    resource = load_local_stack(stack)

    assert isinstance(resource.spec.releases[0], LifecycleRelease)
    assert isinstance(resource.spec.releases[1], OciChartRelease)

    bad = stack.read_text(encoding="utf-8").replace(
        "type: lifecycle, chart: charts/demo, profile: minimal",
        "type: local, chart: charts/demo, name: demo, namespace: demo, values: [], timeout: 1m",
    )
    stack.write_text(bad, encoding="utf-8")
    with pytest.raises(SpecError, match="union_tag_invalid"):
        load_local_stack(stack)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            "runtimeValues: {server.host: '${kind.unknown}'}",
            "Kind runtime placeholders",
        ),
        (
            "readiness: {nodesReady: 1}",
            "bool_type",
        ),
        (
            "readiness: {workloadsReady: {namespace: BAD, timeout: 1m}}",
            "lowercase DNS label",
        ),
        (
            "readiness: {workloadsReady: {namespace: demo, timeout: 0s}}",
            "greater than zero",
        ),
    ],
)
def test_bootstrap_runtime_and_readiness_are_strict(
    tmp_path: Path, extra: str, message: str
) -> None:
    _write(tmp_path, "kind.yaml", "kind: Cluster\n")
    _chart(tmp_path, "charts/demo")
    _write(
        tmp_path,
        ".chart-manager/local-cluster.yaml",
        f"""
apiVersion: local.cmg.io/v1alpha1
kind: LocalCluster
metadata: {{name: local}}
spec:
  cluster: {{config: kind.yaml}}
  bootstrap:
    releases:
      - type: lifecycle
        chart: charts/demo
        profile: kind
        {extra}
""",
    )

    with pytest.raises(SpecError, match=message):
        LocalResourceLoader(tmp_path).load_cluster()


@pytest.mark.parametrize("field", ["runtimeValues: {}", "readiness: {nodesReady: true}"])
def test_stack_rejects_bootstrap_only_contracts(tmp_path: Path, field: str) -> None:
    stack = _write(
        tmp_path,
        "stack.yaml",
        f"""
apiVersion: local.cmg.io/v1alpha1
kind: LocalStack
metadata: {{name: demo}}
spec:
  releases:
    - type: oci
      name: demo
      chart: oci://example.test/charts/demo
      version: 1.0.0
      namespace: demo
      values: []
      timeout: 1m
      {field}
""",
    )

    with pytest.raises(SpecError, match="extra_forbidden"):
        load_local_stack(stack)


@pytest.mark.parametrize(
    "pin",
    [
        "",
        "version: latest",
        "version: '1.2'",
        "digest: sha256:ABC",
        (
            "version: 1.2.3\n"
            "      digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    ],
)
def test_oci_release_requires_one_exact_pin(tmp_path: Path, pin: str) -> None:
    stack = _write(
        tmp_path,
        "bad.yaml",
        f"""
apiVersion: local.cmg.io/v1alpha1
kind: LocalStack
metadata: {{name: bad}}
spec:
  releases:
    - type: oci
      name: remote
      chart: oci://example.test/charts/remote
      {pin}
      namespace: remote
      values: []
      timeout: 1m
""",
    )

    with pytest.raises(SpecError, match=r"exactly one|exact SemVer|sha256"):
        load_local_stack(stack)


def test_raw_release_requires_explicit_helm_settings_and_safe_paths(tmp_path: Path) -> None:
    stack = _write(
        tmp_path,
        "bad.yaml",
        """
apiVersion: local.cmg.io/v1alpha1
kind: LocalStack
metadata: {name: bad}
spec:
  releases:
    - type: oci
      name: remote
      chart: oci://example.test/charts/remote
      version: 1.2.3
      namespace: remote
      values: [../outside.yaml]
""",
    )

    with pytest.raises(SpecError, match=r"release\.values.*repository-relative|timeout"):
        load_local_stack(stack)


def test_resolver_distinguishes_chart_named_stack_and_explicit_stack(tmp_path: Path) -> None:
    chart = _chart(tmp_path, "charts/demo")
    _write(tmp_path, "values/remote.yaml", "{}\n")
    stack = _write(
        tmp_path,
        ".chart-manager/stacks/observability.yaml",
        """
apiVersion: local.cmg.io/v1alpha1
kind: LocalStack
metadata: {name: observability}
spec:
  releases:
    - type: oci
      name: remote
      chart: oci://example.test/charts/remote
      version: 1.2.3
      namespace: monitoring
      values: [values/remote.yaml]
      timeout: 10m
""",
    )
    resolver = LocalTargetResolver(tmp_path)

    chart_target = resolver.resolve("charts/demo")
    named_target = resolver.resolve("observability")
    explicit_target = resolver.resolve(stack)

    assert isinstance(chart_target, ResolvedChartTarget)
    assert chart_target.path == chart
    assert isinstance(named_target, ResolvedStackTarget)
    assert named_target.path == stack
    assert explicit_target == named_target


def test_loader_rejects_missing_and_symlink_escaped_repository_paths(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-kind.yaml"
    outside.write_text("kind: Cluster\n", encoding="utf-8")
    (tmp_path / "escaped.yaml").symlink_to(outside)
    _write(
        tmp_path,
        ".chart-manager/local-cluster.yaml",
        """
apiVersion: local.cmg.io/v1alpha1
kind: LocalCluster
metadata: {name: local}
spec:
  cluster: {config: escaped.yaml}
  bootstrap: {releases: []}
""",
    )

    with pytest.raises(SpecError, match="escapes repository root"):
        LocalResourceLoader(tmp_path).load_cluster()


def test_custom_config_and_stack_directories_are_supported(tmp_path: Path) -> None:
    _write(tmp_path, "values/remote.yaml", "{}\n")
    _write(
        tmp_path,
        "config/compositions/demo.yaml",
        """
apiVersion: local.cmg.io/v1alpha1
kind: LocalStack
metadata: {name: demo}
spec:
  releases:
    - type: oci
      name: demo
      chart: oci://example.test/charts/demo
      version: 1.0.0
      namespace: demo
      values: [values/remote.yaml]
      timeout: 1m
""",
    )

    resolved = LocalTargetResolver(
        tmp_path,
        local_config=Path("config/local-dev.yaml"),
        stacks_dir=Path("compositions"),
    ).resolve("demo")

    assert isinstance(resolved, ResolvedStackTarget)
    assert resolved.path == tmp_path / "config/compositions/demo.yaml"
