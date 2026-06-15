"""Worklist construction tests over a tmp_path tree.

We synthesize charts (Chart.yaml + validate-spec.yaml) rather than rely on
the in-repo charts so the tests are independent of real-repo evolution.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from chart_manager.services.validate.worklist import build_worklist


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
    (chart_dir / "Chart.yaml").write_text(chart_yaml)
    if spec is not None:
        (chart_dir / "validate-spec.yaml").write_text(textwrap.dedent(spec))
    return chart_dir


_DEFAULT_SPEC = """
version: 1
release_name: {name}
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


def test_all_charts_cross_product(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))

    result = build_worklist(root=tmp_path, all_charts=True)

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod"), ("beta", "dev"), ("beta", "prod")}
    assert result.warnings == ()
    assert result.spec_errors == ()


def test_trigger_specific_env(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        root=tmp_path,
        changed_files=["charts/alpha/values-prod.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "prod")}


def test_match_by_basename(tmp_path: Path) -> None:
    spec = """
version: 1
release_name: alpha
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
        root=tmp_path,
        changed_files=["charts/alpha/envs/dev.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev")}


def test_root_policies_fanout(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))

    result = build_worklist(
        root=tmp_path,
        changed_files=["policies/require-non-root.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod"), ("beta", "dev"), ("beta", "prod")}


def test_validate_code_path_fanout(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        root=tmp_path,
        changed_files=["src/chart_manager/services/validate/runner.py"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_other_chart_manager_path_is_ignored(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        root=tmp_path,
        changed_files=["src/chart_manager/cli/grafana_export.py"],
    )

    assert result.rows == ()


def test_chart_yaml_edit_fanouts_to_all_envs(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        root=tmp_path,
        changed_files=["charts/alpha/Chart.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_missing_spec_emits_warning(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=None)
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))

    result = build_worklist(root=tmp_path, all_charts=True)

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("beta", "dev"), ("beta", "prod")}
    assert result.chart_count_unvalidated == 1
    assert any("alpha" in w for w in result.warnings)
    assert result.spec_errors == ()


def test_spec_parse_error_records_spec_error(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec="version: 9\nrelease_name: x\n")

    result = build_worklist(root=tmp_path, all_charts=True)

    assert result.rows == ()
    assert any("alpha" in e for e in result.spec_errors)


def test_skip_true_silently_skipped(tmp_path: Path) -> None:
    skipped_spec = _DEFAULT_SPEC.format(name="alpha") + "skip: true\n"
    _chart(tmp_path, "alpha", spec=skipped_spec)
    _chart(tmp_path, "beta", spec=_DEFAULT_SPEC.format(name="beta"))

    result = build_worklist(root=tmp_path, all_charts=True)

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("beta", "dev"), ("beta", "prod")}
    assert result.warnings == ()
    assert result.chart_count_unvalidated == 0


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
        root=tmp_path,
        changed_files=["charts/common/templates/_helpers.tpl"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {
        ("alpha", "dev"),
        ("alpha", "prod"),
        ("beta", "dev"),
        ("beta", "prod"),
    }


def test_per_chart_policies_dir_edit_fanouts_to_all_envs(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        root=tmp_path,
        changed_files=["charts/alpha/policies/require-x.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_validate_spec_edit_fanouts_to_all_envs(tmp_path: Path) -> None:
    _chart(tmp_path, "alpha", spec=_DEFAULT_SPEC.format(name="alpha"))

    result = build_worklist(
        root=tmp_path,
        changed_files=["charts/alpha/validate-spec.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_overlapping_triggers_union_envs(tmp_path: Path) -> None:
    # `values.yaml` matches BOTH the literal trigger (dev only) and the
    # glob trigger (prod only). Contract: set-union, not last-wins.
    spec = """
version: 1
release_name: alpha
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
        root=tmp_path,
        changed_files=["charts/alpha/values.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_match_by_basename_preserves_multi_dot_stem(tmp_path: Path) -> None:
    # `envs/dev.local.yaml` -> stem `dev.local`. Declared env wins; an
    # undeclared stem produces zero envs (silently ignored).
    spec = """
version: 1
release_name: alpha
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
        root=tmp_path,
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
        root=tmp_path,
        changed_files=["README.md", "docs/foo.md"],
    )

    assert result.rows == ()


def test_triggers_strict_fans_out_unmatched_chart_file_to_all_envs(tmp_path: Path) -> None:
    # `templates/deployment.yaml` matches no explicit trigger. In strict
    # mode the worklist fans out to every env in `environments` instead of
    # silently dropping the file.
    spec = """
version: 1
release_name: alpha
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml]
triggers:
  "values.yaml": [dev, prod]
triggers_strict: true
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        root=tmp_path,
        changed_files=["charts/alpha/templates/deployment.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev"), ("alpha", "prod")}


def test_triggers_strict_does_not_override_explicit_trigger(tmp_path: Path) -> None:
    # An explicit trigger still scopes to its listed envs even with strict on.
    spec = """
version: 1
release_name: alpha
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml]
triggers:
  "values-dev.yaml": [dev]
triggers_strict: true
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        root=tmp_path,
        changed_files=["charts/alpha/values-dev.yaml"],
    )

    pairs = {(r.chart, r.env) for r in result.rows}
    assert pairs == {("alpha", "dev")}


def test_triggers_strict_false_silently_drops_unmatched(tmp_path: Path) -> None:
    # Default (non-strict) behavior preserved: an unmatched chart file
    # produces zero rows.
    spec = """
version: 1
release_name: alpha
environments:
  dev:
    namespace: lab-dev
    values: [values.yaml]
triggers:
  "values.yaml": [dev]
"""
    _chart(tmp_path, "alpha", spec=spec)

    result = build_worklist(
        root=tmp_path,
        changed_files=["charts/alpha/templates/deployment.yaml"],
    )

    assert result.rows == ()
