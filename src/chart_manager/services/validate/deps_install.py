"""Tool installation via mise.

Installs the validate pipeline's pinned tool versions (helm primary +
alternates, kubeconform, kyverno, uv) by shelling `mise install`. Each
version is attempted independently; failures downgrade to warnings (with
the upstream release URL) rather than raising so one bad version doesn't
abort the rest of a deps-install run.

To add a new tool: append its entry to ``_TOOL_VERSIONS`` and its
release-URL template to ``_RELEASE_URLS``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError

# Default version is first; alternates follow. Multi-version entries
# (currently only helm) keep older SDKs installed for charts pinned to
# them via validate-spec.yaml's helm_version field.
HELM_VERSIONS: tuple[str, ...] = ("4.1.3", "3.20.0")
KUBECONFORM_VERSIONS: tuple[str, ...] = ("0.8.0",)
KYVERNO_VERSIONS: tuple[str, ...] = ("1.18.1",)
UV_VERSIONS: tuple[str, ...] = ("0.11.7",)

_TOOL_VERSIONS: dict[str, tuple[str, ...]] = {
    "helm": HELM_VERSIONS,
    "kubeconform": KUBECONFORM_VERSIONS,
    "kyverno": KYVERNO_VERSIONS,
    "uv": UV_VERSIONS,
}

# uv tags don't carry the `v` prefix; the rest do.
_RELEASE_URLS: dict[str, str] = {
    "helm": "https://github.com/helm/helm/releases/tag/v{version}",
    "kubeconform": "https://github.com/yannh/kubeconform/releases/tag/v{version}",
    "kyverno": "https://github.com/kyverno/kyverno/releases/tag/v{version}",
    "uv": "https://github.com/astral-sh/uv/releases/tag/{version}",
}

# Guard against drift: every pinned tool must have a release-URL template
# and vice versa. Catches "added a tool to one map but not the other" at
# import time instead of at first warn-emission.
assert set(_TOOL_VERSIONS) == set(_RELEASE_URLS), (
    "deps_install registries out of sync: "
    f"_TOOL_VERSIONS={sorted(_TOOL_VERSIONS)} "
    f"_RELEASE_URLS={sorted(_RELEASE_URLS)}"
)


# Public list of tools known to this module. CLI surfaces consume this
# to keep --tool's allowed-values list in sync with the registry.
KNOWN_TOOLS: tuple[str, ...] = tuple(_TOOL_VERSIONS)


@dataclass(frozen=True)
class InstallResult:
    tool: str
    version: str
    success: bool
    detail: str | None = None


def release_url(tool: str, version: str) -> str:
    """Return the upstream GitHub release-tag URL for ``tool@version``."""
    return _RELEASE_URLS[tool].format(version=version)


def install_one(
    runner: CommandRunner,
    tool: str,
    *,
    on_warn: Callable[[str], None] = print,
) -> list[InstallResult]:
    """Install every pinned version of ``tool`` via ``mise install``.

    Per-version failures call ``on_warn`` with a message naming the
    tool, version, and upstream release URL, then continue. Raises
    ``ValueError`` only for an unknown tool name (configuration bug,
    not a runtime fault).
    """
    if tool not in _TOOL_VERSIONS:
        raise ValueError(
            f"unknown tool: {tool!r} (known: {sorted(_TOOL_VERSIONS)})"
        )

    results: list[InstallResult] = []
    for version in _TOOL_VERSIONS[tool]:
        try:
            runner.run(["mise", "install", f"{tool}@{version}"], capture=False)
            results.append(InstallResult(tool=tool, version=version, success=True))
        except ExternalCommandError as exc:
            url = release_url(tool, version)
            detail = str(exc)
            on_warn(
                f"warning: failed to install {tool}@{version}: {detail}\n"
                f"  manual install: {url}"
            )
            results.append(
                InstallResult(
                    tool=tool, version=version, success=False, detail=detail
                )
            )
    return results


def install_all(
    runner: CommandRunner,
    *,
    on_warn: Callable[[str], None] = print,
) -> list[InstallResult]:
    """Install every pinned version of every known tool."""
    aggregated: list[InstallResult] = []
    for tool in _TOOL_VERSIONS:
        aggregated.extend(install_one(runner, tool, on_warn=on_warn))
    return aggregated


