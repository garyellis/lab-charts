"""Promote a chart to an environment: clone the flux repo, edit version drift, open a PR."""
from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github, PullRequest
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.events.writer import EventWriter

from .editor import set_version
from .scanner import HelmReleaseMatch, scan
from .state import PROMOTE_PHASE, PromoteStatus
from .telemetry import emit_non_fatal

CloneFn = Callable[[str, Path, str], None]
DowngradeConfirmFn = Callable[[list[HelmReleaseMatch], str], bool]


@dataclass(frozen=True)
class PromoteRequest:
    """Inputs for one promotion: which chart/version into which env of which flux repo."""

    flux_repo: str
    path: Path
    environment: str
    chart_name: str
    version: str
    base_branch: str = "main"
    dry_run: bool = False


@dataclass(frozen=True)
class PromoteResult:
    """Outcome of a promote: the one terminal state plus what matched/changed.

    `status` is the whole state machine. The four boolean properties below are
    compatibility shims over it, kept because seven assertions in
    `tests/test_helmrelease_promote_service.py` read them and because a
    caller asking "did this abort?" reads better than a comparison against an
    enum member. They are derived, never stored, so the pair
    (`already_open=True`, `pull_request=None`) that the old five-boolean
    encoding permitted is now unrepresentable.
    """

    status: PromoteStatus
    matches: list[HelmReleaseMatch]
    changed_files: list[Path] = field(default_factory=list)
    branch: str | None = None
    pull_request: PullRequest | None = None
    downgrades: list[HelmReleaseMatch] = field(default_factory=list)

    @property
    def no_changes(self) -> bool:
        """True when every match was already at the target version."""
        return self.status is PromoteStatus.NO_CHANGES

    @property
    def dry_run(self) -> bool:
        """True when the run planned a PR but wrote nothing.

        Note this is the *outcome*, not the request flag: a `--dry-run`
        invocation that finds no drift reports NO_CHANGES, because "nothing
        to do" is what happened. The old encoding set both booleans; no
        caller distinguished them and both suppress the lifecycle event.
        """
        return self.status is PromoteStatus.DRY_RUN

    @property
    def already_open(self) -> bool:
        """True when a PR for this exact branch was already open."""
        return self.status is PromoteStatus.ALREADY_OPEN

    @property
    def aborted(self) -> bool:
        """True when a downgrade was detected and the confirm callback declined."""
        return self.status is PromoteStatus.ABORTED


def _default_clone(url: str, target: Path, branch: str) -> None:
    """Default clone strategy: shallow `git clone` of one branch."""
    Git.clone(url, target, branch=branch)


