from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.services.upgrader import (
    UpgradeError,
    UpgradeRequest,
    UpgradeService,
    UpgradeTelemetry,
    build_upgrade_plan,
)


def _chart(tmp_path: Path) -> Path:
    chart = tmp_path / "charts" / "my-chart"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: my-chart\nversion: 0.4.2\n", encoding="utf-8"
    )
    return chart


def test_plan_has_deterministic_branch_group_and_scoped_overlay(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    first = build_upgrade_plan(tmp_path, chart)
    second = build_upgrade_plan(tmp_path, Path("my-chart"))
    assert first.branch_prefix == second.branch_prefix == "renovate/my-chart/"
    assert first.group == second.group == "chart-manager:my-chart"
    assert first.runtime_overlay["packageRules"][0]["matchFileNames"] == ["charts/my-chart/**"]
    assert first.runtime_overlay["enabledManagers"] == ["helmv3", "helm-values", "custom.regex"]
    assert first.runtime_overlay["lockFileMaintenance"] == {"enabled": False}
    # The chart scope and its matching branch namespace must outrank the
    # repository's own renovate.json, which Renovate merges last.
    assert first.runtime_overlay["force"] == {
        "includePaths": ["charts/my-chart/**"],
        "branchPrefix": "renovate/my-chart/",
        "branchPrefixOld": "renovate/my-chart/",
    }
    callback = first.runtime_overlay["postUpgradeTasks"]
    assert callback["commands"] == [
        "chart-manager upgrade-finalize --path charts/my-chart"
    ]
    assert '"updateType":"{{updateType}}"' in callback["dataFileTemplate"]


def test_service_uses_injected_adapter_and_factory(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    calls: list[object] = []

    class Adapter:
        def run(self, request: object) -> object:
            calls.append(request)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def factory(plan: object, *, dry_run: bool) -> object:
        return (plan, dry_run)

    result = UpgradeService(Adapter(), factory).upgrade(
        UpgradeRequest(root=tmp_path, chart_path=chart, dry_run=True)
    )
    assert calls
    assert result.outcome == "dry_run"
    assert result.current_version == "0.4.2"
    assert result.to_dict()["chart_path"] == str(chart)


def test_service_projects_new_and_existing_pull_request_status(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    class Adapter:
        def run(self, request: object) -> object:
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    def factory(plan: object, *, dry_run: bool) -> object:
        return plan

    responses = iter(
        (
            (),
            (
                SimpleNamespace(
                    url="https://example.test/pull/7",
                    number=7,
                    branch="renovate/my-chart/my-chart",
                ),
            ),
        )
    )
    service = UpgradeService(
        Adapter(),
        factory,
        pull_request_lookup=lambda prefix: next(responses),
        repository="owner/repository",
        base="main",
    )

    opened = service.upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert opened.outcome == "pr_open"
    assert opened.pr_url == "https://example.test/pull/7"
    assert opened.pr_number == 7
    assert opened.repository == "owner/repository"
    assert opened.base == "main"
    # Reported from the PR's head ref, not re-derived from Renovate's naming.
    assert opened.branch == "renovate/my-chart/my-chart"


def _pr(branch: str = "renovate/my-chart/my-chart") -> SimpleNamespace:
    return SimpleNamespace(url="https://example.test/pull/7", number=7, branch=branch)


class _Ok:
    def run(self, request: object) -> object:
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_proposed_version_is_read_back_from_the_upgrade_branch(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    reads: list[tuple[str, str]] = []

    def read(path: str, ref: str) -> str:
        reads.append((path, ref))
        return "apiVersion: v2\nname: my-chart\nversion: 0.4.3\n"

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=read,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version == "0.4.3"
    assert result.changed is True
    # Read twice against the same branch file: once before Renovate to capture
    # the proposal already on the branch (so a no-op re-run can be told from a
    # retarget), once after for the reported version.
    assert reads == [("charts/my-chart/Chart.yaml", "renovate/my-chart/my-chart")] * 2
    assert result.diagnostics == ()


def test_unbumped_wrapper_version_on_the_branch_is_reported(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    # Renovate records a failed or disallowed post-upgrade command as an
    # artifact error, then opens the pull request and exits zero, so an
    # unbumped wrapper version is the only signal this process can see.
    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=lambda path, ref: "name: my-chart\nversion: 0.4.2\n",
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version == "0.4.2"
    assert any("may not have run" in line for line in result.diagnostics)


def test_unreadable_branch_file_degrades_to_a_diagnostic(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    def unavailable(path: str, ref: str) -> str:
        raise ExternalCommandError("gh api failed")

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=unavailable,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version is None
    # A failed read is a reporting gap, not an unknown pull-request status.
    assert result.outcome == "pr_updated"
    assert any("proposed wrapper version unavailable" in line for line in result.diagnostics)


def test_no_branch_read_without_a_pull_request(tmp_path: Path) -> None:
    chart = _chart(tmp_path)

    def unexpected(path: str, ref: str) -> str:
        raise AssertionError("must not read a branch when no pull request is open")

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (),
        branch_file_reader=unexpected,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "no_changes"
    assert result.proposed_version is None


def test_service_reports_drift_when_a_chart_holds_more_than_one_branch(
    tmp_path: Path,
) -> None:
    chart = _chart(tmp_path)

    class Adapter:
        def run(self, request: object) -> object:
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    found = (
        SimpleNamespace(url="https://example.test/pull/7", number=7, branch="renovate/my-chart/a"),
        SimpleNamespace(url="https://example.test/pull/9", number=9, branch="renovate/my-chart/b"),
    )
    result = UpgradeService(
        Adapter(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: found,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "pr_updated"
    assert any("multiple open pull requests" in line for line in result.diagnostics)


def test_service_does_not_report_no_changes_when_pr_status_is_unavailable(
    tmp_path: Path,
) -> None:
    chart = _chart(tmp_path)

    class Adapter:
        def run(self, request: object) -> object:
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    def unavailable(branch: str) -> object:
        raise ExternalCommandError("gh unavailable")

    result = UpgradeService(
        Adapter(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=unavailable,
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "status_unknown"
    assert result.diagnostics == (
        "pull-request status unavailable: gh unavailable",
        "pull-request status unavailable: gh unavailable",
    )


def test_service_rejects_relevant_uncommitted_inputs_before_renovate(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    called = False

    class Adapter:
        def run(self, request: object) -> object:
            nonlocal called
            called = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(UpgradeError, match="uncommitted changes"):
        UpgradeService(
            Adapter(),
            lambda plan, *, dry_run: plan,
            relevant_changes=lambda paths: ("charts/my-chart/values.yaml",),
        ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert called is False


# ----- build-lifecycle telemetry ------------------------------------------
#
# The mapping itself is covered in test_upgrader_telemetry.py. What matters
# here is that the service emits from the *fully projected* result and only
# after the upgrade is already pushed -- so these tests assert the seam, the
# payload's provenance, and that emission cannot break the run.


class _RecordingEvents:
    """EventWriter stand-in that records build events."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.events: list[dict[str, object]] = []
        self._raises = raises

    def build(self, **kwargs: object) -> None:
        self.events.append(kwargs)
        if self._raises is not None:
            raise self._raises


def _telemetry(events: _RecordingEvents) -> UpgradeTelemetry:
    return UpgradeTelemetry(writer=cast(Any, events))


def test_service_emits_the_version_it_read_back_from_the_branch(tmp_path: Path) -> None:
    chart = _chart(tmp_path)
    events = _RecordingEvents()
    # No pull request beforehand, one after: the run that opens it. A run
    # against an already-open branch proposing the same version transitions
    # nothing and is covered by the de-duplication tests below.
    responses = iter(((), (_pr(),)))

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: next(responses),
        branch_file_reader=lambda path, ref: "name: my-chart\nversion: 0.4.3\n",
        repository="owner/repository",
        telemetry=_telemetry(events),
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version == "0.4.3"
    assert len(events.events) == 1
    emitted = events.events[0]
    assert emitted["chart_name"] == "my-chart"
    assert emitted["chart_version"] == "0.4.3"
    assert emitted["build_correlation_id"] == "owner/repository#7"


def test_service_emits_nothing_when_the_version_read_failed(tmp_path: Path) -> None:
    """An open PR whose version could not be read must not poison the partition.

    This is the same path as
    `test_unreadable_branch_file_degrades_to_a_diagnostic`: outcome is
    `pr_updated` but proposed_version is None, so a naive emit would write a
    correlation id of "my-chart@None".
    """
    chart = _chart(tmp_path)
    events = _RecordingEvents()

    def unavailable(path: str, ref: str) -> str:
        raise ExternalCommandError("gh api failed")

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=unavailable,
        telemetry=_telemetry(events),
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "pr_updated"
    assert events.events == []


def test_dry_run_emits_nothing(tmp_path: Path) -> None:
    """Nothing was pushed, so there is no artifact to report."""
    chart = _chart(tmp_path)
    events = _RecordingEvents()

    UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        telemetry=_telemetry(events),
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart, dry_run=True))

    assert events.events == []


def test_a_failed_emission_does_not_fail_the_upgrade(tmp_path: Path) -> None:
    """The branch is already pushed by the time telemetry runs."""
    chart = _chart(tmp_path)
    events = _RecordingEvents(raises=RuntimeError("cosmos unreachable"))

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=lambda path, ref: "name: my-chart\nversion: 0.4.3\n",
        telemetry=_telemetry(events),
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version == "0.4.3"


def test_service_without_telemetry_still_upgrades(tmp_path: Path) -> None:
    """An upgrade must not require a configured events backend."""
    chart = _chart(tmp_path)

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=lambda path, ref: "name: my-chart\nversion: 0.4.3\n",
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.proposed_version == "0.4.3"


def test_rerun_against_an_unchanged_pull_request_emits_nothing(tmp_path: Path) -> None:
    """The service must read the branch *before* Renovate, or it cannot tell.

    Reported from a real run: three `chart-manager upgrade` invocations against
    one open pull request wrote three identical events. The branch version is
    unchanged across the run, so the second and third transitioned nothing.
    """
    chart = _chart(tmp_path)
    events = _RecordingEvents()
    reads: list[str] = []

    def read(path: str, ref: str) -> str:
        reads.append(ref)
        return "apiVersion: v2\nname: my-chart\nversion: 0.4.3\n"

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=read,
        repository="owner/repository",
        telemetry=_telemetry(events),
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "pr_updated"
    assert result.proposed_version == "0.4.3"
    # Read twice: once before Renovate for the baseline proposal, once after.
    assert len(reads) == 2
    assert events.events == []
    # The pre-read must not leak diagnostics about work the operator did not ask for.
    assert result.diagnostics == ()


def test_rerun_that_retargets_the_version_still_emits(tmp_path: Path) -> None:
    """A major update superseding a patch moves the target while the PR stays open."""
    chart = _chart(tmp_path)
    events = _RecordingEvents()
    versions = iter(("0.4.3", "1.0.0"))  # before Renovate, then after

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: (_pr(),),
        branch_file_reader=lambda path, ref: (
            f"apiVersion: v2\nname: my-chart\nversion: {next(versions)}\n"
        ),
        repository="owner/repository",
        telemetry=_telemetry(events),
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "pr_updated"
    assert result.proposed_version == "1.0.0"
    assert len(events.events) == 1
    assert events.events[0]["chart_version"] == "1.0.0"


def test_no_pre_read_when_no_pull_request_is_open(tmp_path: Path) -> None:
    """The opening run has no branch to compare against, so it cannot be skipped."""
    chart = _chart(tmp_path)
    events = _RecordingEvents()
    reads: list[str] = []
    responses = iter(((), (_pr(),)))

    result = UpgradeService(
        _Ok(),
        lambda plan, *, dry_run: plan,
        pull_request_lookup=lambda prefix: next(responses),
        branch_file_reader=lambda path, ref: (
            reads.append(ref) or "apiVersion: v2\nname: my-chart\nversion: 0.4.3\n"
        ),
        repository="owner/repository",
        telemetry=_telemetry(events),
    ).upgrade(UpgradeRequest(root=tmp_path, chart_path=chart))

    assert result.outcome == "pr_open"
    assert len(reads) == 1  # only the post-Renovate read
    assert len(events.events) == 1
