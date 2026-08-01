"""Result vocabulary for persistent sandbox lifecycle operations.

Pure data: no integrations, no IO, no progress. Everything here is either
frozen (what crosses the service boundary) or an explicitly-named mutable
accumulator (`_DevelopmentClusterRunSummary`, which never leaves the converge
engine).

Kept in its own module because `cli/local.py` renders these and the drift /
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
class PortMappingDrift:
    """Host ports the kind config declares that the live node container lacks.

    Kind bakes `extraPortMappings` into the node container at create time, so
    editing the config and running `down`/`up` does not re-apply them. The
    *decision* is here as data; whether it is narrated as a warning
    (`local up`) or reported as a field (`local status`) is the surface's.

    `error` carries a failed inspection, which is not the same answer as "no
    drift": both leave `missing` empty, and only one of them means the check
    ran.
    """

    missing: tuple[int, ...] = ()
    error: str | None = None

    @property
    def drifted(self) -> bool:
        """True when the check ran and found ports the container is missing."""
        return bool(self.missing)


@dataclass(frozen=True)
class DevelopmentClusterRelease:
    """One Helm release installed on the development cluster.

    A projection of `integrations.helm.ReleaseInfo` so the status result
    crosses the service boundary without an adapter type on it.
    """

    name: str
    namespace: str
    revision: int
    status: str


@dataclass(frozen=True)
class DevelopmentClusterStatus:
    """What exists right now: the cluster, its releases, and how to reach it.

    Every field is a lookup that already had a home in the converge path --
    `helm list -A` from the install-skip snapshot, the VirtualService hosts
    from `access.py`, the port-mapping diff from `drift.py`. Nothing here is
    a second way of asking.

    Best-effort throughout: a cluster that is stopped, or a kubeconfig that
    points nowhere, answers most of these with an error string rather than an
    exception. `status` reports state, so failing to reach the cluster is
    part of the report, not a failure of the command.
    """

    cluster_name: str
    exists: bool
    context: str | None = None
    provider: str | None = None
    releases: tuple[DevelopmentClusterRelease, ...] = ()
    releases_error: str | None = None
    urls: tuple[str, ...] = ()
    urls_error: str | None = None
    port_forward_pid: int | None = None
    drift: PortMappingDrift = field(default_factory=PortMappingDrift)


@dataclass(frozen=True)
class DevelopmentClusterPlanEntry:
    """One release a converge would install, and where it came from.

    `source` is `bootstrap` for a release the LocalCluster owns and
    `target` for one the caller's `--chart`/`--stack` selected.
    """

    chart: str
    profile: str
    namespace: str
    source: str


@dataclass(frozen=True)
class DevelopmentClusterPlan:
    """What a mutating `local` command would do, resolved but not executed.

    Produced by the same preflight the mutating path runs first, so a
    `--dry-run` that prints a plan and a real run that fails during preflight
    fail identically -- a plan that could not be resolved is an error, not an
    empty plan.
    """

    command: str
    cluster_name: str
    target: str | None = None
    target_kind: str | None = None
    destroys: bool = False
    entries: tuple[DevelopmentClusterPlanEntry, ...] = ()


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
