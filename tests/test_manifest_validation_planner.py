"""Manifest-validation planning tests over a temporary chart tree.

We synthesize charts (Chart.yaml + chart-lifecycle.yaml) rather than rely on
the in-repo charts so the tests are independent of real-repo evolution.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from chart_manager.domain.charts import ChartRepository
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.manifest_validation.catalog import load_manifest_validation_target
from chart_manager.services.manifest_validation.planner import build_worklist, select_rows
from chart_manager.services.manifest_validation.resolver import (
    resolve_manifest_validation,
    row_config_for,
)
from chart_manager.services.manifest_validation.validators import (
    KubeconformConfig,
    KyvernoConfig,
)
from tests.conftest import CHARTS_DIR, workspace_for


def _chart(
    root: Path,
    name: str,
    *,
    spec: str | None = None,
    dependencies: list[dict] | None = None,
) -> Path:
    chart_dir = root / "charts" / name
    chart_dir.mkdir(parents=True)
    chart_yaml = f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n"
    if dependencies:
        chart_yaml += "dependencies:\n"
        for dep in dependencies:
            chart_yaml += f"  - name: {dep['name']}\n    version: {dep.get('version', '0.0.0')}\n"
            if "repository" in dep:
                chart_yaml += f"    repository: {dep['repository']}\n"
    (chart_dir / "Chart.yaml").write_text(chart_yaml)
    if spec is not None:
        section = textwrap.dedent(spec).removeprefix("\n")
        envelope = (
            "apiVersion: chartmanager.io/v1alpha1\n"
            "kind: ChartLifecycle\n"
            "metadata:\n"
            f"  name: {name}\n"
            "spec:\n"
            "  validation:\n"
            + textwrap.indent(section, "    ")
        )
        (chart_dir / "chart-lifecycle.yaml").write_text(envelope)
    return chart_dir


_DEFAULT_SPEC = """
releaseName: {name}
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml, values-prod.yaml]
triggers:
  "values.yaml": [dev, prod]
  "values-prod.yaml": [prod]
"""


def test_skip_change_detection_cross_product(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))

    result = build_worklist(workspace=workspace_for(tmp_path), skip_change_detection=True)

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod"), ("beta", "dev"), ("beta", "prod")}
    assert result.warnings == ()
    assert result.spec_errors == ()


def test_skip_change_detection_overrides_a_non_empty_changed_files_list(
    tmp_path: Path,
) -> None:
    """The flag suppresses change-impact analysis; it does not widen scope.

    Selection scope is `selected_charts`, not this flag: with both set the
    worklist is the selected chart's full env cross-product, and the
    changed-files list — which alone would have selected only `prod` — is
    ignored entirely.
    """
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))
    changed = ["charts/alpha/values-prod.yaml"]

    impacted = build_worklist(workspace=workspace_for(tmp_path), changed_files=changed)
    assert {(row.chart, row.env) for row in impacted.rows} == {("alpha", "prod")}

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=changed,
        skip_change_detection=True,
        selected_charts=("alpha",),
    )

    assert {(row.chart, row.env) for row in result.rows} == {
        ("alpha", "dev"),
        ("alpha", "prod"),
    }


def test_selected_charts_do_not_enumerate_or_parse_unrelated_charts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))
    _chart(
        tmp_path,
        "broken",
        spec="releaseName: broken\nenvironments: {}\nmystery: true\n",
    )
    monkeypatch.setattr(
        ChartRepository,
        "list_names",
        lambda _self: pytest.fail("targeted planning must not enumerate the repository"),
    )

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        skip_change_detection=True,
        selected_charts=("alpha",),
    )

    assert {(row.chart, row.env) for row in result.rows} == {
        ("alpha", "dev"),
        ("alpha", "prod"),
    }
    assert result.spec_errors == ()
    assert result.warnings == ()
    assert result.chart_count_unvalidated == 0
    assert set(result.targets) == {"alpha"}


def test_trigger_specific_env(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/values-prod.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "prod")}


def test_match_by_basename(tmp_path: Path) -> None:
    spec = """
releaseName: alpha
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml]
triggers:
  "envs/*.yaml": match-by-basename
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/envs/dev.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev")}


def test_root_policies_fanout(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["policies/require-non-root.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod"), ("beta", "dev"), ("beta", "prod")}


