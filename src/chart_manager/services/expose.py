from __future__ import annotations

import json
import os
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError


@dataclass(frozen=True)
class ExposeRequest:
    cluster_name: str
    service: str  # "<namespace>/<name>"
    ports: list[str]  # "<local>:<remote>" mappings


@dataclass(frozen=True)
class ExposeStatus:
    cluster_name: str
    pid: int
    service: str
    ports: list[str]
    log: Path


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "chart-manager" / "expose"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _local_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
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
        state_dir: Path | None = None,
        kubectl: Kubectl | None = None,
    ) -> None:
        self.state_dir = state_dir or default_state_dir()
        self.kubectl = kubectl or Kubectl()

    def _state_file(self, cluster_name: str) -> Path:
        return self.state_dir / f"{cluster_name}.json"

    def _log_file(self, cluster_name: str) -> Path:
        return self.state_dir / f"{cluster_name}.log"

    def status(self, cluster_name: str) -> ExposeStatus | None:
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
        context = f"kind-{request.cluster_name}"

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
            # All ports never bound in time. Kill the child so we don't leak.
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
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

        return ExposeStatus(
            cluster_name=request.cluster_name,
            pid=proc.pid,
            service=request.service,
            ports=request.ports,
            log=log_file,
        )


def _local_ports(mappings: list[str]) -> list[int]:
    ports: list[int] = []
    for mapping in mappings:
        local, _, _ = mapping.partition(":")
        try:
            ports.append(int(local))
        except ValueError as exc:
            raise ChartManagerError(f"invalid port mapping: {mapping}") from exc
    return ports
