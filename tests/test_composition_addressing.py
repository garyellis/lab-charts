"""Cluster addressing survives the trip from Settings to an actual argv.

`Settings.kube_context` existed before Wave 4 and was threaded into two of
six adapters, so setting it made half the tool honor it and the other half
silently use the ambient kubeconfig -- the failure mode is a converge that
writes to the wrong cluster with no diagnostic. These tests walk the whole
path (Settings -> Container -> adapter -> argv/env) rather than asserting
that a constructor stored a field, because storing it was never the bug.
"""

from __future__ import annotations

from chart_manager.composition import Container, Settings
from tests.conftest import FakeCommandRunner


class _Container(Container):
    """A container whose adapters shell into a fake instead of subprocess."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.runner = FakeCommandRunner(stdout="{}")

    def command_runner(self) -> FakeCommandRunner:
        return self.runner


def _configured() -> _Container:
    return _Container(
        Settings(
            kube_context="kind-b",
            docker_host="tcp://remote:2375",
            command_timeout=30.0,
        )
    )


def test_kube_context_reaches_kubectl_argv() -> None:
    container = _configured()

    container.kubectl().list_gateway_hosts()

    assert container.runner.calls[0][-2:] == ("--context", "kind-b")


def test_kube_context_reaches_helm_and_helmrelease_argv() -> None:
    container = _configured()

    container.helm().status("loki", namespace="loki")
    container.helmrelease_client().list()

    assert container.runner.calls[0][-2:] == ("--kube-context", "kind-b")
    assert container.runner.calls[1][-2:] == ("--context", "kind-b")


def test_docker_host_reaches_the_kind_adapter() -> None:
    container = _configured()

    container.kind().clusters()

    assert container.runner.records[0].env == {"DOCKER_HOST": "tcp://remote:2375"}


def test_command_timeout_reaches_kubectl_and_kind() -> None:
    container = _configured()

    container.kubectl().list_gateway_hosts()
    container.kind().clusters()

    assert {record.timeout for record in container.runner.records} == {30.0}


def test_defaults_add_no_flags_and_no_env() -> None:
    """`Container()` must stay byte-identical to the pre-Wave-4 CLI."""
    container = _Container(Settings())

    container.kubectl().list_gateway_hosts()
    container.kind().clusters()

    assert not [argv for argv in container.runner.calls if "--context" in argv]
    assert {record.env for record in container.runner.records} == {None}
    assert {record.timeout for record in container.runner.records} == {None}


def test_two_containers_address_two_clusters_in_one_process() -> None:
    """The question Wave 4 exists to answer, asserted end to end."""
    a = _Container(Settings(kube_context="kind-a"))
    b = _Container(Settings(kube_context="kind-b"))

    a.kubectl().list_gateway_hosts()
    b.kubectl().list_gateway_hosts()

    assert a.runner.calls[0][-1] == "kind-a"
    assert b.runner.calls[0][-1] == "kind-b"


# ----- the two hardcoded f"kind-{cluster}" workarounds ----------------------
