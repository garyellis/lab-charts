from __future__ import annotations

import base64
import os
import signal
import socket
import subprocess
import time
from contextlib import contextmanager
from typing import IO, Iterator, Sequence

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError


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
                try:
                    os.kill(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


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

    def create_namespace(self, namespace: str) -> None:
        self.runner.run(["kubectl", "create", "namespace", namespace], check=False)

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
