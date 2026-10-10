"""`promote pr`: clone the flux repo, bump the drifted chart version, open the Promotion PR."""
from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github, PullRequest
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.semver import parse_semver
from chart_manager.shared.events.writer import EventWriter

from .editor import set_version
from .scanner import HelmReleaseMatch, scan
from .state import PROMOTE_OUTCOME, PROMOTE_PHASE, PromoteStatus
from .telemetry import emit_promotion

_LOG = logging.getLogger(__name__)

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

    `status` is the whole state machine.
    """

    chart: str
    version: str
    environment: str
    status: PromoteStatus
    matches: list[HelmReleaseMatch]
    changed_files: list[Path] = field(default_factory=list)
    branch: str | None = None
    pull_request: PullRequest | None = None
    downgrades: list[HelmReleaseMatch] = field(default_factory=list)

    @property
    def outcome(self) -> Outcome:
        """How the promotion ended, for its exit code."""
        return PROMOTE_OUTCOME[self.status]


def _loggable_repo(url: str) -> str:
    """`url` with any `user:password@` userinfo removed.

    Every documented invocation passes an SSH remote (`git@github.com:org/repo`),
    which carries no credential -- but the HTTPS form
    `https://x-access-token:<PAT>@github.com/org/repo` is the standard way to
    hand a token to `git clone` in CI, and this value is logged. Split on the
    last `@` so an SSH remote's `git@` prefix is what survives, not what gets
    mistaken for a credential.
    """
    scheme, separator, rest = url.partition("://")
    if not separator or "@" not in rest:
        return url
    return f"{scheme}://{rest.rsplit('@', 1)[1]}"


def run(
    request: PromoteRequest,
    *,
    runner: CommandRunner,
    events: EventWriter,
    confirm_downgrade: Callable[[list[HelmReleaseMatch], str], bool],
) -> PromoteResult:
    """Clone the flux repo, set the chart version where it drifts, and open the Promotion PR.

    One call promotes one chart into one (path, environment); fanning out to several
    environments, and waiting on cluster state between them, is the caller's job.
    `confirm_downgrade` decides whether to go on when a HelmRelease is at a newer version.
    """
    # The clone target is a temp dir that is gone by the time anyone reads
    # this, so the coordinates that matter are the repo and the path within
    # it -- the two inputs that decide which HelmReleases get edited.
    _LOG.info(
        "promotion started: chart=%s version=%s environment=%s repo=%s path=%s "
        "base=%s dry_run=%s",
        request.chart_name,
        request.version,
        request.environment,
        _loggable_repo(request.flux_repo),
        request.path,
        request.base_branch,
        request.dry_run,
    )
    with tempfile.TemporaryDirectory(prefix="chart-manager-promote-") as tmp:
        workdir = Path(tmp) / "flux"
        Git.clone(request.flux_repo, workdir, branch=request.base_branch, runner=runner)
        result = _promote_in_workdir(request, workdir, runner, confirm_downgrade)
    # `pr_url` doubles as the promotion correlation id (see
    # `_emit_promotion`), which is what ties this line to the events store.
    _LOG.info(
        "promotion finished: chart=%s version=%s environment=%s status=%s "
        "matched=%d changed_files=%d branch=%s pr=%s",
        request.chart_name,
        request.version,
        request.environment,
        result.status,
        len(result.matches),
        len(result.changed_files),
        result.branch or "(none)",
        result.pull_request.url if result.pull_request else "(none)",
    )
    _emit_promotion(request, result, events)
    return result


def _emit_promotion(request: PromoteRequest, result: PromoteResult, events: EventWriter) -> None:
    """Map the terminal state to a PromotionPhase event.

    One table lookup, not an if-chain, so this and the CLI printer in
    `commands/promote/cli.py` decode the same status the same way. Statuses
    mapping to None (dry-run, no changes) are not real transitions and must
    leave no mark. The event is
    written after the PR is open, so a failed write is logged, not raised.
    """
    phase = PROMOTE_PHASE[result.status]
    if phase is None:
        return
    pr = result.pull_request
    emit_promotion(
        events,
        chart_name=request.chart_name,
        chart_version=request.version,
        environment=request.environment,
        phase=phase,
        what="promotion",
        pr_url=pr.url if pr else None,
        promotion_correlation_id=pr.url if pr else None,
    )


def _promote_in_workdir(
    request: PromoteRequest,
    workdir: Path,
    runner: CommandRunner,
    confirm_downgrade: Callable[[list[HelmReleaseMatch], str], bool],
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

    # Every result names the chart, version and environment it promoted.
    result = partial(
        PromoteResult,
        chart=request.chart_name,
        version=request.version,
        environment=request.environment,
    )
    matches = scan(scan_root, chart_name=request.chart_name)
    if not matches:
        raise ChartManagerError(
            f"chart {request.chart_name!r} not found under {str(request.path)!r}"
        )
    drift = [m for m in matches if m.current_version != request.version]
    if not drift:
        # NO_CHANGES wins over DRY_RUN even when --dry-run was passed:
        # a dry run that found nothing to plan did not plan anything.
        return result(
            status=PromoteStatus.NO_CHANGES,
            matches=matches,
        )

    downgrades = [m for m in drift if _is_downgrade(m.current_version, request.version)]

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
        return result(
            status=PromoteStatus.DRY_RUN,
            matches=matches,
            changed_files=changed_files,
            branch=branch,
            downgrades=downgrades,
        )

    if downgrades and not confirm_downgrade(downgrades, request.version):
        _LOG.warning(
            "promotion aborted, downgrade declined: chart=%s version=%s "
            "environment=%s downgrades=%d",
            request.chart_name,
            request.version,
            request.environment,
            len(downgrades),
        )
        return result(
            status=PromoteStatus.ABORTED,
            matches=matches,
            branch=branch,
            downgrades=downgrades,
        )

    git = Git(workdir, runner)
    github = Github(workdir, runner)

    existing = github.find_open_pr_for_branch(branch, base=request.base_branch)
    if existing is not None:
        return result(
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
        # Logged too, because this is the one promotion failure that leaves
        # a real mutation behind and the exception text alone does not say
        # which repo the orphan branch is on.
        _LOG.error(
            "promotion pushed but PR creation failed: chart=%s version=%s "
            "environment=%s repo=%s branch=%s: %s",
            request.chart_name,
            request.version,
            request.environment,
            _loggable_repo(request.flux_repo),
            branch,
            exc,
        )
        raise ChartManagerError(
            f"push succeeded but `gh pr create` failed for branch {branch}: {exc}"
        ) from exc
    # PUSHED vs PR_OPENED is decided here, once.
    return result(
        status=PromoteStatus.PR_OPENED if pr.url else PromoteStatus.PUSHED,
        matches=matches,
        changed_files=changed_files,
        branch=branch,
        pull_request=pr,
        downgrades=downgrades,
    )


def _is_downgrade(current: str | None, target: str) -> bool:
    """True if `current` has higher SemVer precedence than `target`.

    Non-comparable strings are never treated as downgrades.
    """
    # Non-version strings (e.g. "latest", a git SHA, an unset field) are not
    # comparable — don't gate on them; the operator chose those identifiers
    # explicitly and we have no signal that this is unsafe.
    if current is None:
        return False
    try:
        return parse_semver(current).precedence > parse_semver(target).precedence
    except ValueError:
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
