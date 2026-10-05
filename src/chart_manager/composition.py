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
process. Cheap adapters (Helm, Kubectl -- subprocess wrappers) are built
per call, matching what the CLI does today. Anything that owns a real client or
a cache is memoized on the container:

  * `command_runner()` -- stateless, shared by every adapter built here.
  * `event_writer()`   -- memoized, so the EventStore it resolves lazily
                          (and the Cosmos/DynamoDB SDK client behind it) is
                          built at most once per container rather than once
                          per emitted event.

Everything addressed by a `root` stays per-call and unmemoized on purpose:
`root` is a per-invocation argument rather than configuration, so a memo
would need it as a key, and each of these constructions is path arithmetic
with no I/O behind it.

Note that store resolution stays *lazy* inside `EventWriter`: `EVENTS_BACKEND`
is still read on first write, not at container construction. Building the
store eagerly would move a failure that `cli/events.py` currently swallows as
non-fatal telemetry to before its try/except, which would be a behavior change.

Test seams
----------
Surfaces keep their module-level `_make_*` factories (see `cli/doctor.py`)
and delegate the body to a container. Tests that
`monkeypatch.setattr(module, "_make_x_service", ...)` keep working unchanged;
tests that want real services with fake adapters can subclass `Container` or
pass a `Settings`.
"""

from __future__ import annotations

from pathlib import Path

from chart_manager.commands.validate.schemas.doctor import KubeconformSchemaDoctor
from chart_manager.integrations.git import Git
from chart_manager.integrations.github import Github
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubeconform import (
    Kubeconform,
)
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.integrations.kyverno import Kyverno
from chart_manager.integrations.renovate import Renovate
from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import WorkspaceNotFoundError
from chart_manager.services.chart_catalog import ChartCatalogService
from chart_manager.services.doctor import CheckProvider, DoctorService
from chart_manager.services.events.store import preflight_event_store
from chart_manager.services.events.writer import EventWriter
from chart_manager.services.grafana.dashboard_export import GrafanaExporter
from chart_manager.shared.charts import dependencies as chart_deps
from chart_manager.shared.settings import Settings, load_settings
from chart_manager.shared.workspace import (
    RepositoryWorkspace,
    load_repository_workspace,
    resolve_repository_root,
)

__all__ = ["Container", "Settings"]


class Container:
    """Assemble configured services. Construct once; the caller holds it."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Bind settings (defaults reproduce today's CLI behavior)."""
        self._settings = settings if settings is not None else load_settings()
        self._command_runner: CommandRunner | None = None
        self._event_writer: EventWriter | None = None
        self._workspaces: dict[Path | None, RepositoryWorkspace] = {}

    @property
    def settings(self) -> Settings:
        """The settings this container was built from."""
        return self._settings

    def workspace(self, root: Path | None = None) -> RepositoryWorkspace:
        """Resolve the single repository layout/policy used by every capability.

        Memoized per root, so workspace.yaml is parsed once per container.
        Raises `WorkspaceNotFoundError` when there is no workspace.yaml.
        """
        key = root.resolve() if root is not None else None
        if key not in self._workspaces:
            configured = root
            if configured is None and "root" in self._settings.model_fields_set:
                configured = self._settings.root
            compiled = load_repository_workspace(resolve_repository_root(configured=configured))
            self._workspaces[key] = compiled
            self._workspaces[compiled.root] = compiled
        return self._workspaces[key]

    # --- adapters ---------------------------------------------------------

    def command_runner(self) -> CommandRunner:
        """The shared subprocess runner (stateless; memoized)."""
        if self._command_runner is None:
            self._command_runner = SubprocessRunner()
        return self._command_runner

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

        Outside a chart repository the schema checks are skipped, and git/gh
        probe `root`, else `Settings.root` (the working directory by default).
        """
        runner = self.command_runner()
        try:
            workspace: RepositoryWorkspace | None = self.workspace(root)
            schemas = KubeconformSchemaDoctor(workspace)
        except WorkspaceNotFoundError as exc:
            workspace, schemas = None, KubeconformSchemaDoctor(None, skip_reason=str(exc))
        probe_root = workspace.root if workspace else (root or self._settings.root).resolve()
        providers: dict[str, CheckProvider] = {
            "helm": self.helm().preflight,
            "kubeconform": Kubeconform(runner, timeout=self._settings.command_timeout).preflight,
            "kyverno": Kyverno(runner, timeout=self._settings.command_timeout).preflight,
            "kubectl": self.kubectl().preflight,
            "kind": self.kind().preflight,
            "git": Git(probe_root, runner).preflight,
            "github": Github(probe_root, runner).preflight,
            "renovate": Renovate(runner).preflight,
            "schemas": schemas.preflight,
            "events": preflight_event_store,
        }
        return DoctorService(providers)

    def grafana_exporter(self) -> GrafanaExporter:
        """Build the dashboard exporter (port-forward + Grafana HTTP API)."""
        return GrafanaExporter(kubectl=self.kubectl())

    def chart_catalog_service(self, root: Path) -> ChartCatalogService:
        """Build the read-only chart/lifecycle catalog for the repo at `root`.

        Built here rather than at the surface: `charts_dir` is the one setting
        that decides which directories are charts at all, and a surface that
        supplies it itself can answer
        `chart list` from a different directory than `plan` selected against.
        """
        return ChartCatalogService(workspace=self.workspace(root))
