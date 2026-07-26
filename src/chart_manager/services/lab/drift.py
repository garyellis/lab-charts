"""Drift detection between the repo's declared cluster shape and the live one.

Two independent checks, both reached from the converge engine but neither
needing it: host port mappings (advisory, warns) and cilium's pinned
apiserver VIP (hard fail, because the alternative is a silently broken
pod network).

Collaborators are `(kind, helm, progress)` plus the repo root.
`kind_config_host_ports` and `_walk` are pure functions over their
arguments -- they are the parts worth testing directly.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kind import Kind
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.cluster_bootstrap import (
    CILIUM_BOOTSTRAP_CHART,
    CILIUM_BOOTSTRAP_NAMESPACE,
    KIND_CONFIG_FILENAME,
)
from chart_manager.services.progress import ProgressCallback, warn

# `helm get values` for the cilium release surfaces the `k8sServiceHost`
# we passed at install time under the cilium subchart key. Detecting drift
# means walking this exact path -- a missing key is benign (older install,
# or chart restructure) and falls through to the warning branch.
CILIUM_SERVICE_HOST_PATH: Final[tuple[str, ...]] = ("cilium", "k8sServiceHost")


def kind_config_host_ports(kind_config: Path) -> set[int]:
    """Parse `extraPortMappings[].hostPort` from a kind-config file.

    Returns the empty set if the file is missing / malformed -- in that
    case there's nothing to compare against, so the drift check is a
    no-op. Limited to control-plane node mapping which is the only one
    kind-config.yaml currently declares.

    Takes the path rather than reading `self.root` so the parse is a pure
    function of its argument: the only thing worth testing here is the
    yaml walk, and it should not need a LabService to reach.
    """
    if not kind_config.is_file():
        return set()
    try:
        data = yaml.safe_load(kind_config.read_text()) or {}
    except yaml.YAMLError:
        return set()
    ports: set[int] = set()
    for node in data.get("nodes") or []:
        for mapping in (node or {}).get("extraPortMappings") or []:
            host_port = (mapping or {}).get("hostPort")
            if isinstance(host_port, int):
                ports.add(host_port)
    return ports


def warn_on_port_mapping_drift(
    cluster_name: str,
    *,
    kind: Kind,
    root: Path,
    progress: ProgressCallback,
) -> None:
    """Diff the kind-config host ports against the live container.

    Kind bakes `extraPortMappings` into the node container spec at
    cluster-create time. Editing kind-config.yaml then `sandbox down`
    + `sandbox up` does NOT re-apply the mapping (docker start preserves
    the old container spec). Detect that and print a warning row so the
    dev knows a `sandbox delete && sandbox up` is required.
    """
    expected = kind_config_host_ports(root / KIND_CONFIG_FILENAME)
    if not expected:
        return
    try:
        live = kind.container_host_ports(cluster_name)
    except (ExternalCommandError, ChartManagerError) as exc:
        progress(
            warn(
                f"could not inspect container port mappings "
                f"({exc}); skipping drift check"
            )
        )
        return
    missing = expected - live
    if not missing:
        return
    progress(
        warn(
            f"kind cluster port mappings do not match kind-config.yaml "
            f"(missing host ports: {sorted(missing)}); run "
            "'sandbox delete && sandbox up' to apply."
        )
    )


def check_cilium_service_host_drift(
    cluster_name: str,
    *,
    kind: Kind,
    helm: Helm,
    progress: ProgressCallback,
) -> None:
    """Fail loud if cilium's pinned k8sServiceHost no longer matches.

    Best-effort: an unreadable values payload (release just removed,
    kubeconfig drift, etc.) warns and continues -- we don't want a
    diagnostic helper to block the install plan. But a *confirmed*
    mismatch is a hard fail, because cilium with the wrong apiserver
    VIP silently breaks all kube-proxy-replacement traffic.
    """
    try:
        current_ip = kind.control_plane_ip(cluster_name)
    except (ExternalCommandError, ChartManagerError) as exc:
        progress(
            warn(
                f"could not read control-plane IP for drift check "
                f"({exc}); skipping"
            )
        )
        return

    try:
        values = helm.get_values(
            CILIUM_BOOTSTRAP_CHART, namespace=CILIUM_BOOTSTRAP_NAMESPACE
        )
    except ExternalCommandError as exc:
        progress(
            warn(
                f"could not read cilium release values for drift check "
                f"({exc}); skipping"
            )
        )
        return

    installed_ip = _walk(values, CILIUM_SERVICE_HOST_PATH)

    if installed_ip is None:
        progress(
            warn(
                "cilium release has no pinned k8sServiceHost; "
                "skipping drift check"
            )
        )
        return

    if str(installed_ip) != current_ip:
        raise ChartManagerError(
            f"cilium k8sServiceHost drift: installed={installed_ip} "
            f"current={current_ip}; run 'mise run sandbox-delete' then "
            f"'mise run sandbox-up' to recover"
        )


def _walk(data: Mapping[str, Any], path: tuple[str, ...]) -> object | None:
    """Descend into a nested mapping by `path`; return None on any miss.

    "Miss" = a path segment is absent, or an intermediate node is not a
    Mapping. Used for drift detection where the values payload may
    legitimately be older / restructured and we want a single "not
    present" signal rather than a sequence of KeyError / TypeError
    branches. Accepts any Mapping so callers handing us a yaml-loaded
    dict-like aren't forced to copy.
    """
    cursor: object = data
    for key in path:
        if not isinstance(cursor, Mapping) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor
