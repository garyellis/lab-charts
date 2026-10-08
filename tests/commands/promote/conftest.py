"""Cluster objects and a clock for driving the promote stages through `FakeCommandRunner`.

`cluster(...)` answers `kubectl get` for HelmReleases the way a real cluster would; anything a
test does not script reads as an empty list.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from tests.conftest import FakeCommandRunner, Predicate, Reply, argv_prefix, plain_argv

CHART = "loki"
VERSION = "0.2.0"
HR = "helmreleases.helm.toolkit.fluxcd.io"


class Clock:
    """Monotonic clock: advances by `step` per read and by every `sleep`."""

    def __init__(self, *, step: float = 0.0) -> None:
        self.step, self.t = step, 0.0

    def __call__(self) -> float:
        value, self.t = self.t, self.t + self.step
        return value

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def condition(type_: str, status: str, reason: str = "", message: str = "") -> dict[str, str]:
    return {"type": type_, "status": status, "reason": reason, "message": message}


def helmrelease(
    name: str = "loki",
    namespace: str = "loki",
    *,
    chart: str = CHART,
    version: str = VERSION,
    generation: int = 1,
    observed: int = 1,
    history: str = VERSION,
    conditions: Sequence[dict[str, str]] | None = None,
    suspend: bool = False,
    target_namespace: str | None = None,
) -> dict[str, Any]:
    """A HelmRelease; by default Ready and Released at chart@version."""
    spec: dict[str, Any] = {"chart": {"spec": {"chart": chart, "version": version}}}
    if suspend:
        spec["suspend"] = True
    if target_namespace is not None:
        spec["targetNamespace"] = target_namespace
    return {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": name, "namespace": namespace, "generation": generation},
        "spec": spec,
        "status": {
            "observedGeneration": observed,
            "history": [{"chartVersion": history}],
            "conditions": list(
                conditions
                if conditions is not None
                else (
                    condition("Ready", "True", "ReconciliationSucceeded"),
                    condition("Released", "True", "InstallSucceeded"),
                )
            ),
        },
    }


def deployment(name: str = "loki-app", *, converged: bool = True) -> dict[str, Any]:
    ready = 1 if converged else 0
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": "loki", "generation": 1},
        "spec": {"replicas": 1},
        "status": {"observedGeneration": ready, "readyReplicas": ready, "availableReplicas": ready},
    }


def pod(name: str, phase: str) -> dict[str, Any]:
    return {"metadata": {"name": name, "namespace": "loki"}, "status": {"phase": phase}}


def items(*objects: dict[str, Any]) -> Reply:
    return Reply(stdout=json.dumps({"items": list(objects)}))


def failure(stderr: str) -> Reply:
    return Reply(returncode=1, stderr=stderr)


EMPTY = json.dumps({"items": []})


def cluster(
    *releases: dict[str, Any] | Sequence[dict[str, Any] | Reply],
    runner: FakeCommandRunner | None = None,
) -> FakeCommandRunner:
    """Serve these HelmReleases; a sequence is one release's reads in order, the last repeating."""
    runner = runner or FakeCommandRunner(stdout=EMPTY)
    reads = [[release] if isinstance(release, dict) else list(release) for release in releases]
    listed = [read[0] for read in reads if isinstance(read[0], dict)]
    runner.respond(argv_prefix("kubectl", "get", HR), stdout=json.dumps({"items": listed}))
    for read, release in zip(reads, listed, strict=True):
        meta = release["metadata"]
        runner.respond_each(
            argv_prefix("kubectl", "-n", meta["namespace"], "get", HR, meta["name"]),
            *(item if isinstance(item, Reply) else Reply(stdout=json.dumps(item)) for item in read),
        )
    return runner


def workloads(name: str = "loki") -> Predicate:
    """Matches the owned-workload listing for HelmRelease `name`."""
    return lambda argv: plain_argv(argv)[:3] == (
        "kubectl",
        "get",
        "deployment,statefulset,daemonset",
    ) and any(f"helm.toolkit.fluxcd.io/name={name}," in arg for arg in argv)


def hook_pods(argv: tuple[str, ...]) -> bool:
    """Matches the `helm.sh/hook=test` pod listing (the `test-success` one stays empty)."""
    return "pods" in argv and any(arg.endswith("helm.sh/hook=test") for arg in argv)



def calls(runner: FakeCommandRunner, *prefix: str) -> list[tuple[str, ...]]:
    """Recorded argv (context dropped) that start with `prefix`."""
    return [argv for argv in map(plain_argv, runner.calls) if argv[: len(prefix)] == prefix]
