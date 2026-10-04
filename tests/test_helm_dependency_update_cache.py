"""Coverage for the dependency freshness gate and per-instance cache.

The lab `up` re-run path is dominated by ~18 `helm dependency update`
invocations (~5-15s each) -- the single biggest tax on iteration. The
`dependency_update_if_stale` elides those when Chart.lock's Helm digest and
dependency identities match the materialized charts.
The per-instance cache then dedupes within a single process.
"""
from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm
from chart_manager.shared.charts import dependencies as chart_deps
from tests.conftest import FakeCommandRunner


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._clear_mise_cache()


def _helm(runner: FakeCommandRunner) -> Helm:
    """A Helm wired the way `Container.helm` wires it.

    The freshness gate is injected, so an un-wired Helm conservatively
    assumes stale and always runs the update. This file is the gate's
    coverage, so it needs the real predicate.
    """
    return Helm(
        runner=runner,
        deps_are_fresh=chart_deps.deps_are_fresh,
        chart_has_dependencies=chart_deps.chart_has_dependencies,
    )


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


def _mtime(path: Path, seconds_ago: float) -> None:
    now = path.stat().st_mtime
    target = now - seconds_ago
    os.utime(path, (target, target))


_LOCK_ONE_DEP = (
    "dependencies:\n"
    "  - name: foo\n"
    "    version: 1.0.0\n"
    "    repository: https://example.test/charts\n"
    "digest: sha256:ac904eb48ba9649a9d5261dfc887cd08080cdddfd2e7bca3217ff88cfaadb27b\n"
)


def _materialize_dep(
    chart: Path,
    name: str = "foo",
    version: str = "1.0.0",
    *,
    helm_gzip_extra: bool = False,
) -> None:
    """Create a minimal real Helm package under ``charts/``."""
    chart_yaml = (
        f"apiVersion: v2\nname: {name}\nversion: {version}\n"
    ).encode()
    info = tarfile.TarInfo(f"{name}/Chart.yaml")
    info.size = len(chart_yaml)
    package = chart / "charts" / f"{name}-{version}.tgz"
    with tarfile.open(package, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(chart_yaml))
    if helm_gzip_extra:
        compressed = package.read_bytes()
        # Helm's Go gzip writer includes FEXTRA. Insert a minimal valid extra
        # field into Python's otherwise equivalent gzip header.
        package.write_bytes(
            compressed[:3]
            + bytes([compressed[3] | 0x04])
            + compressed[4:10]
            + b"\x04\x00HELM"
            + compressed[10:]
        )


def test_dependency_update_if_stale_skips_when_lock_is_fresh(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart)
    # Force Chart.yaml to be older than Chart.lock.
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    runner = FakeCommandRunner()
    helm = _helm(runner)

    ran = helm.dependency_update_if_stale(chart)

    assert ran is False
    assert runner.calls == []


def test_dependency_update_if_stale_ignores_filesystem_mtime(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart)
    # Unrelated metadata edits and filesystem timestamps do not change the
    # dependency content Helm hashes.
    chart_yaml = chart / "Chart.yaml"
    chart_yaml.write_text(chart_yaml.read_text() + "description: edited metadata\n")
    _mtime(chart / "Chart.lock", seconds_ago=60)

    runner = FakeCommandRunner()
    helm = _helm(runner)

    ran = helm.dependency_update_if_stale(chart)

    assert ran is False
    assert runner.calls == []


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
    (chart / "Chart.lock").write_text(
        "dependencies:\n"
        "  - name: foo\n"
        "    version: 1.2.3\n"
        "    repository: https://example.test/charts\n"
        "digest: sha256:e812ebf3588e27c5c0c9bea509cfbd610ffc6311e12fe8766ae4039677b7b44a\n"
    )
    (chart / "charts").mkdir()
    _materialize_dep(chart, version="1.2.3")

    assert chart_deps.deps_are_fresh(chart) is True

    chart_yaml = chart / "Chart.yaml"
    chart_yaml.write_text(chart_yaml.read_text().replace("foo.enabled", "foo.disabled"))
    assert chart_deps.deps_are_fresh(chart) is False


def test_deps_are_fresh_reads_helm_gzip_extra_header(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart, helm_gzip_extra=True)

    assert chart_deps.deps_are_fresh(chart) is True


def test_dependency_update_if_stale_skips_chart_without_dependencies(
    tmp_path: Path,
) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart, dependencies="")
    runner = FakeCommandRunner()

    assert _helm(runner).dependency_update_if_stale(chart) is False
    assert runner.calls == []


