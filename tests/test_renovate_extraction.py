"""Live Renovate extraction coverage for one representative wrapper chart."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_extracts_chart_dependency_and_literal_test_images() -> None:
    """The scoped managers find both Helm and template-owned image updates."""
    if shutil.which("mise") is None:
        pytest.skip("mise is required for Renovate extraction")
    env = {
        **os.environ,
        "LOG_LEVEL": "debug",
        "RENOVATE_CONFIG_FILE": str(ROOT / "renovate-global.json"),
        "RENOVATE_ADDITIONAL_CONFIG_FILE": str(ROOT / "renovate.json"),
        "RENOVATE_CONFIG": json.dumps(
            {
                "includePaths": ["charts/istio-gateway/**"],
                "enabledManagers": ["helmv3", "helm-values", "custom.regex"],
            }
        ),
    }

    completed = subprocess.run(
        [
            "mise",
            "exec",
            "--",
            "renovate",
            "--platform=local",
            "--dry-run=extract",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode == 0, output
    assert '"depName": "gateway"' in output
    assert '"depName": "registry.k8s.io/kubectl"' in output
    assert "charts/istio-gateway/Chart.yaml" in output
    assert "charts/istio-gateway/templates/tests/gateway-ready.yaml" in output
    assert "charts/flink-operator" not in output
