"""Flag declarations more than one command group spells the same way.

Only the genuinely shared ones live here. A flag used by a single group is
declared in that group's module, where its help text sits next to the command
it describes -- collecting every option in one file would put `--stack`'s
wording as far from `local up` as it is possible to get.

The bar for moving one here is that two groups must agree on it forever:
`--root` is the global fallback's per-command override and has to read
identically on all eighteen commands that take it, and `--cluster-name`
addresses the same kind cluster from `chart test` and from
`grafana dashboard export`. Anything else stays with its command.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

#: Per-command override of the global `--root`. See `main._root_default_map`
#: for how the global value reaches it: through Click's `default_map`, which
#: sits below the command line, so naming it here still wins.
RootOption = Annotated[Path, typer.Option("--root", help="Repository root.")]

#: The kind cluster a command addresses. Shared by `chart test` (which may
#: create it) and `grafana dashboard export` (which port-forwards into it).
ClusterNameOption = Annotated[str, typer.Option("--cluster-name", help="kind cluster name.")]

ProvisionHooksOption = Annotated[
    bool | None,
    typer.Option(
        "--run-provision-hooks/--no-run-provision-hooks",
        help=(
            "Run trusted repository provisioning hooks. Defaults on locally and off "
            "when CI is 1/true/yes/on. Hooks execute with your user permissions."
        ),
    ),
]


def provision_hooks_enabled(override: bool | None) -> bool:
    """Resolve the explicit dual flag over the conventional CI default."""
    if override is not None:
        return override
    return os.environ.get("CI", "").strip().lower() not in {"1", "true", "yes", "on"}


__all__ = [
    "ClusterNameOption",
    "ProvisionHooksOption",
    "RootOption",
    "provision_hooks_enabled",
]
