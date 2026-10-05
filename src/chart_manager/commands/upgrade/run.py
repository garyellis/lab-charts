"""Run `chart upgrade`: Renovate proposes dependency updates for one wrapper chart."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path

from chart_manager.commands.upgrade.finalize import DATA_FILE_TEMPLATE, wrapper_version
from chart_manager.commands.upgrade.models import (
    UpgradeError,
    UpgradePlan,
    UpgradeRequest,
    UpgradeResult,
    UpgradeStatus,
)
from chart_manager.commands.upgrade.paths import CHART_FILE, resolve_chart_path
from chart_manager.commands.upgrade.telemetry import UpgradeTelemetry
from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github, PullRequest
from chart_manager.integrations.renovate import Renovate, RenovateRequest
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, YamlError
from chart_manager.plumbing.yaml_files import parse_yaml_mapping
from chart_manager.services.events.writer import EventWriter
from chart_manager.shared.workspace import RepositoryWorkspace

_LOG = logging.getLogger(__name__)

_GLOBAL_CONFIG = "renovate-global.json"
_BRANCH_PREFIX = "renovate/{chart}/"
_GROUP = "chart-manager:{chart}"
#: Renovate's stdout log-level prefixes.
_ERROR_LEVELS = ("ERROR:", "FATAL:")
_WARN_LEVEL = "WARN:"


def run(
    request: UpgradeRequest,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
    events: EventWriter,
) -> UpgradeResult:
    """Run Renovate for one chart and report the pull request it opened or updated."""
    root = workspace.root
    git = Git(root, runner)
    github = Github(root, runner)
    repository = _repository(git)
    plan = build_upgrade_plan(
        root,
        request.chart_path,
        charts_dir=workspace.spec.charts_dir,
    )
    _LOG.info("Planning dependency upgrade for chart %s", plan.chart)
    _LOG.debug("Chart path: %s", plan.chart_path)
    _LOG.debug("Renovate branch prefix: %s", plan.branch_prefix)
    diagnostics: list[str] = []
    _LOG.debug("Checking upgrade inputs for uncommitted changes")
    _require_relevant_files_clean(plan, git)
    chart_config = plan.chart_path / "renovate.json"
    renovate_request = RenovateRequest(
        repo_root=plan.repo_root,
        repository=repository,
        global_config_path=root / _GLOBAL_CONFIG,
        additional_config_path=chart_config if chart_config.is_file() else None,
        runtime_overlay=plan.runtime_overlay,
        dry_run="full" if request.dry_run else None,
        # Renovate's own name first, then the token GitHub Actions provides.
        token=os.environ.get("RENOVATE_TOKEN") or os.environ.get("GITHUB_TOKEN"),
    )
    if request.dry_run:
        diagnostics.extend(_renovate(runner, renovate_request, chart=plan.chart))
        outcome, current_pr = UpgradeStatus.DRY_RUN, None
        previously_proposed = proposed_version = None
    else:
        existing_pr, found_existing = _find_pull_request(github, plan.branch_prefix, diagnostics)
        # Read before Renovate can rewrite the branch, so telemetry can tell a re-run
        # against an unchanged pull request from a retarget.
        previously_proposed, _ = _proposed_version(plan, github, existing_pr)
        diagnostics.extend(_renovate(runner, renovate_request, chart=plan.chart))
        current_pr, found_current = _find_pull_request(github, plan.branch_prefix, diagnostics)
        outcome = _outcome(
            existing_pr, current_pr, lookup_failed=not (found_existing and found_current)
        )
        if outcome is UpgradeStatus.NO_CHANGES:
            diagnostics.append(
                f"Renovate completed without an open pull request under "
                f"{plan.branch_prefix}; no eligible update was proposed"
            )
        proposed_version, diagnostic = _proposed_version(plan, github, current_pr)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    upgrade_result = UpgradeResult(
        chart=plan.chart,
        chart_path=plan.chart_path,
        current_version=plan.current_version,
        proposed_version=proposed_version,
        # The branch Renovate actually opened; None when no PR is open for this chart.
        branch=current_pr.branch if current_pr is not None else None,
        group=plan.group,
        outcome=outcome,
        diagnostics=tuple(diagnostics),
        repository=repository,
        pr_url=current_pr.url if current_pr is not None else None,
        pr_number=current_pr.number if current_pr is not None else None,
    )
    # Emitted last: the upgrade is already pushed, so telemetry can only cost latency.
    UpgradeTelemetry(writer=events).completed(
        upgrade_result, previously_proposed=previously_proposed
    )
    _LOG.info(
        "Upgrade result for %s: outcome=%s current=%s proposed=%s branch=%s pr=%s "
        "diagnostics=%d",
        plan.chart,
        upgrade_result.outcome,
        upgrade_result.current_version,
        upgrade_result.proposed_version or "(none)",
        upgrade_result.branch or "(none)",
        upgrade_result.pr_url or "(none)",
        len(upgrade_result.diagnostics),
    )
    return upgrade_result


def _renovate(runner: CommandRunner, request: RenovateRequest, *, chart: str) -> list[str]:
    """Run Renovate, raise on a failure it reported, and return its stderr and warnings."""
    result = Renovate(runner).run(request)
    stdout, stderr = result.stdout, result.stderr
    if result.returncode:
        raise UpgradeError(
            f"Renovate failed for chart {chart}: {stderr.strip() or stdout.strip()}"
        )
    # Renovate can exit zero after a repository-scoped failure it logged to stdout.
    if _renovate_reported_error(stdout):
        raise UpgradeError(f"Renovate reported an error for chart {chart}: {stdout.strip()}")
    return [line for line in stderr.splitlines() if line.strip()] + list(
        _renovate_warnings(stdout)
    )


def _outcome(
    existing: PullRequest | None, current: PullRequest | None, *, lookup_failed: bool
) -> UpgradeStatus:
    """Classify a pushed run by the chart's open pull request before and after it."""
    if lookup_failed:
        return UpgradeStatus.STATUS_UNKNOWN
    if current is None:
        return UpgradeStatus.NO_CHANGES
    if existing is None:
        return UpgradeStatus.PR_OPEN
    return UpgradeStatus.PR_UPDATED


