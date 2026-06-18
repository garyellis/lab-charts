"""Coverage for `Kind.container_host_ports`.

Used by LabService.up's port-mapping drift check: if kind-config.yaml
declares extraPortMappings the user has since edited but the cluster was
recreated only via `sandbox down` + `sandbox up`, the running container
keeps the old port bindings. We surface the missing host ports as a
warning row in the summary so the dev runs `sandbox delete && sandbox up`.

Discovery is label-based (`io.x-k8s.kind.cluster=<name>`) and unions
host ports across all node containers, so multi-node clusters that bind
extraPortMappings on a worker are handled the same as a single-node
control-plane cluster.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from chart_manager.integrations.kind import Kind
from chart_manager.plumbing.commands import CommandResult, CommandRunner


class _Runner(CommandRunner):
    """Scriptable fake runner.

    `responses` maps the argv tuple to a `(returncode, stdout)` pair. Any
    argv not in the map yields a successful empty response, which keeps
    each test focused on the calls it actually cares about.
    """

    def __init__(
        self,
        responses: dict[tuple[str, ...], tuple[int, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses = responses or {}

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        argv = tuple(args)
        self.calls.append(argv)
        returncode, stdout = self._responses.get(argv, (0, ""))
        return CommandResult(
            args=argv,
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )


def _inspect_payload(ports: dict[str, list[dict[str, str]] | None]) -> str:
    return json.dumps([{"NetworkSettings": {"Ports": ports}}])


def _ps_argv(cluster: str) -> tuple[str, ...]:
    return (
        "docker",
        "ps",
        "-a",
        "--filter",
        f"label=io.x-k8s.kind.cluster={cluster}",
        "--format",
        "{{.Names}}",
    )


def _inspect_argv(container: str) -> tuple[str, ...]:
    return ("docker", "inspect", container)


def test_container_host_ports_matches_expected() -> None:
    cluster = "chart-manager"
    control_plane = f"{cluster}-control-plane"
    runner = _Runner(
        {
            _ps_argv(cluster): (0, control_plane + "\n"),
            _inspect_argv(control_plane): (
                0,
                _inspect_payload(
                    {
                        "30080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}],
                        "30443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
                        "6443/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": "53729"}
                        ],
                    }
                ),
            ),
        }
    )
    ports = Kind(runner=runner).container_host_ports(cluster)
    assert ports == {80, 443, 53729}
    # Argv contract: label-based discovery, then inspect each node.
    assert runner.calls == [_ps_argv(cluster), _inspect_argv(control_plane)]


def test_container_host_ports_unions_across_multi_node_cluster() -> None:
    # Control plane binds the apiserver port; a worker binds the ingress
    # extraPortMappings. The union must surface ports from BOTH nodes.
    cluster = "multinode"
    cp = f"{cluster}-control-plane"
    worker = f"{cluster}-worker"
    runner = _Runner(
        {
            _ps_argv(cluster): (0, f"{cp}\n{worker}\n"),
            _inspect_argv(cp): (
                0,
                _inspect_payload(
                    {"6443/tcp": [{"HostPort": "53729"}]}
                ),
            ),
            _inspect_argv(worker): (
                0,
                _inspect_payload(
                    {
                        "30080/tcp": [{"HostPort": "80"}],
                        "30443/tcp": [{"HostPort": "443"}],
                    }
                ),
            ),
        }
    )
    ports = Kind(runner=runner).container_host_ports(cluster)
    assert ports == {80, 443, 53729}
    assert runner.calls == [
        _ps_argv(cluster),
        _inspect_argv(cp),
        _inspect_argv(worker),
    ]


def test_container_host_ports_empty_when_no_node_containers() -> None:
    # Cluster absent (label query returns no containers) -> empty set so
    # the caller can warn rather than crash.
    runner = _Runner({_ps_argv("missing"): (0, "")})
    assert Kind(runner=runner).container_host_ports("missing") == set()


def test_container_host_ports_empty_when_docker_ps_fails() -> None:
    # docker daemon glitch on the discovery call -> empty set.
    runner = _Runner({_ps_argv("missing"): (1, "")})
    assert Kind(runner=runner).container_host_ports("missing") == set()


def test_container_host_ports_handles_null_bindings() -> None:
    # Ports key exists but a port is unmapped (kind sometimes lists
    # containerd internal ports with no host bindings).
    cluster = "chart-manager"
    cp = f"{cluster}-control-plane"
    runner = _Runner(
        {
            _ps_argv(cluster): (0, cp + "\n"),
            _inspect_argv(cp): (
                0,
                _inspect_payload(
                    {
                        "30080/tcp": [{"HostPort": "80"}],
                        "10250/tcp": None,
                    }
                ),
            ),
        }
    )
    assert Kind(runner=runner).container_host_ports(cluster) == {80}


def test_container_host_ports_skips_non_integer_host_port() -> None:
    # Malformed HostPort -> skip rather than crash. The warning path
    # tolerates partial data.
    cluster = "chart-manager"
    cp = f"{cluster}-control-plane"
    runner = _Runner(
        {
            _ps_argv(cluster): (0, cp + "\n"),
            _inspect_argv(cp): (
                0,
                _inspect_payload(
                    {
                        "30080/tcp": [{"HostPort": "80"}],
                        "30443/tcp": [{"HostPort": "not-a-number"}],
                    }
                ),
            ),
        }
    )
    assert Kind(runner=runner).container_host_ports(cluster) == {80}


def test_container_host_ports_empty_when_payload_malformed() -> None:
    cluster = "x"
    cp = f"{cluster}-control-plane"
    runner = _Runner(
        {
            _ps_argv(cluster): (0, cp + "\n"),
            _inspect_argv(cp): (0, "not json"),
        }
    )
    assert Kind(runner=runner).container_host_ports(cluster) == set()


def test_container_host_ports_skips_node_when_inspect_fails() -> None:
    # If one node's inspect fails, surface what the other node knows
    # rather than collapsing to empty -- the drift check works on a
    # best-effort union.
    cluster = "partial"
    cp = f"{cluster}-control-plane"
    worker = f"{cluster}-worker"
    runner = _Runner(
        {
            _ps_argv(cluster): (0, f"{cp}\n{worker}\n"),
            _inspect_argv(cp): (1, ""),
            _inspect_argv(worker): (
                0,
                _inspect_payload({"30080/tcp": [{"HostPort": "80"}]}),
            ),
        }
    )
    assert Kind(runner=runner).container_host_ports(cluster) == {80}
