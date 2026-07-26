"""argv-level coverage for `integrations/helmrelease.py` (was `flux.py`).

The client is constructed through a real `Kubectl` over a fake
`CommandRunner` rather than a mock: composing the kubectl adapter is what
gives it its `--context` pin and its JSON-parse policy, so faking the
adapter would stop testing the thing that changed. The pod/event tests
that used to live here moved with their methods to
`test_kubectl_waits_and_lists.py`.
"""
from __future__ import annotations

import json
from datetime import UTC

import pytest

from chart_manager.integrations.helmrelease import (
    HelmReleaseClient,
    HelmReleaseRef,
)
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from tests.conftest import FakeCommandRunner, Reply


def _scripted(replies: list[Reply]) -> FakeCommandRunner:
    """One reply per call, in order; an unscripted call fails the test."""
    return FakeCommandRunner(when_exhausted="raise").script(*replies)


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
    runner = _scripted([_ok(json.dumps(payload))])
    refs = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert [(r.name, r.release_name) for r in refs] == [
        ("loki", "loki-prod"),
        ("grafana", "grafana"),
    ]


def test_list_empty_release_name_falls_back_to_metadata_name() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": "loki"},
                "spec": {"releaseName": ""},
            }
        ]
    }
    runner = _scripted([_ok(json.dumps(payload))])
    [ref] = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert ref.release_name == "loki"


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
    runner = _scripted([_ok(json.dumps(payload))])
    [ref] = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert ref.release_name == "cert-manager-cert-manager"
    assert ref.target_namespace == "cert-manager"
    assert ref.storage_namespace == "cert-manager"


def test_list_explicit_release_name_overrides_target_namespace_prefix() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": "obs"},
                "spec": {
                    "releaseName": "custom",
                    "targetNamespace": "ns",
                    "chart": {"spec": {"chart": "loki", "version": "0.1.0"}},
                },
            }
        ]
    }
    runner = _scripted([_ok(json.dumps(payload))])
    [ref] = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert ref.release_name == "custom"


def test_list_empty_release_name_with_target_namespace_uses_prefix() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": "obs"},
                "spec": {
                    "releaseName": "",
                    "targetNamespace": "ns",
                    "chart": {"spec": {"chart": "loki", "version": "0.1.0"}},
                },
            }
        ]
    }
    runner = _scripted([_ok(json.dumps(payload))])
    [ref] = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert ref.release_name == "ns-loki"


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
    runner = _scripted([_ok(json.dumps(payload))])
    [ref] = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert ref.storage_namespace == "loki-storage"
    assert ref.target_namespace == "loki"


def test_list_storage_namespace_falls_back_to_target_namespace() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": "flux-system"},
                "spec": {"targetNamespace": "loki"},
            }
        ]
    }
    runner = _scripted([_ok(json.dumps(payload))])
    [ref] = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert ref.storage_namespace == "loki"


def test_list_storage_namespace_falls_back_to_metadata_namespace() -> None:
    payload = {
        "items": [
            {
                "apiVersion": "helm.toolkit.fluxcd.io/v2",
                "metadata": {"name": "loki", "namespace": "loki"},
                "spec": {},
            }
        ]
    }
    runner = _scripted([_ok(json.dumps(payload))])
    [ref] = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert ref.storage_namespace == "loki"


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
    runner = _scripted([_ok(json.dumps(payload))])
    refs = HelmReleaseClient(Kubectl(runner=runner)).list()
    assert [r.target_namespace for r in refs] == ["loki-target", "grafana-ns"]


def test_list_propagates_external_error_when_crds_absent() -> None:
    runner = _scripted([_fail("error: the server doesn't have a resource type", returncode=1)])
    with pytest.raises(ExternalCommandError):
        HelmReleaseClient(Kubectl(runner=runner)).list()


