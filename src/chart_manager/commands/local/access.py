"""After a converge: is the lab CA trusted, and which URLs (and logins) can be reached?

Best-effort: a failed lookup is recorded as text on the result, never raised.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import chain

from chart_manager.commands.local.models import (
    DevClusterAccessHints,
    DevClusterCredentials,
    RunSummary,
)
from chart_manager.integrations.kubectl import Kubectl, VirtualService
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.plumbing.progress import ProgressCallback, emit, step, warn

# A VirtualService opts in to a credential hint under its URLs with these
# annotations. The Secret is read from the VirtualService's own namespace;
# the username is a literal; the password is the named key in that Secret.
CREDENTIALS_SECRET_ANNOTATION = "chartmanager.io/credentials-secret"
CREDENTIALS_USERNAME_ANNOTATION = "chartmanager.io/credentials-username"
CREDENTIALS_PASSWORD_KEY_ANNOTATION = "chartmanager.io/credentials-password-key"

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
    progress: ProgressCallback | None,
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
    emit(
        progress,
        step(
            "Waiting for",
            f"Certificate/{APPS_WILDCARD_CERT_NAME} -n {APPS_WILDCARD_CERT_NAMESPACE}",
        ),
    )
    try:
        kubectl.wait_certificate_ready(
            APPS_WILDCARD_CERT_NAME,
            namespace=APPS_WILDCARD_CERT_NAMESPACE,
            timeout=APPS_WILDCARD_CERT_TIMEOUT,
        )
    except ChartManagerError as exc:
        emit(
            progress,
            warn(
                f"apps-wildcard cert not Ready "
                f"({exc}); URLs below may serve a TLS error until cert-manager catches up"
            ),
        )


def virtualservice_urls(virtualservices: Sequence[VirtualService]) -> tuple[str, ...]:
    """Turn VirtualService hosts into one sorted, de-duplicated URL each.

    The pure half of `access_hints`, shared with `local status` so both
    print the same block in the same order regardless of kubectl's.
    """
    hosts = {host for vs in virtualservices for host in vs.hosts}
    return tuple(f"https://{host}/" for host in sorted(hosts))


def _credentials(vs: VirtualService, *, kubectl: Kubectl) -> tuple[DevClusterCredentials, ...]:
    """The login for each URL of one VirtualService, if its annotations opt in.

    An incomplete annotation set or a failed Secret read becomes the
    entry's `error`; nothing here raises.
    """
    secret = vs.annotations.get(CREDENTIALS_SECRET_ANNOTATION)
    urls = virtualservice_urls([vs])
    if not secret or not urls:
        return ()
    username = vs.annotations.get(CREDENTIALS_USERNAME_ANNOTATION)
    password_key = vs.annotations.get(CREDENTIALS_PASSWORD_KEY_ANNOTATION)
    password: str | None = None
    error: str | None = None
    if not username or not password_key:
        missing = [
            name
            for name, value in (
                (CREDENTIALS_USERNAME_ANNOTATION, username),
                (CREDENTIALS_PASSWORD_KEY_ANNOTATION, password_key),
            )
            if not value
        ]
        error = f"incomplete credential annotations: missing {', '.join(missing)}"
    else:
        try:
            password = kubectl.get_secret_value(secret, password_key, namespace=vs.namespace)
        except ChartManagerError as exc:
            error = str(exc)
    if error is not None:
        return tuple(DevClusterCredentials(url=url, error=error) for url in urls)
    return tuple(
        DevClusterCredentials(url=url, username=username, password=password) for url in urls
    )


def access_hints(
    summary: RunSummary,
    *,
    kubectl: Kubectl,
) -> DevClusterAccessHints:
    """Resolve the post-converge advisory data for this run.

    Two halves, both best-effort: the CA-trust decision (did the chart that
    owns the lab CA sync?) and the reachable URLs (one per VirtualService
    host), with a login attached to every URL whose VirtualService carries
    the `chartmanager.io/credentials-*` annotations.

    Lookup failures are captured as error strings rather than raised or
    printed -- the surface renders them inline, in the same position the
    successful value would have taken.
    """
    try:
        virtualservices: Sequence[VirtualService] = kubectl.list_virtualservices()
        urls_error: str | None = None
    except ChartManagerError as exc:
        virtualservices = ()
        urls_error = f"could not list VirtualServices ({exc}); skipping URL hints"

    # One Secret read per VirtualService, attached to each of its URLs. A
    # host claimed by two VirtualServices keeps the first one's login.
    by_url: dict[str, DevClusterCredentials] = {}
    for vs in virtualservices:
        for credentials in _credentials(vs, kubectl=kubectl):
            by_url.setdefault(credentials.url, credentials)

    urls = virtualservice_urls(virtualservices)
    return DevClusterAccessHints(
        ca_trust_hint=lab_ca_present(summary),
        urls=urls,
        credentials=tuple(by_url[url] for url in urls if url in by_url),
        urls_error=urls_error,
    )
