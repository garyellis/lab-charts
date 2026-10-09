"""Kubectl cluster addressing, readiness waits, listings, namespaces, pods and events.

`list_virtualservices` returns an empty list only when the VirtualService CRD isn't
installed; any other kubectl failure or unreadable JSON raises.
"""
from __future__ import annotations

import json

import pytest

from chart_manager.integrations.kubectl import HelmReleaseRef, Kubectl, VirtualService
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import CheckStatus
from tests.conftest import FakeCommandRunner, OnPath, Reply, checks_by_name, scripted


def _kubectl(runner: FakeCommandRunner) -> Kubectl:
    return Kubectl(runner, context=None, timeout=None)


# ----- cluster addressing ---------------------------------------------------


def test_every_call_is_pinned_to_the_context_and_timeout() -> None:
    runner = FakeCommandRunner(stdout="{}")
    runner.respond(("kubectl", "get", "--raw=/readyz"), stdout="ok")
    runner.respond(("kubectl", "-n", "obs", "get", "secret"), stdout="cHc=")
    kubectl = Kubectl(runner, context="kind-a", timeout=30.0)
    ref = HelmReleaseRef("loki", "loki", "helm.toolkit.fluxcd.io/v2", "loki", "loki", "loki")

    kubectl.get_secret_value("grafana", "admin-password", namespace="obs")
    kubectl.create_namespace("obs")
    kubectl.wait_apiserver_ready(timeout=60.0)
    kubectl.wait_nodes_ready(timeout=60.0)
    kubectl.wait_certificate_ready("apps-wildcard", namespace="obs", timeout=60.0)
    kubectl.list_virtualservices()
    kubectl.workload_names("deployment", namespace="obs")
    kubectl.rollout_status("deployment", "web", namespace="obs", timeout=60.0)
    kubectl.wait_established("certificates.cert-manager.io", timeout=60.0)
    kubectl.list_helmreleases()
    kubectl.get_helmrelease_status(ref)
    kubectl.list_owned_workloads(ref)
    kubectl.list_test_pods(ref)
    kubectl.pod_logs("obs", "web-0")
    kubectl.namespace_events("obs")
    kubectl.workload_events("Deployment", "obs", "web")
    kubectl.diagnostics("obs")
    kubectl.delete_pod("obs", "web-0", timeout=2.0)

    assert {r.args[-2:] for r in runner.records} == {("--context", "kind-a")}
    # Waits and per-call budgets override the instance cap.
    assert {r.timeout for r in runner.records} == {30.0, 90.0, 2.0}


def test_get_secret_value_decodes_the_key() -> None:
    runner = FakeCommandRunner(stdout="cHc=")

    assert _kubectl(runner).get_secret_value("grafana", "admin-password", namespace="obs") == "pw"


# ----- waits ----------------------------------------------------------------


def test_wait_certificate_ready_surfaces_timeout_as_external_error() -> None:
    runner = FakeCommandRunner(returncode=1, stderr="timed out waiting for the condition")
    with pytest.raises(ExternalCommandError) as excinfo:
        _kubectl(runner).wait_certificate_ready(
            "apps-wildcard", namespace="istio-ingress", timeout=1.0
        )
    assert "timed out" in str(excinfo.value)


def test_a_wait_runs_for_its_length_plus_slack_not_the_instance_cap() -> None:
    runner = FakeCommandRunner()

    Kubectl(runner, context=None, timeout=30.0).wait_established("x.example.io", timeout=300.0)

    [record] = runner.records
    assert (record.args[-1], record.timeout) == ("--timeout=300s", 330.0)


# ----- list_virtualservices -------------------------------------------------


def _vs_payload(items: list[dict[str, object]]) -> str:
    return json.dumps({"items": items})


def test_list_virtualservices_empty_when_crd_missing() -> None:
    # No istio yet (lab pre-istio, sandbox-test): kubectl doesn't know the resource type.
    runner = FakeCommandRunner(returncode=1, stderr="error: the server doesn't have a resource type \"virtualservice\"")
    assert _kubectl(runner).list_virtualservices() == []


