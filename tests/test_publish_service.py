"""Batch publish preflight, version, and partial remote failure semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.integrations.helm import PackageResult, PushResult
from chart_manager.plumbing.errors import ExternalCommandError, SpecError
from chart_manager.services.events.lifecycle import BuildPhase
from chart_manager.services.publish import (
    PublishKind,
    PublishService,
    target_reference,
    with_version_suffix,
)

from .conftest import MakeChart


class _Helm:
    def __init__(self, *, fail_package: str | None = None, fail_push: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.expected_references: list[str | None] = []
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
        expected_reference: str | None = None,
    ) -> PushResult:
        del ca_file
        name = package.name.split("-", 1)[0]
        self.calls.append(("push", name))
        self.expected_references.append(expected_reference)
        if name == self.fail_push:
            raise ExternalCommandError("push failed")
        return PushResult(
            expected_reference or f"{repository}/{name}:version",
            f"sha256:{name}",
            "pushed",
        )


class _Events:
    def __init__(self, *, fail_chart: str | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_chart = fail_chart

    def build(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if kwargs["chart_name"] == self.fail_chart:
            raise RuntimeError("events backend unavailable")


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
    events = _Events()

    with pytest.raises(ExternalCommandError, match="package failed"):
        PublishService(chart_root, helm=helm, events=events).publish(  # type: ignore[arg-type]
            ["alpha", "beta"], repository="oci://registry.local/library"
        )

    assert not any(call[0] == "push" for call in helm.calls)
    assert events.calls == []


def test_push_failures_are_consolidated_and_remaining_pushes_continue(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")
    helm = _Helm(fail_push="alpha")
    events = _Events()

    result = PublishService(chart_root, helm=helm, events=events).publish(  # type: ignore[arg-type]
        ["alpha", "beta"], repository="oci://registry.local/library"
    )

    assert not result.ok
    assert [item.ok for item in result.charts] == [False, True]
    assert helm.calls[-2:] == [("push", "alpha"), ("push", "beta")]
    assert [call["chart_name"] for call in events.calls] == ["beta"]


def test_preview_publish_emits_retry_safe_event_for_each_success(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha", version="1.0.0")
    make_chart("beta", version="2.0.0")
    events = _Events()
    helm = _Helm()
    service = PublishService(chart_root, helm=helm, events=events)  # type: ignore[arg-type]

    first = service.publish(
        ["alpha", "beta"],
        repository="oci://registry.local/library",
        version_suffix="pr.8.gabc",
        build_correlation_id="owner/repository#8",
        pr_url="https://github.test/owner/repository/pull/8",
        git_sha="abcdef12",
        operation_id="100.1",
    )
    service.publish(
        ["alpha", "beta"],
        repository="oci://registry.local/library",
        version_suffix="pr.8.gabc",
        build_correlation_id="owner/repository#8",
    )

    assert first.ok and first.telemetry_ok
    assert [call["phase"] for call in events.calls[:2]] == [
        BuildPhase.PREVIEW_PUBLISHED,
        BuildPhase.PREVIEW_PUBLISHED,
    ]
    assert [call["chart_version"] for call in events.calls[:2]] == [
        "1.0.0-pr.8.gabc",
        "2.0.0-pr.8.gabc",
    ]
    assert events.calls[0]["build_correlation_id"] == "owner/repository#8"
    assert events.calls[0]["pr_url"] == "https://github.test/owner/repository/pull/8"
    assert events.calls[0]["git_sha"] == "abcdef12"
    assert events.calls[0]["detail"] == {
        "publish_kind": "preview",
        "repository": "oci://registry.local/library",
        "reference": "oci://registry.local/library/alpha:1.0.0-pr.8.gabc",
        "digest": "sha256:alpha",
        "operation_id": "100.1",
        "batch_index": 1,
        "batch_count": 2,
    }
    assert events.calls[0]["idempotency_key"] == events.calls[2]["idempotency_key"]
    assert helm.expected_references[:2] == [
        "oci://registry.local/library/alpha:1.0.0-pr.8.gabc",
        "oci://registry.local/library/beta:2.0.0-pr.8.gabc",
    ]


def test_release_publish_uses_final_published_phase(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha", version="1.2.3")
    events = _Events()

    PublishService(chart_root, helm=_Helm(), events=events).publish(  # type: ignore[arg-type]
        ["alpha"],
        repository="oci://registry.local/library",
        publish_kind=PublishKind.RELEASE,
    )

    assert events.calls[0]["phase"] is BuildPhase.PUBLISHED
    assert events.calls[0]["chart_version"] == "1.2.3"


def test_release_kind_rejects_preview_version_suffix(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha", version="1.2.3")

    with pytest.raises(SpecError, match="release publishing"):
        PublishService(chart_root, helm=_Helm()).publish(  # type: ignore[arg-type]
            ["alpha"],
            repository="oci://registry.local/library",
            version_suffix="pr.8.gabc",
            publish_kind=PublishKind.RELEASE,
        )


def test_event_failure_is_reported_without_changing_artifact_success(
    chart_root: Path, make_chart: MakeChart
) -> None:
    make_chart("alpha")
    make_chart("beta")
    events = _Events(fail_chart="alpha")

    result = PublishService(
        chart_root, helm=_Helm(), events=events  # type: ignore[arg-type]
    ).publish(["alpha", "beta"], repository="oci://registry.local/library")

    assert result.ok
    assert not result.telemetry_ok
    assert len(events.calls) == 2
    assert result.telemetry_failures[0].chart == "alpha"
    assert result.telemetry_failures[0].error == "events backend unavailable"


def test_dry_run_packages_everything_and_pushes_nothing(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """The plan is computed for real; only the remote mutation is skipped."""
    make_chart("alpha", version="1.0.0")
    make_chart("beta", version="2.0.0")
    helm = _Helm()
    events = _Events()

    result = PublishService(chart_root, helm=helm, events=events).publish(  # type: ignore[arg-type]
        ["alpha", "beta"],
        repository="oci://registry.local/library/",
        version_suffix="pr.8.gabc",
        dry_run=True,
    )

    assert result.ok and result.dry_run
    assert helm.calls == [
        ("deps", "alpha"),
        ("package", "alpha"),
        ("deps", "beta"),
        ("package", "beta"),
    ]
    assert [(item.chart, item.version, item.reference) for item in result.charts] == [
        ("alpha", "1.0.0-pr.8.gabc", "oci://registry.local/library/alpha:1.0.0-pr.8.gabc"),
        ("beta", "2.0.0-pr.8.gabc", "oci://registry.local/library/beta:2.0.0-pr.8.gabc"),
    ]
    assert all(item.digest is None for item in result.charts)


def test_dry_run_emits_no_lifecycle_event(chart_root: Path, make_chart: MakeChart) -> None:
    """A dry-run event would burn the idempotency key of the real publish.

    `_emit_publish_events` derives that key from the chart identity, so an
    event written here would make the subsequent real publish look like a
    retry of an artifact that was never pushed.
    """
    make_chart("alpha", version="1.2.3")
    events = _Events()
    service = PublishService(chart_root, helm=_Helm(), events=events)  # type: ignore[arg-type]

    service.publish(
        ["alpha"],
        repository="oci://registry.local/library",
        build_correlation_id="owner/repository#8",
        dry_run=True,
    )
    assert events.calls == []

    service.publish(["alpha"], repository="oci://registry.local/library")
    assert [call["phase"] for call in events.calls] == [BuildPhase.PUBLISHED]


def test_dry_run_plan_matches_what_the_real_publish_pushes(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """The property that matters: the plan does not diverge from reality.

    Both runs take the identical argument set through the identical method;
    the only difference is the branch at the push boundary. Comparing the
    planned references against `helm.expected_references` -- the value the
    real run actually handed to `helm push` -- rather than against the fake's
    echoed `PushResult.reference` keeps this assertion independent of what
    the fake chooses to return.

    Residual gap: a real registry may echo a reference that differs from the
    expected one, and no digest is knowable before the push. Neither is
    reachable without a live OCI registry.
    """
    make_chart("alpha", version="1.0.0")
    make_chart("beta", version="2.0.0")
    arguments: dict[str, object] = {
        "repository": "oci://registry.local/library",
        "version_suffix": "pr.8.gabc",
        "build_correlation_id": "owner/repository#8",
    }

    planning_helm = _Helm()
    planned = PublishService(chart_root, helm=planning_helm).publish(  # type: ignore[arg-type]
        ["alpha", "beta"], dry_run=True, **arguments  # type: ignore[arg-type]
    )
    real_helm = _Helm()
    real = PublishService(chart_root, helm=real_helm).publish(  # type: ignore[arg-type]
        ["alpha", "beta"], **arguments  # type: ignore[arg-type]
    )

    assert [(item.chart, item.version) for item in planned.charts] == [
        (item.chart, item.version) for item in real.charts
    ]
    assert [item.reference for item in planned.charts] == real_helm.expected_references
    assert planned.publish_kind is real.publish_kind is PublishKind.PREVIEW
    # The preparation phase is identical; the real run only adds pushes.
    assert real_helm.calls[: len(planning_helm.calls)] == planning_helm.calls


def test_dry_run_rejects_what_a_real_publish_rejects(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """Validation runs before the branch, so a dry run cannot bless bad input."""
    make_chart("alpha")
    make_chart("beta")
    helm = _Helm()

    with pytest.raises(SpecError, match="exactly one"):
        PublishService(chart_root, helm=helm).publish(  # type: ignore[arg-type]
            ["alpha", "beta"],
            repository="oci://registry.local/library",
            version="2.0.0",
            dry_run=True,
        )
    with pytest.raises(SpecError, match="release publishing"):
        PublishService(chart_root, helm=helm).publish(  # type: ignore[arg-type]
            ["alpha"],
            repository="oci://registry.local/library",
            version_suffix="pr.8.gabc",
            publish_kind=PublishKind.RELEASE,
            dry_run=True,
        )
    assert helm.calls == []


def test_dry_run_reports_the_inferred_release_kind(
    chart_root: Path, make_chart: MakeChart
) -> None:
    """Kind inference is silent today; the plan is where it becomes visible."""
    make_chart("alpha", version="1.2.3")

    result = PublishService(chart_root, helm=_Helm()).publish(  # type: ignore[arg-type]
        ["alpha"], repository="oci://registry.local/library", dry_run=True
    )

    assert result.publish_kind is PublishKind.RELEASE
    assert result.charts[0].version == "1.2.3"


def test_target_reference_is_the_one_definition_of_the_push_target() -> None:
    assert (
        target_reference("oci://registry.local/library/", "alpha", "1.0.0")
        == "oci://registry.local/library/alpha:1.0.0"
    )


def test_exact_version_requires_one_chart(chart_root: Path, make_chart: MakeChart) -> None:
    make_chart("alpha")
    make_chart("beta")
    with pytest.raises(SpecError, match="exactly one"):
        PublishService(chart_root, helm=_Helm()).publish(  # type: ignore[arg-type]
            ["alpha", "beta"],
            repository="oci://registry.local/library",
            version="2.0.0",
        )
