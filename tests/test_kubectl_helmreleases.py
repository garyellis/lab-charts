"""Coverage for the Flux HelmRelease queries in `integrations/kubectl.py`.

The queries run through a real `Kubectl` over a fake `CommandRunner`
rather than a mock: the kubectl adapter is what gives them their
`--context` pin and their JSON-parse policy, so faking the adapter would
stop testing them.
"""
from __future__ import annotations

import json
from datetime import UTC

import pytest

from chart_manager.integrations.kubectl import HelmReleaseRef, Kubectl
from chart_manager.plumbing.errors import ExternalCommandError
from tests.conftest import FakeCommandRunner, Reply, scripted


def _kubectl(runner: FakeCommandRunner) -> Kubectl:
    return Kubectl(runner, context=None, timeout=None)


def _ok(stdout: str) -> Reply:
    return Reply(stdout=stdout)


def _fail(stderr: str, returncode: int = 1) -> Reply:
    return Reply(returncode=returncode, stderr=stderr)

def _ref(
    *,
    name: str = "loki",
    namespace: str = "loki",
    target: str | None = None,
    storage: str | None = None,
) -> HelmReleaseRef:
    target_ns = target or namespace
    storage_ns = storage or target_ns
    return HelmReleaseRef(
        name=name,
        namespace=namespace,
        api_version="helm.toolkit.fluxcd.io/v2",
        release_name=name,
        storage_namespace=storage_ns,
        target_namespace=target_ns,
    )


# ----- list ----------------------------------------------------------------


def test_list_parses_mixed_v2_and_v2beta2_payload() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "kind": "HelmRelease",
                "metadata": {"name": "loki", "namespace": "loki"},
                "spec": {"releaseName": "loki-prod"},
            },
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2beta2",
                "kind": "HelmRelease",
                "metadata": {"name": "grafana", "namespace": "grafana"},
                "spec": {},
            },
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    refs = _kubectl(runner).list_helmreleases()
    assert [(r.name, r.release_name) for r in refs] == [
        ("loki", "loki-prod"),
        ("grafana", "grafana"),
    ]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param({"releaseName": ""}, "loki", id="empty-falls-back-to-metadata-name"),
        pytest.param(
            {
                "releaseName": "custom",
                "targetNamespace": "ns",
                "chart": {"spec": {"chart": "loki", "version": "0.1.0"}},
            },
            "custom",
            id="explicit-overrides-target-namespace-prefix",
        ),
        pytest.param(
            {
                "releaseName": "",
                "targetNamespace": "ns",
                "chart": {"spec": {"chart": "loki", "version": "0.1.0"}},
            },
            "ns-loki",
            id="empty-with-target-namespace-uses-prefix",
        ),
    ],
)
def test_list_resolves_the_release_name(spec: dict[str, object], expected: str) -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": "obs"},
                "spec": spec,
            }
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    [ref] = _kubectl(runner).list_helmreleases()
    assert ref.release_name == expected


def test_list_release_name_prefixed_with_target_namespace() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "cert-manager", "namespace": "cert-manager"},
                "spec": {
                    "targetNamespace": "cert-manager",
                    "chart": {"spec": {"chart": "cert-manager", "version": "0.1.0"}},
                },
            }
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    [ref] = _kubectl(runner).list_helmreleases()
    assert ref.release_name == "cert-manager-cert-manager"
    assert ref.target_namespace == "cert-manager"
    assert ref.storage_namespace == "cert-manager"


def test_list_storage_namespace_from_spec_storage_namespace() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": "flux-system"},
                "spec": {
                    "storageNamespace": "loki-storage",
                    "targetNamespace": "loki",
                },
            }
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    [ref] = _kubectl(runner).list_helmreleases()
    assert ref.storage_namespace == "loki-storage"
    assert ref.target_namespace == "loki"


