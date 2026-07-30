"""Result vocabulary for persistent sandbox lifecycle operations.

Pure data: no integrations, no IO, no progress. Everything here is either
frozen (what crosses the service boundary) or an explicitly-named mutable
accumulator (`_DevelopmentClusterRunSummary`, which never leaves the converge
engine).

Kept in its own module because `cli/main.py` renders these and the drift /
access helpers read them — three importers, none of which need the
converge engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DevelopmentClusterEntryOutcome:
    """One converged plan entry: which chart:profile landed in which namespace."""

    chart: str
    profile: str
    namespace: str


@dataclass(frozen=True)
class DevelopmentClusterEntryFailure:
    """One failed plan entry, with the error text the surface should surface."""

    chart: str
    profile: str
    namespace: str
    error: str


@dataclass(frozen=True)
class DevelopmentClusterAccessHints:
    """Post-converge advisory data: what the operator needs to reach the lab.

    Data only -- the wording of the CA-trust instructions is a surface
    concern. `ca_trust_hint` is the *decision* (did the chart that owns the
    lab CA sync this run?), not the text. The two `*_error` fields carry
    best-effort lookup failures so the surface can render them in place
    rather than the service printing them mid-run.
    """

    ca_trust_hint: bool = False
    urls: tuple[str, ...] = ()
    grafana_url: str | None = None
    grafana_credentials: tuple[str, str] | None = None
    grafana_error: str | None = None
    urls_error: str | None = None


@dataclass(frozen=True)
class DevelopmentClusterResult:
    """Per-run accounting returned by target convergence and reset.

    Buckets mirror helmfile/Argo terminology:
      * applied:   helm produced a new release revision
      * no_change: helm returned 0 and revision was unchanged (deep no-op)
      * failed:    subprocess error; release may or may not be in a good state
    """

    applied: tuple[DevelopmentClusterEntryOutcome, ...] = ()
    no_change: tuple[DevelopmentClusterEntryOutcome, ...] = ()
    failed: tuple[DevelopmentClusterEntryFailure, ...] = ()
    hints: DevelopmentClusterAccessHints = field(default_factory=DevelopmentClusterAccessHints)

    @property
    def ok(self) -> bool:
        """True when no plan entry failed. Continue-on-error means partial runs are common."""
        return not self.failed


@dataclass(frozen=True)
class DevelopmentClusterActionResult:
    """Outcome of a stop or destroy operation, including port-forward cleanup.

    `changed` is False when the cluster was already stopped / already absent
    -- both are success, so `ok` is unconditionally True (real failures raise).
    """

    cluster_name: str
    changed: bool
    port_forward_pid: int | None = None

    @property
    def ok(self) -> bool:
        """True whenever a result exists -- failures raise instead of returning."""
        return True


@dataclass
class _DevelopmentClusterRunSummary:
    """Mutable accumulator threaded through the install loop.

    Frozen `DevelopmentClusterResult` is what leaves the service; this is the in-flight
    scratch buffer the loop appends to.
    """

    applied: list[DevelopmentClusterEntryOutcome] = field(default_factory=list)
    no_change: list[DevelopmentClusterEntryOutcome] = field(default_factory=list)
    failed: list[DevelopmentClusterEntryFailure] = field(default_factory=list)

    def freeze(self, hints: DevelopmentClusterAccessHints) -> DevelopmentClusterResult:
        """Snapshot the accumulator into the frozen result the caller gets."""
        return DevelopmentClusterResult(
            applied=tuple(self.applied),
            no_change=tuple(self.no_change),
            failed=tuple(self.failed),
            hints=hints,
        )
