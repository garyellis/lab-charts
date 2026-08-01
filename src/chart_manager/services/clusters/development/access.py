"""Post-converge access advice: is the lab CA in place, and what can I reach?

Collaborators are `(kubectl, progress)` only -- nothing here needs helm,
kind, the chart repository, or the install plan. Everything is best-effort
by design: these functions produce advisory data printed *after* the run
summary, so a lookup failure is captured as text rather than raised.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import chain

from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.clusters.development.models import (
    DevelopmentClusterAccessHints,
    RunSummary,
)
from chart_manager.services.progress import ProgressCallback, step, warn

GRAFANA_RELEASE = "grafana"
GRAFANA_ADMIN_SECRET_KEY = "admin-password"
GRAFANA_ADMIN_USER = "admin"

# Lab CA Certificate (and the namespace it lives in) issued by the
# istio-gateway chart's cert-manager-ca.yaml. The wildcard `*.<appsDomain>`
# leaf cert that the gateway listener serves; we gate URL-print on it being
# Ready so the first browser hit isn't a TLS error.
APPS_WILDCARD_CERT_NAME = "apps-wildcard"
APPS_WILDCARD_CERT_NAMESPACE = "istio-ingress"
APPS_WILDCARD_CERT_TIMEOUT = "120s"

# In-cluster CA secret produced by the lab cert-manager bootstrap. The
# one-line keychain-import hint printed at the end of `up` references this
# exact name+namespace (defined in charts/istio-gateway/templates/
# cert-manager-ca.yaml).
LAB_CA_SECRET_NAME = "lab-root-ca-secret"
LAB_CA_SECRET_NAMESPACE = "cert-manager"

# Charts whose successful install means the lab CA cert chain is in place
# and worth telling the user to trust. istio-gateway is the chart that
# owns the cert-manager ClusterIssuers + the root CA Certificate; if it
# applied or no-changed cleanly, the secret exists.
LAB_CA_OWNER_CHART = "istio-gateway"


def lab_ca_present(summary: RunSummary) -> bool:
    """True if the chart that owns the lab CA synced this run (applied or no-change)."""
    # The istio-gateway chart owns the cert-manager ClusterIssuer chain
    # (lab -> lab-root-ca -> lab-ca-issuer) and the wildcard cert. If it
    # synced cleanly, the lab CA secret should exist; either bucket
    # (applied or no-change) is sufficient -- no-change means it already
    # existed from a prior run.
    return any(
        entry.chart == LAB_CA_OWNER_CHART for entry in chain(summary.applied, summary.no_change)
    )


def wait_apps_wildcard_ready(
    summary: RunSummary,
    *,
    kubectl: Kubectl,
    progress: ProgressCallback,
) -> None:
    """Block until `Certificate/apps-wildcard` reports Ready=True.

    Only runs if the istio-gateway chart was part of this run (applied
    or no-change). In any other path (e.g. `sync grafana`) the wildcard
    cert is either pre-existing-and-Ready or simply outside the scope
    of this run. Best-effort: a `kubectl wait` failure is surfaced as
    a warning rather than aborting, because the URL print is itself
    an advisory.
    """
    if not lab_ca_present(summary):
        return
    progress(
        step(
            "Waiting for",
            f"Certificate/{APPS_WILDCARD_CERT_NAME} -n {APPS_WILDCARD_CERT_NAMESPACE}",
        )
    )
    try:
        kubectl.wait_certificate_ready(
            APPS_WILDCARD_CERT_NAME,
            namespace=APPS_WILDCARD_CERT_NAMESPACE,
            timeout=APPS_WILDCARD_CERT_TIMEOUT,
        )
    except ChartManagerError as exc:
        progress(
            warn(
                f"apps-wildcard cert not Ready "
                f"({exc}); URLs below may serve a TLS error until cert-manager catches up"
            )
        )


def urls_and_grafana_host(hosts: Sequence[str]) -> tuple[tuple[str, ...], str | None]:
    """Turn VirtualService hosts into ordered URLs plus the grafana host, if any.

    The pure half of `access_hints`. Sorting is defensive even though
    `list_virtualservice_hosts` already returns sorted output -- keeping
    the contract local means a future kubectl helper change can't quietly
    destabilize the rendered URL block. Ordering it once (rather than
    per-consumer) also removes the second `sorted(hosts)` the two callers
    used to compute independently.
    """
    ordered = sorted(hosts)
    urls = tuple(f"https://{host}/" for host in ordered)
    grafana_host = next((h for h in ordered if h.startswith(f"{GRAFANA_RELEASE}.")), None)
    return urls, grafana_host


def access_hints(
    summary: RunSummary,
    *,
    kubectl: Kubectl,
    namespace: str,
) -> DevelopmentClusterAccessHints:
    """Resolve the post-converge advisory data for this run.

    Two halves, both best-effort and both empty when no relevant chart
    was synced: the CA-trust decision (did the chart that owns the lab
    CA sync?) and the reachable URLs (one per VirtualService host, with
    the Grafana admin credentials attached to the grafana host).

    Lookup failures are captured as `*_error` strings rather than
    raised or printed -- the surface renders them inline, in the same
    position the successful value would have taken.

    Accumulates into locals and constructs `DevelopmentClusterAccessHints` at exactly one
    exit. There used to be five `return DevelopmentClusterAccessHints(...)` sites, each
    re-passing `ca_trust_hint`; adding a field meant finding all five, and
    forgetting one silently dropped it on whichever lookup-failure path
    was missed.
    """
    urls: tuple[str, ...] = ()
    urls_error: str | None = None
    grafana_url: str | None = None
    grafana_credentials: tuple[str, str] | None = None
    grafana_error: str | None = None

    try:
        hosts: Sequence[str] = kubectl.list_virtualservice_hosts()
    except ChartManagerError as exc:
        hosts = ()
        urls_error = f"could not list VirtualServices ({exc}); skipping URL hints"

    if hosts:
        urls, grafana_host = urls_and_grafana_host(hosts)
        if grafana_host is not None:
            grafana_url = f"https://{grafana_host}/"
            # Read the secret from the namespace Grafana actually landed in,
            # not the run default. A profile may declare its own `namespace:`,
            # and the two coincide today only because the grafana cluster-test configuration
            # omits one -- adding that line would silently degrade this to
            # "secret not found" with no other symptom.
            grafana_namespace = next(
                (
                    entry.namespace
                    for entry in chain(summary.applied, summary.no_change)
                    if entry.chart == GRAFANA_RELEASE
                ),
                namespace,
            )
            try:
                password = kubectl.get_secret_value(
                    GRAFANA_RELEASE,
                    GRAFANA_ADMIN_SECRET_KEY,
                    namespace=grafana_namespace,
                )
            except ChartManagerError as exc:
                grafana_error = str(exc)
            else:
                grafana_credentials = (GRAFANA_ADMIN_USER, password)

    return DevelopmentClusterAccessHints(
        ca_trust_hint=lab_ca_present(summary),
        urls=urls,
        grafana_url=grafana_url,
        grafana_credentials=grafana_credentials,
        grafana_error=grafana_error,
        urls_error=urls_error,
    )