def test_dependency_update_if_stale_runs_when_lock_missing(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    # No Chart.lock -> must run.

    runner = FakeCommandRunner()
    helm = _helm(runner)

    assert helm.dependency_update_if_stale(chart) is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_dependency_update_if_stale_runs_when_charts_dir_missing(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    # Lock fresh but no `charts/` -> deps were never materialized; must run.

    runner = FakeCommandRunner()
    helm = _helm(runner)

    assert helm.dependency_update_if_stale(chart) is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_dependency_update_if_stale_runs_when_charts_dir_partial(tmp_path: Path) -> None:
    """Lock declares N deps but charts/ contains fewer -> force re-update.

    Catches interrupted `helm dependency update` or manual pruning of
    charts/foo.tgz even when the lock digest is current.
    """
    chart = tmp_path / "demo"
    _write_chart(
        chart,
        dependencies=(
            "dependencies:\n"
            "  - name: foo\n"
            "    version: 1.0.0\n"
            "    repository: https://example.test/charts\n"
            "  - name: bar\n"
            "    version: 2.0.0\n"
            "    repository: https://example.test/charts\n"
        ),
    )
    (chart / "Chart.lock").write_text(
        "dependencies:\n"
        "  - name: foo\n"
        "    version: 1.0.0\n"
        "    repository: https://example.test/charts\n"
        "  - name: bar\n"
        "    version: 2.0.0\n"
        "    repository: https://example.test/charts\n"
        "digest: sha256:abc\n"
    )
    (chart / "charts").mkdir()
    # Only one of two declared deps materialized.
    _materialize_dep(chart, name="foo")
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    runner = FakeCommandRunner()
    helm = _helm(runner)

    assert helm.dependency_update_if_stale(chart) is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_dependency_update_if_stale_runs_when_lock_malformed(tmp_path: Path) -> None:
    """Lock without a `dependencies:` key -> force re-update.

    A lock that helm could not have produced is a strong signal that
    something is off; force a real update so helm can either replace it
    or surface a clear error.
    """
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text("digest: sha256:abc\n")
    (chart / "charts").mkdir()
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    runner = FakeCommandRunner()
    helm = _helm(runner)

    assert helm.dependency_update_if_stale(chart) is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_deps_are_fresh_returns_false_on_malformed_lock_yaml(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text("not: valid: yaml: :::\n")
    (chart / "charts").mkdir()
    _materialize_dep(chart)
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    assert chart_deps.deps_are_fresh(chart) is False


def test_deps_are_fresh_fails_closed_on_unsupported_dependency_field(
    tmp_path: Path,
) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    chart_yaml = chart / "Chart.yaml"
    chart_yaml.write_text(chart_yaml.read_text() + "    unsupported: value\n")
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart)

    assert chart_deps.deps_are_fresh(chart) is False


def test_dependency_update_runs_for_wrong_artifact_with_matching_count(
    tmp_path: Path,
) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart, name="wrong")
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    runner = FakeCommandRunner()
    helm = _helm(runner)

    assert helm.dependency_update_if_stale(chart) is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_dependency_update_runs_for_wrong_artifact_version(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart, version="0.9.0")
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    assert _helm(FakeCommandRunner()).dependency_update_if_stale(chart) is True


def test_dependency_update_skips_for_expanded_matching_chart(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    expanded = chart / "charts" / "foo"
    expanded.mkdir(parents=True)
    (expanded / "Chart.yaml").write_text(
        "apiVersion: v2\nname: foo\nversion: 1.0.0\n"
    )
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    runner = FakeCommandRunner()
    assert _helm(runner).dependency_update_if_stale(chart) is False
    assert runner.calls == []


def test_dependency_update_ignores_unrelated_non_chart_files_and_dirs(
    tmp_path: Path,
) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart)
    (chart / "charts" / "README.txt").write_text("not a chart")
    (chart / "charts" / "cache").mkdir()
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    runner = FakeCommandRunner()
    assert _helm(runner).dependency_update_if_stale(chart) is False
    assert runner.calls == []


def test_dependency_update_runs_for_malformed_chart_package(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    (chart / "charts" / "foo-1.0.0.tgz").write_text("not a tar archive")
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    assert _helm(FakeCommandRunner()).dependency_update_if_stale(chart) is True


def test_dependency_update_accounts_for_two_aliases_of_one_package(
    tmp_path: Path,
) -> None:
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
    (chart / "Chart.lock").write_text(
        "dependencies:\n"
        "  - name: foo\n"
        "    version: 1.0.0\n"
        "    repository: https://example.test/charts\n"
        "  - name: foo\n"
        "    version: 1.0.0\n"
        "    repository: https://example.test/charts\n"
        "digest: sha256:2c7dc475034b0488d48755633c791fdb114835bd3797bbf26e67d12d05f4bba3\n"
    )
    (chart / "charts").mkdir()
    _materialize_dep(chart)
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    runner = FakeCommandRunner()
    assert _helm(runner).dependency_update_if_stale(chart) is False
    assert runner.calls == []


def test_dependency_update_if_stale_per_instance_cache_dedupes(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    # No lock -> first call would run; we just want to confirm the second
    # call is a no-op regardless of freshness.

    runner = FakeCommandRunner()
    helm = _helm(runner)

    assert helm.dependency_update_if_stale(chart) is True
    # Even if the lock is still missing the second call must short-circuit
    # on the per-instance set.
    assert helm.dependency_update_if_stale(chart) is False
    assert len(runner.calls) == 1
