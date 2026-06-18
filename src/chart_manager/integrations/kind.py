from __future__ import annotations

import json
from pathlib import Path

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ChartManagerError

# kind labels every node container it creates with this label whose value
# is the cluster name. Discovering containers by label (rather than by the
# `<name>-control-plane` naming convention) is what makes stop/start
# correct for multi-node clusters too.
KIND_CLUSTER_LABEL = "io.x-k8s.kind.cluster"


class Kind:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def clusters(self) -> list[str]:
        result = self.runner.run(["kind", "get", "clusters"], check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def ensure_cluster(self, name: str, *, config: Path | None = None) -> None:
        # `kind get clusters` lists clusters whose node containers exist on
        # the host docker daemon, regardless of whether those containers are
        # currently running. So "present" is a tri-state -- but with multi-
        # node clusters it's really four states because the "some stopped,
        # some running" case (e.g. a crash-restarted worker, an interrupted
        # `down`) must be repaired:
        #   - absent              -> create
        #   - any stopped         -> docker start the stopped ones
        #   - all running         -> no-op
        # Discovery is by label, so workers + control-plane are handled
        # uniformly. We diff the two listings rather than relying on a
        # boolean "any running" flag, which lies for partial states.
        if name in self.clusters():
            all_nodes = self._node_container_names(name, include_stopped=True)
            running_nodes = self._node_container_names(name, include_stopped=False)
            stopped_nodes = [n for n in all_nodes if n not in set(running_nodes)]
            if stopped_nodes:
                # Start ONLY the stopped node containers. `docker start` on
                # an already-running container is a no-op but emits a
                # warning; restricting to the actually-stopped set keeps
                # output clean and the operation truthful.
                self.runner.run(
                    ["docker", "start", *stopped_nodes], capture=False
                )
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

    def stop_cluster(self, name: str) -> bool:
        """Stop (but do not remove) the kind cluster's node containers.

        Preserves the docker volumes backing containerd, etcd, and any host
        path mounts -- so installed Helm releases, PVCs, and cached images
        survive a subsequent `start_cluster` / `ensure_cluster`.

        Returns True when at least one container was stopped. Returns False
        when the cluster has no containers (either never created, or already
        torn down via `delete_cluster`).
        """
        names = self._node_container_names(name, include_stopped=False)
        if not names:
            return False
        self.runner.run(["docker", "stop", *names], capture=False)
        return True

    def start_cluster(self, name: str) -> bool:
        """Start previously-stopped node containers for the named cluster.

        Returns True if any node containers were found (running or stopped)
        and a start was issued; False if no node containers exist. The
        apiserver takes a few seconds to become reachable after start --
        intentionally NOT awaited here, so the caller can decide whether to
        block (e.g. via Kubectl.wait_workloads_ready or a readiness probe).
        """
        names = self._node_container_names(name, include_stopped=True)
        if not names:
            return False
        self.runner.run(["docker", "start", *names], capture=False)
        return True

    def has_running_node(self, name: str) -> bool:
        """True iff at least one node container for the cluster is running.

        Note this is intentionally NOT "the cluster is healthy" -- a
        multi-node cluster can have a running control-plane and a stopped
        worker and this still returns True. Callers that need partial-state
        repair should diff `_node_container_names(include_stopped=True)`
        against `_node_container_names(include_stopped=False)` directly
        (see `ensure_cluster`).
        """
        return bool(self._node_container_names(name, include_stopped=False))

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

    def container_host_ports(self, cluster_name: str) -> set[int]:
        """Return the set of host-side ports the cluster's nodes are bound to.

        Enumerates every node container for the cluster via the
        `io.x-k8s.kind.cluster=<name>` label (same discovery as
        stop/start), `docker inspect`s each, and unions their
        `.NetworkSettings.Ports[<containerPort>/tcp][].HostPort` entries.
        The result is what's *currently* live across the cluster; compare
        against the kind-config's `extraPortMappings` host ports to detect
        drift caused by editing kind-config.yaml without recreating the
        cluster (`kind` bakes port mappings in at create time -- a `down`
        + `up` with new mappings is a no-op).

        Best-effort: an unreadable inspect (cluster absent, docker daemon
        glitch) contributes nothing to the result. An empty return value
        means either no nodes were discoverable or no host ports are
        bound; empty != mismatch, so the caller must compare against the
        expected set explicitly.
        """
        node_names = self._node_container_names(cluster_name, include_stopped=True)
        if not node_names:
            return set()
        host_ports: set[int] = set()
        for container in node_names:
            result = self.runner.run(
                ["docker", "inspect", container],
                check=False,
            )
            if result.returncode != 0:
                continue
            try:
                payload = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                continue
            if not payload:
                continue
            ports_map = (
                (payload[0].get("NetworkSettings") or {}).get("Ports") or {}
            )
            for bindings in ports_map.values():
                for binding in bindings or []:
                    host_port = (binding or {}).get("HostPort")
                    if host_port is None:
                        continue
                    try:
                        host_ports.add(int(host_port))
                    except (TypeError, ValueError):
                        continue
        return host_ports

    # ----- internals --------------------------------------------------------

    def _node_container_names(self, name: str, *, include_stopped: bool) -> list[str]:
        """List node container names for the cluster.

        Uses the `io.x-k8s.kind.cluster=<name>` label so multi-node clusters
        (control-plane + workers) are handled uniformly. When
        ``include_stopped`` is True, also returns containers that are not
        currently running (needed for `start_cluster`).
        """
        args = ["docker", "ps"]
        if include_stopped:
            args.append("-a")
        args.extend(
            [
                "--filter",
                f"label={KIND_CLUSTER_LABEL}={name}",
                "--format",
                "{{.Names}}",
            ]
        )
        result = self.runner.run(args, check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
