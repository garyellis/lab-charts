"""Deterministic, replay-safe wrapper version and changelog finalization."""

from __future__ import annotations

import io
import json
import logging
import re
import stat
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ruamel.yaml import YAML

from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.services.upgrader.errors import UpgradeError
from chart_manager.services.upgrader.models import (
    FinalizeRequest,
    FinalizeResult,
    UpdateMetadata,
)
from chart_manager.services.upgrader.paths import resolve_chart_path, safe_output_path
from chart_manager.settings import DEFAULT_CHARTS_DIR

#: This runs as a Renovate post-upgrade task inside Renovate's own checkout,
#: where nothing renders narration and the only surviving record of the run is
#: whatever reached stderr.
_LOG = logging.getLogger(__name__)

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_HEADING = re.compile(r"^##\s")


class BaselineReader(Protocol):
    """Read repository files as they existed at a git revision."""

    def read(self, root: Path, revision: str, relative_path: Path) -> str:
        """Return file contents or raise ``UpgradeError``."""
        ...


class GitBaselineReader:
    """Baseline reader backed by the repository's command-runner seam."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or SubprocessRunner()

    def read(self, root: Path, revision: str, relative_path: Path) -> str:
        result = self._runner.run(
            ["git", "show", f"{revision}:{relative_path.as_posix()}"],
            cwd=root,
            check=False,
        )
        if result.returncode:
            raise UpgradeError(
                f"cannot read baseline {revision}:{relative_path.as_posix()}: "
                f"{result.stderr.strip()}"
            )
        return result.stdout


def _semver(value: object, *, source: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or (match := _SEMVER.fullmatch(value)) is None:
        raise UpgradeError(f"{source} must be a strict x.y.z version, got {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _updates_from_data(data: Mapping[str, Any] | None) -> tuple[UpdateMetadata, ...]:
    if data is None:
        return ()
    raw: object = data.get("updates", data.get("deps", data.get("dependencies", ())))
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise UpgradeError("Renovate update data must contain an updates array")
    updates: list[UpdateMetadata] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise UpgradeError("each Renovate update entry must be an object")
        updates.append(UpdateMetadata.from_mapping(item))
    return tuple(updates)


def load_update_data(
    path: Path,
    *,
    max_bytes: int = 1024 * 1024,
) -> Mapping[str, Any]:
    """Safely load an explicitly selected Renovate callback data file.

    Renovate legitimately creates this file in its temporary directory, so
    containment by the checkout is neither required nor a useful trust
    boundary. The caller supplies the exact path; this boundary rejects
    symlinks, non-regular files, and unexpectedly large payloads.
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


