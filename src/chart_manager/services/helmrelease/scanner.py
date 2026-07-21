"""
Scan file for FluxCD HelmRelease resources
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chart_manager.plumbing.errors import ChartManagerError

_YAML_SUFFIXES = (".yaml", ".yml")


@dataclass(frozen=True)
class HelmReleaseMatch:
    """A HelmRelease document in a file that matched the chart filter."""

    path: Path
    doc_index: int
    name: str
    namespace: str | None
    current_version: str | None


def _yaml_loader() -> YAML:
    # `rt` (round-trip) preserves comments, key order, and quoting style —
    # essential for in-place edits on GitOps repos humans also review.
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml


def is_helmrelease(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    kind = doc.get("kind")
    if kind != "HelmRelease":
        return False
    api_version = doc.get("apiVersion", "")
    # Flux helm-controller GA is helm.toolkit.fluxcd.io/v2; the v2beta1/v2beta2
    # tracks are still in the wild. Match by group prefix rather than pinning
    # a version so older repos don't silently skip.
    return isinstance(api_version, str) and api_version.startswith("helm.toolkit.fluxcd.io/")


def _chart_fields(doc: dict[str, Any]) -> tuple[str | None, str | None]:
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None, None
    chart = spec.get("chart")
    if not isinstance(chart, dict):
        return None, None
    inner = chart.get("spec")
    if not isinstance(inner, dict):
        return None, None
    chart_name = inner.get("chart")
    version = inner.get("version")
    return (
        str(chart_name) if chart_name is not None else None,
        str(version) if version is not None else None,
    )


def _iter_yaml_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in _YAML_SUFFIXES else []
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in _YAML_SUFFIXES:
            files.append(path)
    return sorted(files)


def scan(path: Path, *, chart_name: str) -> list[HelmReleaseMatch]:
    """Find HelmRelease docs under `path` whose `.spec.chart.spec.chart == chart_name`."""
    if not path.exists():
        raise ChartManagerError(f"scan path does not exist: {path}")
    yaml = _yaml_loader()
    matches: list[HelmReleaseMatch] = []
    for file_path in _iter_yaml_files(path):
        try:
            docs = list(yaml.load_all(file_path.read_text()))
        except YAMLError as exc:
            raise ChartManagerError(f"failed to parse {file_path}: {exc}") from exc
        for index, doc in enumerate(docs):
            if not is_helmrelease(doc):
                continue
            found_chart, version = _chart_fields(doc)
            if found_chart != chart_name:
                continue
            metadata = doc.get("metadata") if isinstance(doc, dict) else None
            name = ""
            namespace: str | None = None
            if isinstance(metadata, dict):
                name = str(metadata.get("name", ""))
                ns = metadata.get("namespace")
                namespace = str(ns) if ns is not None else None
            matches.append(
                HelmReleaseMatch(
                    path=file_path,
                    doc_index=index,
                    name=name,
                    namespace=namespace,
                    current_version=version,
                )
            )
    return matches
