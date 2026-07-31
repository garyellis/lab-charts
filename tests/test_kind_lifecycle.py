"""Kind lifecycle tests: stop/start/ensure on stopped clusters.

We mock CommandRunner so these tests assert the exact docker/kind argv
shape -- the contract with the kind/docker CLIs is what makes stop/start
correct for multi-node clusters (label-based discovery) and idempotent
across the absent/stopped/running tri-state.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from chart_manager.integrations.kind import KIND_CLUSTER_LABEL, Kind, kind_context
from tests.conftest import FakeCommandRunner, Predicate


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
    kind = Kind(runner=runner)

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
    kind = Kind(runner=runner)

    assert kind.stop_cluster("chart-manager") is False
    assert not any(c[:2] == ("docker", "stop") for c in runner.calls)


def test_stop_cluster_handles_docker_ps_failure_as_absent() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_docker_ps(running_only=True), returncode=1)
    kind = Kind(runner=runner)

    assert kind.stop_cluster("chart-manager") is False


# ----- start_cluster --------------------------------------------------------


def test_start_cluster_starts_stopped_containers() -> None:
    runner = FakeCommandRunner()
    # docker ps -a returns stopped containers too.
    runner.respond(
        _is_docker_ps(running_only=False),
        stdout="chart-manager-control-plane\nchart-manager-worker\n",
    )
    kind = Kind(runner=runner)

    assert kind.start_cluster("chart-manager") is True

    ps_calls = [c for c in runner.calls if c[:2] == ("docker", "ps")]
    assert len(ps_calls) == 1
    assert "-a" in ps_calls[0]

    start_calls = [c for c in runner.calls if c[:2] == ("docker", "start")]
    assert start_calls == [
        ("docker", "start", "chart-manager-control-plane", "chart-manager-worker"),
    ]


def test_start_cluster_returns_false_when_no_containers() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_docker_ps(running_only=False), stdout="")
    kind = Kind(runner=runner)

    assert kind.start_cluster("chart-manager") is False
    assert not any(c[:2] == ("docker", "start") for c in runner.calls)


# ----- ensure_cluster on stopped cluster ------------------------------------


def test_ensure_cluster_starts_stopped_cluster() -> None:
    """`kind get clusters` lists the cluster even when its containers are
    stopped -- ensure_cluster must detect that and start them rather than
    no-op'ing or trying to re-create.
    """
    runner = FakeCommandRunner()
    runner.respond(_is_kind_get_clusters, stdout="chart-manager\n")
    # Running query returns empty -> stopped.
    runner.respond(_is_docker_ps(running_only=True), stdout="")
    # docker ps -a returns the stopped containers.
    runner.respond(
        _is_docker_ps(running_only=False),
        stdout="chart-manager-control-plane\n",
    )
    kind = Kind(runner=runner)

    kind.ensure_cluster("chart-manager")

    # Must NOT have called `kind create cluster`.
    assert not any(c[:3] == ("kind", "create", "cluster") for c in runner.calls)
    # Must have issued `docker start` on the discovered containers.
    start_calls = [c for c in runner.calls if c[:2] == ("docker", "start")]
    assert start_calls == [("docker", "start", "chart-manager-control-plane")]


def test_ensure_cluster_starts_only_stopped_nodes_in_partial_state() -> None:
    """Multi-node partial state: one node running, one stopped.

    The bug this guards against: pre-fix ensure_cluster gated on
    `is_running` (any-running), which returned True here and silently
    no-op'd, leaving the stopped worker stopped. The fix diffs the
    "with -a" and "without -a" listings and issues `docker start` only
    on the stopped subset.
    """
    runner = FakeCommandRunner()
    runner.respond(_is_kind_get_clusters, stdout="chart-manager\n")
    # Running set: control-plane only.
    runner.respond(
        _is_docker_ps(running_only=True),
        stdout="chart-manager-control-plane\n",
    )
    # Full set (running + stopped): control-plane + a stopped worker.
    runner.respond(
        _is_docker_ps(running_only=False),
        stdout="chart-manager-control-plane\nchart-manager-worker\n",
    )
    kind = Kind(runner=runner)

    kind.ensure_cluster("chart-manager")

    assert not any(c[:3] == ("kind", "create", "cluster") for c in runner.calls)
    start_calls = [c for c in runner.calls if c[:2] == ("docker", "start")]
    # Only the stopped worker must be started; control-plane is already up
    # and starting it again would be a no-op-with-warning.
    assert start_calls == [("docker", "start", "chart-manager-worker")]


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
    kind = Kind(runner=runner)

    kind.ensure_cluster("chart-manager")

    assert not any(c[:3] == ("kind", "create", "cluster") for c in runner.calls)
    assert not any(c[:2] == ("docker", "start") for c in runner.calls)


def test_ensure_cluster_creates_when_absent() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_kind_get_clusters, stdout="")  # no clusters
    kind = Kind(runner=runner)

    kind.ensure_cluster("chart-manager")

    create_calls = [c for c in runner.calls if c[:3] == ("kind", "create", "cluster")]
    assert len(create_calls) == 1
    assert "--name" in create_calls[0]
    assert "chart-manager" in create_calls[0]
    assert "--image" not in create_calls[0]


def test_ensure_cluster_passes_kind_config_without_overriding_its_image() -> None:
    runner = FakeCommandRunner()
    runner.respond(_is_kind_get_clusters, stdout="")
    kind = Kind(runner=runner)

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
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
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
    kind.start_cluster("a")
    kind.delete_cluster("a")
    kind.container_host_ports("a")


def test_docker_host_is_scoped_onto_every_invocation() -> None:
    runner = FakeCommandRunner(stdout="a\n")
    _exercise(Kind(runner=runner, docker_host="tcp://remote:2375"))

    assert runner.records
    for record in runner.records:
        assert record.env == {"DOCKER_HOST": "tcp://remote:2375"}, record.args


def test_docker_host_default_passes_no_env_at_all() -> None:
    # None must be indistinguishable from the pre-Wave-4 behavior: the child
    # inherits the process environment untouched.
    runner = FakeCommandRunner(stdout="a\n")
    _exercise(Kind(runner=runner))

    assert runner.records
    assert {record.env for record in runner.records} == {None}


def test_two_instances_drive_two_daemons_from_one_runner() -> None:
    runner = FakeCommandRunner(stdout="a\n")
    Kind(runner=runner, docker_host="unix:///a.sock").clusters()
    Kind(runner=runner, docker_host="unix:///b.sock").clusters()

    assert [record.env for record in runner.records] == [
        {"DOCKER_HOST": "unix:///a.sock"},
        {"DOCKER_HOST": "unix:///b.sock"},
    ]


def test_instance_timeout_is_threaded_to_every_invocation() -> None:
    runner = FakeCommandRunner(stdout="a\n")
    _exercise(Kind(runner=runner, timeout=15.0))

    assert {record.timeout for record in runner.records} == {15.0}


def test_timeout_default_is_unbounded() -> None:
    runner = FakeCommandRunner(stdout="a\n")
    _exercise(Kind(runner=runner))

    assert {record.timeout for record in runner.records} == {None}


def test_kind_context_is_the_one_home_for_the_naming_convention() -> None:
    # Two services derived this with their own f-string before it lived here.
    assert kind_context("chart-manager") == "kind-chart-manager"
