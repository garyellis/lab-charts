"""Surface glue every `cli/` module needs and none of them should own.

Three things live here, and each was previously copy-pasted with its
docstring into several command modules:

  * `container()` -- the composition root for one invocation;
  * `exit_if_failed()` -- the surface's rule for a result that reports its
    own failure;
  * `resolve_chart()` -- the shared reading of a chart name or directory.

None of them is a capability. `services/` owns what a command *does*;
`plumbing/exit_codes.py` owns which outcome is which number;
`domain/local_resources.py` owns how a chart name is resolved. What is
left is the few lines of surface that bind them to configuration, which is
exactly what a module with a leading underscore is for -- this is internal
to `cli/` and nothing outside it should import it.

Named `_container` rather than `_wiring` because `services/*/wire.py` means
something else entirely -- a versioned `X -> dict` projection -- and one
`cli/` reader already took the two for the same thing. There is one wiring
concept on this surface and it is the composition root, so the file is named
after it.

Note the test seam. Several `cli/` modules alias `container` into their own
namespace (`from ._container import container as _container`) so a test can
monkeypatch one command group's wiring without reaching into every other
group's. That is an import alias, not a second copy: there is one function
body, and the alias exists purely so the patch stays scoped.

One `Container` per invocation. The root callback calls
`start_invocation()`, after `--config` is applied, and every `container()`
call for the rest of that invocation returns the same object, so its
workspace memo means `workspace.yaml` is parsed once however many commands,
helpers and factories ask for it. Module state rather than `ctx.obj`
because most callers are helpers with no Click context in hand, and Typer
offers no public way to fetch the current one. Each invocation replaces it;
`reset_invocation()` is the test hook that clears it between tests.

The other seam is `container(settings=...)`. `Container` has taken a
`Settings` since it was written, but every CLI call site built one bare, so
the parameter was unreachable from the only surface that exists -- and the
bypass sites that used to construct services inline each built a *second*
`Settings` besides. Both are closed: nothing under `cli/` constructs a
service, and nothing but `main.py` constructs `Settings`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from chart_manager.composition import Container, Settings
from chart_manager.domain.local_resources import ResolvedChartTarget, resolve_chart_target
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for

#: The current invocation's composition root; see the module docstring.
_invocation: Container | None = None


def start_invocation() -> Container:
    """Build and install the composition root for a new CLI invocation.

    Called once, by the root callback in `cli/main.py`, after `--config` has
    been applied so the container's `Settings` read that file.
    """
    global _invocation
    _invocation = Container()
    return _invocation


def reset_invocation() -> None:
    """Forget the current invocation's container (test isolation hook)."""
    global _invocation
    _invocation = None


def container(settings: Settings | None = None) -> Container:
    """Return the composition root for the current CLI invocation.

    Every service on this surface is built through it: constructing them
    inline is what let `Settings.kube_context` be configured and then
    ignored, and it is what `tests/test_layering.py`'s container-bypass scan
    now forbids.

    `settings=None` resolves the process configuration exactly as before
    (`CHART_MANAGER_* env > config.yaml > defaults`), which is what every
    command does. Passing one is the injection point: a test, or a second
    surface that has already resolved its own configuration, gets every
    service built against it in one call rather than per construction site.
    An injected `Settings` always gets a fresh container: it is a different
    configuration from the invocation's. So does a call outside any
    invocation, such as a test driving a helper directly.
    """
    if settings is not None:
        return Container(settings)
    if _invocation is None:
        return Container()
    return _invocation


def exit_if_failed(ok: bool) -> None:
    """The surface's single rule for a result that reports its own failure.

    Services report partial failure on the result object rather than by
    raising, so a surface that only renders it reports success for a run in
    which charts failed.

    A boolean `ok` is all these results carry, so `Outcome.FAILED` is the
    only outcome derivable from it -- "the thing you asked about failed",
    design §6.1's row 1. A command whose result can distinguish *why* it
    failed should map its own outcome instead of funnelling through here,
    the way `cli/helmrelease.py::promote` maps `PROMOTE_OUTCOME`.
    """
    if not ok:
        raise typer.Exit(code=exit_code_for(Outcome.FAILED))


def repository_root() -> Path:
    """Discover the current repository through the composition boundary."""
    return container().workspace().root


def resolve_chart(root: Path, chart: str) -> ResolvedChartTarget:
    """Resolve either a configured chart name or an explicit chart directory.

    Here rather than in one of the four command modules that call it
    (`chart test`, `local up`/`local reset`, `validate`, `upgrade`) because
    it is the point where a chart name means the same thing to all of them,
    and design commitment 6 says no command module carries a path heuristic
    of its own.

    `resolve_chart_target` is a free function over two settings, not a
    constructible object, so there is nothing for the container to build --
    but the *configuration* still comes from `container().settings`, so a
    caller that injected a `Settings` resolves chart names against it too.
    That is the whole reason this reads the container instead of calling
    `Settings()`, which is what the three former copies of these five lines
    each did independently.
    """
    workspace = container().workspace(root)
    return resolve_chart_target(
        workspace.root,
        chart,
        charts_dir=workspace.charts_dir,
        local_config=workspace.local_cluster,
    )


__all__ = [
    "container",
    "exit_if_failed",
    "repository_root",
    "reset_invocation",
    "resolve_chart",
    "start_invocation",
]
