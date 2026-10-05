"""Run `upgrade-finalize`: bump the wrapper version and changelog after Renovate's edits."""

from __future__ import annotations

import json
import logging
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chart_manager.commands.upgrade.models import (
    FinalizeRequest,
    FinalizeResult,
    UpdateMetadata,
    UpgradeError,
)
from chart_manager.commands.upgrade.paths import (
    CHART_FILE,
    resolve_chart_path,
    safe_output_path,
)
from chart_manager.integrations.git import Git
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError, MissingToolError, YamlError
from chart_manager.plumbing.semver import SemVer, parse_bare_version
from chart_manager.plumbing.yaml_files import (
    edit_yaml_documents,
    load_yaml_file,
    parse_yaml_mapping,
)
from chart_manager.shared.workspace import RepositoryWorkspace

#: Inside Renovate's checkout, stderr is the only record of a finalize run.
_LOG = logging.getLogger(__name__)

_HEADING = re.compile(r"^##\s")
_BASELINE_REF = "HEAD"
_CHANGELOG_FILE = "changelog.md"


def wrapper_version(value: object, *, source: str) -> SemVer:
    """Parse a wrapper chart version, which must be a strict x.y.z."""
    try:
        return parse_bare_version(value)
    except ValueError as exc:
        raise UpgradeError(f"{source} must be a strict x.y.z version, got {value!r}") from exc


#: The callback data Renovate writes for `_updates_from_data`; packageFile is
#: carried for a future multi-chart run.
DATA_FILE_TEMPLATE = (
    '{"updates":['
    "{{#each upgrades}}"
    '{"depName":"{{depName}}","currentValue":"{{currentValue}}",'
    '"newValue":"{{newValue}}","manager":"{{manager}}",'
    '"datasource":"{{datasource}}","updateType":"{{updateType}}",'
    '"packageFile":"{{packageFile}}"}'
    "{{#unless @last}},{{/unless}}"
    "{{/each}}"
    "]}"
)