class PromoteService:
    """Clone the flux repo, scan for the chart, edit drift, open a PR."""

    def __init__(
        self,
        *,
        git_factory: Callable[[Path], Git] = Git,
        github_factory: Callable[[Path], Github] = Github,
        clone_fn: CloneFn = _default_clone,
        confirm_downgrade: DowngradeConfirmFn | None = None,
        events: EventWriter | None = None,
        strict_events: bool = False,
    ) -> None:
        """Wire git/github factories, clone + downgrade-confirm strategies, and event writer."""
        self._git_factory = git_factory
        self._github_factory = github_factory
        self._clone_fn = clone_fn
        # When the target version is older than what's on disk for any match,
        # the service stops and asks this callback. None = fail closed (raise).
        # The CLI wires a typer.confirm; a FastAPI handler wires a force-flag check.
        self._confirm_downgrade = confirm_downgrade

        # lazy store
        self._events = events or EventWriter()
        # Telemetry is non-fatal by default (mirrors `cli/events.py:_emit`):
        # the emission happens *after* the PR is already open, so an
        # unconfigured events backend must not turn a successful promotion
        # into a traceback. Opt in to `strict_events` where the event is
        # itself the deliverable (e.g. a backfill job).
        self._strict_events = strict_events

    def promote(self, request: PromoteRequest) -> PromoteResult:
        """Clone into a temp dir, promote in-tree, emit the lifecycle event, and return."""
        with tempfile.TemporaryDirectory(prefix="chart-manager-promote-") as tmp:
            workdir = Path(tmp) / "flux"
            self._clone_fn(request.flux_repo, workdir, request.base_branch)
            result = self._promote_in_workdir(request, workdir)
        self._emit_promotion(request, result)
        return result

    def _emit_promotion(self, request: PromoteRequest, result: PromoteResult) -> None:
        """Map the terminal state to a PromotionPhase event.

        One table lookup, not an if-chain: the CLI printer decodes the same
        status in `cli/helmrelease.py` and the two used to walk the flags in
        different orders, so a new terminal state could be handled by one and
        silently dropped by the other. Statuses mapping to None (dry-run, no
        changes) are not real transitions and must leave no mark.
        """
        phase = PROMOTE_PHASE[result.status]
        if phase is None:
            return
        pr = result.pull_request
        emit_non_fatal(
            lambda: self._events.promote(
                chart_name=request.chart_name,
                chart_version=request.version,
                environment=request.environment,
                phase=phase,
                pr_url=pr.url if pr else None,
                promotion_correlation_id=pr.url if pr else None,
            ),
            strict=self._strict_events,
            what="promotion",
        )

    def _promote_in_workdir(
        self, request: PromoteRequest, workdir: Path
    ) -> PromoteResult:
        """Scan for drift, optionally confirm downgrades, edit files, and open a PR.

        Returns early (no PR) for: path escape guard, no matches (raises),
        no drift, dry-run, aborted downgrade, or an already-open PR.
        """
        workdir_resolved = workdir.resolve()
        scan_root = (workdir_resolved / request.path).resolve()
        # A `--path ../../` typo would silently scan (and edit) files outside
        # the cloned tree. Fail fast with a clear message.
        if not scan_root.is_relative_to(workdir_resolved):
            raise ChartManagerError(f"--path escapes the cloned flux repo: {request.path}")

        matches = scan(scan_root, chart_name=request.chart_name)
        if not matches:
            raise ChartManagerError(
                f"chart {request.chart_name!r} not found under {str(request.path)!r}"
            )
        drift = [m for m in matches if m.current_version != request.version]
        if not drift:
            # NO_CHANGES wins over DRY_RUN even when --dry-run was passed:
            # a dry run that found nothing to plan did not plan anything.
            return PromoteResult(status=PromoteStatus.NO_CHANGES, matches=matches)

        downgrades = [
            m for m in drift if _is_downgrade(m.current_version, request.version)
        ]

        # Dedupe by file path while preserving scan order; a multi-doc file with
        # two HRs for the same chart would otherwise be edited twice.
        changed_files_ordered: dict[Path, None] = {}
        for match in drift:
            changed_files_ordered.setdefault(match.path, None)
        changed_files = list(changed_files_ordered)

        branch = _branch_name(request)
        title = _pr_title(request)
        body = _pr_body(request, drift, workdir_resolved)

        if request.dry_run:
            return PromoteResult(
                status=PromoteStatus.DRY_RUN,
                matches=matches,
                changed_files=changed_files,
                branch=branch,
                downgrades=downgrades,
            )

        if downgrades:
            if self._confirm_downgrade is None:
                raise ChartManagerError(
                    f"refusing to downgrade {request.chart_name} to {request.version}: "
                    f"{len(downgrades)} HelmRelease(s) currently at a newer version. "
                    "Inject a confirm_downgrade callback (or pass --allow-downgrade)."
                )
            if not self._confirm_downgrade(downgrades, request.version):
                return PromoteResult(
                    status=PromoteStatus.ABORTED,
                    matches=matches,
                    branch=branch,
                    downgrades=downgrades,
                )

        git = self._git_factory(workdir)
        github = self._github_factory(workdir)

        existing = github.find_open_pr_for_branch(branch, base=request.base_branch)
        if existing is not None:
            return PromoteResult(
                status=PromoteStatus.ALREADY_OPEN,
                matches=matches,
                branch=branch,
                pull_request=existing,
                downgrades=downgrades,
            )

        for file_path in changed_files:
            set_version(
                file_path,
                chart_name=request.chart_name,
                new_version=request.version,
            )

        git.checkout_new_branch(branch, base=request.base_branch)
        git.add(changed_files)
        git.commit(title, body=body)
        git.push(branch)
        try:
            pr = github.create_pr(
                title=title,
                body=body,
                head=branch,
                base=request.base_branch,
            )
        except ExternalCommandError as exc:
            # Push has already succeeded; surface the branch so the operator
            # can retry the PR step manually rather than guessing the state.
            raise ChartManagerError(
                f"push succeeded but `gh pr create` failed for branch {branch}: {exc}"
            ) from exc
        # PUSHED vs PR_OPENED is decided here, once. The CLI used to derive
        # it from `pull_request.url` being truthy, which put a second decoder
        # of the same state in the surface layer.
        return PromoteResult(
            status=PromoteStatus.PR_OPENED if pr.url else PromoteStatus.PUSHED,
            matches=matches,
            changed_files=changed_files,
            branch=branch,
            pull_request=pr,
            downgrades=downgrades,
        )



def _is_downgrade(current: str | None, target: str) -> bool:
    """True if `current` is a higher semver than `target`.

    Non-comparable strings are never treated as downgrades.
    """
    # Non-version strings (e.g. "latest", a git SHA, an unset field) are not
    # comparable — don't gate on them; the operator chose those identifiers
    # explicitly and we have no signal that this is unsafe.
    if current is None:
        return False
    try:
        return Version(current) > Version(target)
    except InvalidVersion:
        return False


def _branch_name(request: PromoteRequest) -> str:
    """Deterministic promotion branch name (same request => same branch => idempotent PR)."""
    return f"promote/{request.environment}/{request.chart_name}-{request.version}"


def _pr_title(request: PromoteRequest) -> str:
    """Conventional-commit PR title for the promotion."""
    return f"chore({request.environment}): promote {request.chart_name} to {request.version}"


def _pr_body(
    request: PromoteRequest, drift: list[HelmReleaseMatch], workdir: Path
) -> str:
    """Render the PR body listing each HelmRelease's old -> new version."""
    lines = [
        f"Promote `{request.chart_name}` to `{request.version}` in `{request.environment}`.",
        "",
        f"- environment: `{request.environment}`",
        f"- path: `{request.path}`",
        f"- chart: `{request.chart_name}`",
        f"- target version: `{request.version}`",
        "",
        "## HelmReleases updated",
        "",
    ]
    for m in drift:
        ns = f"{m.namespace}/" if m.namespace else ""
        prev = m.current_version or "(unset)"
        try:
            rel = m.path.relative_to(workdir)
        except ValueError:
            rel = m.path
        lines.append(f"- `{ns}{m.name}` ({rel}): `{prev}` -> `{request.version}`")
    return "\n".join(lines) + "\n"