def _repository(git: Git) -> str:
    """Read owner/repository from CI metadata or the origin remote."""
    configured = os.environ.get("GITHUB_REPOSITORY")
    if configured:
        return configured
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", git.remote_url() or "")
    if match is None:
        raise ChartManagerError(
            "cannot determine Renovate repository; configure an origin remote "
            "or set GITHUB_REPOSITORY"
        )
    return match.group(1)


def _proposed_version(
    plan: UpgradePlan,
    github: Github,
    pull_request: PullRequest | None,
) -> tuple[str | None, str | None]:
    """Read the wrapper version `upgrade-finalize` wrote on the upgrade branch, and any diagnostic.

    Reading the pushed branch is also the only check that the callback ran: Renovate records a
    failed post-upgrade command as an artifact error and still opens the pull request.
    """
    if pull_request is None or not pull_request.branch:
        return None, None
    branch = pull_request.branch
    chart_file = f"{plan.chart_path.relative_to(plan.repo_root).as_posix()}/{CHART_FILE}"
    try:
        text = github.read_file_at_ref(chart_file, branch)
    except ChartManagerError as exc:
        version, diagnostic = None, f"proposed wrapper version unavailable: {exc}"
    else:
        version = _chart_version(text)
        if version is None:
            diagnostic = f"no wrapper version found in {chart_file} on {branch}"
        elif version == plan.current_version:
            diagnostic = (
                f"wrapper version on {branch} still matches the baseline "
                f"{plan.current_version}; the upgrade-finalize callback may not have run"
            )
        else:
            return version, None
    _LOG.warning("%s: chart=%s", diagnostic, plan.chart)
    return version, diagnostic


def _require_relevant_files_clean(plan: UpgradePlan, git: Git) -> None:
    paths = (
        plan.chart_path,
        plan.repo_root / _GLOBAL_CONFIG,
        plan.repo_root / "renovate.json",
    )
    changed = git.status_paths(tuple(path.relative_to(git.root) for path in paths))
    if changed:
        rendered = ", ".join(sorted(changed))
        raise UpgradeError(
            "upgrade inputs have uncommitted changes; commit or restore them first: "
            f"{rendered}"
        )


