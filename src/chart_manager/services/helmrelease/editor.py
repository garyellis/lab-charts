from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chart_manager.plumbing.errors import ChartManagerError

from .scanner import is_helmrelease


@dataclass(frozen=True)
class EditResult:
    path: Path
    changed_docs: int


def _editor_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    # Match common Flux repo formatting: don't rewrap long lines, keep the
    # block-style nesting humans expect to review in PRs.
    yaml.width = 4096
    return yaml


def set_version(
    file_path: Path,
    *,
    chart_name: str,
    new_version: str,
) -> EditResult:
    """Rewrite `.spec.chart.spec.version` for every matching HelmRelease in `file_path`."""
    yaml = _editor_yaml()
    try:
        docs = list(yaml.load_all(file_path.read_text()))
    except YAMLError as exc:
        raise ChartManagerError(f"failed to parse {file_path}: {exc}") from exc

    changed = 0
    for doc in docs:
        if not is_helmrelease(doc):
            continue
        inner = _chart_spec_inner(doc)
        if inner is None:
            continue
        if inner.get("chart") != chart_name:
            continue
        if str(inner.get("version")) == new_version:
            continue
        inner["version"] = new_version
        changed += 1

    if changed == 0:
        return EditResult(path=file_path, changed_docs=0)

    buf = io.StringIO()
    yaml.dump_all(docs, buf)
    file_path.write_text(buf.getvalue())
    return EditResult(path=file_path, changed_docs=changed)


def _chart_spec_inner(doc: Any) -> dict[str, Any] | None:
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