def test_validate_code_path_fanout(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        changed_files=["src/chart_manager/services/manifest_validation/runner.py"],
        workspace=workspace_for(
            tmp_path,
            fanout={"validation": ["src/chart_manager/services/manifest_validation/**"]},
        ),
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_other_chart_manager_path_is_ignored(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["src/chart_manager/cli/grafana_export.py"],
    )

    assert result.rows == ()


def test_chart_yaml_edit_fanouts_to_all_envs(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/Chart.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_missing_spec_emits_warning(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=None)
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))

    result = build_worklist(workspace=workspace_for(tmp_path), skip_change_detection=True)

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("beta", "dev"), ("beta", "prod")}
    assert result.chart_count_unvalidated == 1
    assert any("alpha" in w for w in result.warnings)
    assert result.spec_errors == ()


def test_spec_parse_error_records_spec_error(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec="releaseName: x\nmystery: true\n")

    result = build_worklist(workspace=workspace_for(tmp_path), skip_change_detection=True)

    assert result.rows == ()
    assert any("alpha" in e for e in result.spec_errors)


def test_disabled_capability_is_silently_skipped(tmp_path: Path) -> None:
    skipped_spec = _DEFAULT_SPEC.format(name="alpha") + "enabled: false\n"
    _chart(tmp_path, "alpha", spec=skipped_spec)
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))

    result = build_worklist(workspace=workspace_for(tmp_path), skip_change_detection=True)

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("beta", "dev"), ("beta", "prod")}
    assert any("disabled" in warning for warning in result.warnings)
    assert result.chart_count_unvalidated == 1


def test_library_chart_edit_fanouts_to_dependents(tmp_path: Path) -> None:
    # `common` is the library; `alpha` and `beta` depend on it.
    _chart(tmp_path, "common", spec=None, dependencies=None)
    _chart(
        tmp_path,
        "alpha",
        spec=_DEFAULT_SPEC.format(name="alpha"),
        dependencies=[{"name": "common"}],
    )
    _chart(
        tmp_path,
        "beta",
        spec=_DEFAULT_SPEC.format(name="beta"),
        dependencies=[{"name": "common"}],
    )

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/common/templates/_helpers.tpl"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {
        ("alpha", "dev"),
        ("alpha", "prod"),
        ("beta", "dev"),
        ("beta", "prod"),
    }


def test_wrapper_chart_remote_same_name_dependency_respects_triggers(tmp_path: Path) -> None:
    # Wrapper charts depend on a remote upstream of the same name; that edge
    # must not turn a values-file edit into an all-environments fanout.
    _chart(
        tmp_path,
        "alpha",
        spec=_DEFAULT_SPEC.format(name="alpha"),
        dependencies=[{"name": "alpha", "repository": "https://charts.example.com"}],
    )

    result = build_worklist(
        workspace=workspace_for(tmp_path), changed_files=["charts/alpha/values-prod.yaml"]
    )

    assert {(r.chart, r.env) for r in result.rows} == {("alpha", "prod")}


