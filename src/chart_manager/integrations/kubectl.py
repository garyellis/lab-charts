"""kubectl wrapper: secrets, port-forwards, readiness waits, Flux HelmReleases, pods,
events, diagnostics."""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any

from chart_manager.plumbing.commands import CommandRunner
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

_FLUX_GROUP_PREFIX = "helm.toolkit.fluxcd.io/"


@dataclass(frozen=True)
class VirtualService:
    """The slice of one Istio VirtualService the access hints read."""

    namespace: str
    hosts: tuple[str, ...]
    annotations: Mapping[str, str]


@dataclass(frozen=True)
class HelmReleaseRef:
    """Identity of one HelmRelease plus its derived helm release name/namespaces."""

    name: str
    namespace: str
    api_version: str
    release_name: str
    storage_namespace: str
    target_namespace: str


@dataclass(frozen=True)
class ConditionSnapshot:
    """One entry from `status.conditions`, normalized to strings + parsed timestamp."""

    type: str
    status: str
    reason: str
    message: str
    last_transition_time: datetime | None


@dataclass(frozen=True)
class HelmReleaseStatus:
    """Point-in-time snapshot of a HelmRelease's spec/status fields we care about."""

    ref: HelmReleaseRef
    observed_at: datetime
    generation: int
    observed_generation: int
    resource_version: str
    suspended: bool
    desired_chart_name: str | None
    desired_chart_version: str | None
    last_applied_revision: str | None
    history_chart_version: str | None
    conditions: tuple[ConditionSnapshot, ...]

    def condition(self, type_: str) -> ConditionSnapshot | None:
        """Return the first condition of the given type, or None."""
        for cond in self.conditions:
            if cond.type == type_:
                return cond
        return None

    @property
    def ready(self) -> ConditionSnapshot | None:
        """The Ready condition, or None."""
        return self.condition("Ready")

    @property
    def released(self) -> ConditionSnapshot | None:
        """The Released condition, or None."""
        return self.condition("Released")

    @property
    def test_success(self) -> ConditionSnapshot | None:
        """The TestSuccess condition, or None."""
        return self.condition("TestSuccess")


@dataclass(frozen=True)
class OwnedWorkload:
    """Replica counts for one Deployment/StatefulSet/DaemonSet owned by a release."""

    kind: str
    namespace: str
    name: str
    desired: int
    ready: int
    available: int


@dataclass(frozen=True)
class WorkloadRollout:
    """An OwnedWorkload plus a converged verdict (generation observed, all replicas up)."""

    workload: OwnedWorkload
    converged: bool
    generation: int
    observed_generation: int