@pytest.mark.parametrize(
    ("namespace", "spec", "expected"),
    [
        pytest.param("flux-system", {"targetNamespace": "loki"}, "loki", id="target-namespace"),
        pytest.param("obs", {}, "obs", id="metadata-namespace"),
    ],
)
def test_list_storage_namespace_falls_back(
    namespace: str, spec: dict[str, str], expected: str
) -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": namespace},
                "spec": spec,
            }
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    [ref] = _kubectl(runner).list_helmreleases()
    assert ref.storage_namespace == expected


def test_list_target_namespace_independent_of_storage() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": "flux-system"},
                "spec": {"targetNamespace": "loki-target"},
            },
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "grafana", "namespace": "grafana-ns"},
                "spec": {},
            },
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    refs = _kubectl(runner).list_helmreleases()
    assert [r.target_namespace for r in refs] == ["loki-target", "grafana-ns"]


def test_list_propagates_external_error_when_crds_absent() -> None:
    runner = scripted([_fail("error: the server doesn't have a resource type", returncode=1)])
    with pytest.raises(ExternalCommandError):
        _kubectl(runner).list_helmreleases()


def test_list_empty_items_returns_empty_list() -> None:
    runner = scripted([_ok(json.dumps({"items": []}))])
    assert _kubectl(runner).list_helmreleases() == []


# ----- get_status ----------------------------------------------------------


def test_get_status_parses_tz_aware_last_transition_time() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki", "generation": 3, "resourceVersion": "100"},
        "spec": {},
        "status": {
            "observedGeneration": 3,
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "ReconciliationSucceeded",
                    "message": "release reconciled",
                    "lastTransitionTime": "2026-06-15T10:30:00Z",
                }
            ],
        },
    }
    runner = scripted([_ok(json.dumps(payload))])
    status = _kubectl(runner).get_helmrelease_status(_ref())
    assert status.observed_generation == 3
    ready = status.ready
    assert ready is not None
    assert ready.last_transition_time is not None
    assert ready.last_transition_time.tzinfo == UTC
    assert ready.last_transition_time.year == 2026


def test_get_status_with_absent_status_block() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki", "generation": 2},
        "spec": {},
    }
    runner = scripted([_ok(json.dumps(payload))])
    status = _kubectl(runner).get_helmrelease_status(_ref())
    assert status.observed_generation == -1
    assert status.conditions == ()
    assert status.observed_at.tzinfo == UTC


def test_get_status_unparseable_timestamp_is_none() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki"},
        "spec": {},
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True", "lastTransitionTime": "not-a-time"}
            ]
        },
    }
    runner = scripted([_ok(json.dumps(payload))])
    status = _kubectl(runner).get_helmrelease_status(_ref())
    assert status.conditions[0].last_transition_time is None


def test_get_status_exposes_suspended_flag() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki"},
        "spec": {"suspend": True},
        "status": {},
    }
    runner = scripted([_ok(json.dumps(payload))])
    assert _kubectl(runner).get_helmrelease_status(_ref()).suspended is True


def test_get_status_exposes_desired_chart_fields() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki"},
        "spec": {"chart": {"spec": {"chart": "loki", "version": "0.2.0"}}},
        "status": {},
    }
    runner = scripted([_ok(json.dumps(payload))])
    status = _kubectl(runner).get_helmrelease_status(_ref())
    assert status.desired_chart_name == "loki"
    assert status.desired_chart_version == "0.2.0"


def test_get_status_exposes_history_chart_version() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki"},
        "spec": {},
        "status": {
            "history": [
                {"chartVersion": "0.1.9"},
                {"chartVersion": "0.1.8"},
            ]
        },
    }
    runner = scripted([_ok(json.dumps(payload))])
    assert _kubectl(runner).get_helmrelease_status(_ref()).history_chart_version == "0.1.9"


# ----- list_owned_workloads -----------------------------------------------


