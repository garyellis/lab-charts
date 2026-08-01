"""kubectl wrapper: secrets, port-forwards, readiness waits, pods, events, diagnostics."""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from typing import IO, Any

from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.duration import parse_duration as _parse_duration
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import (
    PROBE_TIMEOUT,
    Check,
    CheckStatus,
    first_line,
    probe_binary,
)

_LOG = logging.getLogger(__name__)


class Kubectl:
    """Run kubectl subcommands through a CommandRunner, pinned to one cluster.

    Matches the `Helm` constructor shape. Before that, this adapter took no
    context at all, so `Settings.kube_context` reached two of six adapters
    and every kubectl call in the lab/sandbox/ci/expose services hit
    whatever `kubectl config current-context` happened to be. That is wrong
    the moment two clusters exist and unusable for a process serving
    concurrent requests against different ones.

    `context=None` reproduces the ambient-kubeconfig behavior exactly: no
    flag is added to any argv.

    This is the one home for generic pod/namespace operations. `pod_logs`,
    `delete_pod`, `namespace_events` and `workload_events` used to live on
    the HelmRelease client instead -- nothing about them is Flux-shaped, so
    "which adapter does a new pod method go on?" had no answer, and the
    namespace-events argv was built here *and* there, in two methods that
    were free to drift apart.
    """

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        context: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Bind a runner and pin every invocation to a context and timeout."""
        self.runner = runner or SubprocessRunner()
        self._context = context
        # Per-subprocess wall-clock cap. None = unbounded, which is what
        # every call site got before this existed; `kubectl get` and the
        # rollout waits could otherwise pin a worker indefinitely.
        self.timeout = timeout

    @property
    def context(self) -> str | None:
        """The kubeconfig context this instance is pinned to, if any.

        Read by services that must name the same cluster in a *detached*
        child (port-forward) rather than through `run`.
        """
        return self._context

    def _with_context(self, args: list[str], *, context: str | None = None) -> list[str]:
        """Append --context when a context applies; `context` overrides the pin.

        Appended rather than inserted after `kubectl` because kubectl accepts
        global flags anywhere in argv, and appending leaves every existing
        subcommand-prefix assertion in the suite valid.
        """
        resolved = context if context is not None else self._context
        if resolved is None:
            return args
        return [*args, "--context", resolved]

    def _budget(self, override: float | None) -> float | None:
        """Resolve a per-call timeout against the instance cap.

        `self.timeout` is a deployment knob (`Settings.command_timeout`).
        The HelmRelease watchers own a *tighter*, per-poll budget that
        changes between requests, so the polled methods take an override
        rather than forcing a fresh adapter per poll. None = use the pin.
        """
        return override if override is not None else self.timeout

    # --- preflight ---------------------------------------------------------

    def preflight(self) -> tuple[Check, ...]:
        """Report the kubectl binary and the kubecontext this instance addresses.

        Both belong here rather than in `doctor`: the context pin is this
        adapter's own state (`--context` is appended by `_with_context`), so
        nothing else can say whether the cluster the next kubectl call will
        talk to is even named in the kubeconfig.

        The context check reads the *kubeconfig*, never the apiserver. A
        preflight must be answerable with no cluster running -- an
        unreachable cluster is something to report, not something to hang
        on -- so "is there a context" and "is the cluster up" stay separate
        questions and only the first is asked here.
        """
        binary = probe_binary(
            self.runner,
            "kubectl",
            name="kubectl",
            version_args=("version", "--client", "-o", "json"),
            version_of=_client_version,
            remediation="install kubectl -- https://kubernetes.io/docs/tasks/tools/",
        )
        if binary.status is not CheckStatus.OK:
            return (binary, Check.skipped("kube-context", "kubectl unavailable"))
        return (binary, self._context_check())

    def _context_check(self) -> Check:
        """Resolve the pinned context, or the ambient one, against the kubeconfig."""
        if self._context is None:
            return self._current_context_check()
        try:
            result = self.runner.run(
                ["kubectl", "config", "get-contexts", "-o", "name"],
                check=False,
                timeout=PROBE_TIMEOUT,
            )
        except ExternalCommandError as exc:
            return _kubeconfig_unreadable(first_line(str(exc)))
        known = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if result.returncode != 0 or self._context not in known:
            return Check.failed(
                "kube-context",
                f"configured context {self._context!r} is not in the kubeconfig",
                remediation=(
                    "set CHART_MANAGER_KUBE_CONTEXT (or `kube_context:` in the config "
                    "file) to one of: " + (", ".join(sorted(known)) or "<none>")
                ),
                outcome=Outcome.ENVIRONMENT,
            )
        return Check.ok("kube-context", f"{self._context} (pinned by configuration)")

    def _current_context_check(self) -> Check:
        """The ambient `kubectl config current-context`, when nothing is pinned."""
        try:
            result = self.runner.run(
                ["kubectl", "config", "current-context"],
                check=False,
                timeout=PROBE_TIMEOUT,
            )
        except ExternalCommandError as exc:
            return _kubeconfig_unreadable(first_line(str(exc)))
        current = result.stdout.strip()
        if result.returncode != 0 or not current:
            return Check.failed(
                "kube-context",
                "no current kubecontext",
                remediation=(
                    "`kubectl config use-context <name>`, or pin one with "
                    "CHART_MANAGER_KUBE_CONTEXT"
                ),
                outcome=Outcome.ENVIRONMENT,
            )
        return Check.ok("kube-context", f"{current} (ambient)")

    def get_secret_value(self, name: str, key: str, *, namespace: str) -> str:
        """Return a base64-decoded value from a Secret's `data` field."""
        result = self.runner.run(
            self._with_context(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "get",
                    "secret",
                    name,
                    "-o",
                    f"jsonpath={{.data.{key}}}",
                ]
            ),
            timeout=self.timeout,
        )
        encoded = result.stdout.strip()
        if not encoded:
            raise ChartManagerError(
                f"secret {namespace}/{name} has no key {key!r} (or it is empty)"
            )
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChartManagerError(
                f"secret {namespace}/{name} key {key!r} is not valid base64-utf8: {exc}"
            ) from exc

    def port_forward(
        self,
        *,
        namespace: str,
        service: str,
        ports: Sequence[str],
        context: str | None = None,
        stdout: IO[str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start a detached port-forward and return the Popen handle.

        Caller is responsible for the process lifecycle (signalling, reaping).
        stderr is merged into stdout; the child runs in a new session so it
        survives the CLI process exiting.

        `context` stays a per-call argument (it defaults to the instance pin)
        because one `ExposeService` fronts every cluster the operator has:
        the cluster is named by the request, not by the adapter's lifetime.
        """
        args = self._with_context(
            [
                "kubectl",
                "port-forward",
                "-n",
                namespace,
                f"svc/{service}",
                *ports,
            ],
            context=context,
        )
        return subprocess.Popen(
            args,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    @contextmanager
    def port_forward_session(
        self,
        *,
        namespace: str,
        service: str,
        remote_port: int,
        context: str | None = None,
        readiness_timeout: float = 10.0,
        poll_interval: float = 0.1,
    ) -> Iterator[int]:
        """Run a short-lived port-forward and yield the bound local port.

        Picks a free local port via the kernel, starts kubectl, waits until
        the local side is accepting connections, yields the port number, and
        always SIGTERMs the child on exit. Use for inline API calls (e.g.,
        Grafana export); persistent forwards belong in ExposeService.
        """
        local_port = _pick_free_port()
        proc = self.port_forward(
            context=context,
            namespace=namespace,
            service=service,
            ports=[f"{local_port}:{remote_port}"],
        )
        try:
            _wait_for_local_port(proc, local_port, readiness_timeout, poll_interval)
            yield local_port
        finally:
            if proc.poll() is None:
                with suppress(ProcessLookupError):
                    os.kill(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    def create_namespace(self, namespace: str) -> None:
        """Create a namespace, tolerating it already existing (check=False)."""
        self.runner.run(
            self._with_context(["kubectl", "create", "namespace", namespace]),
            check=False,
            timeout=self.timeout,
        )

    def wait_apiserver_ready(
        self,
        timeout: str = "60s",
        *,
        poll_interval: float = 2.0,
    ) -> None:
        """Block until the apiserver's /readyz endpoint returns 200.

        Needed after `kind start_cluster`: docker has the containers up but
        the apiserver (and the static pods that back it) take several
        seconds to settle, during which any `kubectl get` / `helm list`
        races and fails. Polling `/readyz` is the same gate kubeadm uses
        internally, and it's cheap because it's a single GET against the
        apiserver's own health endpoint -- no etcd traversal.

        `timeout` accepts kube-style duration suffixes (s, m, h) for
        symmetry with the rollout-status callers; parsed locally so this
        method has no kubectl-version dependency.

        Raises ExternalCommandError on timeout. Distinct from
        ChartManagerError so the CLI exit-code mapping treats this as a
        tool-level failure, matching how subprocess failures bubble up
        elsewhere.
        """
        deadline = time.monotonic() + _parse_duration(timeout)
        # Keep up to _MAX_RECENT_STDERRS *distinct* stderrs in arrival order
        # so a flapping endpoint (DNS then 503 then connection refused) is
        # legible in the final timeout message instead of being collapsed to
        # whatever the last poll happened to see.
        recent_stderrs: list[str] = []
        while time.monotonic() < deadline:
            result = self.runner.run(
                self._with_context(["kubectl", "get", "--raw=/readyz"]),
                check=False,
                timeout=self.timeout,
            )
            if result.returncode == 0 and result.stdout.strip() == "ok":
                return
            stderr = (result.stderr or result.stdout or "").strip()
            if stderr and stderr not in recent_stderrs:
                recent_stderrs.append(stderr)
                if len(recent_stderrs) > _MAX_RECENT_STDERRS:
                    recent_stderrs.pop(0)
            time.sleep(poll_interval)
        detail = "; ".join(recent_stderrs) if recent_stderrs else "<empty>"
        raise ExternalCommandError(
            f"kube-apiserver did not become ready within {timeout} "
            f"(recent responses: {detail})"
        )

    def wait_nodes_ready(self, *, timeout: str = "10m") -> None:
        """Wait until every cluster node reports Ready.

        Local bootstrap uses this CNI-neutral gate after installing whichever
        networking chart the repository selected.
        """
        self.runner.run(
            self._with_context(
                [
                    "kubectl",
                    "wait",
                    "--for=condition=Ready",
                    "nodes",
                    "--all",
                    f"--timeout={timeout}",
                ]
            ),
            timeout=self.timeout,
        )

    def wait_certificate_ready(
        self, name: str, *, namespace: str, timeout: str = "120s"
    ) -> None:
        """Block until cert-manager marks `Certificate/<name>` Ready.

        Thin wrapper around `kubectl wait --for=condition=Ready`, with a
        kube-style timeout. The cert-manager Certificate's `Ready` condition
        flips True only after the controller has issued a x509 cert and the
        backing Secret has been populated; this is the right gate for the
        `apps-wildcard` lab cert before we start advertising URLs whose TLS
        depends on it. Propagates ExternalCommandError on timeout / failure.
        """
        self.runner.run(
            self._with_context(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "wait",
                    "--for=condition=Ready",
                    f"certificate/{name}",
                    f"--timeout={timeout}",
                ]
            ),
            capture=False,
            timeout=self.timeout,
        )

    def wait_deployment_available(
        self, name: str, *, namespace: str, timeout: str = "120s"
    ) -> None:
        """Block until Deployment/<name> reports Available=True.

        Used as the cert-manager webhook gate before applying any
        `cert-manager.io/v1` CRs (Certificate / ClusterIssuer). The
        Deployment's Available condition is the only signal that the
        webhook's TLS serving cert has been provisioned and the apiserver
        can reach it; applying a Certificate too early returns an
        admission "no endpoints available for service" error and the
        install loop then has to retry.
        """
        self.runner.run(
            self._with_context(
                [
                    "kubectl",
                    "-n",
                    namespace,
                    "wait",
                    "--for=condition=Available",
                    f"deployment/{name}",
                    f"--timeout={timeout}",
                ]
            ),
            capture=False,
            timeout=self.timeout,
        )

    def list_virtualservice_hosts(self) -> list[str]:
        """Return all VirtualService `.spec.hosts[]` across the cluster.

        Best-effort: when the CRD isn't installed (lab pre-istio, or the
        sandbox-test path entirely) we return [] rather than surfacing the
        kubectl error -- the caller treats "no VirtualServices" as the
        normal early-install state. Result is deduplicated; ordering is
        stable (sorted) so output is reproducible.
        """
        return self._list_hosts(
            "virtualservice",
            lambda item: (item.get("spec") or {}).get("hosts", []) or [],
        )

    def list_gateway_hosts(self) -> list[str]:
        """Return all Gateway `.spec.servers[].hosts[]` across the cluster.

        Mirrors `list_virtualservice_hosts`: best-effort, dedup'd, sorted.
        Used to derive the lab apps-domain for access URLs.
        print -- the gateway's hosts are the source of truth for the
        domain the gateway listener will admit.
        """
        def _extract(item: dict[str, Any]) -> Iterable[Any]:
            """Yield every host from every server block of one Gateway."""
            for server in (item.get("spec") or {}).get("servers", []) or []:
                yield from (server or {}).get("hosts", []) or []

        return self._list_hosts("gateway", _extract)

    def _list_hosts(
        self,
        resource: str,
        extract: Callable[[dict[str, Any]], Iterable[Any]],
    ) -> list[str]:
        """Shared `kubectl get <resource> -A -o json` -> sorted host list.

        Both `list_virtualservice_hosts` and `list_gateway_hosts` share
        the same shell, the same `.items[]` walk, and the same best-
        effort fallback semantics; only the per-item host-extraction
        differs. Centralising the wrapper keeps the two public methods
        as thin wrappers and means any future addition (e.g. an
        HTTPRoute variant for gateway-api) only writes the extractor.

        Best-effort: a non-zero kubectl, missing CRD, or unparseable
        JSON yields []. Non-string hosts and empty strings are dropped.
        """
        result = self.runner.run(
            self._with_context(["kubectl", "get", resource, "-A", "-o", "json"]),
            check=False,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            return []
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return []
        hosts: set[str] = set()
        for item in payload.get("items", []) or []:
            for host in extract(item):
                if isinstance(host, str) and host:
                    hosts.add(host)
        return sorted(hosts)

    def wait_workloads_ready(
        self,
        namespace: str,
        timeout: str = "10m",
        *,
        selector: str | None = None,
    ) -> None:
        """Run rollout status for matching workloads in a namespace, serially.

        A failed listing raises instead of being read as "no workloads here".
        The listing ran with check=False and the caller iterated its stdout,
        so any failure -- expired credentials, an apiserver blip, the wrong
        context -- produced an empty name list and the gate returned
        immediately. A readiness gate that silently passes when it cannot
        see the cluster is worse than no gate, and it did so precisely when
        the cluster was unhealthy. ``selector`` scopes release-owned waits;
        ``None`` preserves the namespace-wide behavior used by environment
        bootstrap and the development converger.
        """
        for kind in ("deployment", "statefulset", "daemonset"):
            list_args = ["kubectl", "-n", namespace, "get", kind]
            if selector is not None:
                list_args.extend(("-l", selector))
            list_args.extend(("-o", "jsonpath={.items[*].metadata.name}"))
            listing = self.runner.run(
                self._with_context(list_args),
                check=False,
                timeout=self.timeout,
            )
            if listing.returncode != 0:
                detail = (listing.stderr or listing.stdout).strip()
                raise ExternalCommandError(
                    f"cannot list {kind} in namespace {namespace}: {detail}",
                    stderr=listing.stderr,
                    returncode=listing.returncode,
                )
            for name in listing.stdout.split():
                self.runner.run(
                    self._with_context(
                        [
                            "kubectl", "-n", namespace, "rollout", "status",
                            f"{kind}/{name}", f"--timeout={timeout}",
                        ]
                    ),
                    capture=False,
                    timeout=self.timeout,
                )

    # --- pods and events ---------------------------------------------------

    def get_json(
        self, args: Sequence[str], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Run `kubectl <args>` and parse stdout as a JSON object.

        Public because `HelmReleaseClient` is built *on* this adapter rather
        than shelling kubectl a second time: it needs the context pin, the
        timeout policy and the parse policy that live here, and duplicating
        them is exactly how the two adapters drifted apart before.

        Raises ExternalCommandError on a non-zero exit (via the runner) and
        on stdout that is not a JSON object.
        """
        result = self.runner.run(
            self._with_context(list(args)), timeout=self._budget(timeout)
        )
        return _parse_json(result.stdout)

    def delete_pod(
        self, namespace: str, name: str, *, timeout: float | None = None
    ) -> None:
        """Delete a pod, tolerating it already being gone (--ignore-not-found)."""
        self.runner.run(
            self._with_context(
                [
                    "kubectl", "-n", namespace, "delete", "pod", name,
                    "--ignore-not-found",
                ]
            ),
            timeout=self._budget(timeout),
        )

    def pod_logs(
        self,
        namespace: str,
        name: str,
        *,
        container: str | None = None,
        tail: int = 200,
        previous: bool = False,
        timeout: float | None = None,
    ) -> str:
        """Return pod logs; empty string if the pod is gone, raises on other failures."""
        args = [
            "kubectl", "-n", namespace, "logs", name,
            f"--tail={tail}",
        ]
        if container is not None:
            args.extend(["-c", container])
        if previous:
            args.append("--previous")
        result = self.runner.run(
            self._with_context(args), check=False, timeout=self._budget(timeout)
        )
        if result.returncode == 0:
            return result.stdout
        stderr = result.stderr or ""
        if "NotFound" in stderr or "not found" in stderr:
            _LOG.warning(
                "pod logs unavailable",
                extra={
                    "namespace": namespace,
                    "pod": name,
                    "reason": stderr.strip()[:200],
                },
            )
            return ""
        raise ExternalCommandError(
            f"command failed ({result.returncode}): {' '.join(args)}\n{stderr.strip()}",
            stderr=stderr,
            returncode=result.returncode,
        )

    def namespace_events(self, namespace: str, *, timeout: float | None = None) -> str:
        """Return namespace events sorted by time; never raises (check=False)."""
        result = self.runner.run(
            self._with_context(
                [
                    "kubectl", "get", "events", "-n", namespace,
                    "--sort-by=.lastTimestamp",
                ]
            ),
            check=False,
            timeout=self._budget(timeout),
        )
        return result.stdout + result.stderr

    def workload_events(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> str:
        """Return events scoped to one workload object; never raises (check=False)."""
        result = self.runner.run(
            self._with_context(
                [
                    "kubectl", "get", "events", "-n", namespace,
                    "--field-selector",
                    f"involvedObject.name={name},involvedObject.kind={kind}",
                    "--sort-by=.lastTimestamp",
                ]
            ),
            check=False,
            timeout=self._budget(timeout),
        )
        return result.stdout + result.stderr

    def diagnostics(self, namespace: str) -> str:
        """Return a markdown-ish dump of pods and events for the namespace; never raises."""
        pods = self.runner.run(
            self._with_context(["kubectl", "get", "pods", "-n", namespace, "-o", "wide"]),
            check=False,
            timeout=self.timeout,
        )
        # The events half delegates instead of building its own argv: this
        # method and `namespace_events` were the two copies of
        # `get events --sort-by=.lastTimestamp` that finding 8 called out.
        return "\n\n".join(
            [
                f"## pods\n{pods.stdout}{pods.stderr}",
                f"## events\n{self.namespace_events(namespace)}",
            ]
        )


_MAX_RECENT_STDERRS = 4


def _parse_json(stdout: str) -> dict[str, Any]:
    """Parse kubectl stdout into a dict; raise ExternalCommandError on non-JSON/non-object.

    ExternalCommandError rather than the broader ChartManagerError so this
    lands in the same bucket as every other adapter's parse failure. The
    HelmRelease monitor's best-effort handlers catch ExternalCommandError;
    raising the parent type here meant a malformed kubectl payload escaped
    them and aborted the run instead of being recorded as a poll error.
    """
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        snippet = (stdout or "")[:200]
        raise ExternalCommandError(
            f"failed to parse kubectl JSON output: {exc}; payload[:200]={snippet!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise ExternalCommandError(
            f"kubectl JSON payload was not an object: {stdout[:200]!r}"
        )
    return payload


def _pick_free_port() -> int:
    """Ask the kernel for a free port. TOCTOU race: the port is released before use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_local_port(
    proc: subprocess.Popen[bytes],
    port: int,
    timeout: float,
    poll_interval: float,
) -> None:
    """Poll until the forwarded local port accepts connections; raise on exit/timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ChartManagerError(
                f"kubectl port-forward exited before binding (rc={proc.returncode})"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(poll_interval)
    raise ChartManagerError(
        f"kubectl port-forward did not bind 127.0.0.1:{port} within {timeout:.0f}s"
    )


def _client_version(stdout: str) -> str:
    """Pull the client gitVersion out of `kubectl version --client -o json`.

    JSON rather than `--short`, which kubectl removed in 1.28; falling back
    to the first line keeps the probe useful against a client whose output
    shape we did not anticipate rather than reporting a healthy binary as
    broken.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return first_line(stdout)
    version = payload.get("clientVersion", {}).get("gitVersion", "")
    return str(version) if version else first_line(stdout)


def _kubeconfig_unreadable(detail: str) -> Check:
    """The kubecontext check when kubectl itself could not answer."""
    return Check.failed(
        "kube-context",
        f"could not read the kubeconfig: {detail}",
        remediation="check KUBECONFIG and that ~/.kube/config is readable",
        outcome=Outcome.ENVIRONMENT,
    )
