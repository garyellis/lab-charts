"""Coverage for `build_helm_dependency_index` edge cases.

The worklist's library-chart fanout relies on this index; these tests
pin its tolerant-by-design behavior so we notice if it silently changes.
"""
from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.graph import build_helm_dependency_index


def _chart(root: Path, name: str, *, chart_yaml: str) -> None:
    chart_dir = root / "charts" / name
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(chart_yaml)


def test_empty_when_no_charts_dir(tmp_path: Path) -> None:
    assert build_helm_dependency_index(tmp_path) == {}


def test_indexes_simple_dependency(tmp_path: Path) -> None:
    _chart(tmp_path, "common", chart_yaml="apiVersion: v2\nname: common\nversion: 0.1.0\n")
    _chart(
        tmp_path,
        "alpha",
        chart_yaml=(
            "apiVersion: v2\nname: alpha\nversion: 0.1.0\n"
            "dependencies:\n  - name: common\n    version: 0.1.0\n"
        ),
    )

    index = build_helm_dependency_index(tmp_path)

    assert index == {"common": {"alpha"}}


def test_handles_cycle_without_crashing(tmp_path: Path) -> None:
    # Helm itself rejects cycles, but the index is just a name->dependents
    # map — it should not recurse, so a cycle is just two reverse edges.
    _chart(
        tmp_path,
        "alpha",
        chart_yaml=(
            "apiVersion: v2\nname: alpha\nversion: 0.1.0\n"
            "dependencies:\n  - name: beta\n    version: 0.1.0\n"
        ),
    )
    _chart(
        tmp_path,
        "beta",
        chart_yaml=(
            "apiVersion: v2\nname: beta\nversion: 0.1.0\n"
            "dependencies:\n  - name: alpha\n    version: 0.1.0\n"
        ),
    )

    index = build_helm_dependency_index(tmp_path)

    assert index == {"beta": {"alpha"}, "alpha": {"beta"}}


def test_skips_dependency_entries_without_name(tmp_path: Path) -> None:
    # An OCI ref without an explicit `name:` (legal in Chart.yaml when
    # `alias` is used, or just authoring sloppiness) is dropped silently
    # rather than crashing the index build.
    _chart(
        tmp_path,
        "alpha",
        chart_yaml=(
            "apiVersion: v2\nname: alpha\nversion: 0.1.0\n"
            "dependencies:\n"
            "  - version: 0.1.0\n"
            "    repository: oci://example.com/charts\n"
            "  - name: common\n"
            "    version: 0.1.0\n"
        ),
    )

    index = build_helm_dependency_index(tmp_path)

    assert index == {"common": {"alpha"}}


def test_unknown_dependency_name_still_indexed(tmp_path: Path) -> None:
    # A dependency on a chart that doesn't exist locally (OCI/remote) is
    # still indexed — the worklist treats "no dependents" as "no fanout"
    # by lookup, so a stale name is just a no-op key.
    _chart(
        tmp_path,
        "alpha",
        chart_yaml=(
            "apiVersion: v2\nname: alpha\nversion: 0.1.0\n"
            "dependencies:\n  - name: not-here\n    version: 0.1.0\n"
        ),
    )

    index = build_helm_dependency_index(tmp_path)

    assert index == {"not-here": {"alpha"}}


def test_malformed_yaml_is_silently_skipped(tmp_path: Path) -> None:
    # Defensive parse — a corrupt Chart.yaml does not poison the rest of
    # the index. (This means a typo silently disables fanout; the M5 CI
    # render phase will surface it loudly. Documented trade-off.)
    _chart(tmp_path, "broken", chart_yaml=":not yaml:\n  - [unclosed\n")
    _chart(
        tmp_path,
        "alpha",
        chart_yaml=(
            "apiVersion: v2\nname: alpha\nversion: 0.1.0\n"
            "dependencies:\n  - name: common\n    version: 0.1.0\n"
        ),
    )

    index = build_helm_dependency_index(tmp_path)

    assert index == {"common": {"alpha"}}
