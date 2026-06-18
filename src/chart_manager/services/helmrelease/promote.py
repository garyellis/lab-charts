from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github, PullRequest
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError

from .editor import set_version
from .scanner import HelmReleaseMatch, scan

CloneFn = Callable[[str, Path, str], None]
DowngradeConfirmFn = Callable[[list[HelmReleaseMatch], str], bool]


@dataclass(frozen=True)
class PromoteRequest:
    flux_repo: str
    path: Path
    environment: str
    chart_name: str
    version: str
    base_branch: str = "main"
    dry_run: bool = False


@dataclass(frozen=True)
class PromoteResult:
    matches: list[HelmReleaseMatch]
    changed_files: list[Path] = field(default_factory=list)
    branch: str | None = None
    pull_request: PullRequest | None = None
    no_changes: bool = False
    dry_run: bool = False
    already_open: bool = False
    aborted: bool = False
    downgrades: list[HelmReleaseMatch] = field(default_factory=list)


def _default_clone(url: str, target: Path, branch: str) -> None:
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
    ) -> None:
        self._git_factory = git_factory
        self._github_factory = github_factory
        self._clone_fn = clone_fn
        # When the target version is older than what's on disk for any match,
        # the service stops and asks this callback. None = fail closed (raise).
        # The CLI wires a typer.confirm; a FastAPI handler wires a force-flag check.
        self._confirm_downgrade = confirm_downgrade

    def promote(self, request: PromoteRequest) -> PromoteResult:
        with tempfile.TemporaryDirectory(prefix="chart-manager-promote-") as tmp:
            workdir = Path(tmp) / "flux"
            self._clone_fn(request.flux_repo, workdir, request.base_branch)
            return self._promote_in_workdir(request, workdir)

    def _promote_in_workdir(
        self, request: PromoteRequest, workdir: Path
    ) -> PromoteResult:
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
            return PromoteResult(matches=matches, no_changes=True, dry_run=request.dry_run)

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
                matches=matches,
                changed_files=changed_files,
                branch=branch,
                dry_run=True,
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
                    matches=matches,
                    branch=branch,
                    aborted=True,
                    downgrades=downgrades,
                )

        git = self._git_factory(workdir)
        github = self._github_factory(workdir)

        existing = github.find_open_pr_for_branch(branch, base=request.base_branch)
        if existing is not None:
            return PromoteResult(
                matches=matches,
                branch=branch,
                pull_request=existing,
                already_open=True,
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
        return PromoteResult(
            matches=matches,
            changed_files=changed_files,
            branch=branch,
            pull_request=pr,
            downgrades=downgrades,
        )


def _is_downgrade(current: str | None, target: str) -> bool:
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
    return f"promote/{request.environment}/{request.chart_name}-{request.version}"


def _pr_title(request: PromoteRequest) -> str:
    return f"chore({request.environment}): promote {request.chart_name} to {request.version}"


def _pr_body(
    request: PromoteRequest, drift: list[HelmReleaseMatch], workdir: Path
) -> str:
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
