"""The per-invocation `Container` and the surface glue every command's `cli.py` needs.

  * `Container` -- the settings, the workspace and the command runner for one
    invocation; each command wires its own adapters from them;
  * `container()` -- returns the current invocation's `Container`;
  * `exit_if_failed()` -- the surface's rule for a result that reports its
    own failure.

Several `cli.py` modules alias `container` into their own namespace
(`from ..._container import container as _container`) so a test can
monkeypatch one command's wiring without reaching into every other one's.

One `Container` per invocation: the root callback calls `start_invocation()`
and `container()` returns it, so workspace.yaml is parsed once per invocation.
"""

from __future__ import annotations

from pathlib import Path

import typer

from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.settings import Settings, load_settings
from chart_manager.shared.workspace import (
    RepositoryWorkspace,
    load_repository_workspace,
    resolve_repository_root,
)


class Container:
    """One invocation's settings, workspace and command runner. Construct once; the caller holds it."""

    def __init__(self, settings: Settings) -> None:
        """Bind the invocation's settings."""
        self._settings = settings
        self._command_runner: CommandRunner | None = None
        self._workspaces: dict[Path | None, RepositoryWorkspace] = {}

    @property
    def settings(self) -> Settings:
        """The settings this container was built from."""
        return self._settings

    def workspace(self, root: Path | None = None) -> RepositoryWorkspace:
        """Resolve the repository workspace, on first use.

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

    def command_runner(self) -> CommandRunner:
        """The shared subprocess runner (stateless; memoized)."""
        if self._command_runner is None:
            self._command_runner = SubprocessRunner()
        return self._command_runner


#: The current invocation's container; see the module docstring.
_invocation: Container | None = None


def start_invocation() -> Container:
    """Build the invocation's container (after `--config` is applied)."""
    global _invocation
    _invocation = Container(load_settings())
    return _invocation


def reset_invocation() -> None:
    """Forget the current invocation's container (test isolation hook)."""
    global _invocation
    _invocation = None


def container() -> Container:
    """Return the current invocation's `Container`.

    Raises `RuntimeError` when no invocation has started: the root callback
    calls `start_invocation()` before any command runs.
    """
    if _invocation is None:
        raise RuntimeError("no invocation has started; call start_invocation() first")
    return _invocation


def exit_if_failed(ok: bool) -> None:
    """The surface's single rule for a result that reports its own failure.

    Commands report partial failure on the result object rather than by
    raising, so a surface that only renders it reports success for a run in
    which charts failed.

    A boolean `ok` is all these results carry, so `Outcome.FAILED` is the
    only outcome derivable from it -- "the thing you asked about failed".
    A command whose result can distinguish *why* it
    failed should map its own outcome instead of funnelling through here,
    the way `commands/promote/cli.py::pr` maps `PROMOTE_OUTCOME`.
    """
    if not ok:
        raise typer.Exit(code=exit_code_for(Outcome.FAILED))


def repository_root() -> Path:
    """Discover the current repository through the invocation's workspace."""
    return container().workspace().root


__all__ = [
    "Container",
    "container",
    "exit_if_failed",
    "repository_root",
    "reset_invocation",
    "start_invocation",
]
