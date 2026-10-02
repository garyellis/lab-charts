"""Repository workspace loading, discovery, matching, and safety invariants."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chart_manager.api.v1alpha1.chart_workspace import ChartWorkspace
from chart_manager.composition import Container
from chart_manager.domain.charts import ChartRepository
from chart_manager.domain.workspace import (
    LEGACY_CHARTS_DIR,
    LEGACY_LOCAL_CLUSTER,
    SCHEMA_LOCK_FILE,
    WORKSPACE_FILE,
    RepositoryWorkspace,
    discover_workspace_root,
    load_repository_workspace,
    resolve_repository_root,
)
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.grafana.dashboard_lint import discover_dashboards
from chart_manager.services.manifest_validation.paths import RenderOutputService
from chart_manager.settings import Settings


def _document(**spec: object) -> dict[str, object]:
    return {
        "apiVersion": "chartmanager.io/v1alpha1",
        "kind": "ChartWorkspace",
        "metadata": {"name": "example"},
        "spec": {
            "chartsDir": "charts",
            "localCluster": ".chart-manager/local-cluster.yaml",
            "renderDir": ".chart-manager/rendered",
            "policiesDir": "policies",
            **spec,
        },
    }


def _write_workspace(root: Path, text: str | None = None) -> Path:
    marker = root / WORKSPACE_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        text
        or """apiVersion: chartmanager.io/v1alpha1
kind: ChartWorkspace
metadata: {name: example}
spec:
  chartsDir: charts
  localCluster: .chart-manager/local-cluster.yaml
  renderDir: .chart-manager/rendered
  policiesDir: policies
""",
        encoding="utf-8",
    )
    return marker


def test_nearest_ancestor_marker_is_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "nested/repo"
    leaf = inner / "charts/app/templates"
    _write_workspace(outer)
    _write_workspace(inner)
    leaf.mkdir(parents=True)

    assert discover_workspace_root(leaf) == inner
    assert resolve_repository_root(configured=None, start=leaf) == inner
    monkeypatch.chdir(leaf)
    assert Container(Settings()).workspace().root == inner


def test_configured_root_precedes_nearest_marker(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    discovered = tmp_path / "discovered"
    leaf = discovered / "nested"
    _write_workspace(configured)
    _write_workspace(discovered)
    leaf.mkdir()

    assert resolve_repository_root(configured=configured, start=leaf) == configured


def test_missing_marker_uses_start_as_legacy_root(tmp_path: Path) -> None:
    assert resolve_repository_root(configured=None, start=tmp_path) == tmp_path
    workspace = load_repository_workspace(tmp_path)
    assert not workspace.authored
    assert workspace.name == tmp_path.name
    assert workspace.charts_dir == Path("charts")


def test_authored_workspace_is_authoritative_over_explicit_legacy_settings(
    tmp_path: Path,
) -> None:
    _write_workspace(tmp_path)

    with pytest.raises(SpecError, match="authoritative"):
        load_repository_workspace(tmp_path, legacy_layout_explicit=True)


def test_settings_detects_legacy_environment_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_workspace(tmp_path)
    monkeypatch.setenv("CHART_MANAGER_CHARTS_DIR", "other/charts")

    with pytest.raises(SpecError, match="authoritative"):
        Container(Settings()).workspace(tmp_path)


def test_settings_legacy_defaults_match_the_workspace_fallback() -> None:
    """`settings.py` spells these itself so it need not import `domain/`."""
    assert Settings.model_fields["charts_dir"].default == LEGACY_CHARTS_DIR
    assert Settings.model_fields["local_config"].default == LEGACY_LOCAL_CLUSTER


def test_charts_dir_dot_is_authored_only() -> None:
    resource = ChartWorkspace.model_validate(_document(chartsDir="."))

    assert resource.spec.charts_dir == Path(".")
    with pytest.raises(ValidationError):
        Settings(charts_dir=Path("."))


def test_workspace_and_nested_policy_are_frozen() -> None:
    resource = ChartWorkspace.model_validate(
        _document(
            validation={
                "kubernetesVersion": "1.35.3",
                "schemas": {
                    "generateFromCRDs": True,
                    "catalog": {
                        "repository": "datreeio/CRDs-catalog",
                        "track": "main",
                    },
                },
            }
        )
    )

    with pytest.raises(ValidationError, match="frozen"):
        resource.spec.charts_dir = Path("other")
    assert resource.spec.validation is not None
    with pytest.raises(ValidationError, match="frozen"):
        resource.spec.validation.schemas.generate_from_crds = False


def test_workspace_validation_policy_is_optional_and_compiled(tmp_path: Path) -> None:
    assert ChartWorkspace.model_validate(_document()).spec.validation is None

    _write_workspace(
        tmp_path,
        """apiVersion: chartmanager.io/v1alpha1
