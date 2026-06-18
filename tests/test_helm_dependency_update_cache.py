"""Coverage for the dependency-update mtime gate and per-instance cache.

The lab `up` re-run path is dominated by ~18 `helm dependency update`
invocations (~5-15s each) -- the single biggest tax on iteration. The
`dependency_update_if_stale` gate elides those when Chart.lock is newer
than Chart.yaml and `charts/` exists; the per-instance cache then dedupes
within a single process.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandResult, CommandRunner


class FakeRunner(CommandRunner):
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(args))
        return CommandResult(args=tuple(args), returncode=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._resolve_via_mise.cache_clear()


def _write_chart(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Chart.yaml").write_text(
        "apiVersion: v2\nname: demo\nversion: 0.1.0\n"
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
    "digest: sha256:abc\n"
)


def _materialize_dep(chart: Path, name: str = "foo") -> None:
    """Drop a fake materialized dependency under charts/.

    `helm dependency update` writes deps as either a subchart subdirectory
    or a .tgz tarball; either form satisfies the freshness gate's
    materialization count. We use a .tgz here because it's cheaper to
    create and the gate doesn't crack the file open.
    """
    (chart / "charts" / f"{name}-1.0.0.tgz").write_text("")


def test_dependency_update_if_stale_skips_when_lock_is_fresh(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart)
    # Force Chart.yaml to be older than Chart.lock.
    _mtime(chart / "Chart.yaml", seconds_ago=60)

    runner = FakeRunner()
    helm = Helm(runner=runner)

    ran = helm.dependency_update_if_stale(chart)

    assert ran is False
    assert runner.calls == []


def test_dependency_update_if_stale_runs_when_chart_yaml_is_newer(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    (chart / "charts").mkdir()
    _materialize_dep(chart)
    # Stale lock: Chart.yaml just edited, lock predates it.
    _mtime(chart / "Chart.lock", seconds_ago=60)

    runner = FakeRunner()
    helm = Helm(runner=runner)

    ran = helm.dependency_update_if_stale(chart)

    assert ran is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_dependency_update_if_stale_runs_when_lock_missing(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    # No Chart.lock -> must run.

    runner = FakeRunner()
    helm = Helm(runner=runner)

    assert helm.dependency_update_if_stale(chart) is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_dependency_update_if_stale_runs_when_charts_dir_missing(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    (chart / "Chart.lock").write_text(_LOCK_ONE_DEP)
    # Lock fresh but no `charts/` -> deps were never materialized; must run.

    runner = FakeRunner()
    helm = Helm(runner=runner)

    assert helm.dependency_update_if_stale(chart) is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_dependency_update_if_stale_runs_when_charts_dir_partial(tmp_path: Path) -> None:
    """Lock declares N deps but charts/ contains fewer -> force re-update.

    Catches the partial-materialization case that the mtime gate alone
    misses (interrupted `helm dependency update`, manual prune of
    charts/foo.tgz, etc.).
    """
    chart = tmp_path / "demo"
    _write_chart(chart)
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

    runner = FakeRunner()
    helm = Helm(runner=runner)

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

    runner = FakeRunner()
    helm = Helm(runner=runner)

    assert helm.dependency_update_if_stale(chart) is True
    assert runner.calls == [("helm", "dependency", "update", str(chart))]


def test_lock_dep_count_returns_none_on_malformed_yaml(tmp_path: Path) -> None:
    """Unparseable YAML must return None, never raise.

    The freshness helper feeds into a gate that runs on every chart in the
    install plan -- a raise here would tank the whole `lab up` run.
    """
    lock = tmp_path / "Chart.lock"
    lock.write_text("not: valid: yaml: :::\n")

    assert helm_module._lock_dep_count(lock) is None


def test_lock_dep_count_counts_declared_deps(tmp_path: Path) -> None:
    lock = tmp_path / "Chart.lock"
    lock.write_text(
        "dependencies:\n"
        "  - name: foo\n"
        "    version: 1.0.0\n"
        "  - name: bar\n"
        "    version: 2.0.0\n"
        "  - name: baz\n"
        "    version: 3.0.0\n"
        "digest: sha256:abc\n"
    )

    assert helm_module._lock_dep_count(lock) == 3


def test_dependency_update_if_stale_per_instance_cache_dedupes(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    _write_chart(chart)
    # No lock -> first call would run; we just want to confirm the second
    # call is a no-op regardless of freshness.

    runner = FakeRunner()
    helm = Helm(runner=runner)

    assert helm.dependency_update_if_stale(chart) is True
    # Even if the lock is still missing the second call must short-circuit
    # on the per-instance set.
    assert helm.dependency_update_if_stale(chart) is False
    assert len(runner.calls) == 1