def test_list_owned_workloads_parses_mixed_kinds_converged() -> None:
    payload = {
        "items": [
            {
                "kind": "Deployment",
                "metadata": {"name": "loki-app", "namespace": "loki", "generation": 4},
                "spec": {"replicas": 2},
                "status": {
                    "observedGeneration": 4,
                    "readyReplicas": 2,
                    "availableReplicas": 2,
                },
            },
            {
                "kind": "DaemonSet",
                "metadata": {"name": "loki-promtail", "namespace": "loki", "generation": 1},
                "spec": {},
                "status": {
                    "observedGeneration": 1,
                    "desiredNumberScheduled": 3,
                    "numberReady": 3,
                    "numberAvailable": 3,
                },
            },
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    rollouts = _kubectl(runner).list_owned_workloads(_ref())
    assert [r.workload.kind for r in rollouts] == ["Deployment", "DaemonSet"]
    assert all(r.converged for r in rollouts)


def test_list_owned_workloads_not_converged_when_observed_generation_lags() -> None:
    payload = {
        "items": [
            {
                "kind": "Deployment",
                "metadata": {"name": "loki-app", "namespace": "loki", "generation": 5},
                "spec": {"replicas": 2},
                "status": {
                    "observedGeneration": 4,
                    "readyReplicas": 2,
                    "availableReplicas": 2,
                },
            }
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    [rollout] = _kubectl(runner).list_owned_workloads(_ref())
    assert rollout.converged is False


def test_list_owned_workloads_daemonset_uses_daemonset_fields() -> None:
    payload = {
        "items": [
            {
                "kind": "DaemonSet",
                "metadata": {"name": "loki-promtail", "namespace": "loki", "generation": 2},
                "spec": {},
                "status": {
                    "observedGeneration": 2,
                    "desiredNumberScheduled": 4,
                    "numberReady": 3,
                    "numberAvailable": 2,
                },
            }
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    [rollout] = _kubectl(runner).list_owned_workloads(_ref())
    assert rollout.workload.desired == 4
    assert rollout.workload.ready == 3
    assert rollout.workload.available == 2
    assert rollout.converged is False


def test_list_owned_workloads_zero_replica_deployment_is_converged() -> None:
    payload = {
        "items": [
            {
                "kind": "Deployment",
                "metadata": {"name": "loki-app", "namespace": "loki", "generation": 7},
                "spec": {"replicas": 0},
                "status": {
                    "observedGeneration": 7,
                },
            }
        ]
    }
    runner = scripted([_ok(json.dumps(payload))])
    [rollout] = _kubectl(runner).list_owned_workloads(_ref())
    assert rollout.workload.desired == 0
    assert rollout.converged is True


# ----- list_test_pods -----------------------------------------------------


def test_list_test_pods_unions_hook_queries_dedupes_and_returns_phase() -> None:
    test_payload = {
        "items": [
            {
                "metadata": {"name": "loki-test", "namespace": "loki"},
                "status": {"phase": "Running"},
            },
            {
                "metadata": {"name": "loki-shared", "namespace": "loki"},
                "status": {"phase": "Succeeded"},
            },
        ]
    }
    test_success_payload = {
        "items": [
            {
                "metadata": {"name": "loki-shared", "namespace": "loki"},
                "status": {"phase": "Failed"},
            },
            {
                "metadata": {"name": "loki-extra", "namespace": "loki"},
                "status": {"phase": "Pending"},
            },
        ]
    }
    runner = scripted(
        [_ok(json.dumps(test_payload)), _ok(json.dumps(test_success_payload))]
    )
    pods = _kubectl(runner).list_test_pods(_ref())
    assert pods == [
        ("loki", "loki-test", "Running"),
        ("loki", "loki-shared", "Succeeded"),
        ("loki", "loki-extra", "Pending"),
    ]


# ----- JSON parse failures ------------------------------------------------


@pytest.mark.parametrize("stdout", ["not actually json " + "x" * 500, "[]"])
def test_unreadable_kubectl_json_raises_external_command_error(stdout: str) -> None:
    """A malformed payload lands in the same bucket as any tool failure.

    The monitor degrades on ExternalCommandError, so a broader error here would
    abort the whole watch instead of being recorded as a poll error.
    """
    with pytest.raises(ExternalCommandError):
        _kubectl(scripted([_ok(stdout)])).list_helmreleases()