class UpgradeFinalizer:
    """Finalize Renovate's edits without trusting an upstream wrapper version."""

    def __init__(
        self,
        baseline: BaselineReader | None = None,
        *,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
    ) -> None:
        self._baseline = baseline or GitBaselineReader()
        self._charts_dir = charts_dir

    def finalize(self, request: FinalizeRequest) -> FinalizeResult:
        root, chart_path, _ = resolve_chart_path(
            request.repo_root,
            request.chart_path,
            charts_dir=self._charts_dir,
        )
        chart_rel = chart_path.relative_to(root)
        _LOG.info(
            "upgrade finalize started: chart=%s path=%s baseline_ref=%s updates=%d "
            "dry_run=%s",
            chart_path.name,
            chart_rel.as_posix(),
            request.baseline_ref,
            len(request.updates),
            request.dry_run,
        )
        baseline_text = self._baseline.read(root, request.baseline_ref, chart_rel / "Chart.yaml")
        yaml = YAML()
        yaml.preserve_quotes = True
        try:
            baseline_doc = yaml.load(baseline_text)
            current = yaml.load((chart_path / "Chart.yaml").read_text(encoding="utf-8"))
        except Exception as exc:
            raise UpgradeError(f"invalid current or baseline Chart.yaml: {exc}") from exc
        if not isinstance(baseline_doc, Mapping) or not isinstance(current, MutableMapping):
            raise UpgradeError("current and baseline Chart.yaml must contain mappings")
        baseline_version = _semver(baseline_doc.get("version"), source="baseline wrapper version")
        current_version = _semver(current.get("version"), source="current wrapper version")
        updates = tuple(
            dict.fromkeys(tuple(request.updates) + _updates_from_data(request.update_data))
        )
        if not updates:
            # Renovate passed no callback metadata, so the update set is
            # reconstructed from the Chart.yaml diff. That inference decides the
            # bump and the changelog body, and it cannot see an image update
            # made outside `dependencies:` -- worth a line when it fires.
            updates = _chart_dependency_diff(baseline_doc, current)
            _LOG.warning(
                "no Renovate update metadata; inferring updates from the Chart.yaml "
                "dependency diff: chart=%s inferred=%d",
                chart_path.name,
                len(updates),
            )
        qualifying = tuple(update for update in updates if update.qualifies)
        for update in qualifying:
            if not update.dependency or not update.current_version or not update.new_version:
                raise UpgradeError(
                    "qualifying Renovate updates require dependency, currentValue, "
                    "and newValue metadata"
                )
        if not qualifying:
            if current_version != baseline_version:
                raise UpgradeError(
                    "wrapper version diverged from baseline without a qualifying "
                    f"image or Helm dependency update: {'.'.join(map(str, current_version))}"
                )
            _LOG.info(
                "upgrade finalize finished: chart=%s version=%s bump=none changed=False "
                "(no qualifying update)",
                chart_path.name,
                ".".join(map(str, current_version)),
            )
            return FinalizeResult(
                chart=chart_path.name,
                previous_version=".".join(map(str, baseline_version)),
                version=".".join(map(str, current_version)),
                bump=None,
                changed=False,
                updates=updates,
            )
        major = any(_is_major(update) for update in qualifying)
        target_tuple = (
            (baseline_version[0] + 1, 0, 0)
            if major
            else (baseline_version[0], baseline_version[1], baseline_version[2] + 1)
        )
        baseline_value = ".".join(map(str, baseline_version))
        target = ".".join(map(str, target_tuple))
        if current_version not in {baseline_version, target_tuple}:
            raise UpgradeError(
                f"wrapper version diverged from baseline {baseline_value} and target {target}: "
                f"{'.'.join(map(str, current_version))}"
            )
        heading = request.target_heading or f"## {target}"
        chart_changed = current_version != target_tuple
        chart_file = safe_output_path(chart_path, "Chart.yaml")
        changelog_file = safe_output_path(chart_path, "changelog.md")
        old_changelog = (
            changelog_file.read_text(encoding="utf-8") if changelog_file.exists() else ""
        )
        entry = _changelog_entry(heading, qualifying)
        new_changelog = _apply_changelog_entry(old_changelog, heading, entry)
        changelog_changed = new_changelog != old_changelog
        if not request.dry_run:
            if chart_changed:
                current["version"] = target
                output = io.StringIO()
                yaml.dump(current, output)
                chart_file.write_text(output.getvalue(), encoding="utf-8")
            if changelog_changed:
                changelog_file.write_text(new_changelog, encoding="utf-8")
        files = tuple(
            path
            for changed, path in (
                (chart_changed, chart_file),
                (changelog_changed, changelog_file),
            )
            if changed
        )
        _LOG.info(
            "upgrade finalize finished: chart=%s previous=%s version=%s bump=%s "
            "changed=%s files=%d qualifying=%d dry_run=%s",
            chart_path.name,
            baseline_value,
            target,
            "major" if major else "patch",
            bool(files),
            len(files),
            len(qualifying),
            request.dry_run,
        )
        return FinalizeResult(
            chart=chart_path.name,
            previous_version=baseline_value,
            version=target,
            bump="major" if major else "patch",
            changed=bool(files),
            files=files,
            updates=qualifying,
        )


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

    Replay must be keyed on the section's content, not on the heading alone.
    A newer update can land on an open upgrade branch before it merges: the
    baseline is unchanged, so the target version -- and therefore the heading
    -- stays the same while the update set underneath it does not. Skipping on
    a matching heading would leave Chart.yaml and the changelog disagreeing.
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
        dependency = update.dependency or "dependency"
        lines.append(f"- {dependency}: {update.current_version} -> {update.new_version}")
    return "\n".join(lines) + "\n\n"
