"""The process exit-code table, per design §6.1.

One table. `Outcome` is the vocabulary a caller speaks; `EXIT_CODE` is the
only place in the codebase that says what number each outcome is worth.
Nothing outside this module may write an exit-code literal.

    | code | Outcome         | meaning                                      |
    |------|-----------------|----------------------------------------------|
    |    0 | SUCCESS         | it worked                                    |
    |    1 | FAILED          | the thing you asked about failed -- a failed |
    |      |                 | validation, install, helm test, or a promote |
    |      |                 | that was aborted/declined                    |
    |    2 | USAGE           | bad flag, bad value, mutual exclusion        |
    |      |                 | (Click's default; reserved)                  |
    |    3 | SPEC            | authored configuration is invalid            |
    |    4 | TOOL            | an external command ran and failed           |
    |    5 | ENVIRONMENT     | no cluster, no kubecontext, backend down     |
    |  127 | MISSING_BINARY  | required binary is not on PATH               |

Scope note (P0). Only the promote vertical routes through this module today
(`services/helmrelease/state.py::PROMOTE_OUTCOME`, mapped to a number by
`cli/helmrelease.py`). The rest of the table is declared now so P2.1 --
which moves tool error from 2 to 4 and maps every `ChartManagerError`
subclass in `main()` -- extends `EXIT_CODE`'s consumers rather than
restructuring the module. **Declaring `TOOL = 4` here is not that move**:
`services/manifest_validation/models.py` still returns 2 for a tool error
and is untouched, because 2 -> 4 is a breaking change that ships alone with
a `CHART_MANAGER_LEGACY_EXIT_CODES=1` escape hatch (§6, R1).

Why the table is keyed on a semantic `Outcome` and not on each caller's own
status enum
------------------------------------------------------------------------
The obvious shape -- `Mapping[PromoteStatus, int]` living here -- would make
`plumbing/` import `services.helmrelease.state`. That inverts the one
dependency direction this codebase actually holds: ~30 modules under
`services/` import `plumbing/`, and *no* module under `plumbing/` imports
`services/`. `tests/test_layering.py::test_plumbing_does_not_import_service_domains`
exists to keep it that way ("generic plumbing must not depend on chart or
validation service policy"), and a second vertical wanting an exit code
would drag a second domain enum in behind the first.

So the split follows the question each layer can actually answer:

  * "Is an aborted promote a failure?" is domain policy. The service owns it,
    as `PROMOTE_OUTCOME: Mapping[PromoteStatus, Outcome]` -- a table with no
    integers in it, sitting beside `PROMOTE_PHASE`, which classifies the same
    six states for the timeline.
  * "What number does a failure exit with?" is surface/process policy. It
    lives here, once.

That is what keeps `PromoteResult`'s wire `ok` field and the process exit
status from drifting: both are derived from the single `PROMOTE_OUTCOME`
lookup -- `ok` is `outcome is Outcome.SUCCESS`, the exit status is
`EXIT_CODE[outcome]` -- rather than from two independently maintained lists
of statuses. `EXIT_CODE[Outcome.SUCCESS] == 0` is asserted in
`tests/test_exit_codes.py`, which is the hinge that makes those two
derivations the same judgement.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

__all__ = [
    "EXIT_CODE",
    "EXIT_ENVIRONMENT",
    "EXIT_FAILED",
    "EXIT_MISSING_BINARY",
    "EXIT_SPEC",
    "EXIT_SUCCESS",
    "EXIT_TOOL",
    "EXIT_USAGE",
    "Outcome",
    "exit_code_for",
]

#: The canonical codes, named so no caller writes a bare integer.
EXIT_SUCCESS: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
EXIT_SPEC: Final = 3
EXIT_TOOL: Final = 4
EXIT_ENVIRONMENT: Final = 5
EXIT_MISSING_BINARY: Final = 127


class Outcome(StrEnum):
    """How a run ended, in terms a non-CLI surface can also use.

    A `StrEnum` so the member reads as itself in a log line or a test
    failure; the string values are *not* a wire contract and nothing
    serializes them today.

    Deliberately not an `IntEnum`. Folding the number into the member would
    make `Outcome` unusable from `services/` without the service handling
    exit codes again, which is the coupling this module exists to break --
    and it would leave no table for P2.1's legacy 4 -> 2 remap to shadow.
    """

    SUCCESS = "success"
    FAILED = "failed"
    USAGE = "usage"
    SPEC = "spec"
    TOOL = "tool"
    ENVIRONMENT = "environment"
    MISSING_BINARY = "missing-binary"


#: The table. Exhaustive over `Outcome` by test, so a new outcome cannot be
#: added without a deliberate decision about what it exits with.
EXIT_CODE: Mapping[Outcome, int] = {
    Outcome.SUCCESS: EXIT_SUCCESS,
    Outcome.FAILED: EXIT_FAILED,
    Outcome.USAGE: EXIT_USAGE,
    Outcome.SPEC: EXIT_SPEC,
    Outcome.TOOL: EXIT_TOOL,
    Outcome.ENVIRONMENT: EXIT_ENVIRONMENT,
    Outcome.MISSING_BINARY: EXIT_MISSING_BINARY,
}


def exit_code_for(outcome: Outcome) -> int:
    """Return the process exit code for `outcome`.

    A function rather than a bare subscript at each call site so P2.1's
    `CHART_MANAGER_LEGACY_EXIT_CODES=1` window has one place to shadow the
    table from, instead of one edit per exit site.
    """
    return EXIT_CODE[outcome]
