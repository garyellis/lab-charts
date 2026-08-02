"""Cluster addressing survives the trip from Settings to an actual argv.

`Settings.kube_context` existed before Wave 4 and was threaded into two of
six adapters, so setting it made half the tool honor it and the other half
silently use the ambient kubeconfig -- the failure mode is a converge that
writes to the wrong cluster with no diagnostic. These tests walk the whole
path (Settings -> Container -> adapter -> argv/env) rather than asserting
that a constructor stored a field, because storing it was never the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.composition import Container, Settings
from chart_manager.integrations.kind import kind_context
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.clusters.environment import EnvironmentHandle
from chart_manager.services.expose import ExposeRequest
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


def test_lab_service_gets_every_adapter_configured() -> None:
    """The service can no longer fall back to an unconfigured adapter."""
    container = _configured()

    service = container.development_cluster_service(Path("."))

    assert service.kubectl.context == "kind-b"
    assert service.helm._context == "kind-b"
    assert service.expose.kubectl.context == "kind-b"


def test_sandbox_service_gets_configured_adapters() -> None:
    container = _configured()

    assert container.ephemeral_test_cluster_service(Path(".")).kubectl.context == "kind-b"


def test_one_client_factory_serves_both_cluster_services() -> None:
    """Both services rebind through the same factory, and it binds all three.

    There used to be two closures here of two different arities -- three
    clients for the development service, two for the ephemeral one -- for one
    job. Arity is exactly what an unpacking caller has to agree with its
    factory about, and getting it wrong is how bootstrap ended up converging
    against the ambient kubecontext.
    """
    container = _configured()
    handle = EnvironmentHandle(
        identity="lab", context="kind-lab", provider_type="kind"
    )

    bound = container.cluster_clients(handle)

    assert bound.helm._context == "kind-lab"
    assert bound.kubectl.context == "kind-lab"
    assert bound.expose.kubectl.context == "kind-lab"
    # `==` rather than `is`: a bound method is a fresh object per attribute
    # access, and equal ones are the same function on the same container.
    assert (
        container.development_cluster_service(Path("."))._client_factory
        == container.ephemeral_test_cluster_service(Path("."))._client_factory
        == container.cluster_clients
    )


def test_two_containers_address_two_clusters_in_one_process() -> None:
    """The question Wave 4 exists to answer, asserted end to end."""
    a = _Container(Settings(kube_context="kind-a"))
    b = _Container(Settings(kube_context="kind-b"))

    a.kubectl().list_gateway_hosts()
    b.kubectl().list_gateway_hosts()

    assert a.runner.calls[0][-1] == "kind-a"
    assert b.runner.calls[0][-1] == "kind-b"


# ----- the two hardcoded f"kind-{cluster}" workarounds ----------------------


def test_expose_falls_back_to_the_kind_convention_when_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No configured context: the request names a kind cluster, kind names it."""
    container = _Container(Settings())
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "chart_manager.integrations.kubectl.subprocess.Popen",
        lambda args, **_kwargs: captured.append(args) or _DeadPopen(),
    )

    service = container.expose_service(state_dir=tmp_path)
    # The stub child reports having exited, so start() always fails; the
    # argv it launched is what this test is about.
    with pytest.raises(ChartManagerError):
        service.start(
            ExposeRequest(cluster_name="demo", service="ns/svc", ports=("80:80",)),
            readiness_timeout=0.0,
        )

    assert captured[0][-2:] == ["--context", kind_context("demo")]
    assert kind_context("demo") == "kind-demo"


def test_expose_prefers_a_configured_context_over_the_convention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _configured()
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "chart_manager.integrations.kubectl.subprocess.Popen",
        lambda args, **_kwargs: captured.append(args) or _DeadPopen(),
    )

    service = container.expose_service(state_dir=tmp_path)
    with pytest.raises(ChartManagerError):
        service.start(
            ExposeRequest(cluster_name="demo", service="ns/svc", ports=("80:80",)),
            readiness_timeout=0.0,
        )

    assert captured[0][-2:] == ["--context", "kind-b"]


class _DeadPopen:
    """A Popen stand-in that reports having exited immediately."""

    pid = 1234
    returncode = 1

    def poll(self) -> int:
        return 1

    def wait(self, timeout: float | None = None) -> int:
        return 1
