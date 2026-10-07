"""The process exit-code table.

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

2 means what Click means by it and nothing else, so a CI wrapper can
separate "you typed a bad flag" (2) from "kubeconform would not run" (4).

Why the table is keyed on a semantic `Outcome` and not on each caller's own
status enum
------------------------------------------------------------------------
The obvious shape -- `Mapping[PromoteStatus, int]` living here -- would make
`plumbing/` import `commands.promote.state`. That inverts the one
dependency direction this codebase actually holds: every package imports
`plumbing/`, and `plumbing/` imports no other tier (the `Tiers point down`
import contract). A second vertical wanting an exit code would drag a second
domain enum in behind the first.

So the split follows the question each layer can actually answer:

  * "Is an aborted promote a failure?" is domain policy. The promote command owns it,
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

__all__ = [
    "EXIT_CODE",
    "Outcome",
    "exit_code_for",
]


class Outcome(StrEnum):
    """How a run ended, in terms a non-CLI surface can also use.

    A `StrEnum` so the member reads as itself in a log line or a test
    failure; the string values are *not* a wire contract and nothing
    serializes them today.

    Deliberately not an `IntEnum`. Folding the number into the member would
    make `Outcome` unusable from a command without the command handling
    exit codes again, which is the coupling this module exists to break:
    `RunResult.outcome()` and `PROMOTE_OUTCOME` both answer "what happened"
    with these members and never learn what they are worth.
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
    Outcome.SUCCESS: 0,
    Outcome.FAILED: 1,
    Outcome.USAGE: 2,
    Outcome.SPEC: 3,
    Outcome.TOOL: 4,
    Outcome.ENVIRONMENT: 5,
    Outcome.MISSING_BINARY: 127,
}


def exit_code_for(outcome: Outcome) -> int:
    """Return the process exit code for `outcome`.

    A function rather than a bare subscript at each call site because it is
    the name the rest of the tree greps for and the gate scans for: an exit
    site that reads `exit_code_for(Outcome.SPEC)` states which kind of
    failure it is, where `EXIT_CODE[...]` or a literal states only a number.
    """
    return EXIT_CODE[outcome]
