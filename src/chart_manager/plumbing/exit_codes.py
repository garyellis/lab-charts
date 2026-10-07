"""The process exit-code table.

`Outcome` says how a run ended; `EXIT_CODE` says what number each outcome
exits with.

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

The table is keyed on `Outcome`, not on a command's own status enum, so
`plumbing/` imports no command package. Each command maps its statuses to an
`Outcome` (e.g. `PROMOTE_OUTCOME`).
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

    The string values are for log lines and test failures, not a wire contract.
    """

    SUCCESS = "success"
    FAILED = "failed"
    USAGE = "usage"
    SPEC = "spec"
    TOOL = "tool"
    ENVIRONMENT = "environment"
    MISSING_BINARY = "missing-binary"


#: Exhaustive over `Outcome`, by test.
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
    """Return the process exit code for `outcome`."""
    return EXIT_CODE[outcome]
