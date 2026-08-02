"""Detached kubectl port-forward lifecycle, with on-disk state per kind cluster."""
from __future__ import annotations

import json
import os
import signal
import socket
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from chart_manager.integrations.kind import kind_context
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError

# The lab gateway listens on 443 and 80; these are the host ports the kind
# node publishes them on. An empty `--port` list means "the usual lab pair",
# so the mapping lives here rather than in each surface's flag default.
DEFAULT_PORTS: Final[tuple[str, ...]] = ("8443:443", "8080:80")

# Printed URL scheme is a heuristic on the *remote* port: the gateway serves
# TLS on the well-known TLS ports and plaintext everywhere else. It is only
# ever used to build an advisory URL, never to decide how to connect.
_HTTPS_REMOTE_PORTS: Final[frozenset[str]] = frozenset({"443", "8443"})

# No Gateway installed yet (pre-lab, or a single-chart cluster test) means
# there is no apps domain to derive; `localhost` matches the appsDomain
# default in charts/istio-gateway/values-ci.yaml.
APPS_DOMAIN_FALLBACK: Final[str] = "localhost"


@dataclass(frozen=True)
class ExposeRequest:
    """Args for `ExposeService.start`."""

    cluster_name: str
    service: str  # "<namespace>/<name>"
    ports: list[str] = field(default_factory=lambda: list(DEFAULT_PORTS))  # "<local>:<remote>"

    def __post_init__(self) -> None:
        """An empty port list means the caller wants the lab defaults."""
        # Surfaces collect repeatable --port flags into a list that is empty
        # when the user passed none; normalizing here keeps the default map
        # out of every surface.
        if not self.ports:
            object.__setattr__(self, "ports", list(DEFAULT_PORTS))


@dataclass(frozen=True)
class ExposedUrl:
    """One advertised URL derived from a port mapping plus the apps domain."""

    url: str
    local_port: str
    remote_port: str


@dataclass(frozen=True)
class ExposeStatus:
    """Snapshot of a live port-forward process.

    `urls` is populated by `start`, which is the only path that knows the
    apps domain; `status` reads the on-disk state file and deliberately
    leaves it empty rather than paying a `kubectl get gateway -A` on every
    liveness probe.
    """

    cluster_name: str
    pid: int
    service: str
    ports: list[str]
    log: Path
    apps_domain: str = APPS_DOMAIN_FALLBACK
    urls: tuple[ExposedUrl, ...] = ()


