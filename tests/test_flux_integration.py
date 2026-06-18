from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC
from pathlib import Path

import pytest

from chart_manager.integrations.flux import (
    Flux,
    HelmReleaseRef,
)
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError


class _ScriptedRunner(CommandRunner):
    """Returns pre-baked CommandResults in call order; records every argv."""

    def __init__(self, results: Sequence[CommandResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []

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
        if not self._results:
            raise AssertionError(f"unscripted call: {argv}")
        result = self._results.pop(0)
        if check and result.returncode != 0:
            raise ExternalCommandError(
                f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr}",
                stderr=result.stderr,
                returncode=result.returncode,
            )
        return CommandResult(
            args=argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def _ok(stdout: str) -> CommandResult:
    return CommandResult(args=(), returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str, returncode: int = 1) -> CommandResult:
    return CommandResult(args=(), returncode=returncode, stdout="", stderr=stderr)


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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    refs = Flux(runner=runner).list()
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [ref] = Flux(runner=runner).list()
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [ref] = Flux(runner=runner).list()
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [ref] = Flux(runner=runner).list()
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [ref] = Flux(runner=runner).list()
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [ref] = Flux(runner=runner).list()
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [ref] = Flux(runner=runner).list()
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [ref] = Flux(runner=runner).list()
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    refs = Flux(runner=runner).list()
    assert [r.target_namespace for r in refs] == ["loki-target", "grafana-ns"]


def test_list_propagates_external_error_when_crds_absent() -> None:
    runner = _ScriptedRunner([_fail("error: the server doesn't have a resource type", returncode=1)])
    with pytest.raises(ExternalCommandError):
        Flux(runner=runner).list()


def test_list_empty_items_returns_empty_list() -> None:
    runner = _ScriptedRunner([_ok(json.dumps({"items": []}))])
    assert Flux(runner=runner).list() == []


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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    status = Flux(runner=runner).get_status(_ref())
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    status = Flux(runner=runner).get_status(_ref())
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    status = Flux(runner=runner).get_status(_ref())
    assert status.conditions[0].last_transition_time is None


def test_get_status_exposes_suspended_flag() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki"},
        "spec": {"suspend": True},
        "status": {},
    }
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    assert Flux(runner=runner).get_status(_ref()).suspended is True


def test_get_status_exposes_desired_chart_fields() -> None:
    payload = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "metadata": {"name": "loki", "namespace": "loki"},
        "spec": {"chart": {"spec": {"chart": "loki", "version": "0.2.0"}}},
        "status": {},
    }
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    status = Flux(runner=runner).get_status(_ref())
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    assert Flux(runner=runner).get_status(_ref()).history_chart_version == "0.1.9"


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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    rollouts = Flux(runner=runner).list_owned_workloads(_ref())
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [rollout] = Flux(runner=runner).list_owned_workloads(_ref())
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [rollout] = Flux(runner=runner).list_owned_workloads(_ref())
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
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    [rollout] = Flux(runner=runner).list_owned_workloads(_ref())
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
    runner = _ScriptedRunner(
        [_ok(json.dumps(test_payload)), _ok(json.dumps(test_success_payload))]
    )
    pods = Flux(runner=runner).list_test_pods(_ref())
    assert pods == [
        ("loki", "loki-test", "Running"),
        ("loki", "loki-shared", "Succeeded"),
        ("loki", "loki-extra", "Pending"),
    ]


# ----- pod_logs / delete_pod ----------------------------------------------


def test_pod_logs_missing_pod_returns_empty_no_raise() -> None:
    runner = _ScriptedRunner(
        [_fail('Error from server (NotFound): pods "loki-test" not found', returncode=1)]
    )
    assert Flux(runner=runner).pod_logs("loki", "loki-test") == ""


def test_pod_logs_previous_flag_in_argv() -> None:
    runner = _ScriptedRunner([_ok("log line")])
    Flux(runner=runner).pod_logs("loki", "loki-test", previous=True)
    assert "--previous" in runner.calls[0]


def test_delete_pod_uses_ignore_not_found_flag() -> None:
    runner = _ScriptedRunner([_ok("")])
    Flux(runner=runner).delete_pod("loki", "loki-test")
    assert "--ignore-not-found" in runner.calls[0]


# ----- _get_json ----------------------------------------------------------


def test_get_json_non_json_stdout_raises_chart_manager_error() -> None:
    runner = _ScriptedRunner([_ok("not actually json " + "x" * 500)])
    with pytest.raises(ChartManagerError) as excinfo:
        Flux(runner=runner).list()
    assert "kubectl JSON" in str(excinfo.value)
    assert "not actually json" in str(excinfo.value)
    assert not isinstance(excinfo.value, ExternalCommandError)


# ----- workload_events ----------------------------------------------------


def test_workload_events_field_selector_argv() -> None:
    runner = _ScriptedRunner([_ok("evt1\n")])
    Flux(runner=runner).workload_events("Deployment", "loki", "loki-app")
    argv = runner.calls[0]
    assert "--field-selector" in argv
    selector_idx = argv.index("--field-selector") + 1
    assert argv[selector_idx] == "involvedObject.name=loki-app,involvedObject.kind=Deployment"
    assert "--sort-by=.lastTimestamp" in argv


# ----- context kwarg ------------------------------------------------------


def test_context_kwarg_appends_kubectl_flag_on_list() -> None:
    payload = {"items": []}
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    Flux(runner=runner, context="kind-foo").list()
    argv = runner.calls[0]
    assert argv[-2:] == ("--context", "kind-foo")


def test_context_default_omits_kubectl_flag() -> None:
    payload = {"items": []}
    runner = _ScriptedRunner([_ok(json.dumps(payload))])
    Flux(runner=runner).list()
    assert "--context" not in runner.calls[0]


def test_context_kwarg_appended_on_delete_pod_argv() -> None:
    runner = _ScriptedRunner([_ok("")])
    Flux(runner=runner, context="prod").delete_pod("loki", "loki-test")
    argv = runner.calls[0]
    assert argv[-2:] == ("--context", "prod")

