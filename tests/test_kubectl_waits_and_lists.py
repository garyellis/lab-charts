"""Coverage for the M1c-added Kubectl helpers.

  * `wait_certificate_ready` / `wait_deployment_available`: thin wrappers
    around `kubectl wait`; we assert the argv shape and propagate the
    runner's exit code as ExternalCommandError on failure.
  * `list_virtualservice_hosts` / `list_gateway_hosts`: best-effort
    listings used by LabService and the `sandbox expose` CLI. Empty list
    on missing CRD / parse error is the contract -- callers treat that
    as "no hosts yet" rather than as a hard error.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


class _Recorder(CommandRunner):
    """Records every invocation; returns (returncode, stdout) per call.

    A single response is repeated for subsequent calls so tests can be
    written against the contract rather than against call counts.
    """

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

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
        result = CommandResult(
            args=argv,
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
        )
        if check and result.returncode != 0:
            raise ExternalCommandError(
                f"command failed ({result.returncode}): {' '.join(args)}\n{self._stderr}"
            )
        return result


# ----- wait_certificate_ready -----------------------------------------------


def test_wait_certificate_ready_invokes_kubectl_with_expected_argv() -> None:
    runner = _Recorder()
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
    runner = _Recorder(returncode=1, stderr="timed out waiting for the condition")
    with pytest.raises(ExternalCommandError) as excinfo:
        Kubectl(runner=runner).wait_certificate_ready(
            "apps-wildcard", namespace="istio-ingress", timeout="1s"
        )
    assert "timed out" in str(excinfo.value)


# ----- wait_deployment_available --------------------------------------------


def test_wait_deployment_available_invokes_kubectl_with_expected_argv() -> None:
    runner = _Recorder()
    Kubectl(runner=runner).wait_deployment_available(
        "cert-manager-webhook", namespace="cert-manager", timeout="120s"
    )

    assert runner.calls == [
        (
            "kubectl",
            "-n",
            "cert-manager",
            "wait",
            "--for=condition=Available",
            "deployment/cert-manager-webhook",
            "--timeout=120s",
        )
    ]


def test_wait_deployment_available_surfaces_failure() -> None:
    runner = _Recorder(returncode=1, stderr="not found")
    with pytest.raises(ExternalCommandError):
        Kubectl(runner=runner).wait_deployment_available(
            "cert-manager-webhook", namespace="cert-manager", timeout="1s"
        )


# ----- list_virtualservice_hosts --------------------------------------------


def _vs_payload(items: list[dict[str, object]]) -> str:
    return json.dumps({"items": items})


def test_list_virtualservice_hosts_empty_when_kubectl_fails() -> None:
    # Missing CRD -> kubectl exits non-zero -- list_virtualservice_hosts
    # is best-effort and returns an empty list rather than raising.
    runner = _Recorder(returncode=1, stderr="error: the server doesn't have a resource type \"virtualservice\"")
    assert Kubectl(runner=runner).list_virtualservice_hosts() == []


def test_list_virtualservice_hosts_empty_when_no_items() -> None:
    runner = _Recorder(stdout=_vs_payload([]))
    assert Kubectl(runner=runner).list_virtualservice_hosts() == []


def test_list_virtualservice_hosts_returns_single_vs() -> None:
    runner = _Recorder(
        stdout=_vs_payload(
            [{"spec": {"hosts": ["grafana.localhost"]}}]
        )
    )
    assert Kubectl(runner=runner).list_virtualservice_hosts() == ["grafana.localhost"]


def test_list_virtualservice_hosts_dedupes_and_sorts_many_vs() -> None:
    # Two VS, one with multiple hosts, with a duplicate across VS to prove
    # the dedup. Sorted output keeps printouts byte-stable.
    runner = _Recorder(
        stdout=_vs_payload(
            [
                {"spec": {"hosts": ["grafana.localhost", "prom.localhost"]}},
                {"spec": {"hosts": ["loki.localhost", "grafana.localhost"]}},
            ]
        )
    )
    assert Kubectl(runner=runner).list_virtualservice_hosts() == [
        "grafana.localhost",
        "loki.localhost",
        "prom.localhost",
    ]


def test_list_virtualservice_hosts_ignores_malformed_json() -> None:
    runner = _Recorder(stdout="this is not json")
    assert Kubectl(runner=runner).list_virtualservice_hosts() == []


# ----- list_gateway_hosts ---------------------------------------------------


def _gw_payload(items: list[dict[str, object]]) -> str:
    return json.dumps({"items": items})


def test_list_gateway_hosts_empty_when_no_gateway_installed() -> None:
    runner = _Recorder(returncode=1, stderr="no resources found")
    assert Kubectl(runner=runner).list_gateway_hosts() == []


def test_list_gateway_hosts_returns_servers_hosts_flattened() -> None:
    runner = _Recorder(
        stdout=_gw_payload(
            [
                {
                    "spec": {
                        "servers": [
                            {"hosts": ["*.localhost"]},
                            {"hosts": ["*.localhost"]},  # dedup
                        ]
                    }
                }
            ]
        )
    )
    assert Kubectl(runner=runner).list_gateway_hosts() == ["*.localhost"]


def test_list_gateway_hosts_handles_multiple_gateways() -> None:
    runner = _Recorder(
        stdout=_gw_payload(
            [
                {"spec": {"servers": [{"hosts": ["*.kind.local"]}]}},
                {"spec": {"servers": [{"hosts": ["*.k8s.home.lab.io"]}]}},
            ]
        )
    )
    assert Kubectl(runner=runner).list_gateway_hosts() == [
        "*.k8s.home.lab.io",
        "*.kind.local",
    ]
