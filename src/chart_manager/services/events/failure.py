"""The one place the events failure policy lives.

Telemetry is emitted *after* the work that produced it, so an unconfigured or
unreachable events backend must never turn a successful run into a traceback.
That rule was written three times independently -- `promote.py`, the
helmrelease telemetry wiring, and `cli/events.py` -- before the upgrade
service became the fourth caller and made the duplication worth removing.

It lives under `services/events/` rather than beside any one consumer: the
policy belongs to the events capability, and hanging it off a sibling service
(`services/helmrelease/`) would make every future emitter import from an
unrelated domain to get it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

_LOG = logging.getLogger(__name__)

__all__ = ["emit_non_fatal"]


def emit_non_fatal(
    emit: Callable[[], None], *, strict: bool, what: str
) -> Exception | None:
    """Run `emit`, returning a swallowed failure unless `strict`.

    `strict` exists for callers where the event *is* the deliverable (a
    backfill job, a test asserting the payload); everything on an operator
    path wants the swallow. The word "non-fatal" in the log line is load
    bearing -- it is how an operator tells a dropped event from a dropped
    promotion. The returned exception lets batch callers report every dropped
    event after attempting the whole batch.
    """
    try:
        emit()
    except Exception as exc:  # telemetry must not break the run that produced it
        if strict:
            raise
        # The exception *type* is carried alongside its text because the text
        # of a boto/httpx transport failure is frequently empty: "(non-fatal):
        # " with nothing after it is indistinguishable from a bug in this
        # line. Lazy %-formatting, not an f-string, so an operator running at
        # WARNING never pays to render a message the handler drops.
        _LOG.warning(
            "%s event emission failed (non-fatal): %s: %s",
            what,
            type(exc).__name__,
            exc,
        )
        return exc
    return None
