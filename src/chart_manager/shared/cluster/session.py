"""One kind cluster: provision or attach to it, and stop or tear it down.

A `Session` is fixed once made: helm and kubectl are pinned to the cluster's context, so
nothing that runs against it can fall back to the workstation's ambient kubeconfig.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind, kind_context
from chart_manager.integrations.kubectl import Kubectl
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.progress import ProgressCallback, emit, step
from chart_manager.settings import Settings


@dataclass(frozen=True)
class Session:
    """A kind cluster and the clients addressed at it."""

    name: str
    context: str
    kind: Kind
    helm: Helm
    kubectl: Kubectl


def provision(
    cluster: LocalCluster,
    *,
    root: Path,
    name: str,
    run_hooks: bool,
    runner: CommandRunner,
    settings: Settings,
    replace: bool = False,
    progress: ProgressCallback | None = None,
) -> Session:
    """Create or start the cluster, then wait until its apiserver answers.

    With `run_hooks`, the authored preProvision hook runs first and the postProvision hook
    runs once the apiserver answers; the apiserver is then waited on again. With `replace`,
    an existing cluster is deleted after the preProvision hook.
    """
    session = attach(name, runner=runner, settings=settings)
    hooks = cluster.spec.cluster.hooks if run_hooks else None
    env = {
        "CHART_MANAGER_ROOT": str(root.resolve()),
        "CHART_MANAGER_CLUSTER_NAME": name,
        "CHART_MANAGER_KIND_CONFIG": str(kind_config_path(root, cluster)),
    }
    if hooks is not None and hooks.pre_provision is not None:
        _hook(
            hooks.pre_provision,
            root,
            {**env, "CHART_MANAGER_HOOK_PHASE": "preProvision"},
            runner=runner,
            settings=settings,
        )
    if replace:
        emit(progress, step("Deleting cluster", name))
        teardown(session)
    emit(progress, step("Ensuring cluster", name))
    session.kind.ensure_cluster(name, config=kind_config_path(root, cluster))
    emit(progress, step("Waiting for kube-apiserver"))
    session.kubectl.wait_apiserver_ready()
    if hooks is not None and hooks.post_provision is not None:
        post = {
            **env,
            "CHART_MANAGER_HOOK_PHASE": "postProvision",
            "CHART_MANAGER_KUBE_CONTEXT": session.context,
            "CHART_MANAGER_PROVIDER_TYPE": "kind",
        }
        _hook(hooks.post_provision, root, post, runner=runner, settings=settings)
        emit(progress, step("Waiting for kube-apiserver after post-provision hook"))
        session.kubectl.wait_apiserver_ready()
    return session


def attach(name: str, *, runner: CommandRunner, settings: Settings) -> Session:
    """Address the cluster `name` without creating or checking it."""
    context = kind_context(name)
    return Session(
        name=name,
        context=context,
        kind=Kind(runner, docker_host=settings.docker_host, timeout=settings.command_timeout),
        helm=Helm(runner, context=context),
        kubectl=Kubectl(runner, context=context, timeout=settings.command_timeout),
    )


def find(name: str, *, runner: CommandRunner, settings: Settings) -> Session | None:
    """The session for `name` if that cluster exists, else None."""
    session = attach(name, runner=runner, settings=settings)
    return session if name in session.kind.clusters() else None


def stop(session: Session) -> bool:
    """Stop the cluster's node containers, keeping its state; False if none were running."""
    return session.kind.stop_cluster(session.name)


def teardown(session: Session) -> bool:
    """Delete the cluster; False if it did not exist."""
    return session.kind.delete_cluster(session.name)


def _hook(
    argv: list[str], root: Path, env: dict[str, str], *, runner: CommandRunner, settings: Settings
) -> None:
    runner.run(argv, cwd=root.resolve(), capture=False, timeout=settings.command_timeout, env=env)


def kind_config_path(root: Path, cluster: LocalCluster) -> Path:
    """The LocalCluster's authored kind config, resolved against the root."""
    return (root / cluster.spec.cluster.config).resolve()
