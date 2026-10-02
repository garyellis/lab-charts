"""Repository workspace loading, discovery, matching, and safety invariants."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from chart_manager import settings as settings_module
from chart_manager.api.v1alpha1.chart_workspace import ChartWorkspace
from chart_manager.cli import main
from chart_manager.cli._container import reset_invocation
from chart_manager.composition import Container
from chart_manager.domain.charts import ChartRepository
from chart_manager.domain.workspace import (
    SCHEMA_LOCK_FILE,
    WORKSPACE_FILE,
    discover_workspace_root,
    load_repository_workspace,
    resolve_repository_root,
)
from chart_manager.plumbing.errors import SpecError, WorkspaceNotFoundError
from chart_manager.plumbing.exit_codes import exit_code_for
from chart_manager.services.grafana.dashboard_lint import discover_dashboards
from chart_manager.services.manifest_validation.paths import RenderOutputService
from chart_manager.settings import Settings, load_settings

from .conftest import RENDER_DIR, cli, workspace_for, write_workspace


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


def test_missing_marker_is_an_error_naming_the_discovery_start(tmp_path: Path) -> None:
    expected = (
        f"no .chart-manager/workspace.yaml in {tmp_path.resolve()} or any parent; "
        "run from a chart repository checkout or set CHART_MANAGER_ROOT"
    )

    with pytest.raises(WorkspaceNotFoundError) as excinfo:
        resolve_repository_root(configured=None, start=tmp_path)
    assert str(excinfo.value) == expected


def test_environment_root_without_marker_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHART_MANAGER_ROOT", str(tmp_path))

    with pytest.raises(WorkspaceNotFoundError) as excinfo:
        Container(Settings()).workspace()
    assert str(excinfo.value) == f"{tmp_path.resolve()} has no .chart-manager/workspace.yaml"


def test_config_file_root_without_marker_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "not-a-checkout"
    target.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(f"root: {target}\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "_config_file", config)

    with pytest.raises(WorkspaceNotFoundError) as excinfo:
        Container(Settings()).workspace()
    assert str(excinfo.value) == f"{target.resolve()} has no .chart-manager/workspace.yaml"


def test_explicit_root_never_walks_up(tmp_path: Path) -> None:
    """An explicit root that is a subdirectory of a workspace is still wrong."""
    _write_workspace(tmp_path)
    nested = tmp_path / "charts"
    nested.mkdir()

    assert resolve_repository_root(configured=nested, start=nested) == nested.resolve()
    with pytest.raises(WorkspaceNotFoundError) as excinfo:
        Container(Settings()).workspace(nested)
    assert str(excinfo.value) == f"{nested.resolve()} has no .chart-manager/workspace.yaml"


def test_missing_workspace_exits_with_the_environment_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = cli("chart", "list")

    # `CliRunner` stops at Click; the exception is what `main()` maps.
    assert isinstance(result.exception, WorkspaceNotFoundError)
    assert "set CHART_MANAGER_ROOT" in str(result.exception)
    assert exit_code_for(main._outcome_for(result.exception)) == 5


def test_missing_workspace_exit_code_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main()` -- not Click -- owns the error-to-exit-code mapping."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["chart-manager", "chart", "list"])
    try:
        with pytest.raises(SystemExit) as excinfo:
            main.main()
    finally:
        reset_invocation()

    assert excinfo.value.code == 5
    assert "no .chart-manager/workspace.yaml" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["charts_dir", "local_config"])
def test_layout_keys_in_config_file_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(f"{key}: charts\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "_config_file", config)

    with pytest.raises(SpecError) as excinfo:
        load_settings()
    assert str(excinfo.value).startswith(f"invalid settings ({config}): {key}: ")


def _exit_through_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[object, str]:
    """Run the real entry point; return its exit code and stderr."""
    monkeypatch.setattr("sys.argv", ["chart-manager", *argv])
    try:
        with pytest.raises(SystemExit) as excinfo:
            main.main()
    finally:
        reset_invocation()
        settings_module.set_config_file(settings_module.DEFAULT_CONFIG_FILE)
    return excinfo.value.code, capsys.readouterr().err


@pytest.mark.parametrize(
    "body,key",
    [("charts_dir: charts\n", "charts_dir"), ("kube_contxt: kind-lab\n", "kube_contxt")],
)
def test_an_unknown_config_key_exits_with_the_spec_code_and_no_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
    key: str,
) -> None:
    """Both construction sites: the bootstrap in `main()` and the root callback."""
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "custom.yaml"
    config.write_text(body, encoding="utf-8")
    (tmp_path / ".chart-manager").mkdir()
    (tmp_path / ".chart-manager" / "config.yaml").write_text(body, encoding="utf-8")

    # The default config file is read by the bootstrap Settings in `main()`.
    code, err = _exit_through_main(monkeypatch, capsys, "version")
    assert code == 3
    assert f"invalid settings (.chart-manager/config.yaml): {key}: " in err
    assert "Traceback" not in err

    # `--config` is only applied in the root callback, by `start_invocation()`.
    (tmp_path / ".chart-manager" / "config.yaml").unlink()
    code, err = _exit_through_main(monkeypatch, capsys, "--config", str(config), "version")
    assert code == 3
    assert f"invalid settings ({config}): {key}: " in err
    assert "Traceback" not in err


def test_an_invalid_config_value_names_the_key_and_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("log_level: chatty\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "_config_file", config)

    with pytest.raises(SpecError, match=r"^invalid settings \(.*config\.yaml\): log_level: "):
        load_settings()


def test_other_chart_manager_environment_variables_still_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`extra="forbid"` covers init and config-file input, never the environment.

    These variables share the `CHART_MANAGER_` prefix but are read by other
    code (hooks, publish, the exit-code table), not by `Settings`; a
    `forbid` that rejected them would break every hook invocation.
    """
    for variable in (
        "CHART_MANAGER_CHART",
        "CHART_MANAGER_PROFILE",
        "CHART_MANAGER_HOOK_PHASE",
        "CHART_MANAGER_OCI_REPOSITORY",
        "CHART_MANAGER_LEGACY_EXIT_CODES",
    ):
        monkeypatch.setenv(variable, "x")
    monkeypatch.setenv("CHART_MANAGER_KUBE_CONTEXT", "kind-lab")

    assert Settings().kube_context == "kind-lab"