kind: ChartWorkspace
metadata: {name: example}
spec:
  chartsDir: charts
  localCluster: .chart-manager/local-cluster.yaml
  renderDir: .chart-manager/rendered
  policiesDir: policies
  validation:
    kubernetesVersion: "1.35.3"
    schemas:
      generateFromCRDs: true
      catalog:
        repository: datreeio/CRDs-catalog
        track: main
""",
    )

    workspace = load_repository_workspace(tmp_path)

    assert workspace.validation is not None
    assert workspace.name == "example"
    assert workspace.validation.kubernetes_version == "1.35.3"
    assert workspace.validation.schemas.generate_from_crds is True
    assert workspace.validation.schemas.catalog.repository == "datreeio/CRDs-catalog"
    assert workspace.validation.schemas.catalog.track == "main"


@pytest.mark.parametrize("version", ["", "v1.35.3", "1.35", "1.35.x", " 1.35.3"])
def test_workspace_validation_requires_pinned_kubernetes_version(version: str) -> None:
    with pytest.raises(ValidationError, match=r"pinned X\.Y\.Z"):
        ChartWorkspace.model_validate(
            _document(
                validation={
                    "kubernetesVersion": version,
                    "schemas": {
                        "generateFromCRDs": True,
                        "catalog": {
                            "repository": "datreeio/CRDs-catalog",
                            "track": "main",
                        },
                    },
                }
            )
        )


@pytest.mark.parametrize("repository", ["", "datreeio", "/catalog", "a/b/c", "a /b"])
def test_workspace_validation_requires_singular_catalog(repository: str) -> None:
    with pytest.raises(ValidationError, match="owner/name"):
        ChartWorkspace.model_validate(
            _document(
                validation={
                    "kubernetesVersion": "1.35.3",
                    "schemas": {
                        "generateFromCRDs": True,
                        "catalog": {"repository": repository, "track": "main"},
                    },
                }
            )
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("chartsDir", "../charts"),
        ("localCluster", "."),
        ("renderDir", "/tmp/rendered"),
        ("policiesDir", "C:/policies"),
        ("policiesDir", "policy\\rules"),
    ],
)
def test_workspace_rejects_unsafe_paths(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ChartWorkspace.model_validate(_document(**{field: value}))


def test_fanout_normalizes_dedupes_and_sorts() -> None:
    resource = ChartWorkspace.model_validate(
        _document(
            fanout={
                "validation": ["./z/**", "a/file", "z/**"],
                "clusterTest": ["src/**/test.py"],
            }
        )
    )

    assert resource.spec.fanout.validation == ("a/file", "z/**")


@pytest.mark.parametrize(
    "pattern",
    ["", "/absolute", "a//b", "a/./b", "a/../b", "C:/repo", "a\\b", "a/**b", "a/?"],
)
def test_fanout_rejects_unsafe_or_unsupported_patterns(pattern: str) -> None:
    with pytest.raises(ValidationError):
        ChartWorkspace.model_validate(_document(fanout={"validation": [pattern]}))


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("tooling/file.py", "tooling/file.py", True),
        ("tooling", "tooling/nested/file.py", True),
        ("charts/*/values.yaml", "charts/app/values.yaml", True),
        ("charts/*/values.yaml", "charts/team/app/values.yaml", False),
        ("charts/**/foo.tmpl", "charts/foo.tmpl", True),
        ("charts/**/foo.tmpl", "charts/a/b/foo.tmpl", True),
        ("charts/**/foo.tmpl", "charts/a/b/other.tmpl", False),
    ],
)
def test_fanout_matching(pattern: str, path: str, expected: bool) -> None:
    workspace = RepositoryWorkspace(
        root=Path("/repo"),
        validation_fanout=(pattern,),
        cluster_test_fanout=(),
        shared_prerequisites=(),
    )

    assert workspace.matches_validation_fanout(path) is expected


def test_implicit_fanout_includes_marker_policies_cluster_and_prerequisites(
    tmp_path: Path,
) -> None:
    local = tmp_path / ".chart-manager/local-cluster.yaml"
    local.parent.mkdir()
    local.write_text(
        """apiVersion: chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {name: default}
