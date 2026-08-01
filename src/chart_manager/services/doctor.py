"""Aggregate the per-integration preflights into one report.

What this module is *not*: it does not know that helm's version flag is
`--short`, that kubeconform spells it `-v`, or what a reachable Cosmos
container looks like. Every one of those lives with the adapter that shells
out to the tool, per the position recorded in `MY_COMMENTS.md`:

    each integration should maintain its own preflight checks [...] per
    integration matter, not matter of the cli/command surface.

So this layer holds exactly the two things no single integration can own:

  * **the fold** -- many `Check`s into one `Outcome`, and therefore one
    process exit code;
  * **the requirement table** -- which capabilities a given command needs,
    which is what `--for` narrows on.

`cli/doctor.py` holds a third thing, rendering, and nothing else. The point
of the split is that a REST handler wanting the same answer builds the same
container, calls `run()`, and serialises `DoctorReport.to_dict()` -- no part
of the diagnostic is trapped inside the Typer command.

Why the requirement table lives here and not in `cli/`
------------------------------------------------------
It reads like surface vocabulary (its keys are command paths) but it is not:
the question it answers is "does promoting a HelmRelease need `gh`?", which
is a fact about the *capability*, not about how the capability was invoked.
Putting it in `cli/` would mean a second surface either re-derives it or
imports it back out of the CLI, and the CLI is supposed to be the leaf.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check, CheckStatus

#: What the container hands over per capability: run your checks, tell me how
#: it went. Zero-argument because every adapter is already configured by the
#: time the composition root binds this -- context, docker host, timeouts and
#: binary paths are all resolved there, so a provider cannot be called with
#: settings that differ from the ones the real command would use.
CheckProvider = Callable[[], Sequence[Check]]

#: Which capabilities each command actually needs, for `--for`.
#:
#: Keys are command paths as typed. `tests/test_doctor_service.py` asserts
#: every one of them resolves against the live app, so a command rename
#: cannot leave a filter here pointing at a command nobody can run -- the
#: same guard, and the same reasoning, as `tests/test_cli_argv_table.py`.
#:
#: An empty set is a real answer, not an oversight: `chart list` reads the
#: repository and shells out to nothing, and saying so is more useful than
#: refusing to answer.
COMMAND_REQUIREMENTS: Final[Mapping[str, frozenset[str]]] = {
    "chart cache clean": frozenset(),
    "chart list": frozenset(),
    "chart publish": frozenset({"helm", "events"}),
    "chart show": frozenset(),
    "chart test": frozenset({"helm", "kubectl", "kind"}),
    "chart upgrade": frozenset({"git", "github", "renovate", "events"}),
    "chart validate": frozenset({"helm", "kubeconform", "kyverno"}),
    "event emit build": frozenset({"events"}),
    "event emit promote": frozenset({"events"}),
    "grafana export-dashboard": frozenset({"kubectl"}),
    "grafana lint-dashboards": frozenset(),
    "helmrelease monitor": frozenset({"kubectl", "events"}),
    "helmrelease promote": frozenset({"git", "events"}),
    "helmrelease test": frozenset({"helm", "kubectl", "events"}),
    "local down": frozenset({"kind"}),
    "local reset": frozenset({"helm", "kubectl", "kind"}),
    "local up": frozenset({"helm", "kubectl", "kind"}),
    "plan": frozenset({"git"}),
    "upgrade-finalize": frozenset({"git"}),
    "version": frozenset(),
}

#: Most fundamental failure first. The aggregate outcome is the first member
#: of this tuple that any check reported, which is the same order an operator
#: would fix them in: a tool that is not installed makes every later question
#: unanswerable (an "unreachable cluster" is often just an absent kubectl),
#: authored configuration that is wrong is next because nothing external has
#: to change to fix it, and only then the environment and the tools
#: themselves.
#:
#: Ordering matters because the exit code is a single number: `doctor` on a
#: machine with no helm *and* no cluster has to pick one, and 127 -- the
#: shell's own "command not found", which install-the-toolchain wrappers key
#: on -- is the more actionable of the two.
_OUTCOME_PRECEDENCE: Final[tuple[Outcome, ...]] = (
    Outcome.MISSING_BINARY,
    Outcome.SPEC,
    Outcome.ENVIRONMENT,
    Outcome.TOOL,
    Outcome.FAILED,
)


@dataclass(frozen=True)
class DoctorReport:
    """Every check that ran, and what the run as a whole means."""

    checks: tuple[Check, ...]
    #: The `--for` argument this report was narrowed by, or None for a full run.
    selector: str | None = None

    @property
    def ok(self) -> bool:
        """True when nothing failed. A skipped check is not a failure."""
        return all(check.status is not CheckStatus.FAILED for check in self.checks)

    @property
    def outcome(self) -> Outcome:
        """The single outcome this run exits with.

        Derived from the checks by `_OUTCOME_PRECEDENCE` rather than stored,
        so `ok` and the exit code cannot disagree -- the same discipline
        `PromoteResult` follows, where the wire `ok` and the exit status are
        both reads of one lookup.
        """
        reported = {check.outcome for check in self.checks}
        for candidate in _OUTCOME_PRECEDENCE:
            if candidate in reported:
                return candidate
        return Outcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        """The `-o json` document.

        Stable shape: `ok`, `outcome`, `for`, and a `checks` array of
        `{name, status, detail, remediation}`. `for` is spelled as the flag
        is, because a consumer correlating a report with the command that
        produced it should not have to learn a second name for it.
        """
        return {
            "ok": self.ok,
            "outcome": str(self.outcome),
            "for": self.selector,
            "checks": [check.to_dict() for check in self.checks],
        }


class DoctorService:
    """Run the configured preflight providers and fold their results.

    Providers arrive already bound to their adapters from
    `composition.Container.doctor_service`, keyed by capability name. Report
    order follows that mapping's insertion order rather than being sorted, so
    the composition root decides how the table reads (toolchain, then
    cluster, then telemetry) and `-o json` stays byte-stable across runs.
    """

    def __init__(self, providers: Mapping[str, CheckProvider]) -> None:
        """Bind the capability -> provider mapping for this container."""
        self._providers = dict(providers)

    def capabilities(self) -> tuple[str, ...]:
        """Capability names this service can check, in report order."""
        return tuple(self._providers)

    def commands(self) -> tuple[str, ...]:
        """Command paths `--for` accepts, sorted for a usage message."""
        return tuple(sorted(COMMAND_REQUIREMENTS))

    def run(self, *, for_command: str | None = None) -> DoctorReport:
        """Run every provider, or only those `for_command` needs.

        A provider that raises is reported as a failed check rather than
        allowed to propagate. `doctor` exists to be run when things are
        already broken, so one adapter throwing an unexpected exception must
        not cost the operator the other nine answers.
        """
        if for_command is None:
            selected = self._providers
        else:
            required = COMMAND_REQUIREMENTS.get(for_command)
            if required is None:
                raise ChartManagerError(f"unknown command for preflight: {for_command}")
            selected = {
                name: provider
                for name, provider in self._providers.items()
                if name in required
            }
        checks: list[Check] = []
        for name, provider in selected.items():
            checks.extend(_run_provider(name, provider))
        return DoctorReport(checks=tuple(checks), selector=for_command)


def _run_provider(name: str, provider: CheckProvider) -> tuple[Check, ...]:
    """Call one provider, converting an unexpected exception into a check."""
    try:
        return tuple(provider())
    except Exception as exc:
        # Broad on purpose: an adapter's preflight is allowed to be wrong
        # about its own tool without taking the whole diagnostic down with
        # it. `ENVIRONMENT` rather than a new outcome because from the
        # caller's side the fact is the same -- this capability could not be
        # confirmed usable.
        return (
            Check.failed(
                name,
                f"the {name} preflight raised {type(exc).__name__}: {exc}",
                remediation="re-run with -v; if it persists this is a chart-manager bug",
                outcome=Outcome.ENVIRONMENT,
            ),
        )


__all__ = [
    "COMMAND_REQUIREMENTS",
    "CheckProvider",
    "DoctorReport",
    "DoctorService",
]
