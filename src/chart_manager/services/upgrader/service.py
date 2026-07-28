"""Public orchestration for one chart-scoped Renovate run."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ruamel.yaml import YAML, YAMLError

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.upgrader.errors import UpgradeError
from chart_manager.services.upgrader.models import (
    UpgradePlan,
    UpgradeRequest,
    UpgradeResult,
)
from chart_manager.services.upgrader.paths import resolve_chart_path


class RenovateAdapter(Protocol):
    """Structural seam implemented by the Renovate integration."""

    def run(self, request: Any) -> Any:
        """Run Renovate and return an object with returncode/stdout/stderr."""
        ...


class RenovateRequestFactory(Protocol):
    """Build the adapter-owned request without coupling this service to it."""

    def __call__(self, plan: UpgradePlan, *, dry_run: bool) -> Any:
        """Translate a validated plan to the adapter's request."""
        ...


class PullRequestLike(Protocol):
    """Minimal PR projection needed by the upgrade result."""

    url: str
    number: int | None
    branch: str


PullRequestLookup = Callable[[str], Sequence[PullRequestLike]]
RelevantChanges = Callable[[Sequence[Path]], Sequence[str]]
BranchFileReader = Callable[[str, str], str]


class UpgradeService:
    """Validate chart scope, build deterministic policy, and invoke Renovate."""

    def __init__(
        self,
        renovate: RenovateAdapter,
        request_factory: RenovateRequestFactory,
        *,
        pull_request_lookup: PullRequestLookup | None = None,
        relevant_changes: RelevantChanges | None = None,
        branch_file_reader: BranchFileReader | None = None,
        repository: str | None = None,
        base: str | None = None,
    ) -> None:
        self._renovate = renovate
        self._request_factory = request_factory
        self._pull_request_lookup = pull_request_lookup
        self._relevant_changes = relevant_changes
        self._branch_file_reader = branch_file_reader
        self._repository = repository
        self._base = base

    def upgrade(self, request: UpgradeRequest) -> UpgradeResult:
        plan = build_upgrade_plan(request.root, request.chart_path)
        diagnostics: list[str] = []
        self._require_relevant_files_clean(plan)
        existing_pr, found_existing = (
            (None, True)
            if request.dry_run
            else self._find_pull_request(plan.branch_prefix, diagnostics)
        )
        result = self._renovate.run(self._request_factory(plan, dry_run=request.dry_run))
        returncode = int(getattr(result, "returncode", 1))
        stdout = str(getattr(result, "stdout", ""))
        stderr = str(getattr(result, "stderr", ""))
        if returncode:
            raise UpgradeError(
                f"Renovate failed for chart {plan.chart}: {stderr.strip() or stdout.strip()}"
            )
        diagnostics.extend(line for line in stderr.splitlines() if line.strip())
        current_pr, found_current = (
            (existing_pr, True)
            if request.dry_run
            else self._find_pull_request(plan.branch_prefix, diagnostics)
        )
        lookup_failed = not (found_existing and found_current)
        if request.dry_run:
            outcome = "dry_run"
        elif lookup_failed:
            outcome = "status_unknown"
        elif current_pr is None:
            outcome = "no_changes"
        elif existing_pr is None:
            outcome = "pr_open"
        else:
            outcome = "pr_updated"
        return UpgradeResult(
            chart=plan.chart,
            chart_path=plan.chart_path,
            current_version=plan.current_version,
            proposed_version=self._proposed_version(plan, current_pr, diagnostics),
            # The branch Renovate actually opened, not a local re-derivation of
            # its naming algorithm; None when no PR is open for this chart.
            branch=current_pr.branch if current_pr is not None else None,
            group=plan.group,
            outcome=outcome,
            diagnostics=tuple(diagnostics),
            repository=self._repository,
            base=self._base,
            pr_url=current_pr.url if current_pr is not None else None,
            pr_number=current_pr.number if current_pr is not None else None,
        )

    def _proposed_version(
        self,
        plan: UpgradePlan,
        pull_request: PullRequestLike | None,
        diagnostics: list[str],
    ) -> str | None:
        """Read the wrapper version the finalizer wrote on the upgrade branch.

        The version is decided by `upgrade-finalize`, which Renovate runs as a
        post-upgrade task inside its own checkout, so this process never sees
        it directly. Reading it back from the pushed branch reports the artifact
        that exists rather than a local prediction of it -- and doubles as the
        only check that the callback ran at all: Renovate records a failed or
        disallowed post-upgrade command as an artifact error and still opens the
        pull request with a zero exit code.
        """
        if self._branch_file_reader is None or pull_request is None or not pull_request.branch:
            return None
        relative = plan.chart_path.relative_to(plan.repo_root).as_posix()
        try:
            text = self._branch_file_reader(f"{relative}/Chart.yaml", pull_request.branch)
        except ChartManagerError as exc:
            diagnostics.append(f"proposed wrapper version unavailable: {exc}")
            return None
        version = _chart_version(text)
        if version is None:
            diagnostics.append(
                f"no wrapper version found in {relative}/Chart.yaml on {pull_request.branch}"
            )
            return None
        if version == plan.current_version:
            diagnostics.append(
                f"wrapper version on {pull_request.branch} still matches the baseline "
                f"{plan.current_version}; the upgrade-finalize callback may not have run"
            )
        return version

    def _require_relevant_files_clean(self, plan: UpgradePlan) -> None:
        if self._relevant_changes is None:
            return
        paths = (
            plan.chart_path,
            plan.repo_root / "renovate-global.json",
            plan.repo_root / "renovate.json",
        )
        changed = tuple(self._relevant_changes(paths))
        if changed:
            rendered = ", ".join(sorted(changed))
            raise UpgradeError(
                "upgrade inputs have uncommitted changes; commit or restore them first: "
                f"{rendered}"
            )

    def _find_pull_request(
        self,
        branch_prefix: str,
        diagnostics: list[str],
    ) -> tuple[PullRequestLike | None, bool]:
        """Return the chart's open PR and whether its status is known."""
        if self._pull_request_lookup is None:
            return None, True
        try:
            found = tuple(self._pull_request_lookup(branch_prefix))
        except ChartManagerError as exc:
            diagnostics.append(f"pull-request status unavailable: {exc}")
            return None, False
        if len(found) > 1:
            # One chart is expected to hold one branch. More than one means the
            # grouping config no longer collapses this chart's updates into a
            # single branch; report it rather than silently picking one.
            branches = ", ".join(sorted(pr.branch for pr in found))
            diagnostics.append(
                f"multiple open pull requests under {branch_prefix}: {branches}"
            )
        return (found[0] if found else None), True


def _chart_version(text: str) -> str | None:
    """Return the wrapper version from a Chart.yaml document, if it has one."""
    try:
        document = YAML(typ="safe").load(text)
    except YAMLError:
        return None
    if not isinstance(document, Mapping):
        return None
    version = document.get("version")
    return version if isinstance(version, str) else None


def build_upgrade_plan(root: Path, chart_path: Path) -> UpgradePlan:
    """Build deterministic chart identity, branch, group and callback overlay."""
    repo_root, resolved, chart = resolve_chart_path(root, chart_path)
    version = chart.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9]\d*)\.\d+\.\d+", version):
        raise UpgradeError(f"Chart.yaml version must be a strict x.y.z version, got {version!r}")
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
    data_template = (
        '{"updates":['
        "{{#each upgrades}}"
        '{"depName":"{{depName}}","currentValue":"{{currentValue}}",'
        '"newValue":"{{newValue}}","manager":"{{manager}}",'
        '"datasource":"{{datasource}}","updateType":"{{updateType}}"}'
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
