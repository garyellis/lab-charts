"""Advisory drift detection for the repository's Kind cluster shape."""

from __future__ import annotations

from pathlib import Path

import yaml

from chart_manager.integrations.kind import Kind
from chart_manager.plumbing.errors import ChartManagerError, ExternalCommandError
from chart_manager.services.progress import ProgressCallback, warn

KIND_CONFIG_FILENAME = "kind-config.yaml"


def kind_config_host_ports(kind_config: Path) -> set[int]:
    """Parse `extraPortMappings[].hostPort` from a kind-config file.

    Returns the empty set if the file is missing / malformed -- in that
    case there's nothing to compare against, so the drift check is a
    no-op. Limited to control-plane node mapping which is the only one
    kind-config.yaml currently declares.

    Takes the path rather than reading `self.root` so the parse is a pure
    function of its argument: the only thing worth testing here is the
    yaml walk, and it should not need a DevelopmentClusterService to reach.
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
    config: Path | None = None,
) -> None:
    """Diff the kind-config host ports against the live container.

    Kind bakes `extraPortMappings` into the node container spec at
    cluster-create time. Editing kind-config.yaml then `local down`
    + `local up` does NOT re-apply the mapping (docker start preserves
    the old container spec). Detect that and print a warning row so the
    dev knows a `local reset` is required.
    """
    expected = kind_config_host_ports(config or root / KIND_CONFIG_FILENAME)
    if not expected:
        return
    try:
        live = kind.container_host_ports(cluster_name)
    except (ExternalCommandError, ChartManagerError) as exc:
        progress(warn(f"could not inspect container port mappings ({exc}); skipping drift check"))
        return
    missing = expected - live
    if not missing:
        return
    progress(
        warn(
            f"kind cluster port mappings do not match kind-config.yaml "
            f"(missing host ports: {sorted(missing)}); run "
            "'chart-manager local reset <same-target>' to apply "
            f"(current cluster: {cluster_name})."
        )
    )
