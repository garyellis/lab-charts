"""Request/result vocabulary for the lab lifecycle verbs.

Pure data: no integrations, no IO, no progress. Everything here is either
frozen (what crosses the service boundary) or an explicitly-named mutable
accumulator (`_RunSummary`, which never leaves the converge engine).

Kept in its own module because `cli/main.py` renders these and the drift /
access helpers read them — three importers, none of which need the
converge engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_CLUSTER_NAME = "chart-manager"
DEFAULT_CHART = "grafana-dashboards"
DEFAULT_PROFILE = "prototyping"
DEFAULT_NAMESPACE = "observability"


@dataclass(frozen=True)
class LabUpOptions:
    """Args for `LabService.up`."""

    chart: str = DEFAULT_CHART
    profile: str = DEFAULT_PROFILE
    cluster_name: str = DEFAULT_CLUSTER_NAME
    namespace: str = DEFAULT_NAMESPACE
    # Converge-by-default is the helmfile/Argo workflow: every chart in the
    # install plan runs `helm upgrade --install`, helm itself no-ops the
    # ones that haven't changed. `skip_installed=True` restores the prior
    # behavior of skipping anything already in `helm list -A` -- faster on
    # large stacks but silently ignores values-file edits, which is exactly
    # the surprise that motivated the converge-by-default flip.
    skip_installed: bool = False


@dataclass(frozen=True)
class LabSyncOptions:
    """Args for `LabService.sync` -- targeted upgrade of named charts.

    Reuses `LabUpOptions` fields (chart/profile drive the install-plan
    membership check, cluster_name + namespace drive cluster ensure /
    default namespace) but is a distinct type because `skip_installed`
    has no meaning for sync (it's already a targeted-converge verb).
    """

    chart_names: tuple[str, ...]
    chart: str = DEFAULT_CHART
    profile: str = DEFAULT_PROFILE
    cluster_name: str = DEFAULT_CLUSTER_NAME
    namespace: str = DEFAULT_NAMESPACE


@dataclass(frozen=True)
class EntryOutcome:
    """One converged plan entry: which chart:profile landed in which namespace."""

    chart: str
    profile: str
    namespace: str


@dataclass(frozen=True)
class EntryFailure:
    """One failed plan entry, with the error text the surface should surface."""

    chart: str
    profile: str
    namespace: str
    error: str


@dataclass(frozen=True)
class AccessHints:
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
class LabResult:
    """Per-run accounting of converge outcomes, returned by `up` and `sync`.

    Buckets mirror helmfile/Argo terminology:
      * applied:   helm produced a new release revision
      * no_change: helm returned 0 and revision was unchanged (deep no-op)
      * failed:    subprocess error; release may or may not be in a good state
    """

    applied: tuple[EntryOutcome, ...] = ()
    no_change: tuple[EntryOutcome, ...] = ()
    failed: tuple[EntryFailure, ...] = ()
    hints: AccessHints = field(default_factory=AccessHints)

    @property
    def ok(self) -> bool:
        """True when no plan entry failed. Continue-on-error means partial runs are common."""
        return not self.failed


@dataclass(frozen=True)
class ClusterActionResult:
    """Outcome of `down` / `delete`: was the cluster there, and did we reap a forward?

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
class _RunSummary:
    """Mutable accumulator threaded through the install loop.

    Frozen `LabResult` is what leaves the service; this is the in-flight
    scratch buffer the loop appends to.
    """

    applied: list[EntryOutcome] = field(default_factory=list)
    no_change: list[EntryOutcome] = field(default_factory=list)
    failed: list[EntryFailure] = field(default_factory=list)

    def freeze(self, hints: AccessHints) -> LabResult:
        """Snapshot the accumulator into the frozen result the caller gets."""
        return LabResult(
            applied=tuple(self.applied),
            no_change=tuple(self.no_change),
            failed=tuple(self.failed),
            hints=hints,
        )
