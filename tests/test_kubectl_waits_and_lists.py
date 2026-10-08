"""Kubectl readiness waits, best-effort listings, cluster addressing, pods and events.

`list_virtualservices` returns an empty list only when the VirtualService CRD isn't
installed; any other kubectl failure or unreadable JSON raises.
"""
from __future__ import annotations

import base64
import json

import pytest

from chart_manager.integrations import kubectl as kubectl_module
from chart_manager.integrations.kubectl import Kubectl, VirtualService
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import CheckStatus
from tests.conftest import FakeCommandRunner, OnPath, Reply, checks_by_name, scripted

# ----- wait_certificate_ready -----------------------------------------------


def test_wait_nodes_ready_is_a_cni_neutral_cluster_gate() -> None:
    runner = FakeCommandRunner()

    Kubectl(runner=runner).wait_nodes_ready(timeout="3m")

    assert runner.calls == [
        (
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "nodes",
            "--all",
            "--timeout=3m",
        )
    ]


def test_wait_certificate_ready_invokes_kubectl_with_expected_argv() -> None:
    runner = FakeCommandRunner()
    Kubectl(runner=runner).wait_certificate_ready(
        "apps-wildcard", namespace="istio-ingress", timeout="60s"
    )

    assert runner.calls == [
        (
            "kubectl",
            "-n",
            "istio-ingress",
            "wait",
            "--for=condition=Ready",
            "certificate/apps-wildcard",
            "--timeout=60s",
        )
    ]


def test_wait_certificate_ready_surfaces_timeout_as_external_error() -> None:
    runner = FakeCommandRunner(returncode=1, stderr="timed out waiting for the condition")
    with pytest.raises(ExternalCommandError) as excinfo:
        Kubectl(runner=runner).wait_certificate_ready(
            "apps-wildcard", namespace="istio-ingress", timeout="1s"
        )
    assert "timed out" in str(excinfo.value)


# ----- list_virtualservices -------------------------------------------------


def _vs_payload(items: list[dict[str, object]]) -> str:
    return json.dumps({"items": items})


def test_list_virtualservices_empty_when_crd_missing() -> None:
    # No istio yet (lab pre-istio, sandbox-test): kubectl doesn't know the resource type.
    runner = FakeCommandRunner(returncode=1, stderr="error: the server doesn't have a resource type \"virtualservice\"")
    assert Kubectl(runner=runner).list_virtualservices() == []


def test_list_virtualservices_empty_when_no_items() -> None:
    runner = FakeCommandRunner(stdout=_vs_payload([]))
    assert Kubectl(runner=runner).list_virtualservices() == []


def test_list_virtualservices_keeps_namespace_hosts_and_annotations() -> None:
    runner = FakeCommandRunner(
        stdout=_vs_payload(
            [
                {
                    "metadata": {
                        "namespace": "observability",
                        "annotations": {"chartmanager.io/credentials-secret": "grafana"},
                    },
                    "spec": {"hosts": ["grafana.localhost"]},
                },
                {"metadata": {"namespace": "logging"}, "spec": {"hosts": ["loki.localhost"]}},
            ]
        )
    )
    assert Kubectl(runner=runner).list_virtualservices() == [
        VirtualService(
            namespace="observability",
            hosts=("grafana.localhost",),
            annotations={"chartmanager.io/credentials-secret": "grafana"},
        ),
        VirtualService(namespace="logging", hosts=("loki.localhost",), annotations={}),
    ]


def test_list_virtualservices_drops_empty_and_non_string_hosts() -> None:
    runner = FakeCommandRunner(
        stdout=_vs_payload(
            [{"metadata": {"namespace": "ns"}, "spec": {"hosts": ["ok.localhost", "", 7]}}]
        )
    )
    [vs] = Kubectl(runner=runner).list_virtualservices()
    assert vs.hosts == ("ok.localhost",)


