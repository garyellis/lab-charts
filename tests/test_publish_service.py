"""Batch publish preflight, version, and partial remote failure semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.integrations.helm import PackageResult, PushResult
from chart_manager.plumbing.errors import ExternalCommandError, SpecError
from chart_manager.services.publish import PublishService, with_version_suffix

from .conftest import MakeChart


class _Helm:
    def __init__(self, *, fail_package: str | None = None, fail_push: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_package = fail_package
        self.fail_push = fail_push

    def dependency_update(self, chart: Path) -> None:
        self.calls.append(("deps", chart.name))

    def package(
        self, chart: Path, output: Path, *, version: str | None = None
    ) -> PackageResult:
        self.calls.append(("package", chart.name))
        if chart.name == self.fail_package:
            raise ExternalCommandError("package failed")
        selected = version or "base"
        return PackageResult(output / f"{chart.name}-{selected}.tgz", "packaged")

    def push(
        self,
        package: Path,
        repository: str,
        *,
        ca_file: Path | None = None,
    ) -> PushResult:
        del ca_file
        name = package.name.split("-", 1)[0]
        self.calls.append(("push", name))
        if name == self.fail_push:
            raise ExternalCommandError("push failed")
        return PushResult(f"{repository}/{name}:version", f"sha256:{name}", "pushed")


def test_version_suffix_preserves_existing_prerelease_and_build() -> None:
    assert with_version_suffix("6.2.1", "pr.318.g1a2b3c4") == "6.2.1-pr.318.g1a2b3c4"
    assert (
        with_version_suffix("6.2.1-rc.1+build.7", "pr.318.gabc")
        == "6.2.1-rc.1.pr.318.gabc+build.7"
    )


@pytest.mark.parametrize("suffix", ["", "-pr.1", "pr..1", "pr.01", "pr_1"])
def test_invalid_suffix_is_rejected(suffix: str) -> None:
    with pytest.raises(SpecError, match="suffix"):
        with_version_suffix("1.2.3", suffix)


def test_batch_prepares_every_chart_before_any_push(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha", version="1.0.0")
    make_chart("beta", version="2.0.0")
    helm = _Helm()

    result = PublishService(chart_root, helm=helm).publish(  # type: ignore[arg-type]
        ["alpha", "beta"],
        repository="oci://registry.local/library",
        version_suffix="pr.8.gabc",
    )

    assert result.ok
    assert helm.calls == [
        ("deps", "alpha"),
        ("package", "alpha"),
        ("deps", "beta"),
        ("package", "beta"),
        ("push", "alpha"),
        ("push", "beta"),
    ]


def test_preflight_failure_pushes_nothing(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart("alpha")
    make_chart("beta")
    helm = _Helm(fail_package="beta")

    with pytest.raises(ExternalCommandError, match="package failed"):
        PublishService(chart_root, helm=helm).publish(  # type: ignore[arg-type]
            ["alpha", "beta"], repository="oci://registry.local/library"
        )

    assert not any(call[0] == "push" for call in helm.calls)


def test_push_failures_are_consolidated_and_remaining_pushes_continue(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")
    helm = _Helm(fail_push="alpha")

    result = PublishService(chart_root, helm=helm).publish(  # type: ignore[arg-type]
        ["alpha", "beta"], repository="oci://registry.local/library"
    )

    assert not result.ok
    assert [item.ok for item in result.charts] == [False, True]
    assert helm.calls[-2:] == [("push", "alpha"), ("push", "beta")]


def test_exact_version_requires_one_chart(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart("alpha")
    make_chart("beta")
    with pytest.raises(SpecError, match="exactly one"):
        PublishService(chart_root, helm=_Helm()).publish(  # type: ignore[arg-type]
            ["alpha", "beta"],
            repository="oci://registry.local/library",
            version="2.0.0",
        )