def test_per_chart_policies_dir_edit_fanouts_to_all_envs(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/policies/require-x.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_chart_lifecycle_edit_fanouts_to_all_envs(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/chart-lifecycle.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_overlapping_triggers_union_envs(tmp_path: Path) -> None:
    # `values.yaml` matches BOTH the literal trigger (dev only) and the
    # glob trigger (prod only). Contract: set-union, not last-wins.
    spec = """
releaseName: alpha
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml]
triggers:
  "values.yaml": [dev]
  "*.yaml": [prod]
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/values.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_match_by_basename_preserves_multi_dot_stem(tmp_path: Path) -> None:
    # `envs/dev.local.yaml` -> stem `dev.local`. Declared env wins; an
    # undeclared stem produces zero envs (silently ignored).
    spec = """
releaseName: alpha
environments:
  dev.local:
    namespace: lab-dev-local
    values: [values.yaml]
  dev:
    namespace: lab-dev
    values: [values.yaml]
triggers:
  "envs/*.yaml": match-by-basename
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=[
            "charts/alpha/envs/dev.local.yaml",
            "charts/alpha/envs/staging.yaml",  # undeclared -> dropped
        ],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev.local")}


def test_unrelated_file_is_ignored(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["README.md", "docs/foo.md"],
    )

    assert result.rows == ()


def test_unmatched_changes_policy_fans_out_to_all_envs(tmp_path: Path) -> None:
    # `crds/widget.yaml` matches no explicit trigger. In strict
    # mode the worklist fans out to every env in `environments` instead of
    # silently dropping the file.
    spec = """
releaseName: alpha
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml]
triggers:
  "values.yaml": [dev, prod]
unmatchedChanges: all-environments
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/crds/widget.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_unmatched_changes_policy_does_not_override_explicit_trigger(tmp_path: Path) -> None:
    # An explicit trigger still scopes to its listed envs even with strict on.
    spec = """
releaseName: alpha
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml]
triggers:
  "values-dev.yaml": [dev]
unmatchedChanges: all-environments
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/values-dev.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev")}


def test_unmatched_changes_warn_drops_unmatched_work(tmp_path: Path) -> None:
    # Default (non-strict) behavior preserved: an unmatched chart file
    # produces zero rows.
    spec = """
releaseName: alpha
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
triggers:
  "values.yaml": [dev]
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/crds/widget.yaml"],
    )

    assert result.rows == ()
    assert result.ignored_changes == ()
    assert result.unmatched_changes == (Path("charts/alpha/crds/widget.yaml"),)
    assert any("matches no trigger" in warning for warning in result.warnings)
    assert any("no environments selected" in warning for warning in result.warnings)


def test_explicit_trigger_ignore_is_distinct_from_unmatched_change(
    tmp_path: Path,
) -> None:
    spec = """
releaseName: alpha
environments:
  dev:
    namespace: lab-dev
triggers:
  "values.yaml": [dev]
triggerIgnores:
  - "README.md"
  - "docs/**"
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=[
            "charts/alpha/README.md",
            "charts/alpha/docs/configuration.md",
            "charts/alpha/crds/widget.yaml",
        ],
    )

    assert result.rows == ()
    assert result.ignored_changes == (
        Path("charts/alpha/README.md"),
        Path("charts/alpha/docs/configuration.md"),
    )
    assert result.unmatched_changes == (Path("charts/alpha/crds/widget.yaml"),)
    assert sum("explicitly ignored" in warning for warning in result.warnings) == 2
    assert sum("matches no trigger" in warning for warning in result.warnings) == 1


def test_explicit_ignore_takes_precedence_over_overlapping_trigger(
    tmp_path: Path,
) -> None:
    spec = """
releaseName: alpha
environments:
  dev:
    namespace: lab-dev
triggers:
  "*.md": [dev]
triggerIgnores:
  - "README.md"
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/README.md"],
    )

    assert result.rows == ()
    assert result.ignored_changes == (Path("charts/alpha/README.md"),)
    assert result.unmatched_changes == ()


def test_strict_fanout_still_records_unmatched_trigger_coverage(
    tmp_path: Path,
) -> None:
    spec = """
releaseName: alpha
environments:
  dev:
    namespace: lab-dev
  prod:
    namespace: lab-prod
unmatchedChanges: all-environments
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/crds/widget.yaml"],
    )

    assert {(row.chart, row.env) for row in result.rows} == {
        ("alpha", "dev"),
        ("alpha", "prod"),
    }
    assert result.unmatched_changes == (Path("charts/alpha/crds/widget.yaml"),)
    assert any(
        "unmatchedChanges=all-environments selected all environments" in warning
        for warning in result.warnings
    )


def test_catalog_composes_validate_spec_over_authoritative_helm_chart(
    tmp_path: Path,
) -> None:
    chart_dir = _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(workspace=workspace_for(tmp_path), skip_change_detection=True)

    target = result.targets["alpha"]
    assert target.name == "alpha"
    assert target.path == chart_dir
    assert target.chart.metadata.version == "0.1.0"
    assert target.spec_path == chart_dir / "chart-lifecycle.yaml"


def test_repository_scan_records_malformed_chart_metadata_without_aborting(
    tmp_path: Path,
) -> None:
    malformed = _chart(tmp_path, "broken", spec=_DEFAULT_SPEC.format(name="broken"))
    (malformed / "Chart.yaml").write_text("name: [not-a-string]\n")
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(workspace=workspace_for(tmp_path), skip_change_detection=True)

    assert {(row.chart, row.env) for row in result.rows} == {
        ("alpha", "dev"),
        ("alpha", "prod"),
    }
    assert any(error.startswith("broken:") for error in result.spec_errors)


def test_explicit_validatable_chart_load_is_strict(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=None)

    with pytest.raises(
        ChartManagerError,
        match=r"has no validation configuration in chart-lifecycle\.yaml",
    ):
        load_manifest_validation_target(tmp_path, "alpha", charts_dir=CHARTS_DIR)


