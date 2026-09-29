"""In-place version edits for HelmRelease YAML files."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chart_manager.plumbing.errors import ChartManagerError, YamlError
from chart_manager.plumbing.yaml_files import edit_yaml_documents

from .scanner import is_helmrelease


@dataclass(frozen=True)
class EditResult:
    """Result of editing one file: how many docs were rewritten."""

    path: Path
    changed_docs: int


def set_version(
    file_path: Path,
    *,
    chart_name: str,
    new_version: str,
) -> EditResult:
    """Rewrite `.spec.chart.spec.version` for every matching HelmRelease in `file_path`."""
    changed = 0
    def edit(docs: list[Any]) -> None:
        nonlocal changed
        for doc in docs:
            if not is_helmrelease(doc):
                continue
            inner = _chart_spec_inner(doc)
            if inner is None or inner.get("chart") != chart_name:
                continue
            if str(inner.get("version")) == new_version:
                continue
            inner["version"] = new_version
            changed += 1

    try:
        edit_yaml_documents(file_path, edit)
    except YamlError as exc:
        raise ChartManagerError(f"failed to edit {file_path}: {exc}") from exc
    return EditResult(path=file_path, changed_docs=changed)


def _chart_spec_inner(doc: Any) -> dict[str, Any] | None:
    """Return the `.spec.chart.spec` mapping, or None if the shape is absent."""
    if not isinstance(doc, dict):
        return None
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    chart = spec.get("chart")
    if not isinstance(chart, dict):
        return None
    inner = chart.get("spec")
    return inner if isinstance(inner, dict) else None