class Kubectl:
    """Run kubectl subcommands through a CommandRunner, pinned to one cluster.

    Matches the `Helm` constructor shape. Before that, this adapter took no
    context at all, so `Settings.kube_context` reached two of six adapters
    and every other kubectl call hit
    whatever `kubectl config current-context` happened to be. That is wrong
    the moment two clusters exist and unusable for a process serving
    concurrent requests against different ones.

    `context=None` reproduces the ambient-kubeconfig behavior exactly: no
    flag is added to any argv.

    This is the one home for kubectl queries, the Flux HelmRelease ones
    included: HelmReleases are ordinary custom resources.
    """

    def __init__(
        self,
        runner: CommandRunner,
        *,
        context: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Bind a runner and pin every invocation to a context and timeout."""
        self.runner = runner
        self._context = context
        # Per-subprocess wall-clock cap. None = unbounded, which is what
        # every call site got before this existed; `kubectl get` and the
        # rollout waits could otherwise pin a worker indefinitely.
        self.timeout = timeout

    @property
    def context(self) -> str | None:
        """The kubeconfig context this instance is pinned to, if any.

        Read by callers that must name the same cluster in a *detached*
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

        `context` defaults to the instance pin.
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
        Grafana export).
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

        Needed after kind starts stopped nodes: docker has the containers up but
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

    def list_virtualservices(self) -> list[VirtualService]:
        """Return every VirtualService across the cluster: namespace, hosts, annotations.

        Best-effort: when kubectl fails (e.g. the CRD isn't installed, lab
        pre-istio or the sandbox-test path) or prints unreadable JSON, we
        return [] rather than surfacing the error -- the caller treats "no VirtualServices" as the
        normal early-install state. Non-string hosts and annotation values
        are dropped; order is kubectl's (namespace, then name).
        """
        result = self.runner.run(
            self._with_context(["kubectl", "get", "virtualservice", "-A", "-o", "json"]),
            check=False,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            return []
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return []
        found: list[VirtualService] = []
        for item in payload.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") or {}
            hosts = (item.get("spec") or {}).get("hosts", []) or []
            annotations = metadata.get("annotations") or {}
            found.append(
                VirtualService(
                    namespace=str(metadata.get("namespace", "")),
                    hosts=tuple(h for h in hosts if isinstance(h, str) and h),
                    annotations={
                        k: v
                        for k, v in annotations.items()
                        if isinstance(k, str) and isinstance(v, str)
                    },
                )
            )
        return found

    def workload_names(
        self, kind: str, *, namespace: str, selector: str | None = None
    ) -> list[str]:
        """Names of the `kind` workloads in `namespace`, optionally matching `selector`.

        A failed listing raises rather than reading as "none here": a readiness gate
        that passes when it cannot see the cluster is worse than no gate.
        """
        listing = self.runner.run(
            self._with_context(
                [
                    "kubectl", "-n", namespace, "get", kind,
                    *(("-l", selector) if selector is not None else ()),
                    "-o", "jsonpath={.items[*].metadata.name}",
                ]
            ),
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
        return listing.stdout.split()

    def rollout_status(self, kind: str, name: str, *, namespace: str, timeout: str) -> None:
        """Block until one Deployment, StatefulSet or DaemonSet has rolled out."""
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

    def wait_established(self, crd: str, *, timeout: str) -> None:
        """Block until a CustomResourceDefinition is `Established`."""
        self.runner.run(
            self._with_context(
                [
                    "kubectl", "wait", "--for=condition=Established",
                    f"customresourcedefinition/{crd}", f"--timeout={timeout}",
                ]
            ),
            capture=False,
            timeout=self.timeout,
        )

    def wait_workloads_ready(
        self,
        namespace: str,
        timeout: str = "10m",
        *,
        selector: str | None = None,
    ) -> None:
        """Run rollout status for every matching workload in a namespace, serially."""
        for kind in ("deployment", "statefulset", "daemonset"):
            for name in self.workload_names(kind, namespace=namespace, selector=selector):
                self.rollout_status(kind, name, namespace=namespace, timeout=timeout)

    # --- Flux HelmReleases -------------------------------------------------
    # Read-only `kubectl get`s of custom resources; nothing invokes `flux`. No
    # retries or waits: callers own budgets and should bound concurrency (~8 per
    # kubeconfig, given exec-auth token-cache races on EKS/GKE).

    def list_helmreleases(
        self,
        *,
        namespace: str | None = None,
        timeout: float | None = None,
    ) -> list[HelmReleaseRef]:
        """List HelmReleases (all namespaces by default); unparseable items are skipped."""
        args = ["kubectl", "get", "helmreleases.helm.toolkit.fluxcd.io"]
        if namespace is None:
            args.append("-A")
        else:
            args.extend(["-n", namespace])
        args.extend(["-o", "json"])
        payload = self._get_json(args, timeout=timeout)
        refs: list[HelmReleaseRef] = []
        for item in payload.get("items", []) or []:
            ref = _ref_from_item(item)
            if ref is not None:
                refs.append(ref)
        return refs

    def get_helmrelease_status(
        self,
        ref: HelmReleaseRef,
        *,
        timeout: float | None = None,
    ) -> HelmReleaseStatus:
        """Fetch one HelmRelease and snapshot its status (stamped with wall-clock time)."""
        args = [
            "kubectl", "-n", ref.namespace, "get",
            "helmreleases.helm.toolkit.fluxcd.io", ref.name, "-o", "json",
        ]
        payload = self._get_json(args, timeout=timeout)
        observed_at = datetime.now(UTC)
        return _status_from_item(payload, ref, observed_at)

    def list_owned_workloads(
        self,
        ref: HelmReleaseRef,
        *,
        timeout: float | None = None,
    ) -> list[WorkloadRollout]:
        """List workloads labeled as owned by this release, with rollout convergence."""
        args = [
            "kubectl", "get", "deployment,statefulset,daemonset",
            "-A", "-l", _flux_owner_selector(ref), "-o", "json",
        ]
        payload = self._get_json(args, timeout=timeout)
        rollouts: list[WorkloadRollout] = []
        for item in payload.get("items", []) or []:
            rollout = _rollout_from_item(item)
            if rollout is not None:
                rollouts.append(rollout)
        return rollouts

    def list_test_pods(
        self,
        ref: HelmReleaseRef,
        *,
        timeout: float | None = None,
    ) -> list[tuple[str, str, str]]:
        """Return (namespace, name, phase) for this release's helm test hook pods.

        Queries the target namespace for both `helm.sh/hook=test` and the
        legacy `test-success` label, deduping pods that carry both.
        """
        seen: set[tuple[str, str]] = set()
        pods: list[tuple[str, str, str]] = []
        for hook in ("test", "test-success"):
            args = [
                "kubectl", "-n", ref.target_namespace, "get", "pods",
                "-l", f"{_flux_owner_selector(ref)},helm.sh/hook={hook}", "-o", "json",
            ]
            payload = self._get_json(args, timeout=timeout)
            for item in payload.get("items", []) or []:
                metadata = item.get("metadata") or {}
                ns = str(metadata.get("namespace") or "")
                name = str(metadata.get("name") or "")
                if not name:
                    continue
                key = (ns, name)
                if key in seen:
                    continue
                seen.add(key)
                phase = str((item.get("status") or {}).get("phase") or "")
                pods.append((ns, name, phase))
        return pods

    def _get_json(
        self, args: Sequence[str], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Run `kubectl <args>` and parse stdout as a JSON object.

        Raises ExternalCommandError on a non-zero exit (via the runner) and
        on stdout that is not a JSON object.
        """
        result = self.runner.run(
            self._with_context(list(args)), timeout=self._budget(timeout)
        )
        return _parse_json(result.stdout)

    # --- pods and events ---------------------------------------------------

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


def _flux_owner_selector(ref: HelmReleaseRef) -> str:
    """The label selector Flux stamps on every object a HelmRelease owns."""
    return (
        f"helm.toolkit.fluxcd.io/name={ref.name},"
        f"helm.toolkit.fluxcd.io/namespace={ref.namespace}"
    )


def _ref_from_item(item: Any) -> HelmReleaseRef | None:
    """Build a HelmReleaseRef from a raw item, deriving the helm release name.

    Returns None for non-Flux or malformed items. Encodes the helm-controller
    naming rule (releaseName > targetNamespace-name > name).
    """
    if not isinstance(item, dict):
        return None
    api_version = item.get("apiVersion", "")
    # Match by group prefix so v2beta1/v2beta2/v2 all flow through one path.
    if not (isinstance(api_version, str) and api_version.startswith(_FLUX_GROUP_PREFIX)):
        return None
    metadata = _dict(item.get("metadata"))
    name = str(metadata.get("name") or "")
    namespace = str(metadata.get("namespace") or "")
    if not name or not namespace:
        return None
    spec = _dict(item.get("spec"))
    spec_release_name = spec.get("releaseName")
    target_ns_raw = spec.get("targetNamespace")
    target_ns = str(target_ns_raw) if target_ns_raw else None
    # Flux helm-controller release name rule:
    #   spec.releaseName if set, else "<targetNamespace>-<metadata.name>"
    #   when targetNamespace is set (even if it equals metadata.namespace),
    #   else metadata.name. Empty string "" on either field is treated as
    #   unset to match how the controller's truthy check behaves.
    if spec_release_name:
        release_name = str(spec_release_name)
    elif target_ns:
        release_name = f"{target_ns}-{name}"
    else:
        release_name = name
    target_namespace = target_ns if target_ns else namespace
    storage_namespace = str(
        spec.get("storageNamespace")
        or spec.get("targetNamespace")
        or namespace
    )
    return HelmReleaseRef(
        name=name,
        namespace=namespace,
        api_version=api_version,
        release_name=release_name,
        storage_namespace=storage_namespace,
        target_namespace=target_namespace,
    )


def _status_from_item(
    payload: dict[str, Any],
    ref: HelmReleaseRef,
    observed_at: datetime,
) -> HelmReleaseStatus:
    """Extract the fields we track from a HelmRelease object into a status snapshot."""
    metadata = _dict(payload.get("metadata"))
    spec = _dict(payload.get("spec"))
    status = _dict(payload.get("status"))

    spec_chart = _dict(spec.get("chart"))
    chart_spec = _dict(spec_chart.get("spec"))

    history = status.get("history") if isinstance(status.get("history"), list) else []
    history_chart_version: str | None = None
    # status.history is newest-first; [0] is the latest release attempt.
    if history and isinstance(history[0], dict):
        raw = history[0].get("chartVersion")
        history_chart_version = str(raw) if raw is not None else None

    conditions = tuple(
        _condition_from_item(c)
        for c in (status.get("conditions") or [])
        if isinstance(c, dict)
    )

    return HelmReleaseStatus(
        ref=ref,
        observed_at=observed_at,
        generation=int(metadata.get("generation") or 0),
        # -1 sentinel = controller has not observed any generation yet.
        observed_generation=int(status.get("observedGeneration", -1)),
        resource_version=str(metadata.get("resourceVersion") or ""),
        suspended=bool(spec.get("suspend")),
        desired_chart_name=_opt_str(chart_spec.get("chart")),
        desired_chart_version=_opt_str(chart_spec.get("version")),
        last_applied_revision=_opt_str(status.get("lastAppliedRevision")),
        history_chart_version=history_chart_version,
        conditions=conditions,
    )


def _condition_from_item(item: dict[str, Any]) -> ConditionSnapshot:
    """Normalize one raw condition dict into a ConditionSnapshot."""
    return ConditionSnapshot(
        type=str(item.get("type") or ""),
        status=str(item.get("status") or ""),
        reason=str(item.get("reason") or ""),
        message=str(item.get("message") or ""),
        last_transition_time=_parse_iso8601(item.get("lastTransitionTime")),
    )


def _parse_iso8601(value: Any) -> datetime | None:
    """Parse a k8s timestamp to aware-UTC datetime; None if missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _rollout_from_item(item: Any) -> WorkloadRollout | None:
    """Map a workload object to a WorkloadRollout; None for unsupported kinds.

    Converged = controller observed the current generation AND ready/available
    both equal desired. Replica fields differ per kind (DaemonSets have no
    spec.replicas; StatefulSets may omit availableReplicas, so ready is the
    fallback).
    """
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "")
    metadata = _dict(item.get("metadata"))
    namespace = str(metadata.get("namespace") or "")
    name = str(metadata.get("name") or "")
    if not name or not namespace:
        return None
    spec = _dict(item.get("spec"))
    status = _dict(item.get("status"))

    if kind == "Deployment":
        desired = int(spec.get("replicas", 1))
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas") or 0)
    elif kind == "StatefulSet":
        desired = int(spec.get("replicas", 1))
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas", ready) or 0)
    elif kind == "DaemonSet":
        desired = int(status.get("desiredNumberScheduled") or 0)
        ready = int(status.get("numberReady") or 0)
        available = int(status.get("numberAvailable") or 0)
    else:
        return None

    generation = int(metadata.get("generation") or 0)
    observed_generation = int(status.get("observedGeneration") or 0)

    converged = (
        observed_generation == generation
        and ready == desired
        and available == desired
        and desired >= 0
    )

    return WorkloadRollout(
        workload=OwnedWorkload(
            kind=kind,
            namespace=namespace,
            name=name,
            desired=desired,
            ready=ready,
            available=available,
        ),
        converged=converged,
        generation=generation,
        observed_generation=observed_generation,
    )


def _opt_str(value: Any) -> str | None:
    """str() the value, passing None through."""
    if value is None:
        return None
    return str(value)


def _dict(value: Any) -> dict[str, Any]:
    """Return a string-keyed mapping for a decoded JSON object, else an empty one."""
    if not isinstance(value, dict):
        return {}
    return value