@pytest.mark.parametrize(
    "reply", [{"returncode": 1, "stderr": "connection refused"}, {"stdout": "not json"}],
    ids=["cluster-unreachable", "unreadable-json"],
)
def test_list_virtualservices_raises_on_other_failures(reply: dict[str, int | str]) -> None:
    runner = FakeCommandRunner(**reply)
    with pytest.raises(ExternalCommandError):
        Kubectl(runner=runner).list_virtualservices()


# ----- wait_workloads_ready -------------------------------------------------
#
# The readiness gate lists workloads with check=False and then iterates the
# listing's stdout. A failed listing therefore used to yield an empty name
# list, so the gate returned instantly and the caller proceeded as though the
# namespace had converged -- silently disabling itself exactly when the
# cluster was unreachable. These pin both halves of the contract.


def _ok(stdout: str = "") -> Reply:
    return Reply(stdout=stdout)

# Strict on purpose: an extra or missing call is the regression.
def test_wait_workloads_ready_rolls_out_each_listed_workload() -> None:
    runner = scripted(
        [
            _ok("web api"),  # deployments
            _ok(),           # rollout web
            _ok(),           # rollout api
            _ok(""),         # statefulsets: none
            _ok(""),         # daemonsets: none
        ]
    )

    Kubectl(runner=runner).wait_workloads_ready("obs", timeout="90s")

    rollouts = [c for c in runner.calls if "rollout" in c]
    assert rollouts == [
        ("kubectl", "-n", "obs", "rollout", "status", "deployment/web", "--timeout=90s"),
        ("kubectl", "-n", "obs", "rollout", "status", "deployment/api", "--timeout=90s"),
    ]


def test_wait_workloads_ready_raises_when_the_listing_fails() -> None:
    """A listing failure must not be read as "the namespace has no workloads"."""
    runner = scripted([Reply(returncode=1, stderr="Unauthorized")])

    with pytest.raises(ExternalCommandError) as exc:
        Kubectl(runner=runner).wait_workloads_ready("obs")

    assert "cannot list deployment in namespace obs" in str(exc.value)
    assert "Unauthorized" in str(exc.value)
    # It failed on the listing rather than proceeding to any rollout wait.
    assert not [c for c in runner.calls if "rollout" in c]


def test_wait_workloads_ready_accepts_a_genuinely_empty_namespace() -> None:
    runner = scripted([_ok(""), _ok(""), _ok("")])

    Kubectl(runner=runner).wait_workloads_ready("empty")

    assert len(runner.calls) == 3
    assert not [c for c in runner.calls if "rollout" in c]


def test_wait_workloads_ready_scopes_listings_to_selector() -> None:
    runner = scripted([_ok("web"), _ok(), _ok(""), _ok("")])

    Kubectl(runner=runner).wait_workloads_ready(
        "shared",
        selector="app.kubernetes.io/instance=grafana",
    )

    listings = [call for call in runner.calls if "get" in call]
    assert len(listings) == 3
    assert all(
        call[call.index("-l") : call.index("-l") + 2]
        == ("-l", "app.kubernetes.io/instance=grafana")
        for call in listings
    )


# ----- cluster addressing ---------------------------------------------------
# `Kubectl` once took no context at all, so `Settings.kube_context`
# reached two of six adapters and every other kubectl call read
# the ambient kubeconfig. These pin both halves: pinned adds the flag
# everywhere, unpinned is byte-identical to the old behavior.


def _kubectl_argvs(kubectl: Kubectl, runner: FakeCommandRunner) -> list[tuple[str, ...]]:
    """Exercise one call on every argv-building path and return what ran."""
    kubectl.create_namespace("obs")
    kubectl.wait_certificate_ready("apps-wildcard", namespace="istio-ingress")
    kubectl.list_virtualservices()
    kubectl.diagnostics("obs")
    return runner.calls


def test_context_flag_is_appended_to_every_kubectl_invocation() -> None:
    runner = FakeCommandRunner(stdout="{}")
    argvs = _kubectl_argvs(Kubectl(runner=runner, context="kind-a"), runner)

    assert argvs
    for argv in argvs:
        assert argv[-2:] == ("--context", "kind-a"), argv


