"""Filesystem predicates over one chart directory's declared/materialized deps.

These are chart-repository knowledge, not helm-CLI knowledge: they read
`Chart.yaml`, `Chart.lock` and `charts/`, and touch no helm binary, no
`CommandRunner` and no adapter state. They lived in `integrations/helm.py`
purely because `dependency_update_if_stale` was their first caller, which
made the adapter the place a reader had to look for "what does this repo
consider a fresh chart".

Deliberately separate from `plumbing/charts.py`: that module is the repo
index (`ChartRepository`, test-spec loading, `SpecError`/`ChartNotFoundError`
semantics). Nothing here raises -- every function answers a yes/no/how-many
question and degrades to the conservative answer, because each one gates a
*skip*, and being wrong must cost a redundant `helm dependency update`
rather than a missing dependency.

`plumbing/graph.py` re-parses `Chart.yaml` for dependencies with its own
duplicate try/except. This is the home those can converge on; that
convergence has not been done.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def is_local_chart(chart_ref: str | Path) -> bool:
    """True if `chart_ref` is an existing filesystem path (not oci:// or http(s)://)."""
    ref = str(chart_ref)
    if ref.startswith(("oci://", "http://", "https://")):
        return False
    return Path(ref).exists()


def chart_has_dependencies(chart_path: Path) -> bool:
    """True if Chart.yaml declares a non-empty `dependencies:` list; False on parse errors."""
    chart_yaml = chart_path / "Chart.yaml"
    if not chart_yaml.is_file():
        return False
    try:
        data = yaml.safe_load(chart_yaml.read_text()) or {}
    except (yaml.YAMLError, OSError):
        # Defer the actual error to `helm template`, which will surface a
        # clear chart-loading message. We only return False so we don't
        # spuriously call `helm dependency update` on an unparseable chart.
        return False
    if not isinstance(data, dict):
        return False
    deps = data.get("dependencies") or []
    return isinstance(deps, list) and bool(deps)


def lock_dep_count(lock_path: Path) -> int | None:
    """Return the number of dependencies declared in a Chart.lock.

    Returns None when the lock cannot be parsed, has no `dependencies:`
    key, or yields a non-list value -- any of which forces the caller to
    re-run `helm dependency update` rather than trust a stale or
    malformed lock. We never raise from this helper because it's a hint
    for a freshness gate, not a contract.
    """
    try:
        data = yaml.safe_load(lock_path.read_text()) or {}
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    deps = data.get("dependencies")
    if not isinstance(deps, list):
        return None
    return len(deps)


def deps_are_fresh(chart_path: Path) -> bool:
    """Return True if Chart.lock looks newer than Chart.yaml AND charts/ is consistent.

    Four-condition gate, all must hold to skip the update:
      * Chart.lock exists
      * charts/ directory exists (`helm dependency update` writes deps there)
      * Chart.lock mtime >= Chart.yaml mtime
      * Chart.lock's `dependencies:` count matches the number of subchart
        artifacts under charts/ (subdirectories + .tgz tarballs). A partial
        materialization (interrupted update, manually pruned charts/)
        defeats the mtime check on its own.

    Any failure to stat / parse (race against a delete, malformed lock,
    permission error) returns False so the caller falls through to a real
    `helm dependency update` -- we never want this gate to mask a missing
    or partially-installed dependency.
    """
    chart_yaml = chart_path / "Chart.yaml"
    chart_lock = chart_path / "Chart.lock"
    charts_dir = chart_path / "charts"
    try:
        if not chart_lock.is_file():
            return False
        if not charts_dir.is_dir():
            return False
        if not chart_yaml.is_file():
            # No Chart.yaml is an upstream bug; let `helm dependency update`
            # produce its own error rather than silently skipping.
            return False
        if chart_lock.stat().st_mtime < chart_yaml.stat().st_mtime:
            return False
    except OSError:
        return False

    expected = lock_dep_count(chart_lock)
    if expected is None:
        # Malformed or missing dependencies key -> force a real update so
        # helm can produce a clean lock and error message.
        return False

    # Count materialized deps: helm writes each dependency either as a
    # subdirectory (local repo or expanded chart) or as a .tgz tarball
    # under charts/. Either form counts toward consistency with the lock.
    try:
        materialized = sum(
            1
            for entry in charts_dir.iterdir()
            if entry.is_dir() or (entry.is_file() and entry.suffix == ".tgz")
        )
    except OSError:
        return False
    return materialized == expected