def test_charts_dir_dot_is_accepted_by_the_workspace_schema() -> None:
    resource = ChartWorkspace.model_validate(_document(chartsDir="."))

    assert resource.spec.charts_dir == Path(".")


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

    assert workspace.spec.validation is not None
    assert workspace.name == "example"
    assert workspace.spec.validation.kubernetes_version == "1.35.3"
    assert workspace.spec.validation.schemas.generate_from_crds is True
    assert workspace.spec.validation.schemas.catalog.repository == "datreeio/CRDs-catalog"
    assert workspace.spec.validation.schemas.catalog.track == "main"


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
    workspace = workspace_for(Path("/repo"), fanout={"validation": [pattern]})

    assert workspace.matches_validation_fanout(path) is expected


def test_implicit_fanout_includes_marker_policies_cluster_and_prerequisites(
    tmp_path: Path,
) -> None:
    local = tmp_path / ".chart-manager/local-cluster.yaml"
    local.parent.mkdir(exist_ok=True)
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
    workspace = workspace_for(tmp_path, clusterTest={"sharedPrerequisites": ["base"]})

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
        RenderOutputService(tmp_path, render_dir=RENDER_DIR)


def test_render_cleanup_rechecks_symlinks_created_after_construction(
    tmp_path: Path,
) -> None:
    service = RenderOutputService(tmp_path, render_dir=RENDER_DIR)
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
    assert validation.workspace.spec.charts_dir == Path("helm/charts")
    assert validation.workspace.spec.policies_dir == Path("compliance/policies")
    assert validation.workspace.spec.render_dir == Path("artifacts/rendered")


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
    workspace = workspace_for(tmp_path, chartsDir=Path("."))
    assert workspace.chart_name_from_repo_path("alpha/values.yaml") == "alpha"
    assert discover_dashboards(workspace=workspace) == [dashboard]


# --- the wrapped spec: escape checks, with_charts_dir, equality --------------