def test_context_default_leaves_every_argv_untouched() -> None:
    runner = FakeCommandRunner(stdout="{}")
    argvs = _kubectl_argvs(Kubectl(runner=runner), runner)

    assert argvs
    assert not [argv for argv in argvs if "--context" in argv]


def test_get_secret_value_is_addressed_too() -> None:
    # Separate from the loop above because it needs decodable stdout.
    runner = FakeCommandRunner(stdout=base64.b64encode(b"pw").decode())

    assert Kubectl(runner=runner, context="kind-a").get_secret_value(
        "grafana", "admin-password", namespace="observability"
    ) == "pw"
    assert runner.calls[0][-2:] == ("--context", "kind-a")


def test_two_instances_address_two_clusters_from_one_runner() -> None:
    """The point of the whole change: no ambient state between them."""
    runner = FakeCommandRunner(stdout="{}")
    Kubectl(runner=runner, context="kind-a").list_virtualservices()
    Kubectl(runner=runner, context="kind-b").list_virtualservices()

    assert [argv[-1] for argv in runner.calls] == ["kind-a", "kind-b"]


def test_instance_timeout_is_threaded_to_every_invocation() -> None:
    runner = FakeCommandRunner(stdout="{}")
    _kubectl_argvs(Kubectl(runner=runner, timeout=30.0), runner)

    assert {record.timeout for record in runner.records} == {30.0}


def test_timeout_default_is_unbounded() -> None:
    runner = FakeCommandRunner(stdout="{}")
    _kubectl_argvs(Kubectl(runner=runner), runner)

    assert {record.timeout for record in runner.records} == {None}


def test_port_forward_argv_uses_the_instance_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # port_forward is the one justified direct-Popen path (detached child,
    # start_new_session), so it cannot be observed through the runner seam.
    captured: list[list[str]] = []
    monkeypatch.setattr(
        kubectl_module.subprocess,
        "Popen",
        lambda args, **_kwargs: captured.append(args) or object(),
    )

    Kubectl(FakeCommandRunner(), context="kind-a").port_forward(
        namespace="istio-ingress", service="istio-gateway", ports=["8080:80"]
    )
    Kubectl(FakeCommandRunner(), context="kind-a").port_forward(
        namespace="istio-ingress", service="istio-gateway", ports=["8080:80"], context="kind-b"
    )

    assert captured[0][-2:] == ["--context", "kind-a"]
    # A per-call context wins: one caller can address every cluster.
    assert captured[1][-2:] == ["--context", "kind-b"]


# ----- pods and events ------------------------------------------------------
# These four moved here from the HelmRelease client, which owned them only
# because it happened to be the adapter the monitor already held. Nothing in
# them is Flux-shaped, and `namespace_events` is the argv `diagnostics` had
# independently open-coded.


def test_pod_logs_missing_pod_returns_empty_no_raise() -> None:
    runner = FakeCommandRunner(
        returncode=1, stderr='Error from server (NotFound): pods "loki-test" not found'
    )
    assert Kubectl(runner=runner).pod_logs("loki", "loki-test") == ""


def test_pod_logs_other_failure_raises_with_structured_fields() -> None:
    runner = FakeCommandRunner(returncode=7, stderr="connection refused")

    with pytest.raises(ExternalCommandError) as excinfo:
        Kubectl(runner=runner).pod_logs("loki", "loki-test")

    assert excinfo.value.returncode == 7
    assert excinfo.value.stderr == "connection refused"


def test_pod_logs_previous_flag_in_argv() -> None:
    runner = FakeCommandRunner(stdout="log line")
    Kubectl(runner=runner).pod_logs("loki", "loki-test", previous=True)

    assert "--previous" in runner.calls[0]