def _find_pull_request(
    github: Github,
    branch_prefix: str,
    diagnostics: list[str],
) -> tuple[PullRequest | None, bool]:
    """Return the chart's open PR and whether its status is known."""
    try:
        found = github.find_open_prs_for_branch_prefix(branch_prefix)
    except ChartManagerError as exc:
        _LOG.warning(
            "pull-request lookup failed; upgrade outcome degrades to "
            "status_unknown: branch_prefix=%s: %s",
            branch_prefix,
            exc,
        )
        diagnostics.append(f"pull-request status unavailable: {exc}")
        return None, False
    if len(found) > 1:
        # One chart should hold one branch; more means grouping no longer collapses it.
        branches = ", ".join(sorted(pr.branch for pr in found))
        _LOG.warning(
            "multiple open pull requests for one chart; the first is used: "
            "branch_prefix=%s branches=%s",
            branch_prefix,
            branches,
        )
        diagnostics.append(
            f"multiple open pull requests under {branch_prefix}: {branches}"
        )
    return (found[0] if found else None), True


def _chart_version(text: str) -> str | None:
    """Return the wrapper version from a Chart.yaml document, if it has one."""
    try:
        document = parse_yaml_mapping(text, source="proposed Chart.yaml")
    except YamlError:
        return None
    version = document.get("version")
    return version if isinstance(version, str) else None


def _renovate_reported_error(output: str) -> bool:
    """Detect repository failures Renovate logged despite returning zero."""
    return any(
        line.lstrip().startswith(_ERROR_LEVELS)
        for line in output.splitlines()
    )


def _renovate_warnings(output: str) -> tuple[str, ...]:
    """Retain warning headlines from Renovate's stdout logger."""
    return tuple(
        line.strip()
        for line in output.splitlines()
        if line.lstrip().startswith(_WARN_LEVEL)
    )


def build_upgrade_plan(
    root: Path,
    chart_path: Path,
    *,
    charts_dir: Path,
) -> UpgradePlan:
    """Build deterministic chart identity, branch, group and callback overlay."""
    repo_root, resolved, chart = resolve_chart_path(
        root,
        chart_path,
        charts_dir=charts_dir,
    )
    version = str(wrapper_version(chart.get("version"), source="Chart.yaml version"))
    name = resolved.name
    group = _GROUP.format(chart=name)
    # Renovate prunes stale branches by `branchPrefix` alone; a per-chart prefix keeps a
    # one-chart run from autoclosing every other chart's pull request.
    branch_prefix = _BRANCH_PREFIX.format(chart=name)
    relative = resolved.relative_to(repo_root).as_posix()
    overlay: Mapping[str, object] = {
        # `force` survives the merge with the repository's renovate.json, so a stray
        # branchPrefix there cannot break the per-chart scope.
        "force": {
            "includePaths": [f"{relative}/**"],
            "branchPrefix": branch_prefix,
            # Otherwise Renovate moves a new branch back under the default "renovate/".
            "branchPrefixOld": branch_prefix,
        },
        "enabledManagers": ["helmv3", "helm-values", "custom.regex"],
        "lockFileMaintenance": {"enabled": False},
        "packageRules": [
            {
                "matchFileNames": [f"{relative}/**"],
                "groupName": group,
                "groupSlug": name,
                "separateMajorMinor": False,
                "separateMinorPatch": False,
                "separateMultipleMajor": False,
                "groupSingleUpdates": True,
            }
        ],
        "postUpgradeTasks": {
            "commands": [f"chart-manager upgrade-finalize --path {relative}"],
            "fileFilters": [f"{relative}/**"],
            "executionMode": "branch",
            "dataFileTemplate": DATA_FILE_TEMPLATE,
        },
    }
    return UpgradePlan(
        repo_root=repo_root,
        chart_path=resolved,
        chart=name,
        current_version=version,
        branch_prefix=branch_prefix,
        group=group,
        runtime_overlay=overlay,
    )