def test_compiler_resolves_chart_relative_paths_independently_of_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = (
        _DEFAULT_SPEC.format(name="alpha")
        + """
schemaLocations:
  - schemas/{{.Group}}/{{.ResourceKind}}.json
policies:
  extra: [extra-policies]
"""
    )
    chart_dir = _chart(tmp_path, "alpha", spec=spec)
    (chart_dir / "values.yaml").write_text("{}\n")
    (chart_dir / "values-prod.yaml").write_text("{}\n")
    (chart_dir / "extra-policies").mkdir()
    (tmp_path / "schemas").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    build = build_worklist(workspace=workspace_for(tmp_path), skip_change_detection=True)
    compiled = resolve_manifest_validation(build.targets["alpha"], tmp_path)
    dev = next(row for row in build.rows if row.env == "dev")
    config = row_config_for(compiled, dev)
    kubeconform = config.validator_invocations[0].config
    kyverno = config.validator_invocations[1].config
    assert isinstance(kubeconform, KubeconformConfig)
    assert isinstance(kyverno, KyvernoConfig)

    assert config.values == [(chart_dir / "values.yaml").resolve()]
    assert kyverno.policy_paths == ((chart_dir / "extra-policies").resolve(),)
    assert kubeconform.schema_locations == (
        str((tmp_path / "schemas" / "{{.Group}}" / "{{.ResourceKind}}.json").resolve()),
    )


def test_compiler_does_not_accept_repository_relative_extra_policy(
    tmp_path: Path,
) -> None:
    spec = (
        _DEFAULT_SPEC.format(name="alpha")
        + """
policies:
  extra: [legacy-policies]
"""
    )
    chart_dir = _chart(tmp_path, "alpha", spec=spec)
    (chart_dir / "values.yaml").write_text("{}\n")
    (chart_dir / "values-prod.yaml").write_text("{}\n")
    repository_policy = tmp_path / "legacy-policies"
    repository_policy.mkdir()

    build = build_worklist(workspace=workspace_for(tmp_path), skip_change_detection=True)
    compiled = resolve_manifest_validation(build.targets["alpha"], tmp_path)

    assert len(compiled.warnings) == 1
    assert "policy directory does not exist" in compiled.warnings[0]
    assert str(chart_dir / "legacy-policies") in compiled.warnings[0]


def test_explicit_filter_diagnostics_use_catalog_not_affected_rows(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))
    build = build_worklist(workspace=workspace_for(tmp_path), changed_files=["README.md"])
    available_environments = {
        environment for target in build.targets.values() for environment in target.spec.environments
    }

    known_noop = select_rows(
        build.rows,
        charts={"alpha"},
        envs={"dev"},
        available_charts=set(build.targets),
        available_environments=available_environments,
    )
    unknown = select_rows(
        build.rows,
        charts={"ghost"},
        envs={"staging"},
        available_charts=set(build.targets),
        available_environments=available_environments,
    )

    assert known_noop.rows == ()
    assert known_noop.unmatched_charts == ()
    assert known_noop.unmatched_environments == ()
    assert unknown.unmatched_charts == ("ghost",)
    assert unknown.unmatched_environments == ("staging",)


# A chart that authors only values-file triggers, like most wrapper charts.
# Common chart files are covered by the planner's built-in default triggers.
_CI_SPEC = """
releaseName: alpha
environments:
  ci:
    namespace: lab-ci
    values: [values.yaml, values-ci.yaml]
  dev:
    namespace: lab-dev
    values: [values.yaml]
triggers:
  "values.yaml": [ci, dev]
  "values-dev.yaml": [dev]
"""


@pytest.mark.parametrize(
    "chart_file",
    [
        "templates/deployment.yaml",
        "templates/nested/_helpers.tpl",
        "files/dashboard.json",
        "values.schema.json",
        "Chart.lock",
    ],
)
def test_rendering_default_triggers_select_all_environments(
    tmp_path: Path,
    chart_file: str,
) -> None:
    # Rendering inputs render or validate differently under each env's values.
    _chart(tmp_path, "alpha", spec=_CI_SPEC)

    result = build_worklist(
        workspace=workspace_for(tmp_path), changed_files=[f"charts/alpha/{chart_file}"]
    )

    assert {(row.chart, row.env) for row in result.rows} == {
        ("alpha", "ci"),
        ("alpha", "dev"),
    }
    assert result.unmatched_changes == ()
    assert not any("matches no trigger" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "chart_file",
    [
        "tests/test-connection.yaml",
        "values-ci.yaml",
    ],
)
def test_ci_only_default_triggers_select_ci_without_unmatched_warning(
    tmp_path: Path,
    chart_file: str,
) -> None:
    _chart(tmp_path, "alpha", spec=_CI_SPEC)

    result = build_worklist(
        workspace=workspace_for(tmp_path), changed_files=[f"charts/alpha/{chart_file}"]
    )

    assert {(row.chart, row.env) for row in result.rows} == {("alpha", "ci")}
    assert result.unmatched_changes == ()
    assert not any("matches no trigger" in warning for warning in result.warnings)


