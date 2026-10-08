"""Coverage for `Kind.container_host_ports`.

Used by `local up`'s port-mapping drift check: if kind-config.yaml
declares extraPortMappings the user has since edited but the cluster was
recreated only via `local down` + `local up`, the running container
keeps the old port bindings. We surface the missing host ports as a
warning row in the summary so the dev runs `local reset`.

Discovery is label-based (`io.x-k8s.kind.cluster=<name>`) and unions
host ports across all node containers, so multi-node clusters that bind
extraPortMappings on a worker are handled the same as a single-node
control-plane cluster.
"""
from __future__ import annotations

import json

import pytest

from chart_manager.integrations.kind import Kind
from tests.conftest import FakeCommandRunner


def _runner(
    responses: dict[tuple[str, ...], tuple[int, str]] | None = None,
) -> FakeCommandRunner:
    """Answer the given argvs with `(returncode, stdout)`.

    Any argv not in the map succeeds with empty stdout, which keeps each
    test focused on the calls it actually cares about.
    """
    runner = FakeCommandRunner()
    for argv, (returncode, stdout) in (responses or {}).items():
        runner.respond(argv, returncode=returncode, stdout=stdout)
    return runner

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


CP = "kind-control-plane"
WORKER = "kind-worker"


@pytest.mark.parametrize(
    ("ps", "inspect", "expected"),
    [
        pytest.param(
            (0, f"{CP}\n"),
            {
                CP: (
                    0,
                    _inspect_payload(
                        {
                            "30080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}],
                            "30443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
                            "6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "53729"}],
                        }
                    ),
                )
            },
            {80, 443, 53729},
            id="single-node",
        ),
        # The control plane binds the apiserver port and a worker binds the
        # ingress extraPortMappings; the union has ports from both nodes.
        pytest.param(
            (0, f"{CP}\n{WORKER}\n"),
            {
                CP: (0, _inspect_payload({"6443/tcp": [{"HostPort": "53729"}]})),
                WORKER: (
                    0,
                    _inspect_payload(
                        {"30080/tcp": [{"HostPort": "80"}], "30443/tcp": [{"HostPort": "443"}]}
                    ),
                ),
            },
            {80, 443, 53729},
            id="multi-node-union",
        ),
        # An absent cluster gives an empty set so the caller can warn, not crash.
        pytest.param((0, ""), {}, set(), id="no-node-containers"),
        pytest.param((1, ""), {}, set(), id="docker-ps-fails"),
        # Kind sometimes lists containerd internal ports with no host bindings.
        pytest.param(
            (0, f"{CP}\n"),
            {CP: (0, _inspect_payload({"30080/tcp": [{"HostPort": "80"}], "10250/tcp": None}))},
            {80},
            id="null-binding-skipped",
        ),
        pytest.param(
            (0, f"{CP}\n"),
            {
                CP: (
                    0,
                    _inspect_payload(
                        {
                            "30080/tcp": [{"HostPort": "80"}],
                            "30443/tcp": [{"HostPort": "not-a-number"}],
                        }
                    ),
                )
            },
            {80},
            id="non-integer-host-port-skipped",
        ),
        pytest.param((0, f"{CP}\n"), {CP: (0, "not json")}, set(), id="malformed-payload-empty"),
        # One node's failed inspect still leaves the other node's ports.
        pytest.param(
            (0, f"{CP}\n{WORKER}\n"),
            {CP: (1, ""), WORKER: (0, _inspect_payload({"30080/tcp": [{"HostPort": "80"}]}))},
            {80},
            id="failed-inspect-skips-the-node",
        ),
    ],
)
def test_container_host_ports_reads_host_ports_from_docker(
    ps: tuple[int, str], inspect: dict[str, tuple[int, str]], expected: set[int]
) -> None:
    runner = _runner(
        {_ps_argv("kind"): ps, **{_inspect_argv(name): reply for name, reply in inspect.items()}}
    )

    assert Kind(runner=runner).container_host_ports("kind") == expected
    # Label-based discovery, then one inspect per listed node.
    assert runner.calls == [_ps_argv("kind"), *(_inspect_argv(name) for name in inspect)]
