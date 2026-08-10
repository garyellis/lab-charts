"""Authored provisioning-hook contract, runner, activation, and safety gates."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from chart_manager.api.local.v1alpha1 import LocalCluster
from chart_manager.cli._options import provision_hooks_enabled
from chart_manager.domain.local_resources import LocalResourceLoader
from chart_manager.plumbing.errors import ExternalCommandError, SpecError
from chart_manager.services.clusters.development.models import DevelopmentClusterPlan
from chart_manager.services.clusters.development.wire import plan_to_dict
from chart_manager.services.clusters.environment import EnvironmentHandle
from chart_manager.services.clusters.provisioning_hooks import ProvisioningHookRunner

from .conftest import FakeCommandRunner


def _document(hooks: str) -> str:
    return f"""
apiVersion: local.chartmanager.io/v1alpha1
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
    config.parent.mkdir()
    config.write_text(_document(hooks), encoding="utf-8")
    return LocalResourceLoader(tmp_path).load_cluster()


def test_hook_contract_accepts_one_argv_per_phase_and_runner_scopes_metadata(
    tmp_path: Path,
) -> None:
    cluster = _repository(
        tmp_path,
        "      preProvision: [./scripts/prepare, before]\n"
        "      postProvision: [tool-on-path, after]",
    )
    runner = FakeCommandRunner()
    hooks = ProvisioningHookRunner(tmp_path, runner=runner, timeout=17)

    hooks.run("preProvision", cluster, cluster_name="lab")
    hooks.run(
        "postProvision",
        cluster,
        cluster_name="lab",
        environment=EnvironmentHandle("lab", "kind-lab", "kind"),
    )

    assert runner.calls == [
        ("./scripts/prepare", "before"),
        ("tool-on-path", "after"),
    ]
    pre, post = runner.records
    assert pre.cwd == post.cwd == tmp_path.resolve()
    assert pre.capture is post.capture is False
    assert pre.timeout == post.timeout == 17
    assert pre.env == {
        "CHART_MANAGER_HOOK_PHASE": "preProvision",
        "CHART_MANAGER_ROOT": str(tmp_path.resolve()),
        "CHART_MANAGER_CLUSTER_NAME": "lab",
        "CHART_MANAGER_KIND_CONFIG": str(tmp_path / "kind.yaml"),
    }
    assert post.env is not None
    assert post.env["CHART_MANAGER_KUBE_CONTEXT"] == "kind-lab"
    assert post.env["CHART_MANAGER_PROVIDER_TYPE"] == "kind"


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
    config.parent.mkdir()
    config.write_text(_document(hooks), encoding="utf-8")

    with pytest.raises(SpecError, match=message):
        LocalResourceLoader(tmp_path).load_cluster()


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


def test_reset_pre_hook_failure_prevents_destroy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chart_manager.services.clusters.development.service import DevelopmentClusterService

    cluster = _repository(tmp_path, "      preProvision: [./scripts/prepare]")
    runner = FakeCommandRunner(returncode=9, stderr="blocked")

    class Provider:
        def __init__(self) -> None:
            self.destroyed = False

        def destroy(self, _handle: object) -> bool:
            self.destroyed = True
            return True

        def handle(self, _spec: object) -> EnvironmentHandle:
            return EnvironmentHandle("lab", "kind-lab", "kind")

    provider = Provider()
    service = DevelopmentClusterService(
        tmp_path,
        helm=SimpleNamespace(),  # type: ignore[arg-type]
        kind=SimpleNamespace(),  # type: ignore[arg-type]
        kubectl=SimpleNamespace(),  # type: ignore[arg-type]
        expose=SimpleNamespace(),  # type: ignore[arg-type]
        environment_provider=provider,  # type: ignore[arg-type]
        command_runner=runner,
    )
    monkeypatch.setattr(
        service,
        "_prepare_target",
        lambda *_a, **_k: SimpleNamespace(
            local_cluster=cluster,
            steps=(),
            config=tmp_path / "kind.yaml",
        ),
    )

    with pytest.raises(ExternalCommandError, match="blocked"):
        service.reset_target(
            SimpleNamespace(name="demo", kind="chart"),  # type: ignore[arg-type]
            profile=None,
            cluster_name="lab",
        )

    assert provider.destroyed is False
