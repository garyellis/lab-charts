"""Coverage for `Helm.get_values`.

Drives the cilium-k8sServiceHost drift detection in `LabService.up`; if
helm's `get values -o json` contract drifts (or our parsing does) we want
to catch it here, not as a confusing "drift detected but it's not" error
in the lab installer.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from chart_manager.integrations import helm as helm_module
from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


class FakeRunner(CommandRunner):
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._stdout = stdout
        self._returncode = returncode

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(args))
        if check and self._returncode != 0:
            raise ExternalCommandError(
                f"command failed ({self._returncode}): {' '.join(args)}"
            )
        return CommandResult(
            args=tuple(args),
            returncode=self._returncode,
            stdout=self._stdout,
            stderr="",
        )


@pytest.fixture(autouse=True)
def _clear_mise_cache() -> None:
    helm_module._resolve_via_mise.cache_clear()


def test_get_values_parses_nested_json() -> None:
    payload = json.dumps(
        {"cilium": {"k8sServiceHost": "172.18.0.2", "k8sServicePort": "6443"}}
    )
    runner = FakeRunner(stdout=payload)

    values = Helm(runner=runner).get_values("cilium", namespace="kube-system")

    assert runner.calls == [
        ("helm", "get", "values", "cilium", "-n", "kube-system", "-o", "json")
    ]
    assert values == {
        "cilium": {"k8sServiceHost": "172.18.0.2", "k8sServicePort": "6443"}
    }


def test_get_values_empty_stdout_returns_empty_dict() -> None:
    # An empty user-values payload (release installed with no overrides)
    # surfaces as an empty string from `helm get values -o json`; treat
    # as "no overrides" rather than blowing up.
    runner = FakeRunner(stdout="")

    assert Helm(runner=runner).get_values("x", namespace="y") == {}


def test_get_values_null_payload_returns_empty_dict() -> None:
    runner = FakeRunner(stdout="null")

    assert Helm(runner=runner).get_values("x", namespace="y") == {}


def test_get_values_non_object_raises() -> None:
    # A JSON array at the top level would mean either a helm contract
    # change or something has gone catastrophically wrong; surface it
    # rather than silently returning {}.
    runner = FakeRunner(stdout="[1, 2, 3]")

    with pytest.raises(ExternalCommandError):
        Helm(runner=runner).get_values("x", namespace="y")


def test_get_values_invalid_json_raises() -> None:
    runner = FakeRunner(stdout="not-json")

    with pytest.raises(ExternalCommandError):
        Helm(runner=runner).get_values("x", namespace="y")
