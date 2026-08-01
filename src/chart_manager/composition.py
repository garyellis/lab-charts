"""Composition root: the one place adapters are wired into services.

Every surface -- the Typer CLI today, a REST/GraphQL/RPC handler or a Slack
Bolt app tomorrow -- builds its capabilities here instead of reaching for
`integrations/` itself. That is what makes the layering rule enforceable:

    cli/  ->  services/, plumbing/, composition
    composition -> integrations/, services/, plumbing/
    services/ -> integrations/, plumbing/
    integrations/ -> plumbing/

`composition.py` is the ONLY module outside `integrations/` and `services/`
permitted to import `integrations/`. The rule is machine-checked by ruff's
`flake8-tidy-imports` banned-api (TID251); see `[tool.ruff.lint.
flake8-tidy-imports.banned-api]` and the per-file-ignores in pyproject.toml.

Lifetime
--------
`Container` is a plain object; the caller holds it. There is no module-level
singleton and no global mutable state -- a long-lived server constructs one
`Container` at startup and reuses it, while the CLI constructs one per
process. Cheap adapters (HelmReleaseClient, Helm -- subprocess wrappers) are built
per call, matching what the CLI does today. Anything that owns a real client or
a cache is memoized on the container:

  * `command_runner()` -- stateless, shared by every adapter built here.
  * `event_writer()`   -- memoized, so the EventStore it resolves lazily
                          (and the Cosmos/DynamoDB SDK client behind it) is
                          built at most once per container rather than once
                          per emitted event.

Note that store resolution stays *lazy* inside `EventWriter`: `EVENTS_BACKEND`
is still read on first write, not at container construction. Building the
store eagerly would move a failure that `cli/events.py` currently swallows as
non-fatal telemetry to before its try/except, which would be a behavior change.

Test seams
----------
Surfaces keep their module-level `_make_*` factories (see
`cli/helmrelease.py`, `cli/validate.py`) and delegate the body to a
container. Tests that `monkeypatch.setattr(module, "_make_x_service", ...)`
keep working unchanged; tests that want real services with fake adapters can
subclass `Container` or pass a `Settings`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.helmrelease import HelmReleaseClient
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.integrations.kyverno import Kyverno
from chart_manager.integrations.renovate import Renovate, RenovateRequest
from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.ci import CiService
from chart_manager.services.clusters.development import DevelopmentClusterService
from chart_manager.services.clusters.environment import (
    EnvironmentHandle,
    KindEnvironmentProvider,
)
from chart_manager.services.clusters.ephemeral import EphemeralTestClusterService
from chart_manager.services.doctor import CheckProvider, DoctorService
from chart_manager.services.domain import chart_deps
from chart_manager.services.events.store import preflight_event_store
from chart_manager.services.events.writer import EventWriter
from chart_manager.services.expose import ExposeService
from chart_manager.services.grafana.dashboard_export import GrafanaExporter
from chart_manager.services.helmrelease import (
    HelmReleaseRef,
    MonitorService,
    PromoteService,
    TestService,
    Transition,
)
from chart_manager.services.helmrelease.promote import DowngradeConfirmFn
from chart_manager.services.manifest_validation.app import ManifestValidationService
from chart_manager.services.manifest_validation.progress import ProgressDisplay
from chart_manager.services.progress import ProgressCallback
from chart_manager.services.publish import PublishService
from chart_manager.services.upgrader import (
    GitBaselineReader,
    PullRequestLike,
    UpgradeFinalizer,
    UpgradePlan,
    UpgradeService,
    UpgradeTelemetry,
)
from chart_manager.settings import Settings

__all__ = ["Container", "HelmReleaseProgress", "Settings"]

#: Narration callback shape shared by MonitorService and TestService.
HelmReleaseProgress = Callable[[HelmReleaseRef, Transition], None]

#: Operator-warning channel shape accepted by ManifestValidationService.
WarnCallback = Callable[[str], None]


class Container:
    """Assemble configured services. Construct once; the caller holds it."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Bind settings (defaults reproduce today's CLI behavior)."""
        self._settings = settings if settings is not None else Settings()
        self._command_runner: CommandRunner | None = None
        self._event_writer: EventWriter | None = None

    @property
    def settings(self) -> Settings:
        """The settings this container was built from."""
        return self._settings

    # --- adapters ---------------------------------------------------------

    def command_runner(self) -> CommandRunner:
        """The shared subprocess runner (stateless; memoized)."""
        if self._command_runner is None:
            self._command_runner = SubprocessRunner()
        return self._command_runner

    def helmrelease_client(self) -> HelmReleaseClient:
        """A read-only Flux HelmRelease client, addressed by `kubectl()`.

        Composed rather than given its own runner: HelmReleases are ordinary
        custom resources, so every query is a `kubectl get`, and the context
        pin has to be the same one the diagnostics calls use.
        """
        return HelmReleaseClient(self.kubectl())

    def helm(self, *, verbose: bool = True, context: str | None = None) -> Helm:
        """A helm client. `verbose` defaults to the adapter's own default.

        The dependency-freshness predicates are injected here rather than
        imported by the adapter: reading Chart.yaml/Chart.lock is service
        policy, and `integrations/` must not reach up into `services/`.
        """
        return Helm(
            self.command_runner(),
            verbose=verbose,
            context=context if context is not None else self._settings.kube_context,
            deps_are_fresh=chart_deps.deps_are_fresh,
            chart_has_dependencies=chart_deps.chart_has_dependencies,
        )

    def kubectl(self, *, context: str | None = None) -> Kubectl:
        """A kubectl client bound to the configured kube context."""
        return Kubectl(
            self.command_runner(),
            context=context if context is not None else self._settings.kube_context,
            timeout=self._settings.command_timeout,
        )

    def kind(self) -> Kind:
        """A kind/docker client bound to the configured docker daemon."""
        return Kind(
            self.command_runner(),
            docker_host=self._settings.docker_host,
            timeout=self._settings.command_timeout,
        )

    # --- capabilities -----------------------------------------------------

    def event_writer(self) -> EventWriter:
        """The lifecycle-event writer (memoized: one EventStore per container).

        Memoization is the point. `EventWriter` resolves its store lazily and
        caches it per instance, so a fresh writer per call means a fresh
        `EVENTS_BACKEND` read and a fresh SDK client per call -- invisible in
        a process-per-invocation CLI, a per-request leak in a server.
        """
        if self._event_writer is None:
            self._event_writer = EventWriter(source=self._settings.event_source)
        return self._event_writer

    def doctor_service(self, root: Path | None = None) -> DoctorService:
        """Assemble the preflight providers, one per capability.

        This is the whole of `doctor`'s wiring, and it is deliberately dull:
        every value is a *bound method on a configured adapter*, so each
        check runs against exactly the helm binary, kube context and docker
        daemon the real command would use. A provider built any other way --
        a lambda constructing its own adapter, a check implemented in `cli/`
        -- would be free to probe a different tool than the one that then
        fails, which is the failure mode a preflight exists to remove.

        Insertion order is the report order: toolchain first (the things you
        install), then the cluster-facing pair, then the repository tools,
        then telemetry. `DoctorService` preserves it rather than sorting, so
        the order is decided here, next to the reasoning.

        `root` addresses the git/gh checks; it falls back to the configured
        repository root exactly as the CLI's `--root` does.
        """
        runner = self.command_runner()
        resolved_root = (root if root is not None else self._settings.root).resolve()
        providers: dict[str, CheckProvider] = {
            "helm": self.helm().preflight,
            "kubeconform": Kubeconform(runner, timeout=self._settings.command_timeout).preflight,
            "kyverno": Kyverno(runner, timeout=self._settings.command_timeout).preflight,
            "kubectl": self.kubectl().preflight,
            "kind": self.kind().preflight,
            "git": Git(resolved_root, runner, charts_dir=self._settings.charts_dir).preflight,
            "github": Github(resolved_root, runner).preflight,
            "renovate": Renovate(runner).preflight,
            "events": preflight_event_store,
        }
        return DoctorService(providers)

    def monitor_service(self, *, progress: HelmReleaseProgress | None = None) -> MonitorService:
        """Build the HelmRelease convergence monitor.

        Gets the same memoized writer as `promote_service`: a rollout's
        WAITING_ROLLOUT/ROLLOUT_OK events must land on the same store as the
        FLUX_PR_OPEN that opened the interval, or the duration DESIGN.md wants
        is spread across two backends. The service still emits nothing unless
        the request names an environment.
        """
        return MonitorService(
            client=self.helmrelease_client(),
            kubectl=self.kubectl(),
            progress=progress,
            events=self.event_writer(),
        )

    def test_service(self, *, progress: HelmReleaseProgress | None = None) -> TestService:
        """Build the `helm test` runner for matched HelmReleases.

        `verbose=False` is passed explicitly rather than left to the adapter
        default: it is a policy about *this* service (four concurrent
        `helm test` streams interleave into garbage, so TestService captures
        output onto the result instead). `TestService.__init__` states the
        same default for callers that pass no helm at all; passing it here
        keeps the wiring readable and identical to the previous CLI factory.
        """
        return TestService(
            client=self.helmrelease_client(),
            kubectl=self.kubectl(),
            helm=self.helm(verbose=False),
            progress=progress,
            events=self.event_writer(),
        )

    def promote_service(
        self, *, confirm_downgrade: DowngradeConfirmFn | None = None
    ) -> PromoteService:
        """Build the chart-version promotion service.

        `confirm_downgrade` is a surface decision, not configuration: the CLI
        wires an interactive `typer.confirm`, an HTTP handler wires a
        force-flag check. Passing the container's memoized writer keeps
        promotion telemetry on the same store as `chart-manager events`.
        """
        return PromoteService(
            confirm_downgrade=confirm_downgrade,
            events=self.event_writer(),
        )

    def expose_service(
        self, *, state_dir: Path | None = None, context: str | None = None
    ) -> ExposeService:
        """Build the detached port-forward manager."""
        return ExposeService(state_dir=state_dir, kubectl=self.kubectl(context=context))

    def grafana_exporter(self) -> GrafanaExporter:
        """Build the dashboard exporter (port-forward + Grafana HTTP API)."""
        return GrafanaExporter(kubectl=self.kubectl())

    def development_cluster_service(
        self, root: Path, *, progress: ProgressCallback | None = None
    ) -> DevelopmentClusterService:
        """Build the full-stack lab converger for the repo at `root`.

        `root` is a per-invocation argument, not configuration -- see
        `Settings`. Every cluster-facing adapter is passed in, so there is
        no path by which the service can fall back to an unconfigured one.
        """
        kind = self.kind()

        def clients(handle: EnvironmentHandle) -> tuple[Helm, Kubectl, ExposeService]:
            return (
                self.helm(context=handle.context),
                self.kubectl(context=handle.context),
                self.expose_service(context=handle.context),
            )

        return DevelopmentClusterService(
            root,
            helm=self.helm(),
            kind=kind,
            kubectl=self.kubectl(),
            expose=self.expose_service(),
            progress=progress,
            charts_dir=self._settings.charts_dir,
            local_config=self._settings.local_config,
            environment_provider=KindEnvironmentProvider(kind),
            client_factory=clients,
        )

    def ephemeral_test_cluster_service(
        self,
        root: Path,
        *,
        progress: ProgressCallback | None = None,
        charts_dir: Path | None = None,
    ) -> EphemeralTestClusterService:
        """Build the local chart-test installer for the repository at `root`."""
        kind = self.kind()

        def clients(handle: EnvironmentHandle) -> tuple[Helm, Kubectl]:
            return (
                self.helm(context=handle.context),
                self.kubectl(context=handle.context),
            )

        return EphemeralTestClusterService(
            root,
            helm=self.helm(),
            kind=kind,
            kubectl=self.kubectl(),
            progress=progress,
            charts_dir=charts_dir or self._settings.charts_dir,
            local_config=self._settings.local_config,
            environment_provider=KindEnvironmentProvider(kind),
            client_factory=clients,
        )

    def ci_service(self, root: Path) -> CiService:
        """Build the CI selection verbs for the repo at `root`."""
        return CiService(
            root,
            charts_dir=self._settings.charts_dir,
            local_config=self._settings.local_config,
        )

    def publish_service(self, root: Path) -> PublishService:
        """Build the headless OCI publisher for charts below ``root``."""
        return PublishService(
            root,
            helm=self.helm(verbose=False),
            events=self.event_writer(),
            charts_dir=self._settings.charts_dir,
        )

    def validate_app(
        self,
        *,
        progress: ProgressDisplay | None = None,
        on_warn: WarnCallback | None = None,
        charts_dir: Path | None = None,
    ) -> ManifestValidationService:
        """Build the validate pipeline entry point (render -> schema -> policy)."""
        return ManifestValidationService(
            progress=progress,
            on_warn=on_warn,
            command_runner=self.command_runner(),
            charts_dir=charts_dir or self._settings.charts_dir,
        )

    def upgrade_service(self, root: Path) -> UpgradeService:
        """Build the chart-scoped Renovate orchestrator with one shared runner.

        Gets the same memoized writer as the promotion services: `PR_OPEN` is
        the *start* of the build timeline whose `merged`/`published` phases CI
        emits through `chart-manager event emit build`, so it has to land on the
        same store or the timeline is split across two backends.
        """
        resolved_root = root.resolve()
        renovate = Renovate(self.command_runner())
        repository = self._repository_slug(resolved_root)
        github = Github(resolved_root, self.command_runner())

        def request_factory(plan: UpgradePlan, *, dry_run: bool) -> RenovateRequest:
            chart_config = plan.chart_path / "renovate.json"
            return RenovateRequest(
                repo_root=plan.repo_root,
                repository=repository,
                global_config_path=resolved_root / "renovate-global.json",
                additional_config_path=chart_config if chart_config.is_file() else None,
                runtime_overlay=plan.runtime_overlay,
                dry_run="full" if dry_run else None,
                # Renovate names this setting RENOVATE_TOKEN, while GitHub
                # Actions exposes its repository token as GITHUB_TOKEN. Honor
                # the explicit Renovate name first and use the standard CI
                # token as the composition-boundary fallback.
                token=os.environ.get("RENOVATE_TOKEN")
                or os.environ.get("GITHUB_TOKEN"),
            )

        def relevant_changes(paths: Sequence[Path]) -> Sequence[str]:
            args = ["git", "status", "--porcelain=v1", "--untracked-files=all", "--"]
            args.extend(str(path.relative_to(resolved_root)) for path in paths)
            result = self.command_runner().run(args, cwd=resolved_root)
            return tuple(
                line[3:].strip()
                for line in result.stdout.splitlines()
                if len(line) > 3 and line[3:].strip()
            )

        def pull_request_lookup(branch_prefix: str) -> Sequence[PullRequestLike]:
            return cast(
                Sequence[PullRequestLike],
                github.find_open_prs_for_branch_prefix(branch_prefix),
            )

        return UpgradeService(
            renovate=renovate,
            request_factory=request_factory,
            pull_request_lookup=pull_request_lookup,
            relevant_changes=relevant_changes,
            branch_file_reader=github.read_file_at_ref,
            repository=repository,
            telemetry=UpgradeTelemetry(writer=self.event_writer()),
            charts_dir=self._settings.charts_dir,
        )

    def upgrade_finalizer(self, root: Path) -> UpgradeFinalizer:
        """Build the trusted callback finalizer with the shared git runner."""
        del root  # address is carried by FinalizeRequest; retained for surface symmetry.
        return UpgradeFinalizer(
            baseline=GitBaselineReader(self.command_runner()),
            charts_dir=self._settings.charts_dir,
        )

    def _repository_slug(self, root: Path) -> str:
        """Read owner/repository from CI metadata or the configured origin."""
        configured = os.environ.get("GITHUB_REPOSITORY")
        if configured:
            return configured
        result = self.command_runner().run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            check=False,
        )
        remote = result.stdout.strip()
        match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", remote)
        if result.returncode or match is None:
            raise ChartManagerError(
                "cannot determine Renovate repository; configure an origin remote "
                "or set GITHUB_REPOSITORY"
            )
        return match.group(1)