def default_state_dir() -> Path:
    """Return the XDG state dir for expose bookkeeping files."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "chart-manager" / "expose"


def _alive(pid: int) -> bool:
    """True if `pid` is a live process (signal-0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user -- still alive.
        return True
    return True


def _local_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    """True if a TCP connect to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class ExposeService:
    """Manage a detached kubectl port-forward keyed by kind cluster name."""

    def __init__(
        self,
        *,
        kubectl: Kubectl,
        state_dir: Path | None = None,
    ) -> None:
        """Wire the state dir and kubectl.

        `kubectl` is required, not defaulted: an `or Kubectl()` fallback here
        built an adapter with no context, so a service constructed outside
        the composition root addressed whatever cluster the ambient
        kubeconfig happened to select.
        """
        self.state_dir = state_dir or default_state_dir()
        self.kubectl = kubectl

    def _state_file(self, cluster_name: str) -> Path:
        """Path of the JSON state file for `cluster_name`."""
        return self.state_dir / f"{cluster_name}.json"

    def _log_file(self, cluster_name: str) -> Path:
        """Path of the port-forward log file for `cluster_name`."""
        return self.state_dir / f"{cluster_name}.log"

    def apps_domain(self) -> str:
        """Best-effort apps-domain detection from the installed Gateways.

        Reads `kubectl get gateway -A`, harvests `.spec.servers[].hosts[]`,
        strips the wildcard `*.` prefix, and returns the most-common host
        suffix. Ties break on alphabetical order so output is reproducible.
        Falls back to `APPS_DOMAIN_FALLBACK` when no Gateway is installed.

        A single kubectl call so callers can resolve the domain once and
        reuse it across every mapping they render.
        """
        try:
            hosts = self.kubectl.list_gateway_hosts()
        except ChartManagerError:
            return APPS_DOMAIN_FALLBACK
        suffixes = [h[2:] if h.startswith("*.") else h for h in hosts]
        counts = Counter(s for s in suffixes if s)
        if not counts:
            return APPS_DOMAIN_FALLBACK
        # Counter.most_common is insertion-stable on ties; pick the
        # alphabetically smallest suffix in the top-frequency band so the
        # output is deterministic regardless of host iteration order.
        top_freq = max(counts.values())
        return min(s for s, c in counts.items() if c == top_freq)

    def status(self, cluster_name: str) -> ExposeStatus | None:
        """Return live port-forward status, or None if no state or the process is dead."""
        state_file = self._state_file(cluster_name)
        if not state_file.exists():
            return None
        data = json.loads(state_file.read_text())
        pid = data.get("pid", -1)
        if not _alive(pid):
            return None
        return ExposeStatus(
            cluster_name=cluster_name,
            pid=pid,
            service=data["service"],
            ports=data["ports"],
            log=Path(data["log"]),
        )

    def stop(self, cluster_name: str) -> int | None:
        """Stop the port-forward and clear state. Returns the PID stopped, or None."""
        state_file = self._state_file(cluster_name)
        if not state_file.exists():
            return None
        data = json.loads(state_file.read_text())
        # Clear state before killing so a stale/dead pid can't wedge future starts.
        state_file.unlink(missing_ok=True)
        pid = data.get("pid")
        if not pid or not _alive(pid):
            return None
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return None
        return pid

    def start(
        self,
        request: ExposeRequest,
        *,
        readiness_timeout: float = 10.0,
        poll_interval: float = 0.1,
    ) -> ExposeStatus:
        """Launch a detached port-forward and wait until all local ports accept connections.

        Raises ChartManagerError if a forward is already running for this
        cluster, the child exits early, or the ports don't bind within
        `readiness_timeout`. State is persisted only after readiness.
        """
        if "/" not in request.service:
            raise ChartManagerError(
                f"--service must be namespace/name, got: {request.service}"
            )

        self.state_dir.mkdir(parents=True, exist_ok=True)
        state_file = self._state_file(request.cluster_name)
        log_file = self._log_file(request.cluster_name)

        existing = self.status(request.cluster_name)
        if existing is not None:
            raise ChartManagerError(
                f"port-forward already running for cluster {request.cluster_name} "
                f"(pid {existing.pid}); stop it first"
            )
        state_file.unlink(missing_ok=True)

        namespace, name = request.service.split("/", 1)
        # An explicitly configured context wins; otherwise the request names
        # a kind cluster and kind owns the mapping from cluster name to
        # context. One ExposeService fronts every cluster, so this cannot be
        # decided once at construction.
        context = self.kubectl.context or kind_context(request.cluster_name)

        # The child dup's the log file descriptor at fork, so the parent
        # handle can (and must) be closed once Popen returns.
        log_handle = log_file.open("w")
        try:
            try:
                proc = self.kubectl.port_forward(
                    context=context,
                    namespace=namespace,
                    service=name,
                    ports=request.ports,
                    stdout=log_handle,
                )
            except FileNotFoundError as exc:
                raise ChartManagerError("kubectl not found on PATH") from exc
        finally:
            log_handle.close()

        local_ports = _local_ports(request.ports)
        deadline = time.monotonic() + readiness_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                output = log_file.read_text().strip()
                raise ChartManagerError(
                    f"port-forward exited immediately (rc={proc.returncode})\n{output}"
                )
            if all(_local_port_open(p) for p in local_ports):
                break
            time.sleep(poll_interval)
        else:
            # The deadline can pass before the loop ever polls (zero/tiny
            # timeout), so an already-dead child must be reported as such
            # here too, not blindly signalled -- its pid may have been
            # recycled by an unrelated process.
            if proc.poll() is not None:
                output = log_file.read_text().strip()
                raise ChartManagerError(
                    f"port-forward exited immediately (rc={proc.returncode})\n{output}"
                )
            # All ports never bound in time. Terminate through the handle we
            # own so the signal cannot reach anything but our child.
            with suppress(ProcessLookupError):
                proc.terminate()
            output = log_file.read_text().strip()
            raise ChartManagerError(
                f"port-forward did not become ready within {readiness_timeout:.0f}s\n{output}"
            )

        state = {
            "cluster": request.cluster_name,
            "service": request.service,
            "ports": request.ports,
            "pid": proc.pid,
            "log": str(log_file),
        }
        state_file.write_text(json.dumps(state, indent=2))

        # Resolve the apps domain once per start: the URL list is the whole
        # reason a surface calls this verb, and re-listing Gateways per
        # mapping would be a kubectl call per printed line.
        domain = self.apps_domain()
        return ExposeStatus(
            cluster_name=request.cluster_name,
            pid=proc.pid,
            service=request.service,
            ports=request.ports,
            log=log_file,
            apps_domain=domain,
            urls=_resolve_urls(request.ports, domain),
        )


def _resolve_urls(mappings: list[str], apps_domain: str) -> tuple[ExposedUrl, ...]:
    """Turn "<local>:<remote>" mappings into the wildcard URLs they expose."""
    urls: list[ExposedUrl] = []
    for mapping in mappings:
        local, _, remote = mapping.partition(":")
        scheme = "https" if remote in _HTTPS_REMOTE_PORTS else "http"
        urls.append(
            ExposedUrl(
                url=f"{scheme}://*.{apps_domain}:{local}/",
                local_port=local,
                remote_port=remote,
            )
        )
    return tuple(urls)


def _local_ports(mappings: list[str]) -> list[int]:
    """Parse the local port numbers from "<local>:<remote>" mappings."""
    ports: list[int] = []
    for mapping in mappings:
        local, _, _ = mapping.partition(":")
        try:
            ports.append(int(local))
        except ValueError as exc:
            raise ChartManagerError(f"invalid port mapping: {mapping}") from exc
    return ports