def test_list_virtualservices_empty_when_no_items() -> None:
    runner = FakeCommandRunner(stdout=_vs_payload([]))
    assert _kubectl(runner).list_virtualservices() == []


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
    assert _kubectl(runner).list_virtualservices() == [
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
    [vs] = _kubectl(runner).list_virtualservices()
    assert vs.hosts == ("ok.localhost",)


@pytest.mark.parametrize(
    "reply", [{"returncode": 1, "stderr": "connection refused"}, {"stdout": "not json"}],
    ids=["cluster-unreachable", "unreadable-json"],
)
def test_list_virtualservices_raises_on_other_failures(reply: dict[str, int | str]) -> None:
    runner = FakeCommandRunner(**reply)
    with pytest.raises(ExternalCommandError):
        _kubectl(runner).list_virtualservices()


# ----- wait_workloads_ready -------------------------------------------------
#
# A failed listing must raise, never read as an empty, converged namespace.


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

    _kubectl(runner).wait_workloads_ready("obs", timeout=90.0)

    rollouts = [c for c in runner.calls if "rollout" in c]
    assert rollouts == [
        ("kubectl", "-n", "obs", "rollout", "status", "deployment/web", "--timeout=90s"),
        ("kubectl", "-n", "obs", "rollout", "status", "deployment/api", "--timeout=90s"),
    ]


def test_wait_workloads_ready_raises_when_the_listing_fails() -> None:
    """A listing failure must not be read as "the namespace has no workloads"."""
    runner = scripted([Reply(returncode=1, stderr="Unauthorized")])

    with pytest.raises(ExternalCommandError) as exc:
        _kubectl(runner).wait_workloads_ready("obs", timeout=90.0)

    assert "cannot list deployment in namespace obs" in str(exc.value)
    assert "Unauthorized" in str(exc.value)
    # It failed on the listing rather than proceeding to any rollout wait.
    assert not [c for c in runner.calls if "rollout" in c]


def test_wait_workloads_ready_accepts_a_genuinely_empty_namespace() -> None:
    runner = scripted([_ok(""), _ok(""), _ok("")])

    _kubectl(runner).wait_workloads_ready("empty", timeout=90.0)

    assert len(runner.calls) == 3
    assert not [c for c in runner.calls if "rollout" in c]


def test_wait_workloads_ready_scopes_listings_to_selector() -> None:
    runner = scripted([_ok("web"), _ok(), _ok(""), _ok("")])

    _kubectl(runner).wait_workloads_ready(
        "shared",
        timeout=90.0,
        selector="app.kubernetes.io/instance=grafana",
    )

    listings = [call for call in runner.calls if "get" in call]
    assert len(listings) == 3
    assert all(
        call[call.index("-l") : call.index("-l") + 2]
        == ("-l", "app.kubernetes.io/instance=grafana")
        for call in listings
    )


# ----- namespaces, pods and events -----------------------------------------


def test_create_namespace_tolerates_one_that_already_exists() -> None:
    runner = FakeCommandRunner(
        returncode=1,
        stderr='Error from server (AlreadyExists): namespaces "obs" already exists',
    )

    _kubectl(runner).create_namespace("obs")


def test_create_namespace_raises_any_other_failure() -> None:
    runner = FakeCommandRunner(returncode=1, stderr="error: You must be logged in")

    with pytest.raises(ExternalCommandError, match="logged in"):
        _kubectl(runner).create_namespace("obs")


def test_pod_logs_missing_pod_returns_empty_no_raise() -> None:
    runner = FakeCommandRunner(
        returncode=1, stderr='Error from server (NotFound): pods "loki-test" not found'
    )
    assert _kubectl(runner).pod_logs("loki", "loki-test") == ""


def test_pod_logs_other_failure_raises_with_structured_fields() -> None:
    runner = FakeCommandRunner(returncode=7, stderr="connection refused")

    with pytest.raises(ExternalCommandError) as excinfo:
        _kubectl(runner).pod_logs("loki", "loki-test")

    assert excinfo.value.returncode == 7
    assert excinfo.value.stderr == "connection refused"


def test_workload_events_selects_the_one_workload() -> None:
    runner = FakeCommandRunner(stdout="evt1\n")
    _kubectl(runner).workload_events("Deployment", "loki", "loki-app")

    assert "involvedObject.name=loki-app,involvedObject.kind=Deployment" in runner.calls[0]


def test_namespace_events_returns_stdout_and_stderr_without_raising() -> None:
    runner = FakeCommandRunner(returncode=1, stdout="evt\n", stderr="warn\n")

    assert _kubectl(runner).namespace_events("loki") == "evt\nwarn\n"


def test_diagnostics_reports_pods_and_events_without_raising() -> None:
    runner = FakeCommandRunner(returncode=1, stderr="refused\n")

    assert _kubectl(runner).diagnostics("loki") == "## pods\nrefused\n\n\n## events\nrefused\n"


def test_kubectl_reports_the_ambient_context(on_path: OnPath) -> None:
    """No pin: the check answers with whatever the kubeconfig points at."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "current-context"), stdout="kind-lab\n")

    checks = checks_by_name(_kubectl(runner).preflight())

    assert checks["kubectl"].detail.startswith("v1.31.0")
    assert checks["kube-context"].status is CheckStatus.OK
    assert "kind-lab" in checks["kube-context"].detail


def test_no_current_kubecontext_is_an_environment_failure(on_path: OnPath) -> None:
    """Exit 5, per the table: nothing is missing, the environment is unset."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "current-context"), returncode=1)

    context = checks_by_name(_kubectl(runner).preflight())["kube-context"]

    assert context.status is CheckStatus.FAILED
    assert context.outcome is Outcome.ENVIRONMENT


def test_a_pinned_context_missing_from_the_kubeconfig_fails(on_path: OnPath) -> None:
    """`CHART_MANAGER_KUBE_CONTEXT` naming a context nobody has is a real bug."""
    on_path("kubectl")
    runner = FakeCommandRunner()
    runner.respond(("kubectl", "version"), stdout='{"clientVersion":{"gitVersion":"v1.31.0"}}')
    runner.respond(("kubectl", "config", "get-contexts"), stdout="kind-lab\nprod\n")

    kubectl = Kubectl(runner, context="kind-gone", timeout=None)
    context = checks_by_name(kubectl.preflight())["kube-context"]

    assert context.status is CheckStatus.FAILED
    assert context.outcome is Outcome.ENVIRONMENT
    assert "kind-lab" in (context.remediation or ""), "say which contexts do exist"


def test_the_context_check_is_skipped_when_kubectl_is_absent(on_path: OnPath) -> None:
    """One broken install, one line of blame -- not two."""
    on_path()

    checks = checks_by_name(_kubectl(FakeCommandRunner()).preflight())

    assert checks["kubectl"].outcome is Outcome.MISSING_BINARY
    assert checks["kube-context"].status is CheckStatus.SKIPPED
    assert checks["kube-context"].outcome is Outcome.SUCCESS, "a skip is not a failure"
