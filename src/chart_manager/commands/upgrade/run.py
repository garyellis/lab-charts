"""Run `chart upgrade`: Renovate proposes dependency updates for one wrapper chart."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path

from chart_manager.commands.upgrade.models import (
    UpgradeError,
    UpgradePlan,
    UpgradeRequest,
    UpgradeResult,
)
from chart_manager.commands.upgrade.paths import resolve_chart_path
from chart_manager.commands.upgrade.telemetry import UpgradeTelemetry
from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github, PullRequest
from chart_manager.integrations.renovate import Renovate, RenovateRequest
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, YamlError
from chart_manager.plumbing.semver import parse_bare_version
from chart_manager.plumbing.yaml_files import parse_yaml_mapping
from chart_manager.services.events.writer import EventWriter
from chart_manager.shared.workspace import RepositoryWorkspace

_LOG = logging.getLogger(__name__)


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
        request.root,
        request.chart_path,
        charts_dir=workspace.spec.charts_dir,
    )
    _LOG.info("Planning dependency upgrade for chart %s", plan.chart)
    _LOG.debug("Chart path: %s", plan.chart_path)
    _LOG.debug("Renovate branch prefix: %s", plan.branch_prefix)
    diagnostics: list[str] = []
    _LOG.debug("Checking upgrade inputs for uncommitted changes")
    _require_relevant_files_clean(plan, git)
    existing_pr, found_existing = (
        (None, True)
        if request.dry_run
        else _find_pull_request(github, plan.branch_prefix, diagnostics)
    )
    # The version already on the open branch, read before Renovate can rewrite it, so
    # telemetry can tell a re-run against an unchanged pull request from a retarget.
    previously_proposed = (
        _proposed_version(plan, github, existing_pr, []) if existing_pr is not None else None
    )
    chart_config = plan.chart_path / "renovate.json"
    result = Renovate(runner).run(
        RenovateRequest(
            repo_root=plan.repo_root,
            repository=repository,
            global_config_path=root / "renovate-global.json",
            additional_config_path=chart_config if chart_config.is_file() else None,
            runtime_overlay=plan.runtime_overlay,
            dry_run="full" if request.dry_run else None,
            # Renovate's own name first, then the token GitHub Actions provides.
            token=os.environ.get("RENOVATE_TOKEN") or os.environ.get("GITHUB_TOKEN"),
        )
    )
    returncode = int(getattr(result, "returncode", 1))
    stdout = str(getattr(result, "stdout", ""))
    stderr = str(getattr(result, "stderr", ""))
    if returncode:
        raise UpgradeError(
            f"Renovate failed for chart {plan.chart}: {stderr.strip() or stdout.strip()}"
        )
    # Renovate can exit zero after a repository-scoped failure it logged to stdout.
    if _renovate_reported_error(stdout):
        raise UpgradeError(
            f"Renovate reported an error for chart {plan.chart}: {stdout.strip()}"
        )
    diagnostics.extend(line for line in stderr.splitlines() if line.strip())
    diagnostics.extend(_renovate_warnings(stdout))
    current_pr, found_current = (
        (existing_pr, True)
        if request.dry_run
        else _find_pull_request(github, plan.branch_prefix, diagnostics)
    )
    lookup_failed = not (found_existing and found_current)
    if request.dry_run:
        outcome = "dry_run"
    elif lookup_failed:
        outcome = "status_unknown"
    elif current_pr is None:
        outcome = "no_changes"
        diagnostics.append(
            f"Renovate completed without an open pull request under "
            f"{plan.branch_prefix}; no eligible update was proposed"
        )
    elif existing_pr is None:
        outcome = "pr_open"
    else:
        outcome = "pr_updated"
    upgrade_result = UpgradeResult(
        chart=plan.chart,
        chart_path=plan.chart_path,
        current_version=plan.current_version,
        proposed_version=_proposed_version(plan, github, current_pr, diagnostics),
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
    diagnostics: list[str],
) -> str | None:
    """Read the wrapper version `upgrade-finalize` wrote on the upgrade branch.

    The callback runs inside Renovate's checkout, so reading the pushed branch is also
    the only check that it ran: Renovate records a failed post-upgrade command as an
    artifact error and still opens the pull request with a zero exit code.
    """
    if pull_request is None or not pull_request.branch:
        return None
    relative = plan.chart_path.relative_to(plan.repo_root).as_posix()
    try:
        text = github.read_file_at_ref(f"{relative}/Chart.yaml", pull_request.branch)
    except ChartManagerError as exc:
        _LOG.warning(
            "proposed wrapper version unavailable: chart=%s branch=%s file=%s: %s",
            plan.chart,
            pull_request.branch,
            f"{relative}/Chart.yaml",
            exc,
        )
        diagnostics.append(f"proposed wrapper version unavailable: {exc}")
        return None
    version = _chart_version(text)
    if version is None:
        _LOG.warning(
            "no wrapper version on the upgrade branch: chart=%s branch=%s file=%s",
            plan.chart,
            pull_request.branch,
            f"{relative}/Chart.yaml",
        )
        diagnostics.append(
            f"no wrapper version found in {relative}/Chart.yaml on {pull_request.branch}"
        )
        return None
    if version == plan.current_version:
        _LOG.warning(
            "wrapper version unchanged on the upgrade branch; the "
            "upgrade-finalize callback may not have run: chart=%s branch=%s "
            "baseline=%s",
            plan.chart,
            pull_request.branch,
            plan.current_version,
        )
        diagnostics.append(
            f"wrapper version on {pull_request.branch} still matches the baseline "
            f"{plan.current_version}; the upgrade-finalize callback may not have run"
        )
    return version


def _require_relevant_files_clean(plan: UpgradePlan, git: Git) -> None:
    paths = (
        plan.chart_path,
        plan.repo_root / "renovate-global.json",
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
        line.lstrip().startswith(("ERROR:", "FATAL:"))
        for line in output.splitlines()
    )


def _renovate_warnings(output: str) -> tuple[str, ...]:
    """Retain warning headlines from Renovate's stdout logger."""
    return tuple(
        line.strip()
        for line in output.splitlines()
        if line.lstrip().startswith("WARN:")
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
    raw_version = chart.get("version")
    try:
        version = str(parse_bare_version(raw_version))
    except ValueError as exc:
        raise UpgradeError(
            f"Chart.yaml version must be a strict x.y.z version, got {raw_version!r}"
        ) from exc
    name = resolved.name
    group = f"chart-manager:{name}"
    # Renovate's stale-branch pruning is scoped by `branchPrefix` alone, while
    # this run's extraction is scoped to one chart by `includePaths`. A shared
    # "renovate/" prefix would therefore make every run look like the complete
    # truth for the whole namespace and autoclose every other chart's PR. A
    # per-chart prefix makes the two scopes agree, so pruning stays enabled and
    # only ever reaches this chart's own branches.
    branch_prefix = f"renovate/{name}/"
    relative = resolved.relative_to(repo_root).as_posix()
    # `packageFile` is repo-relative and always a file inside the chart
    # directory, so it attributes a custom.regex match under templates/ as
    # reliably as a Chart.yaml dependency. It is redundant while `includePaths`
    # scopes a run to a single chart; it is carried now so that widening a run to
    # several charts is a filtering change rather than a template change.
    data_template = (
        '{"updates":['
        "{{#each upgrades}}"
        '{"depName":"{{depName}}","currentValue":"{{currentValue}}",'
        '"newValue":"{{newValue}}","manager":"{{manager}}",'
        '"datasource":"{{datasource}}","updateType":"{{updateType}}",'
        '"packageFile":"{{packageFile}}"}'
        "{{#unless @last}},{{/unless}}"
        "{{/each}}"
        "]}"
    )
    overlay: Mapping[str, object] = {
        # `force` is global-only config that Renovate re-applies at the end of
        # every config merge, including the repository's own renovate.json,
        # which is otherwise merged as the child and wins. The chart scope and
        # its matching branch namespace are the two keys that must survive that
        # merge, so a stray branchPrefix in renovate.json cannot silently
        # re-break cross-chart isolation.
        "force": {
            "includePaths": [f"{relative}/**"],
            "branchPrefix": branch_prefix,
            # Defaults to "renovate/". Left alone, Renovate rewrites the branch
            # name back onto the old prefix whenever the new branch does not
            # exist yet, which would undo the scoping on every first run.
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
            "dataFileTemplate": data_template,
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
