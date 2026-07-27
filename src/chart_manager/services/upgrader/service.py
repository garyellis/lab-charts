"""Public orchestration for one chart-scoped Renovate run."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

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


PullRequestLookup = Callable[[str], PullRequestLike | None]
RelevantChanges = Callable[[Sequence[Path]], Sequence[str]]


class UpgradeService:
    """Validate chart scope, build deterministic policy, and invoke Renovate."""

    def __init__(
        self,
        renovate: RenovateAdapter,
        request_factory: RenovateRequestFactory,
        *,
        pull_request_lookup: PullRequestLookup | None = None,
        relevant_changes: RelevantChanges | None = None,
        repository: str | None = None,
        base: str | None = None,
    ) -> None:
        self._renovate = renovate
        self._request_factory = request_factory
        self._pull_request_lookup = pull_request_lookup
        self._relevant_changes = relevant_changes
        self._repository = repository
        self._base = base

    def upgrade(self, request: UpgradeRequest) -> UpgradeResult:
        plan = build_upgrade_plan(request.root, request.chart_path)
        diagnostics: list[str] = []
        self._require_relevant_files_clean(plan)
        existing_pr = (
            None
            if request.dry_run
            else self._find_pull_request(plan.branch, diagnostics)
        )
        lookup_failed = bool(diagnostics)
        result = self._renovate.run(self._request_factory(plan, dry_run=request.dry_run))
        returncode = int(getattr(result, "returncode", 1))
        stdout = str(getattr(result, "stdout", ""))
        stderr = str(getattr(result, "stderr", ""))
        if returncode:
            raise UpgradeError(
                f"Renovate failed for chart {plan.chart}: {stderr.strip() or stdout.strip()}"
            )
        diagnostics.extend(line for line in stderr.splitlines() if line.strip())
        diagnostics_before_lookup = len(diagnostics)
        current_pr = (
            existing_pr
            if request.dry_run
            else self._find_pull_request(plan.branch, diagnostics)
        )
        lookup_failed = lookup_failed or len(diagnostics) > diagnostics_before_lookup
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
            proposed_version=None,
            branch=plan.branch,
            group=plan.group,
            outcome=outcome,
            diagnostics=tuple(diagnostics),
            repository=self._repository,
            base=self._base,
            pr_url=current_pr.url if current_pr is not None else None,
            pr_number=current_pr.number if current_pr is not None else None,
        )

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
        branch: str,
        diagnostics: list[str],
    ) -> PullRequestLike | None:
        if self._pull_request_lookup is None:
            return None
        try:
            return self._pull_request_lookup(branch)
        except ChartManagerError as exc:
            diagnostics.append(f"pull-request status unavailable: {exc}")
            return None


def build_upgrade_plan(root: Path, chart_path: Path) -> UpgradePlan:
    """Build deterministic chart identity, branch, group and callback overlay."""
    repo_root, resolved, chart = resolve_chart_path(root, chart_path)
    version = chart.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9]\d*)\.\d+\.\d+", version):
        raise UpgradeError(f"Chart.yaml version must be a strict x.y.z version, got {version!r}")
    name = resolved.name
    group = f"chart-manager:{name}"
    branch = f"renovate/{name}"
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
        "includePaths": [f"{relative}/**"],
        "enabledManagers": ["helmv3", "helm-values", "custom.regex"],
        "branchPrefix": "renovate/",
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
        branch=branch,
        group=group,
        runtime_overlay=overlay,
    )
