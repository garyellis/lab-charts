"""Kind lifecycle tests: stop/start/ensure on stopped clusters.

We mock CommandRunner so these tests assert the exact docker/kind argv
shape -- the contract with the kind/docker CLIs is what makes stop/start
correct for multi-node clusters (label-based discovery) and idempotent
across the absent/stopped/running tri-state.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.integrations.kind import KIND_CLUSTER_LABEL, Kind, kind_context
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import PROBE_TIMEOUT, CheckStatus
from chart_manager.plumbing.yaml_files import parse_yaml
from tests.conftest import FakeCommandRunner, OnPath, Predicate, checks_by_name


def _kind(runner: FakeCommandRunner) -> Kind:
    return Kind(runner, docker_host=None, timeout=None)


def _is_docker_ps(running_only: bool) -> Predicate:
    def predicate(argv: tuple[str, ...]) -> bool:
        if argv[:2] != ("docker", "ps"):
            return False
        has_a_flag = "-a" in argv
        # running_only=True  -> match invocations WITHOUT -a (active set)
        # running_only=False -> match invocations WITH    -a (include stopped)
        if running_only:
            return not has_a_flag
        return has_a_flag
    return predicate


def _is_kind_get_clusters(argv: tuple[str, ...]) -> bool:
    return argv[:3] == ("kind", "get", "clusters")


# ----- stop_cluster ---------------------------------------------------------


def test_stop_cluster_stops_all_node_containers() -> None:
    runner = FakeCommandRunner()
    # docker ps (running only) returns multi-node cluster's containers.
    runner.respond(
        _is_docker_ps(running_only=True),
        stdout="chart-manager-control-plane\nchart-manager-worker\nchart-manager-worker2\n",
    )
    kind = _kind(runner)

    assert kind.stop_cluster("chart-manager") is True

    docker_ps = [c for c in runner.calls if c[:2] == ("docker", "ps")]
    assert len(docker_ps) == 1
    assert "-a" not in docker_ps[0]
    assert f"label={KIND_CLUSTER_LABEL}=chart-manager" in docker_ps[0]
    assert "--format" in docker_ps[0]

    stop_calls = [c for c in runner.calls if c[:2] == ("docker", "stop")]
    assert len(stop_calls) == 1
    # All three containers passed in a single `docker stop` invocation.
    assert stop_calls[0] == (
        "docker",
        "stop",
        "chart-manager-control-plane",
        "chart-manager-worker",
        "chart-manager-worker2",
    )


def test_stop_cluster_returns_false_when_no_containers() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_docker_ps(running_only=True), stdout="")
    kind = _kind(runner)

    assert kind.stop_cluster("chart-manager") is False
    assert not any(c[:2] == ("docker", "stop") for c in runner.calls)


def test_stop_cluster_handles_docker_ps_failure_as_absent() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_docker_ps(running_only=True), returncode=1)
    kind = _kind(runner)

    assert kind.stop_cluster("chart-manager") is False




# ----- ensure_cluster on stopped cluster ------------------------------------


@pytest.mark.parametrize(
    ("running", "every", "started"),
    [
        pytest.param(
            "",
            "chart-manager-control-plane\n",
            "chart-manager-control-plane",
            id="all-stopped",
        ),
        # Gating on any-running would no-op here and leave the worker stopped.
        pytest.param(
            "chart-manager-control-plane\n",
            "chart-manager-control-plane\nchart-manager-worker\n",
            "chart-manager-worker",
            id="partial-state",
        ),
    ],
)
def test_ensure_cluster_starts_only_the_stopped_nodes(
    running: str, every: str, started: str
) -> None:
    """`kind get clusters` lists the cluster even when its containers are
    stopped -- ensure_cluster must start the stopped subset (the `-a` listing
    minus the running one) rather than no-op'ing or trying to re-create.
    """
    runner = FakeCommandRunner()
    runner.respond(_is_kind_get_clusters, stdout="chart-manager\n")
    runner.respond(_is_docker_ps(running_only=True), stdout=running)
    runner.respond(_is_docker_ps(running_only=False), stdout=every)
    kind = _kind(runner)

    kind.ensure_cluster("chart-manager")

    assert not any(c[:3] == ("kind", "create", "cluster") for c in runner.calls)
    start_calls = [c for c in runner.calls if c[:2] == ("docker", "start")]
    assert start_calls == [("docker", "start", started)]


def test_ensure_cluster_noop_when_already_running() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_kind_get_clusters, stdout="chart-manager\n")
    runner.respond(
        _is_docker_ps(running_only=True),
        stdout="chart-manager-control-plane\n",
    )
    # When all nodes are running, the -a listing matches the running
    # listing exactly -- there is nothing to start.
    runner.respond(
        _is_docker_ps(running_only=False),
        stdout="chart-manager-control-plane\n",
    )
    kind = _kind(runner)

    kind.ensure_cluster("chart-manager")

    assert not any(c[:3] == ("kind", "create", "cluster") for c in runner.calls)
    assert not any(c[:2] == ("docker", "start") for c in runner.calls)


def test_ensure_cluster_creates_when_absent() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_kind_get_clusters, stdout="")  # no clusters
    kind = _kind(runner)

    kind.ensure_cluster("chart-manager")

    create_calls = [c for c in runner.calls if c[:3] == ("kind", "create", "cluster")]
    assert len(create_calls) == 1
    assert "--name" in create_calls[0]
    assert "chart-manager" in create_calls[0]
    assert "--image" not in create_calls[0]


def test_ensure_cluster_passes_kind_config_without_overriding_its_image() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_kind_get_clusters, stdout="")
    kind = _kind(runner)

    kind.ensure_cluster("chart-manager", config=Path("kind-config.yaml"))

    create_call = next(c for c in runner.calls if c[:3] == ("kind", "create", "cluster"))
    assert create_call == (
        "kind",
        "create",
        "cluster",
        "--name",
        "chart-manager",
        "--config",
        "kind-config.yaml",
    )


def test_repository_kind_configs_own_the_digest_pinned_node_image() -> None:
    root = Path(__file__).resolve().parents[1]
    configs = (root / "kind-config.yaml",)

    for config in configs:
        document = parse_yaml(config.read_text(encoding="utf-8"))
        nodes = document["nodes"]
        assert nodes
        images = {node["image"] for node in nodes}
        assert all("@sha256:" in image for image in images)


# ----- daemon addressing ----------------------------------------------------
# kind names its cluster with `--name` on every subcommand, so cluster
# identity was never ambient here. The docker daemon was: `Kind` inherited
# whatever DOCKER_HOST the process had. These pin the scoped-env contract.


def _exercise(kind: Kind) -> None:
    """Touch every kind/docker argv-building path."""
    kind.clusters()
    kind.stop_cluster("a")
    kind.delete_cluster("a")
    kind.container_host_ports("a")


def test_every_invocation_is_scoped_to_the_docker_host_and_timeout() -> None:
    runner = FakeCommandRunner(stdout="a\n")
    _exercise(Kind(runner, docker_host="tcp://remote:2375", timeout=15.0))

    assert runner.records
    for r in runner.records:
        assert (r.env, r.timeout) == ({"DOCKER_HOST": "tcp://remote:2375"}, 15.0), r.args


def test_unset_docker_host_and_timeout_inherit_the_env_unbounded() -> None:
    # No env at all: the child inherits the process environment untouched.
    runner = FakeCommandRunner(stdout="a\n")
    _exercise(_kind(runner))

    assert {(r.env, r.timeout) for r in runner.records} == {(None, None)}


def test_kind_context_is_the_one_home_for_the_naming_convention() -> None:
    # Two modules derived this with their own f-string before it lived here.
    assert kind_context("chart-manager") == "kind-chart-manager"


def test_a_stopped_docker_daemon_is_reported_not_a_missing_binary(on_path: OnPath) -> None:
    """The common case a binary-only check calls healthy."""
    on_path("kind", "docker")
    runner = FakeCommandRunner()
    runner.respond(("kind", "version"), stdout="kind v0.24.0\n")
    runner.respond(("docker", "--version"), stdout="Docker version 27.3.1\n")
    runner.respond(
        ("docker", "version", "--format"),
        returncode=1,
        stderr="Cannot connect to the Docker daemon\n",
    )

    checks = checks_by_name(_kind(runner).preflight())

    assert checks["kind"].status is CheckStatus.OK
    assert checks["docker"].status is CheckStatus.OK
    assert checks["docker-daemon"].outcome is Outcome.ENVIRONMENT


def test_the_daemon_probe_is_scoped_to_the_configured_docker_host(on_path: OnPath) -> None:
    """A preflight against the ambient daemon says nothing about the pinned one."""
    on_path("kind", "docker")
    runner = FakeCommandRunner(stdout="27.3.1\n")

    Kind(runner, docker_host="tcp://remote:2375", timeout=None).preflight()

    daemon_call = next(r for r in runner.records if r.args[:2] == ("docker", "version"))
    assert daemon_call.env == {"DOCKER_HOST": "tcp://remote:2375"}
    assert daemon_call.timeout == PROBE_TIMEOUT