def test_list_empty_items_returns_empty_list() -> None:
    runner = _scripted([_ok(json.dumps({"items": []}))])
    assert HelmReleaseClient(Kubectl(runner=runner)).list() == []


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
    runner = _scripted([_ok(json.dumps(payload))])
    status = HelmReleaseClient(Kubectl(runner=runner)).get_status(_ref())
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
    runner = _scripted([_ok(json.dumps(payload))])
    status = HelmReleaseClient(Kubectl(runner=runner)).get_status(_ref())
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
    runner = _scripted([_ok(json.dumps(payload))])
    status = HelmReleaseClient(Kubectl(runner=runner)).get_status(_ref())
    assert status.conditions[0].last_transition_time is None


def test_get_status_exposes_suspended_flag() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki"},
        "spec": {"suspend": True},
        "status": {},
    }
    runner = _scripted([_ok(json.dumps(payload))])
    assert HelmReleaseClient(Kubectl(runner=runner)).get_status(_ref()).suspended is True


def test_get_status_exposes_desired_chart_fields() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki"},
        "spec": {"chart": {"spec": {"chart": "loki", "version": "0.2.0"}}},
        "status": {},
    }
    runner = _scripted([_ok(json.dumps(payload))])
    status = HelmReleaseClient(Kubectl(runner=runner)).get_status(_ref())
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
    runner = _scripted([_ok(json.dumps(payload))])
    assert HelmReleaseClient(Kubectl(runner=runner)).get_status(_ref()).history_chart_version == "0.1.9"


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
    runner = _scripted([_ok(json.dumps(payload))])
    rollouts = HelmReleaseClient(Kubectl(runner=runner)).list_owned_workloads(_ref())
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
    runner = _scripted([_ok(json.dumps(payload))])
    [rollout] = HelmReleaseClient(Kubectl(runner=runner)).list_owned_workloads(_ref())
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
    runner = _scripted([_ok(json.dumps(payload))])
    [rollout] = HelmReleaseClient(Kubectl(runner=runner)).list_owned_workloads(_ref())
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
    runner = _scripted([_ok(json.dumps(payload))])
    [rollout] = HelmReleaseClient(Kubectl(runner=runner)).list_owned_workloads(_ref())
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
    runner = _scripted(
        [_ok(json.dumps(test_payload)), _ok(json.dumps(test_success_payload))]
    )
    pods = HelmReleaseClient(Kubectl(runner=runner)).list_test_pods(_ref())
    assert pods == [
        ("loki", "loki-test", "Running"),
        ("loki", "loki-shared", "Succeeded"),
        ("loki", "loki-extra", "Pending"),
    ]


# ----- _get_json ----------------------------------------------------------


def test_get_json_non_json_stdout_raises_external_command_error() -> None:
    """A malformed payload must land in the same bucket as any tool failure.

    MonitorService degrades on ExternalCommandError; while this raised the
    broader ChartManagerError instead, a malformed kubectl payload escaped
    those handlers and aborted the whole watch rather than being recorded
    as a poll error.
    """
    runner = _scripted([_ok("not actually json " + "x" * 500)])
    with pytest.raises(ExternalCommandError) as excinfo:
        HelmReleaseClient(Kubectl(runner=runner)).list()
    assert "kubectl JSON" in str(excinfo.value)
    assert "not actually json" in str(excinfo.value)
    assert isinstance(excinfo.value, ChartManagerError)


# ----- context kwarg ------------------------------------------------------


def test_context_kwarg_appends_kubectl_flag_on_list() -> None:
    payload = {"items": []}
    runner = _scripted([_ok(json.dumps(payload))])
    HelmReleaseClient(Kubectl(runner=runner, context="kind-foo")).list()
    argv = runner.calls[0]
    assert argv[-2:] == ("--context", "kind-foo")


def test_context_default_omits_kubectl_flag() -> None:
    payload = {"items": []}
    runner = _scripted([_ok(json.dumps(payload))])
    HelmReleaseClient(Kubectl(runner=runner)).list()
    assert "--context" not in runner.calls[0]

