from __future__ import annotations

from pathlib import Path

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError


class Kind:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def clusters(self) -> list[str]:
        result = self.runner.run(["kind", "get", "clusters"], check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def ensure_cluster(self, name: str, *, config: Path | None = None) -> None:
        if name in self.clusters():
            return
        args = ["kind", "create", "cluster", "--name", name]
        if config is not None:
            args.extend(["--config", str(config)])
        self.runner.run(args, capture=False)

    def delete_cluster(self, name: str) -> bool:
        if name not in self.clusters():
            return False
        self.runner.run(["kind", "delete", "cluster", "--name", name], capture=False)
        return True

    def control_plane_ip(self, name: str) -> str:
        # cilium replaces kube-proxy and needs the API server reachable
        # without a Service VIP. On kind the control-plane container's
        # IP on the `kind` docker network is what cluster-internal
        # traffic uses; we look it up via `docker inspect` rather than
        # `kubectl get endpoints` so this works before the cluster has
        # a CNI and pods/endpoints can reconcile.
        container = f"{name}-control-plane"
        result = self.runner.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.NetworkSettings.Networks.kind.IPAddress}}",
                container,
            ]
        )
        ip = result.stdout.strip()
        if not ip:
            raise ChartManagerError(
                f"could not determine control-plane IP for kind cluster {name!r} "
                f"(docker inspect of {container} returned empty)"
            )
        return ip
