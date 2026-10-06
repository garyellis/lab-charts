"""`pr.run()`: clone the flux repo, bump the drifted HelmReleases and open the Promotion PR."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from chart_manager.commands.promote.pr import PromoteRequest, PromoteResult, run
from chart_manager.commands.promote.scanner import HelmReleaseMatch
from chart_manager.commands.promote.state import PromoteStatus
from chart_manager.plumbing.commands import CommandResult
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.shared.events.model import PromotionPhase
from chart_manager.shared.events.writer import EventWriter
from tests.commands.promote.conftest import EventLog, calls
from tests.conftest import FakeCommandRunner, argv_prefix

URL = "git@github.com:org/lab-fluxcd.git"
PR_URL = "https://github.com/org/flux/pull/42"
BRANCH = "promote/prod/loki-0.1.2"

_HR = """\
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: {name}
  namespace: loki
spec:
  chart:
    spec:
      chart: {chart}
      version: "{version}"
"""


class _FluxRemote(FakeCommandRunner):
    """`git clone` copies `repo` into the target; gh answers with `open_pr` and `PR_URL`."""

    def __init__(self, repo: Path, *, open_pr: str | None = None) -> None:
        super().__init__()
        self.repo = repo
        listed = [{"url": open_pr, "number": 7, "baseRefName": "main"}] if open_pr else []
        self.respond(argv_prefix("gh", "pr", "list"), stdout=json.dumps(listed))
        self.respond(argv_prefix("gh", "pr", "create"), stdout=f"{PR_URL}\n")

    def run(self, args: Sequence[str], **kwargs: object) -> CommandResult:
        result = super().run(args, **kwargs)  # type: ignore[arg-type]
        if tuple(args[:2]) == ("git", "clone"):
            shutil.copytree(self.repo, args[-1])
        return result


def _repo(tmp_path: Path, files: Mapping[str, str]) -> Path:
    """A flux repo whose `prod/<file>` holds one loki HelmRelease per listed version."""
    repo = tmp_path / "flux"
    for name, versions in files.items():
        target = repo / "prod" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "".join(
                _HR.format(name=f"loki-{i}", chart="loki", version=v)
                for i, v in enumerate(versions.split(","))
            )
        )
    return repo


def _promote(
    runner: FakeCommandRunner,
    version: str = "0.1.2",
    *,
    path: str = "prod/",
    chart: str = "loki",
    dry_run: bool = False,
    confirm: Callable[[list[HelmReleaseMatch], str], bool] = lambda _d, _t: True,
    events: EventLog | None = None,
) -> PromoteResult:
    return run(
        PromoteRequest(
            flux_repo=URL,
            path=Path(path),
            environment="prod",
            chart_name=chart,
            version=version,
            dry_run=dry_run,
        ),
        runner=runner,
        events=EventWriter(source="chart-manager", store=lambda: events or EventLog()),
        confirm_downgrade=confirm,
    )


def _mutations(runner: FakeCommandRunner) -> list[tuple[str, ...]]:
    """Every git/gh call that changes the repo or GitHub."""
    return [
        argv[:2]
        for argv in runner.calls
        if argv[:2] in {("git", "checkout"), ("git", "add"), ("git", "commit"), ("git", "push")}
        or argv[:3] == ("gh", "pr", "create")
    ]


def test_drift_opens_one_promotion_pr(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a/loki.yaml": "0.1.1", "b/loki.yaml": "0.1.1,0.1.1"})
    grafana = _HR.format(name="grafana", chart="grafana", version="1")
    (repo / "prod/grafana.yaml").write_text(grafana)
    runner = _FluxRemote(repo)

    result = _promote(runner)

    clone = runner.calls[0]
    assert clone[:6] == ("git", "clone", "--depth", "1", "--branch", "main")
    assert clone[6] == URL
    workdir = Path(clone[7]).resolve()
    files = [workdir / "prod/a/loki.yaml", workdir / "prod/b/loki.yaml"]
    title = "chore(prod): promote loki to 0.1.2"
    assert result.status is PromoteStatus.PR_OPENED
    assert result.branch == BRANCH
    assert sorted(result.changed_files) == files
    assert result.pull_request is not None and result.pull_request.url == PR_URL
    assert runner.calls[1] == (
        "gh", "pr", "list", "--head", BRANCH, "--state", "open",
        "--json", "url,number,baseRefName", "--base", "main",
    )  # fmt: skip
    # A file holding two drifted HelmReleases is staged once.
    assert sorted(runner.calls[3][3:]) == [str(f) for f in files]
    assert runner.calls[2] == ("git", "checkout", "-B", BRANCH, "main")
    assert runner.calls[4][:4] == ("git", "commit", "-m", title)
    assert runner.calls[5] == ("git", "push", "-u", "origin", BRANCH)
    create = runner.calls[6]
    assert create[:4] == ("gh", "pr", "create", "--title") and create[4] == title
    assert create[-4:] == ("--head", BRANCH, "--base", "main")
    body = create[6]
    # The body names each file relative to the repo, not the temp clone.
    assert "(prod/a/loki.yaml): `0.1.1` -> `0.1.2`" in body
    assert str(workdir) not in body
    assert runner.calls[4][5] == body


@pytest.mark.parametrize("dry_run", [False, True])
def test_no_drift_is_no_changes_even_on_a_dry_run(tmp_path: Path, dry_run: bool) -> None:
    runner = _FluxRemote(_repo(tmp_path, {"loki.yaml": "0.1.2"}))

    result = _promote(runner, dry_run=dry_run)

    assert result.status is PromoteStatus.NO_CHANGES
    assert result.changed_files == []
    assert runner.calls[1:] == []


def test_dry_run_plans_the_pr_and_touches_nothing(tmp_path: Path) -> None:
    runner = _FluxRemote(_repo(tmp_path, {"loki.yaml": "0.1.1"}))

    result = _promote(runner, dry_run=True)

    assert result.status is PromoteStatus.DRY_RUN
    assert result.branch == BRANCH
    assert [f.name for f in result.changed_files] == ["loki.yaml"]
    assert runner.calls[1:] == []


def test_an_open_pr_for_the_branch_is_returned_without_mutating(tmp_path: Path) -> None:
    runner = _FluxRemote(_repo(tmp_path, {"loki.yaml": "0.1.1"}), open_pr=PR_URL)

    result = _promote(runner)

    assert result.status is PromoteStatus.ALREADY_OPEN
    assert result.pull_request is not None and result.pull_request.url == PR_URL
    assert result.changed_files == []
    assert _mutations(runner) == []


@pytest.mark.parametrize(
    ("chart", "path", "message"),
    [("certz-manager", "prod/", "certz-manager.*not found"), ("loki", "../", "escapes")],
)
def test_a_missing_chart_or_an_escaping_path_raises(
    tmp_path: Path, chart: str, path: str, message: str
) -> None:
    runner = _FluxRemote(_repo(tmp_path, {"loki.yaml": "0.1.1"}))
    with pytest.raises(ChartManagerError, match=message):
        _promote(runner, chart=chart, path=path)


def test_a_failed_clone_raises(tmp_path: Path) -> None:
    runner = _FluxRemote(tmp_path)
    runner.respond(argv_prefix("git", "clone"), returncode=128, stderr="repository not found")
    with pytest.raises(ExternalCommandError, match="repository not found"):
        _promote(runner)


# --- the downgrade gate -----------------------------------------------------


def test_a_confirmed_downgrade_opens_the_pr(tmp_path: Path) -> None:
    runner = _FluxRemote(_repo(tmp_path, {"loki.yaml": "0.2.0"}))
    asked: list[tuple[list[str | None], str]] = []

    def confirm(downgrades: list[HelmReleaseMatch], target: str) -> bool:
        asked.append(([m.current_version for m in downgrades], target))
        return True

    result = _promote(runner, "0.1.0", confirm=confirm)

    assert asked == [(["0.2.0"], "0.1.0")]
    assert result.status is PromoteStatus.PR_OPENED
    assert [m.current_version for m in result.downgrades] == ["0.2.0"]


def test_a_declined_downgrade_aborts_without_mutating(tmp_path: Path) -> None:
    runner = _FluxRemote(_repo(tmp_path, {"loki.yaml": "0.2.0"}))

    result = _promote(runner, "0.1.0", confirm=lambda _d, _t: False)

    assert result.status is PromoteStatus.ABORTED
    assert result.changed_files == []
    assert len(result.downgrades) == 1
    assert runner.calls[1:] == []


@pytest.mark.parametrize(
    ("current", "target", "dry_run", "downgrade"),
    [
        ("0.2.0", "0.1.0", True, True),  # a dry run reports it without asking
        ("0.1.0", "0.2.0", False, False),
        ("latest", "0.1.0", False, False),  # not comparable, so not gated
        ("1.0.0-pr.2", "1.0.0-pr.1", True, True),
        ("1.0.0-1", "1.0.0", True, False),  # a prerelease, not a PEP 440 post-release
        ("1.0.0+build.2", "1.0.0+build.1", True, False),  # build metadata has no precedence
    ],
)
def test_the_downgrade_gate_follows_semver_precedence(
    tmp_path: Path, current: str, target: str, dry_run: bool, downgrade: bool
) -> None:
    def never(_d: list[HelmReleaseMatch], _t: str) -> bool:
        raise AssertionError("confirm_downgrade must not be asked")

    result = _promote(
        _FluxRemote(_repo(tmp_path, {"loki.yaml": current})), target, dry_run=dry_run, confirm=never
    )

    assert len(result.downgrades) == int(downgrade)


# --- the promotion event ----------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target", "dry_run", "open_pr", "confirm", "status", "phases"),
    [
        ("0.1.2", "0.1.2", False, None, True, PromoteStatus.NO_CHANGES, []),
        ("0.1.1", "0.1.2", True, None, True, PromoteStatus.DRY_RUN, []),
        ("0.2.0", "0.1.0", False, None, False, PromoteStatus.ABORTED, [PromotionPhase.ABANDONED]),
        (
            "0.1.1", "0.1.2", False, PR_URL, True,
            PromoteStatus.ALREADY_OPEN, [PromotionPhase.AWAITING_MERGE],
        ),
        (
            "0.1.1", "0.1.2", False, None, True,
            PromoteStatus.PR_OPENED, [PromotionPhase.FLUX_PR_OPEN],
        ),
    ],
)  # fmt: skip
def test_each_terminal_status_emits_its_phase(
    tmp_path: Path,
    current: str,
    target: str,
    dry_run: bool,
    open_pr: str | None,
    confirm: bool,
    status: PromoteStatus,
    phases: list[PromotionPhase],
) -> None:
    events = EventLog()
    result = _promote(
        _FluxRemote(_repo(tmp_path, {"loki.yaml": current}), open_pr=open_pr),
        target,
        dry_run=dry_run,
        confirm=lambda _d, _t: confirm,
        events=events,
    )

    assert result.status is status
    assert events.phases == phases
    if phases and result.pull_request is not None:
        assert events.events[0].promotion_correlation_id == result.pull_request.url


def test_a_failed_event_write_does_not_fail_the_promotion(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    events = EventLog(raises=KeyError("COSMOS_ENDPOINT"))
    runner = _FluxRemote(_repo(tmp_path, {"loki.yaml": "0.1.1"}))

    result = _promote(runner, events=events)

    assert result.status is PromoteStatus.PR_OPENED
    assert len(events.events) == 1
    assert "non-fatal" in caplog.text
    assert calls(runner, "gh", "pr", "create")
