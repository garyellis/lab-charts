from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from typing import IO, Any

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.duration import parse_duration as _parse_duration
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError


class Kubectl:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def get_secret_value(self, name: str, key: str, *, namespace: str) -> str:
        """Return a base64-decoded value from a Secret's `data` field."""
        result = self.runner.run(
            [
                "kubectl",
                "-n",
                namespace,
                "get",
                "secret",
                name,
                "-o",
                f"jsonpath={{.data.{key}}}",
            ],
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
        context: str,
        namespace: str,
        service: str,
        ports: Sequence[str],
        stdout: IO[str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start a detached port-forward and return the Popen handle.

        Caller is responsible for the process lifecycle (signalling, reaping).
        stderr is merged into stdout; the child runs in a new session so it
        survives the CLI process exiting.
        """
        args = [
            "kubectl",
            "--context",
            context,
            "port-forward",
            "-n",
            namespace,
            f"svc/{service}",
            *ports,
        ]
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
        context: str,
        namespace: str,
        service: str,
        remote_port: int,
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
        self.runner.run(["kubectl", "create", "namespace", namespace], check=False)

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
                ["kubectl", "get", "--raw=/readyz"],
                check=False,
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
            [
                "kubectl",
                "-n",
                namespace,
                "wait",
                "--for=condition=Ready",
                f"certificate/{name}",
                f"--timeout={timeout}",
            ],
            capture=False,
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
            [
                "kubectl",
                "-n",
                namespace,
                "wait",
                "--for=condition=Available",
                f"deployment/{name}",
                f"--timeout={timeout}",
            ],
            capture=False,
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
        Used to derive the lab apps-domain for the `sandbox expose` URL
        print -- the gateway's hosts are the source of truth for the
        domain the gateway listener will admit.
        """
        def _extract(item: dict[str, Any]) -> Iterable[Any]:
            for server in (item.get("spec") or {}).get("servers", []) or []:
                for host in (server or {}).get("hosts", []) or []:
                    yield host

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
            ["kubectl", "get", resource, "-A", "-o", "json"],
            check=False,
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

    def wait_workloads_ready(self, namespace: str, timeout: str = "10m") -> None:
        for kind in ("deployment", "statefulset", "daemonset"):
            listing = self.runner.run(
                [
                    "kubectl", "-n", namespace, "get", kind,
                    "-o", "jsonpath={.items[*].metadata.name}",
                ],
                check=False,
            )
            for name in listing.stdout.split():
                self.runner.run(
                    [
                        "kubectl", "-n", namespace, "rollout", "status",
                        f"{kind}/{name}", f"--timeout={timeout}",
                    ],
                    capture=False,
                )

    def diagnostics(self, namespace: str) -> str:
        sections: list[str] = []
        commands = [
            ("pods", ["kubectl", "get", "pods", "-n", namespace, "-o", "wide"]),
            ("events", ["kubectl", "get", "events", "-n", namespace, "--sort-by=.lastTimestamp"]),
        ]
        for title, args in commands:
            result = self.runner.run(args, check=False)
            sections.append(f"## {title}\n{result.stdout}{result.stderr}")
        return "\n\n".join(sections)


_MAX_RECENT_STDERRS = 4


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_local_port(
    proc: subprocess.Popen[bytes],
    port: int,
    timeout: float,
    poll_interval: float,
) -> None:
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