def _updates_from_data(data: Mapping[str, Any]) -> tuple[UpdateMetadata, ...]:
    """Parse the updates `DATA_FILE_TEMPLATE` renders."""
    raw = data.get("updates", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise UpgradeError("Renovate update data must contain an updates array")
    updates: list[UpdateMetadata] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise UpgradeError("each Renovate update entry must be an object")
        updates.append(
            UpdateMetadata(
                dependency=str(item.get("depName", "")),
                current_version=str(item.get("currentValue", "")),
                new_version=str(item.get("newValue", "")),
                manager=str(item.get("manager", "")),
                datasource=str(item.get("datasource", "")),
                update_type=str(item.get("updateType", "")),
            )
        )
    return tuple(dict.fromkeys(updates))


def load_update_data(
    path: Path,
    *,
    max_bytes: int = 1024 * 1024,
) -> Mapping[str, Any]:
    """Load Renovate's callback data file, rejecting symlinks, non-regular and oversized files.

    Renovate writes it in its own temporary directory, so it is not checked against the checkout.
    """
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as exc:
        raise UpgradeError(f"Renovate data file does not exist: {path}") from exc
    if path.is_symlink():
        raise UpgradeError(f"Renovate data file must not be a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise UpgradeError(f"Renovate data file must be a regular file: {path}")
    if metadata.st_size > max_bytes:
        raise UpgradeError(
            f"Renovate data file exceeds {max_bytes} byte safety limit: {path}"
        )
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"invalid Renovate data file {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise UpgradeError("Renovate data file must contain a JSON object")
    return value


@dataclass(frozen=True)
class _Bump:
    """The wrapper version finalize moves to, and the updates that justify it."""

    previous: SemVer
    target: SemVer
    major: bool
    updates: tuple[UpdateMetadata, ...]


def run(
    request: FinalizeRequest,
    *,
    workspace: RepositoryWorkspace,
    runner: CommandRunner,
) -> FinalizeResult:
    """Finalize Renovate's edits without trusting an upstream wrapper version."""
    root, chart_path, _ = resolve_chart_path(
        workspace.root,
        request.chart_path,
        charts_dir=workspace.spec.charts_dir,
    )
    chart_rel = chart_path.relative_to(root)
    _LOG.info(
        "upgrade finalize started: chart=%s path=%s baseline_ref=%s updates=0 dry_run=False",
        chart_path.name,
        chart_rel.as_posix(),
        _BASELINE_REF,
    )
    baseline_file = chart_rel / CHART_FILE
    try:
        baseline_text = Git(root, runner).show(_BASELINE_REF, baseline_file)
    except MissingToolError:
        raise
    except ExternalCommandError as exc:
        raise UpgradeError(
            f"cannot read baseline {_BASELINE_REF}:{baseline_file.as_posix()}: "
            f"{exc.stderr.strip()}"
        ) from exc
    try:
        baseline_doc = parse_yaml_mapping(baseline_text, source="baseline Chart.yaml")
        current = load_yaml_file(chart_path / CHART_FILE)
    except YamlError as exc:
        raise UpgradeError(f"invalid current or baseline Chart.yaml: {exc}") from exc
    baseline_version = wrapper_version(
        baseline_doc.get("version"), source="baseline wrapper version"
    )
    current_version = wrapper_version(current.get("version"), source="current wrapper version")
    updates = _updates_from_data(request.update_data)
    if not updates:
        # The inferred set decides the bump and changelog but cannot see an image
        # update outside `dependencies:`, so say when it fires.
        updates = _chart_dependency_diff(baseline_doc, current)
        _LOG.warning(
            "no Renovate update metadata; inferring updates from the Chart.yaml "
            "dependency diff: chart=%s inferred=%d",
            chart_path.name,
            len(updates),
        )
    bump = _decide(baseline_version, current_version, updates)
    if bump is None:
        _LOG.info(
            "upgrade finalize finished: chart=%s version=%s bump=none changed=False "
            "(no qualifying update)",
            chart_path.name,
            current_version,
        )
        return FinalizeResult(
            chart=chart_path.name,
            previous_version=str(baseline_version),
            version=str(current_version),
            changed=False,
        )
    chart_changed, changelog_changed = _write(chart_path, current_version, bump)
    _LOG.info(
        "upgrade finalize finished: chart=%s previous=%s version=%s bump=%s "
        "changed=%s files=%d qualifying=%d dry_run=False",
        chart_path.name,
        bump.previous,
        bump.target,
        "major" if bump.major else "patch",
        chart_changed or changelog_changed,
        chart_changed + changelog_changed,
        len(bump.updates),
    )
    return FinalizeResult(
        chart=chart_path.name,
        previous_version=str(bump.previous),
        version=str(bump.target),
        changed=chart_changed or changelog_changed,
    )


def _decide(
    baseline: SemVer, current: SemVer, updates: Sequence[UpdateMetadata]
) -> _Bump | None:
    """Pick the target version from the qualifying updates; None when nothing qualifies."""
    qualifying = tuple(update for update in updates if update.qualifies)
    for update in qualifying:
        if not update.dependency or not update.current_version or not update.new_version:
            raise UpgradeError(
                "qualifying Renovate updates require dependency, currentValue, "
                "and newValue metadata"
            )
    if not qualifying:
        if current != baseline:
            raise UpgradeError(
                "wrapper version diverged from baseline without a qualifying "
                f"image or Helm dependency update: {current}"
            )
        return None
    major = any(_is_major(update) for update in qualifying)
    target = (
        SemVer(baseline.major + 1, 0, 0)
        if major
        else SemVer(baseline.major, baseline.minor, baseline.patch + 1)
    )
    if current not in {baseline, target}:
        raise UpgradeError(
            f"wrapper version diverged from baseline {baseline} and target {target}: "
            f"{current}"
        )
    return _Bump(previous=baseline, target=target, major=major, updates=qualifying)


def _write(chart_path: Path, current: SemVer, bump: _Bump) -> tuple[bool, bool]:
    """Write the target version and its changelog section; report which files changed."""
    target = str(bump.target)
    heading = f"## {target}"
    chart_changed = current != bump.target
    chart_file = safe_output_path(chart_path, CHART_FILE)
    changelog_file = safe_output_path(chart_path, _CHANGELOG_FILE)
    old_changelog = (
        changelog_file.read_text(encoding="utf-8") if changelog_file.exists() else ""
    )
    entry = _changelog_entry(heading, bump.updates)
    new_changelog = _apply_changelog_entry(old_changelog, heading, entry)
    if chart_changed:
        def update_version(documents: list[Any]) -> None:
            if len(documents) != 1 or not isinstance(documents[0], dict):
                raise YamlError("current Chart.yaml must contain one mapping document")
            documents[0]["version"] = target

        try:
            edit_yaml_documents(chart_file, update_version)
        except YamlError as exc:
            raise UpgradeError(f"invalid current Chart.yaml: {exc}") from exc
    if new_changelog != old_changelog:
        changelog_file.write_text(new_changelog, encoding="utf-8")
    return chart_changed, new_changelog != old_changelog


def _is_major(update: UpdateMetadata) -> bool:
    if update.update_type.lower() == "major":
        return True
    old = _loose_major(update.current_version)
    new = _loose_major(update.new_version)
    return old is not None and new is not None and new > old


def _chart_dependency_diff(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[UpdateMetadata, ...]:
    """Infer Helm dependency changes when callback metadata is unavailable."""

    def dependencies(document: Mapping[str, Any]) -> dict[str, str]:
        raw = document.get("dependencies", ())
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise UpgradeError("Chart.yaml dependencies must be an array")
        found: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise UpgradeError("Chart.yaml dependency entries must be mappings")
            name, version = item.get("name"), item.get("version")
            if isinstance(name, str) and isinstance(version, str):
                found[name] = version
        return found

    before, after = dependencies(baseline), dependencies(current)
    return tuple(
        UpdateMetadata(
            dependency=name,
            current_version=before[name],
            new_version=after[name],
            manager="helmv3",
            datasource="helm",
        )
        for name in sorted(before.keys() & after.keys())
        if before[name] != after[name]
    )


def _loose_major(value: str) -> int | None:
    match = re.search(r"(?<!\d)(\d+)(?:\.\d+)", value)
    return int(match.group(1)) if match else None


def _apply_changelog_entry(old: str, heading: str, entry: str) -> str:
    """Return the changelog with ``heading``'s section replaced by ``entry``.

    The section is rewritten, not skipped, because a newer update on an open branch keeps the
    heading (same baseline, same target) but changes the updates under it.
    """
    lines = old.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        return entry + old.lstrip("\n") if old else entry
    end = next(
        (index for index in range(start + 1, len(lines)) if _HEADING.match(lines[index])),
        len(lines),
    )
    return "".join(lines[:start]) + entry + "".join(lines[end:])


def _changelog_entry(heading: str, updates: Sequence[UpdateMetadata]) -> str:
    lines = [heading, ""]
    for update in sorted(
        updates,
        key=lambda item: (
            item.datasource.lower(),
            item.manager.lower(),
            item.dependency.lower(),
            item.new_version,
        ),
    ):
        lines.append(f"- {update.dependency}: {update.current_version} -> {update.new_version}")
    return "\n".join(lines) + "\n\n"