def test_delete_pod_uses_ignore_not_found_flag() -> None:
    runner = FakeCommandRunner()
    Kubectl(runner=runner).delete_pod("loki", "loki-test")

    assert "--ignore-not-found" in runner.calls[0]


def test_workload_events_field_selector_argv() -> None:
    runner = FakeCommandRunner(stdout="evt1\n")
    Kubectl(runner=runner).workload_events("Deployment", "loki", "loki-app")

    argv = runner.calls[0]
    assert "--field-selector" in argv
    selector_idx = argv.index("--field-selector") + 1
    assert argv[selector_idx] == "involvedObject.name=loki-app,involvedObject.kind=Deployment"
    assert "--sort-by=.lastTimestamp" in argv


def test_namespace_events_returns_stdout_and_stderr_without_raising() -> None:
    runner = FakeCommandRunner(returncode=1, stdout="evt\n", stderr="warn\n")

    assert Kubectl(runner=runner).namespace_events("loki") == "evt\nwarn\n"
    assert runner.calls[0] == (
        "kubectl", "get", "events", "-n", "loki", "--sort-by=.lastTimestamp",
    )


def test_diagnostics_events_section_reuses_namespace_events_argv() -> None:
    """The duplicate argv is gone: `diagnostics` delegates the events half."""
    runner = FakeCommandRunner(stdout="body")
    Kubectl(runner=runner).diagnostics("loki")

    assert runner.calls == [
        ("kubectl", "get", "pods", "-n", "loki", "-o", "wide"),
        ("kubectl", "get", "events", "-n", "loki", "--sort-by=.lastTimestamp"),
    ]


def test_per_call_timeout_overrides_the_instance_cap() -> None:
    # The HelmRelease watchers own a per-poll budget that is tighter than
    # the deployment-wide cap and changes between requests; without the
    # override they would need a fresh adapter per poll.
    runner = FakeCommandRunner()
    kubectl = Kubectl(runner=runner, timeout=30.0)
    kubectl.delete_pod("loki", "loki-test", timeout=2.0)
    kubectl.delete_pod("loki", "loki-other")

    assert [record.timeout for record in runner.records] == [2.0, 30.0]


def test_kubectl_reports_the_ambient_context(on_path: OnPath) -> None:
    """No pin: the check answers with whatever the kubeconfig points at."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "current-context"), stdout="kind-lab\n")

    checks = checks_by_name(Kubectl(runner).preflight())

    assert checks["kubectl"].detail.startswith("v1.31.0")
    assert checks["kube-context"].status is CheckStatus.OK
    assert "kind-lab" in checks["kube-context"].detail


def test_no_current_kubecontext_is_an_environment_failure(on_path: OnPath) -> None:
    """Exit 5, per the table: nothing is missing, the environment is unset."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "current-context"), returncode=1)

    context = checks_by_name(Kubectl(runner).preflight())["kube-context"]

    assert context.status is CheckStatus.FAILED
    assert context.outcome is Outcome.ENVIRONMENT


def test_a_pinned_context_missing_from_the_kubeconfig_fails(on_path: OnPath) -> None:
    """`CHART_MANAGER_KUBE_CONTEXT` naming a context nobody has is a real bug."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "get-contexts"), stdout="kind-lab\nprod\n")

    context = checks_by_name(Kubectl(runner, context="kind-gone").preflight())["kube-context"]

    assert context.status is CheckStatus.FAILED
    assert context.outcome is Outcome.ENVIRONMENT
    assert "kind-lab" in (context.remediation or ""), "say which contexts do exist"


def test_the_context_check_is_skipped_when_kubectl_is_absent(on_path: OnPath) -> None:
    """One broken install, one line of blame -- not two."""
    on_path()

    checks = checks_by_name(Kubectl(FakeCommandRunner()).preflight())

    assert checks["kubectl"].outcome is Outcome.MISSING_BINARY
    assert checks["kube-context"].status is CheckStatus.SKIPPED
    assert checks["kube-context"].outcome is Outcome.SUCCESS, "a skip is not a failure"