def test_loader_rejects_a_layout_path_escaping_through_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (tmp_path / "outside").mkdir()
    root.mkdir()
    (root / "charts").symlink_to(tmp_path / "outside", target_is_directory=True)
    write_workspace(root)

    with pytest.raises(SpecError, match=r"spec\.chartsDir resolves outside repository root"):
        load_repository_workspace(root)


def test_loader_rejects_a_render_dir_with_a_symlink_component(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    write_workspace(tmp_path, renderDir="linked/rendered")

    with pytest.raises(SpecError, match=r"spec\.renderDir must not contain symlink components"):
        load_repository_workspace(tmp_path)


@pytest.mark.parametrize("path", [Path("/abs/charts"), Path("../charts"), Path("a/../b")])
def test_with_charts_dir_rejects_paths_the_spec_would_reject(
    tmp_path: Path, path: Path
) -> None:
    write_workspace(tmp_path)
    workspace = load_repository_workspace(tmp_path)

    with pytest.raises(SpecError) as raised:
        workspace.with_charts_dir(path)

    assert str(raised.value).startswith(f"invalid chart directory {path}: ")
    assert "\n" not in str(raised.value)


def test_with_charts_dir_rejects_a_path_escaping_through_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (tmp_path / "outside").mkdir()
    root.mkdir()
    (root / "vendor").symlink_to(tmp_path / "outside", target_is_directory=True)
    write_workspace(root)
    workspace = load_repository_workspace(root)

    with pytest.raises(SpecError, match=r"spec\.chartsDir resolves outside repository root"):
        workspace.with_charts_dir(Path("vendor"))


def test_with_charts_dir_rechecks_the_other_layout_paths(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    workspace = load_repository_workspace(tmp_path)
    (tmp_path / "real").mkdir()
    (tmp_path / ".chart-manager" / "rendered").symlink_to(
        tmp_path / "real", target_is_directory=True
    )

    with pytest.raises(
        SpecError, match=r"^spec\.renderDir must not contain symlink components"
    ):
        workspace.with_charts_dir(Path("vendor"))


def test_with_charts_dir_accepts_the_root_itself(tmp_path: Path) -> None:
    write_workspace(tmp_path)
    workspace = load_repository_workspace(tmp_path).with_charts_dir(Path("."))

    assert workspace.spec.charts_dir == Path(".")
    assert workspace.charts_root == workspace.root
    assert workspace.chart_path("alpha") == workspace.root / "alpha"


def test_with_charts_dir_repoints_only_the_chart_directory(tmp_path: Path) -> None:
    write_workspace(
        tmp_path,
        fanout={"validation": ["tooling/**"]},
        clusterTest={"sharedPrerequisites": ["base"]},
    )
    original = load_repository_workspace(tmp_path)
    workspace = original.with_charts_dir(Path("vendor/helm"))

    assert workspace.spec.charts_dir == Path("vendor/helm")
    assert workspace.chart_path("alpha") == tmp_path.resolve() / "vendor/helm/alpha"
    assert workspace.repo_chart_path("alpha", "values.yaml") == Path(
        "vendor/helm/alpha/values.yaml"
    )
    assert workspace.chart_name_from_repo_path("vendor/helm/alpha/Chart.yaml") == "alpha"
    assert (workspace.root, workspace.name) == (original.root, original.name)
    assert workspace.spec.fanout == original.spec.fanout
    assert workspace.spec.cluster_test == original.spec.cluster_test
    assert workspace.spec.render_dir == original.spec.render_dir
    assert original.spec.charts_dir == Path("charts")


def test_loaded_workspaces_compare_and_hash_by_value(tmp_path: Path) -> None:
    write_workspace(
        tmp_path,
        validation={
            "kubernetesVersion": "1.35.3",
            "schemas": {
                "generateFromCRDs": True,
                "catalog": {"repository": "datreeio/CRDs-catalog", "track": "main"},
            },
        },
        fanout={"validation": ["tooling/**"], "clusterTest": ["kind/**"]},
    )
    first = load_repository_workspace(tmp_path)
    second = load_repository_workspace(tmp_path)

    assert first is not second
    assert first == second
    assert hash(first) == hash(second)
    same_dir = first.with_charts_dir(first.spec.charts_dir)
    assert same_dir == first
    assert hash(same_dir) == hash(first)
    assert first.with_charts_dir(Path("other")) != first
