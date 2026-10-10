"""Build every preflight check from settings and the runner, and run them all."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial

from chart_manager.commands import validate
from chart_manager.commands.doctor.models import DoctorReport
from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.integrations.kyverno import Kyverno
from chart_manager.integrations.renovate import Renovate
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import WorkspaceNotFoundError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check
from chart_manager.settings import Settings
from chart_manager.shared.events.store import preflight_event_store
from chart_manager.shared.workspace import RepositoryWorkspace


def run(
    *,
    settings: Settings,
    runner: CommandRunner,
    workspace: Callable[[], RepositoryWorkspace],
) -> DoctorReport:
    """Run every check, in report order: toolchain, cluster, repository tools, telemetry.

    `workspace` loads the repository; outside one the schema checks are skipped
    and git/gh probe `settings.root`.
    """
    loaded: RepositoryWorkspace | WorkspaceNotFoundError
    try:
        loaded = workspace()
    except WorkspaceNotFoundError as exc:
        loaded = exc
    root = settings.root.resolve() if isinstance(loaded, WorkspaceNotFoundError) else loaded.root
    timeout = settings.command_timeout
    context = settings.kube_context
    checks: dict[str, Callable[[], Sequence[Check]]] = {
        "helm": Helm(runner, binary="helm", context=context, timeout=timeout).preflight,
        "kubeconform": Kubeconform(runner, timeout=timeout).preflight,
        "kyverno": Kyverno(runner, timeout=timeout).preflight,
        "kubectl": Kubectl(runner, context=context, timeout=timeout).preflight,
        "kind": Kind(runner, docker_host=settings.docker_host, timeout=timeout).preflight,
        "git": Git(root, runner).preflight,
        "github": Github(root, runner).preflight,
        "renovate": partial(
            Renovate(runner).preflight, token_configured=settings.renovate_token is not None
        ),
        "schemas": partial(
            validate.schema_preflight,
            loaded,
            validate.open_schema_store(runner, settings.schema_cache_root),
        ),
        "events": partial(preflight_event_store, settings),
    }
    results = (result for name, check in checks.items() for result in _run_check(name, check))
    return DoctorReport(checks=tuple(results))


def _run_check(name: str, check: Callable[[], Sequence[Check]]) -> tuple[Check, ...]:
    """Run one integration's checks, reporting an unexpected exception as a failed check."""
    try:
        return tuple(check())
    except Exception as exc:
        # Broad on purpose: doctor runs when things are broken, so one adapter
        # raising must not cost the operator the other answers.
        return (
            Check.failed(
                name,
                f"the {name} preflight raised {type(exc).__name__}: {exc}",
                remediation="re-run with -v; if it persists this is a chart-manager bug",
                outcome=Outcome.ENVIRONMENT,
            ),
        )