def test_readme_is_not_covered_by_default_triggers(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_CI_SPEC)

    result = build_worklist(
        workspace=workspace_for(tmp_path), changed_files=["charts/alpha/README.md"]
    )

    assert result.rows == ()
    assert result.unmatched_changes == (Path("charts/alpha/README.md"),)
    assert any("matches no trigger" in warning for warning in result.warnings)


def test_authored_trigger_replaces_default_with_the_same_pattern(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_CI_SPEC + '  "templates/**": [dev]\n')

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/templates/deployment.yaml"],
    )

    assert {(row.chart, row.env) for row in result.rows} == {("alpha", "dev")}
    assert result.unmatched_changes == ()


def test_authored_ci_trigger_narrows_all_environments_default(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_CI_SPEC + '  "templates/**": [ci]\n')

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/templates/deployment.yaml"],
    )

    assert {(row.chart, row.env) for row in result.rows} == {("alpha", "ci")}
    assert result.unmatched_changes == ()


def test_authored_all_environments_trigger_selects_every_environment(
    tmp_path: Path,
) -> None:
    spec = _DEFAULT_SPEC.format(name="alpha") + '  "crds/**": all-environments\n'
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/crds/widget.yaml"],
    )

    assert {(row.chart, row.env) for row in result.rows} == {
        ("alpha", "dev"),
        ("alpha", "prod"),
    }
    assert result.unmatched_changes == ()


def test_authored_templates_all_environments_trigger_selects_every_environment(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha", spec=_CI_SPEC + '  "templates/**": all-environments\n')

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/templates/deployment.yaml"],
    )

    assert {(row.chart, row.env) for row in result.rows} == {
        ("alpha", "ci"),
        ("alpha", "dev"),
    }
    assert result.unmatched_changes == ()


def test_authored_empty_trigger_opts_out_of_default(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_CI_SPEC + '  "templates/**": []\n')

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/templates/deployment.yaml"],
    )

    assert result.rows == ()
    assert result.unmatched_changes == ()


def test_trigger_ignores_take_precedence_over_default_triggers(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_CI_SPEC + 'triggerIgnores:\n  - "templates/NOTES.txt"\n')

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/templates/NOTES.txt"],
    )

    assert result.rows == ()
    assert result.ignored_changes == (Path("charts/alpha/templates/NOTES.txt"),)
    assert result.unmatched_changes == ()


def test_trigger_ignores_take_precedence_over_all_environments_default(
    tmp_path: Path,
) -> None:
    _chart(tmp_path, "alpha", spec=_CI_SPEC + 'triggerIgnores:\n  - "values.schema.json"\n')

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/values.schema.json"],
    )

    assert result.rows == ()
    assert result.ignored_changes == (Path("charts/alpha/values.schema.json"),)
    assert result.unmatched_changes == ()


def test_default_trigger_without_target_env_falls_through_to_warning(
    tmp_path: Path,
) -> None:
    # `_DEFAULT_SPEC` has no `ci` environment, so the tests/** default
    # contributes nothing and must not silently count as a match. (The
    # all-environments defaults always select something, so they cannot
    # exercise this fallback.)
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/tests/test-connection.yaml"],
    )

    assert result.rows == ()
    assert result.unmatched_changes == (Path("charts/alpha/tests/test-connection.yaml"),)
    assert any("no environments selected" in warning for warning in result.warnings)


def test_default_trigger_without_target_env_honors_all_environments_policy(
    tmp_path: Path,
) -> None:
    _chart(
        tmp_path,
        "alpha",
        spec=_DEFAULT_SPEC.format(name="alpha") + "unmatchedChanges: all-environments\n",
    )

    result = build_worklist(
        workspace=workspace_for(tmp_path),
        changed_files=["charts/alpha/tests/test-connection.yaml"],
    )

    assert {(row.chart, row.env) for row in result.rows} == {
        ("alpha", "dev"),
        ("alpha", "prod"),
    }
    assert result.unmatched_changes == (Path("charts/alpha/tests/test-connection.yaml"),)
