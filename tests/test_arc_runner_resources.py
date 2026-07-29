"""Capacity guardrails for the self-hosted ARC job runner."""

from pathlib import Path

import yaml


def test_arc_runner_has_ci_sized_cpu_and_memory() -> None:
    root = Path(__file__).resolve().parents[1]
    values = yaml.safe_load((root / "charts/arc-runner-set/values.yaml").read_text())
    runner = values["gha-runner-scale-set"]["template"]["spec"]["containers"][0]

    assert runner["name"] == "runner"
    assert runner["resources"] == {
        "requests": {"cpu": "1", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "2Gi"},
    }