spec:
  cluster: {config: kind/config.yaml}
  bootstrap:
    releases:
      - {type: lifecycle, chart: charts/cni, profile: minimal}
""",
        encoding="utf-8",
    )
    workspace = RepositoryWorkspace(
        root=tmp_path,
        validation_fanout=(),
        cluster_test_fanout=(),
        shared_prerequisites=("base",),
    )

    assert workspace.matches_validation_fanout("policies/rule.yaml")
    assert workspace.matches_validation_fanout(WORKSPACE_FILE)
    assert workspace.matches_validation_fanout(SCHEMA_LOCK_FILE)
    assert workspace.matches_cluster_test_fanout(WORKSPACE_FILE)
    assert workspace.matches_cluster_test_fanout("kind/config.yaml")
    assert workspace.matches_cluster_test_fanout("charts/cni/templates/cni.yaml")
    assert workspace.matches_cluster_test_fanout("charts/base/templates/crd.yaml")


def test_render_cleanup_rejects_symlink_components(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / ".chart-manager"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SpecError, match="must not contain symlinks"):
        RenderOutputService(tmp_path)


def test_render_cleanup_rechecks_symlinks_created_after_construction(
    tmp_path: Path,
) -> None:
    service = RenderOutputService(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-late-outside"
    outside.mkdir()
    marker = tmp_path / ".chart-manager"
    marker.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SpecError, match="must not contain symlinks"):
        service.clean()


def test_compiled_workspace_is_shared_across_composed_subsystems(tmp_path: Path) -> None:
    _write_workspace(
        tmp_path,
        """apiVersion: chartmanager.io/v1alpha1
kind: ChartWorkspace
metadata: {name: example}
spec:
  chartsDir: helm/charts
  localCluster: ops/local.yaml
  renderDir: artifacts/rendered
  policiesDir: compliance/policies
""",
    )
    container = Container(Settings())
    workspace = container.workspace(tmp_path)

    assert container.chart_catalog_service(tmp_path).repository.charts_dir == (
        tmp_path / "helm/charts"
    )
    assert container.local_target_resolver(tmp_path).local_config == Path("ops/local.yaml")
    assert container.render_output_service(tmp_path).path == tmp_path / "artifacts/rendered"
    assert container.impact_service(tmp_path).workspace is workspace
    assert container.ci_service(tmp_path).workspace is workspace
    assert container.publish_service(tmp_path).repository.charts_dir == (
        tmp_path / "helm/charts"
    )
    assert container.upgrade_finalizer(tmp_path)._charts_dir == Path("helm/charts")
    validation = container.validate_app(root=tmp_path)
    assert validation.workspace is workspace
    assert validation.workspace.charts_dir == Path("helm/charts")
    assert validation.workspace.policies_dir == Path("compliance/policies")
    assert validation.workspace.render_dir == Path("artifacts/rendered")


def test_charts_dir_dot_works_for_discovery_ci_and_grafana(tmp_path: Path) -> None:
    for name in ("alpha", "grafana-dashboards"):
        chart = tmp_path / name
        chart.mkdir()
        (chart / "Chart.yaml").write_text(
            f"apiVersion: v2\nname: {name}\nversion: 1.0.0\n",
            encoding="utf-8",
        )
    dashboard = tmp_path / "grafana-dashboards/dashboards/team/example.json"
    dashboard.parent.mkdir(parents=True)
    dashboard.write_text("{}", encoding="utf-8")

    repository = ChartRepository(tmp_path, charts_dir=Path("."))

    assert repository.list_names() == ["alpha", "grafana-dashboards"]
    workspace = RepositoryWorkspace(root=tmp_path.resolve(), charts_dir=Path("."))
    assert workspace.chart_name_from_repo_path("alpha/values.yaml") == "alpha"
    assert discover_dashboards(workspace=workspace) == [dashboard]
