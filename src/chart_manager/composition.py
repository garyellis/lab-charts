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

Everything addressed by a `root` stays per-call and unmemoized on purpose:
`root` is a per-invocation argument rather than configuration, so a memo
would need it as a key, and each of these constructions is path arithmetic
with no I/O behind it.
"""

from __future__ import annotations

from pathlib import Path

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.settings import Settings, load_settings
from chart_manager.shared.charts import dependencies as chart_deps
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
