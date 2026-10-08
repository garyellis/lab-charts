"""`ensure_dependencies` runs `helm dependency update` only when Helm's lock is stale.

Fresh means Chart.lock's Helm digest and the materialized dependency identities agree;
filesystem timestamps play no part.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from chart_manager.integrations.helm import Helm
from chart_manager.shared.charts import dependencies as chart_deps
from chart_manager.shared.charts.dependency_update import ensure_dependencies
from tests.conftest import ONE_DEPENDENCY_LOCK, FakeCommandRunner, materialize_dependency


def _write_chart(path: Path, dependencies: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Chart.yaml").write_text(
        "apiVersion: v2\nname: demo\nversion: 0.1.0\n"
        + (
            dependencies
            if dependencies is not None
            else (
                "dependencies:\n"
                "  - name: foo\n"
                "    version: 1.0.0\n"
                "    repository: https://example.test/charts\n"
            )
        )
    )


def _ran_update(chart: Path) -> bool:
    """Whether `ensure_dependencies` ran `helm dependency update` for `chart`."""
    runner = FakeCommandRunner()
    ensure_dependencies(Helm(runner), chart)
    assert runner.calls in ([], [("helm", "dependency", "update", str(chart))])
    return bool(runner.calls)


def _locked(chart: Path, lock: str = ONE_DEPENDENCY_LOCK) -> None:
    (chart / "Chart.lock").write_text(lock)
    (chart / "charts").mkdir()


def test_a_fresh_lock_skips_the_update_whatever_the_file_times(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    _locked(chart)
    materialize_dependency(chart)
    # Unrelated metadata edits and timestamps do not change the content Helm hashes.
    chart_yaml = chart / "Chart.yaml"
    chart_yaml.write_text(chart_yaml.read_text() + "description: edited metadata\n")
    lock = chart / "Chart.lock"
    os.utime(lock, (lock.stat().st_mtime - 60,) * 2)

    assert _ran_update(chart) is False


def test_dependency_digest_covers_all_helm_dependency_fields(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(
        chart,
        dependencies=(
            "dependencies:\n"
            "  - name: foo\n"
            "    version: ^1.0.0\n"
            "    repository: https://example.test/charts\n"
            "    condition: foo.enabled\n"
            "    tags: [backend, cache]\n"
            "    enabled: true\n"
            "    import-values:\n"
            "      - data\n"
            "      - child: exports.data\n"
            "        parent: imports\n"
            "    alias: db\n"
        ),
    )
    _locked(
        chart,
        "dependencies:\n"
        "  - name: foo\n"
        "    version: 1.2.3\n"
        "    repository: https://example.test/charts\n"
        "digest: sha256:e812ebf3588e27c5c0c9bea509cfbd610ffc6311e12fe8766ae4039677b7b44a\n",
    )
    materialize_dependency(chart, version="1.2.3")

    assert chart_deps.deps_are_fresh(chart) is True

    chart_yaml = chart / "Chart.yaml"
    chart_yaml.write_text(chart_yaml.read_text().replace("foo.enabled", "foo.disabled"))
    assert chart_deps.deps_are_fresh(chart) is False


def test_deps_are_fresh_reads_helm_gzip_extra_header(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    _locked(chart)
    materialize_dependency(chart, helm_gzip_extra=True)

    assert chart_deps.deps_are_fresh(chart) is True


def test_a_chart_without_dependencies_skips_the_update(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: demo\nversion: 0.1.0\n")

    assert _ran_update(chart) is False


@pytest.mark.parametrize(("configured", "bound"), [(None, 600.0), (30.0, 30.0)])
def test_a_missing_lock_runs_the_update_within_the_configured_timeout_or_ten_minutes(
    tmp_path: Path, configured: float | None, bound: float
) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    runner = FakeCommandRunner()

    ensure_dependencies(Helm(runner, timeout=configured), chart)

    assert [(r.args, r.timeout) for r in runner.records] == [
        (("helm", "dependency", "update", str(chart)), bound)
    ]


def test_a_lock_without_materialized_charts_runs_the_update(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(ONE_DEPENDENCY_LOCK)

    assert _ran_update(chart) is True


def test_a_partly_materialized_charts_dir_runs_the_update(tmp_path: Path) -> None:
    """Catches an interrupted `helm dependency update` or a pruned charts/foo.tgz."""
    chart = tmp_path / "demo"
    two = (
        "  - name: foo\n"
        "    version: 1.0.0\n"
        "    repository: https://example.test/charts\n"
        "  - name: bar\n"
        "    version: 2.0.0\n"
        "    repository: https://example.test/charts\n"
    )
    _write_chart(chart, dependencies=f"dependencies:\n{two}")
    _locked(chart, f"dependencies:\n{two}digest: sha256:abc\n")
    materialize_dependency(chart, name="foo")

    assert _ran_update(chart) is True


@pytest.mark.parametrize(
    "lock", ["digest: sha256:abc\n", "not: valid: yaml: :::\n"], ids=["no-dependencies", "yaml"]
)
def test_a_malformed_lock_runs_the_update(tmp_path: Path, lock: str) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    _locked(chart, lock)
    materialize_dependency(chart)

    assert _ran_update(chart) is True


def test_deps_are_fresh_fails_closed_on_unsupported_dependency_field(
    tmp_path: Path,
) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    chart_yaml = chart / "Chart.yaml"
    chart_yaml.write_text(chart_yaml.read_text() + "    unsupported: value\n")
    _locked(chart)
    materialize_dependency(chart)

    assert chart_deps.deps_are_fresh(chart) is False


@pytest.mark.parametrize(
    ("name", "version"), [("wrong", "1.0.0"), ("foo", "0.9.0")], ids=["name", "version"]
)
def test_a_wrong_materialized_artifact_runs_the_update(
    tmp_path: Path, name: str, version: str
) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    _locked(chart)
    materialize_dependency(chart, name=name, version=version)

    assert _ran_update(chart) is True


@pytest.mark.parametrize(
    "artifact", ["foo-1.0.0.tgz", "foo/Chart.yaml"], ids=["packaged", "expanded"]
)
def test_a_malformed_downloaded_chart_runs_the_update(tmp_path: Path, artifact: str) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    _locked(chart)
    path = chart / "charts" / artifact
    path.parent.mkdir(exist_ok=True)
    path.write_text("not: : valid: yaml:\n")

    assert _ran_update(chart) is True


def test_an_expanded_matching_chart_is_fresh(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    _locked(chart)
    expanded = chart / "charts" / "foo"
    expanded.mkdir()
    (expanded / "Chart.yaml").write_text("apiVersion: v2\nname: foo\nversion: 1.0.0\n")

    assert _ran_update(chart) is False


def test_unrelated_files_and_dirs_under_charts_are_ignored(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    _locked(chart)
    materialize_dependency(chart)
    (chart / "charts" / "README.txt").write_text("not a chart")
    (chart / "charts" / "cache").mkdir()

    assert _ran_update(chart) is False


def test_two_aliases_of_one_package_are_fresh(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    dependency = (
        "    version: 1.0.0\n"
        "    repository: https://example.test/charts\n"
    )
    _write_chart(
        chart,
        dependencies=(
            "dependencies:\n"
            "  - name: foo\n"
            "    alias: primary\n"
            f"{dependency}"
            "  - name: foo\n"
            "    alias: secondary\n"
            f"{dependency}"
        ),
    )
    _locked(
        chart,
        "dependencies:\n"
        "  - name: foo\n"
        f"{dependency}"
        "  - name: foo\n"
        f"{dependency}"
        "digest: sha256:2c7dc475034b0488d48755633c791fdb114835bd3797bbf26e67d12d05f4bba3\n",
    )
    materialize_dependency(chart)

    assert _ran_update(chart) is False
