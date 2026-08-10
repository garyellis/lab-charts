"""Fail-fast execution of authored local-cluster provisioning hooks."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from chart_manager.api.local.v1alpha1 import LocalCluster
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.services.clusters._shared import kind_config_path
from chart_manager.services.clusters.environment import EnvironmentHandle

HookPhase = Literal["preProvision", "postProvision"]


class ProvisioningHookRunner:
    """Run one optional argv hook per provisioning phase through the shared runner."""

    def __init__(
        self,
        root: Path,
        *,
        runner: CommandRunner,
        timeout: float | None = None,
    ) -> None:
        self.root = root.resolve()
        self.runner = runner
        self.timeout = timeout

    def run(
        self,
        phase: HookPhase,
        cluster: LocalCluster,
        *,
        cluster_name: str,
        environment: EnvironmentHandle | None = None,
    ) -> None:
        hooks = cluster.spec.cluster.hooks
        command = None
        if hooks is not None:
            command = hooks.pre_provision if phase == "preProvision" else hooks.post_provision
        if command is None:
            return
        env = {
            "CHART_MANAGER_HOOK_PHASE": phase,
            "CHART_MANAGER_ROOT": str(self.root),
            "CHART_MANAGER_CLUSTER_NAME": cluster_name,
            "CHART_MANAGER_KIND_CONFIG": str(kind_config_path(self.root, cluster)),
        }
        if phase == "postProvision":
            if environment is None:
                raise ValueError("postProvision requires a resolved environment")
            env.update(
                {
                    "CHART_MANAGER_KUBE_CONTEXT": environment.context,
                    "CHART_MANAGER_PROVIDER_TYPE": environment.provider_type,
                }
            )
        self.runner.run(
            command,
            cwd=self.root,
            capture=False,
            timeout=self.timeout,
            env=env,
        )


__all__ = ["HookPhase", "ProvisioningHookRunner"]
