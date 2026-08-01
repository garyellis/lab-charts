"""Surface glue every `cli/` module needs and none of them should own.

Three things live here, and each was previously copy-pasted with its
docstring into several command modules:

  * `container()` -- the composition root for one invocation;
  * `exit_if_failed()` -- the surface's rule for a result that reports its
    own failure;
  * `resolve_chart()` -- the shared reading of a chart name or directory.

None of them is a capability. `services/` owns what a command *does*;
`plumbing/exit_codes.py` owns which outcome is which number;
`services/local_resources.py` owns how a chart name is resolved. What is
left is the few lines of surface that bind them to configuration, which is
exactly what a module with a leading underscore is for -- this is internal
to `cli/` and nothing outside it should import it.

Note the test seam. Several `cli/` modules alias `container` into their own
namespace (`from ._wiring import container as _container`) so a test can
monkeypatch one command group's wiring without reaching into every other
group's. That is an import alias, not a second copy: there is one function
body, and the alias exists purely so the patch stays scoped.
"""

from __future__ import annotations

from pathlib import Path

import typer

from chart_manager.composition import Container, Settings
from chart_manager.plumbing.exit_codes import Outcome, exit_code_for
from chart_manager.services.local_resources import ResolvedChartTarget, resolve_chart_target


def container() -> Container:
    """Build the composition root for one CLI invocation.

    Every cluster-facing service on this surface is built through it:
    constructing them inline is what let `Settings.kube_context` be
    configured and then ignored.
    """
    return Container()


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


def resolve_chart(root: Path, chart: str) -> ResolvedChartTarget:
    """Resolve either a configured chart name or an explicit chart directory.

    Here rather than in one of the two command modules that call it
    (`chart test`, `local up`/`local reset`) because it is the point where a
    chart name means the same thing to both, and design commitment 6 says no
    command module carries a path heuristic of its own.
    """
    settings = Settings()
    return resolve_chart_target(
        root,
        chart,
        charts_dir=settings.charts_dir,
        local_config=settings.local_config,
    )


__all__ = ["container", "exit_if_failed", "resolve_chart"]
