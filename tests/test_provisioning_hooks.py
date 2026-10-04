"""Authored provisioning-hook contract, activation, and safety gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.cli._options import provision_hooks_enabled
from chart_manager.commands.local.models import DevelopmentClusterPlan
from chart_manager.commands.local.wire import plan_to_dict
from chart_manager.plumbing.errors import SpecError
from chart_manager.shared.cluster.local_cluster import load_cluster
from tests.conftest import workspace_for


def _document(hooks: str) -> str:
    return f"""
apiVersion: chartmanager.io/v1alpha1
kind: LocalCluster
metadata: {{name: default}}
spec:
  cluster:
    config: kind.yaml
    hooks:
{hooks}
  bootstrap: {{releases: []}}
"""


def _repository(tmp_path: Path, hooks: str) -> LocalCluster:
    (tmp_path / "kind.yaml").write_text("kind: Cluster\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "prepare").write_text("#!/bin/sh\n", encoding="utf-8")
    config = tmp_path / ".chart-manager" / "local-cluster.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(_document(hooks), encoding="utf-8")
    return load_cluster(workspace_for(tmp_path))


@pytest.mark.parametrize(
    ("hooks", "message"),
    [
        ("      preProvision: ./prepare", "list_type"),
        ("      preProvision: []", "non-empty argv"),
        ('      preProvision: [""]', "non-empty argv"),
        ("      preProvision: [/tmp/prepare]", "relative"),
        ("      preProvision: [../prepare]", "without"),
        ("      preProvision: [./missing]", "file does not exist"),
    ],
)
def test_hook_contract_rejects_shell_empty_and_unsafe_commands(
    tmp_path: Path, hooks: str, message: str
) -> None:
    (tmp_path / "kind.yaml").write_text("kind: Cluster\n", encoding="utf-8")
    config = tmp_path / ".chart-manager" / "local-cluster.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(_document(hooks), encoding="utf-8")

    with pytest.raises(SpecError, match=message):
        load_cluster(workspace_for(tmp_path))


@pytest.mark.parametrize("value", ["1", "TRUE", " yes ", "On"])
def test_ci_truthy_disables_hooks_unless_explicitly_overridden(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CI", value)
    assert provision_hooks_enabled(None) is False
    assert provision_hooks_enabled(True) is True
    assert provision_hooks_enabled(False) is False


def test_non_ci_default_enables_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    assert provision_hooks_enabled(None) is True


def test_local_machine_plan_reports_hook_argv_and_activation() -> None:
    payload = plan_to_dict(
        DevelopmentClusterPlan(
            command="up",
            cluster_name="lab",
            provisioning_hooks_enabled=False,
            provisioning_hooks=(("preProvision", ("./prepare", "arg")),),
        )
    )

    assert payload["provisioning_hooks_enabled"] is False
    assert payload["provisioning_hooks"] == [
        {"phase": "preProvision", "argv": ["./prepare", "arg"]}
    ]
