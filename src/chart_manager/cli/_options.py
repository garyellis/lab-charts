"""Flag declarations more than one command group spells the same way.

Only the genuinely shared ones live here. A flag used by a single group is
declared in that group's module, where its help text sits next to the command
it describes -- collecting every option in one file would put `--stack`'s
wording as far from `local up` as it is possible to get.

The bar for moving one here is that two groups must agree on it forever.
Anything else stays with its command.
"""

from __future__ import annotations

from typing import Annotated

import typer

#: The kind cluster a command addresses; `chart test` may create it.
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


def provision_hooks_enabled(override: bool | None, *, ci: bool) -> bool:
    """Resolve the explicit dual flag over the conventional CI default."""
    return override if override is not None else not ci


__all__ = [
    "ClusterNameOption",
    "ProvisionHooksOption",
    "provision_hooks_enabled",
]
