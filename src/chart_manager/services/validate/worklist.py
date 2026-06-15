"""Worklist construction.

Three layers:
  - `build_single_row`: M1b's one-row helper for `validate render/schema/policy`.
  - `discover_*`: per-chart discovery (M3).
  - `build_worklist`: M4's git-driven full fanout, consuming per-chart
    `validate-spec.yaml` and a list of changed files.

The worklist is intentionally pure: callers (CLI) decide whether to source
changed files from git, --changed-files, or --all. The function itself
takes only inputs.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.graph import build_helm_dependency_index
from chart_manager.plumbing.validate_models import WorklistRow
from chart_manager.plumbing.validate_spec import (
    MATCH_BY_BASENAME,
    ValidateSpec,
    load_validate_spec,
    resolve_namespace,
)


def build_single_row(*, chart: str, env: str, namespace: str, release: str) -> WorklistRow:
    return WorklistRow(chart=chart, env=env, release=release, namespace=namespace)


def discover_policies(root: Path, chart: str) -> list[Path]:
    """Return the kyverno policy directories that apply to one chart.

    Two scopes — repo-wide `<root>/policies` and per-chart
    `<root>/charts/<chart>/policies` — filtered to existing directories.
    Order matters only insofar as it's the order kyverno walks: repo-wide
    first so per-chart overrides (when they exist) layer on top.
    """
    candidates = [root / "policies", root / "charts" / chart / "policies"]
    return [p for p in candidates if p.is_dir()]


def discover_validate_spec(root: Path, chart: str) -> Path | None:
    """Return `<root>/charts/<chart>/validate-spec.yaml` if present."""
    candidate = root / "charts" / chart / "validate-spec.yaml"
    return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class LoadedSpec:
    """Per-chart spec load outcome.

    Exactly one of `spec` and `error` is set; `missing=True` means there
    was no validate-spec.yaml at all (warn + continue, per decision #1).
    """
    chart: str
    spec: ValidateSpec | None = None
    error: str | None = None
    missing: bool = False


@dataclass(frozen=True)
class WorklistBuildResult:
    rows: tuple[WorklistRow, ...] = ()
    spec_errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    chart_count_unvalidated: int = 0
    # Specs we successfully loaded; CLI uses these to populate RowConfig
    # with values/policy paths/kubernetes_version without re-reading YAML.
    specs: dict[str, ValidateSpec] = field(default_factory=dict)


def load_chart_specs(root: Path, charts: Iterable[str]) -> list[LoadedSpec]:
    out: list[LoadedSpec] = []
    for chart in charts:
        spec_path = discover_validate_spec(root, chart)
        if spec_path is None:
            out.append(LoadedSpec(chart=chart, missing=True))
            continue
        try:
            spec = load_validate_spec(spec_path)
        except SpecError as exc:
            out.append(LoadedSpec(chart=chart, error=str(exc)))
            continue
        out.append(LoadedSpec(chart=chart, spec=spec))
    return out


def build_worklist(
    *,
    root: Path,
    changed_files: list[str] | None = None,
    all_charts: bool = False,
) -> WorklistBuildResult:
    """Build a full worklist from per-chart specs + a list of changed files.

    Resolution order:
      1. `all_charts=True` -> cross-product every chart x env, ignoring git.
      2. `changed_files` provided -> apply §D fanout rules to that list.
      3. Neither -> degrade to all_charts=True semantics (caller already
         resolved the git fallback chain).
    """
    root = root.resolve()
    charts_dir = root / "charts"
    all_chart_names = _list_chart_dirs(charts_dir)
    loaded = load_chart_specs(root, all_chart_names)
    by_chart: dict[str, LoadedSpec] = {ls.chart: ls for ls in loaded}

    spec_errors = tuple(
        f"{ls.chart}: {ls.error}" for ls in loaded if ls.error is not None
    )
    warnings: list[str] = []
    unvalidated = 0
    for ls in loaded:
        if ls.missing:
            warnings.append(f"chart {ls.chart} has no validate-spec.yaml — skipping")
            unvalidated += 1

    usable_specs: dict[str, ValidateSpec] = {
        name: ls.spec for name, ls in by_chart.items()
        if ls.spec is not None and not ls.spec.skip
    }

    if all_charts or changed_files is None:
        rows = _cross_product(usable_specs)
        return WorklistBuildResult(
            rows=rows,
            spec_errors=spec_errors,
            warnings=tuple(warnings),
            chart_count_unvalidated=unvalidated,
            specs=usable_specs,
        )

    fanout_all = False
    accumulated: set[tuple[str, str]] = set()
    dep_index = build_helm_dependency_index(root)

    for raw in changed_files:
        if not raw:
            continue
        rel = Path(raw)
        parts = rel.parts

        # Repo-wide rule: policies/ edit invalidates everything.
        if parts and parts[0] == "policies":
            fanout_all = True
            continue

        # Validate-code edits invalidate everything (narrowed rule).
        if _is_validate_code_path(parts):
            fanout_all = True
            continue
        if _is_other_chart_manager_path(parts):
            continue

        # Per-chart paths.
        if len(parts) >= 2 and parts[0] == "charts":
            chart_name = parts[1]
            if len(parts) == 2:
                # `charts/<C>` itself — treat as a chart-wide edit.
                _add_all_envs(accumulated, usable_specs, chart_name)
                _fanout_dependents(accumulated, usable_specs, dep_index, chart_name)
                continue
            chart_rel = Path(*parts[2:])
            if _is_chart_wide_trigger(chart_rel):
                _add_all_envs(accumulated, usable_specs, chart_name)
                _fanout_dependents(accumulated, usable_specs, dep_index, chart_name)
                continue
            # Edits to a chart that other charts depend on (typical library
            # chart pattern) fan out to dependents even if this chart itself
            # has no validate-spec.yaml.
            _fanout_dependents(accumulated, usable_specs, dep_index, chart_name)
            spec = usable_specs.get(chart_name)
            if spec is None:
                continue
            envs = _envs_for_chart_file(spec, chart_rel)
            for env in envs:
                accumulated.add((chart_name, env))
            continue

        # Anything else under the repo root is ignored.

    if fanout_all:
        rows = _cross_product(usable_specs)
    else:
        rows = _materialize(usable_specs, sorted(accumulated))

    return WorklistBuildResult(
        rows=rows,
        spec_errors=spec_errors,
        warnings=tuple(warnings),
        chart_count_unvalidated=unvalidated,
        specs=usable_specs,
    )


_CHART_WIDE_FILES = {"Chart.yaml", "validate-spec.yaml"}


def _is_chart_wide_trigger(chart_rel: Path) -> bool:
    parts = chart_rel.parts
    if not parts:
        return False
    if parts[0] in _CHART_WIDE_FILES:
        return True
    # `charts/<C>/policies/**` invalidates every env for that chart.
    return parts[0] == "policies"


def _envs_for_chart_file(spec: ValidateSpec, chart_rel: Path) -> list[str]:
    """Resolve a chart-relative changed file to the envs it impacts.

    Contract:
      - Multiple matching triggers are UNIONED (set semantics), not
        last-wins. Overlapping globs (e.g. ``"values.yaml"`` and
        ``"*.yaml"``) both contribute.
      - A file that matches NO trigger:
          * default (``triggers_strict=false``): silently ignored.
          * ``triggers_strict=true``: fans out to every env in
            ``environments``. Catches under-enumerated triggers (e.g. a
            chart whose author wrote a trigger for ``values.yaml`` but
            forgot ``templates/``).
      - ``match-by-basename`` uses ``Path.stem`` so ``envs/dev.yaml`` -> ``dev``
        and ``envs/dev.local.yaml`` -> ``dev.local``. Multi-dot envs work
        as long as the env name is declared in ``environments``.
    """
    chart_rel_str = chart_rel.as_posix()
    envs: set[str] = set()
    matched = False
    for pattern, value in spec.triggers.items():
        if not fnmatch.fnmatchcase(chart_rel_str, pattern):
            continue
        matched = True
        if value == MATCH_BY_BASENAME:
            stem = chart_rel.stem  # envs/dev.yaml -> dev
            if stem in spec.environments:
                envs.add(stem)
            continue
        if isinstance(value, list):
            for env in value:
                if env in spec.environments:
                    envs.add(env)
    if not matched and spec.triggers_strict:
        return sorted(spec.environments)
    return sorted(envs)


def _add_all_envs(
    sink: set[tuple[str, str]],
    specs: dict[str, ValidateSpec],
    chart: str,
) -> None:
    spec = specs.get(chart)
    if spec is None:
        return
    for env in spec.environments:
        sink.add((chart, env))


def _fanout_dependents(
    sink: set[tuple[str, str]],
    specs: dict[str, ValidateSpec],
    dep_index: dict[str, set[str]],
    chart: str,
) -> None:
    for dependent in dep_index.get(chart, set()):
        _add_all_envs(sink, specs, dependent)


def _materialize(
    specs: dict[str, ValidateSpec],
    pairs: list[tuple[str, str]],
) -> tuple[WorklistRow, ...]:
    rows: list[WorklistRow] = []
    for chart, env in pairs:
        spec = specs.get(chart)
        if spec is None or env not in spec.environments:
            continue
        rows.append(
            WorklistRow(
                chart=chart,
                env=env,
                release=spec.release_name,
                namespace=resolve_namespace(spec, env),
            )
        )
    return tuple(rows)


def _cross_product(specs: dict[str, ValidateSpec]) -> tuple[WorklistRow, ...]:
    pairs: list[tuple[str, str]] = []
    for chart in sorted(specs):
        for env in sorted(specs[chart].environments):
            pairs.append((chart, env))
    return _materialize(specs, pairs)


def _list_chart_dirs(charts_dir: Path) -> list[str]:
    if not charts_dir.is_dir():
        return []
    return sorted(
        p.name for p in charts_dir.iterdir()
        if p.is_dir() and (p / "Chart.yaml").is_file()
    )


_VALIDATE_CODE_PREFIXES = (
    ("src", "chart_manager", "services", "validate"),
)
_VALIDATE_INTEGRATIONS = {"helm.py", "kubeconform.py", "kyverno.py"}


def _is_validate_code_path(parts: tuple[str, ...]) -> bool:
    for prefix in _VALIDATE_CODE_PREFIXES:
        if parts[: len(prefix)] == prefix:
            return True
    # src/chart_manager/integrations/{helm,kubeconform,kyverno}.py
    return (
        len(parts) >= 4
        and parts[:3] == ("src", "chart_manager", "integrations")
        and parts[3] in _VALIDATE_INTEGRATIONS
    )


def _is_other_chart_manager_path(parts: tuple[str, ...]) -> bool:
    # Everything else under src/chart_manager/ is out of scope for fanout.
    return len(parts) >= 2 and parts[:2] == ("src", "chart_manager")
