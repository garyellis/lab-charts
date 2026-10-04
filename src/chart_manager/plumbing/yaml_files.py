"""The single YAML-library boundary for chart-manager production code."""

from __future__ import annotations

import copy
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from chart_manager.plumbing.errors import YamlError

YamlDocumentEditor = Callable[[list[Any]], None]


def _safe_yaml() -> YAML:
    """Return the YAML 1.2 safe loader used for read-only documents."""
    yaml = YAML(typ="safe")
    # Kubernetes OpenAPI schemas legitimately use the scalar ``=`` in enum
    # lists. ruamel resolves that token to YAML's standard ``value`` tag but
    # its safe constructor does not register a handler for the tag, causing
    # rendered Prometheus CRDs to fail inventory. Preserve the scalar value;
    # no application-specific object construction is involved.
    yaml.constructor.add_constructor(
        "tag:yaml.org,2002:value",
        lambda constructor, node: constructor.construct_scalar(node),
    )
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _round_trip_yaml(*, explicit_start: bool = False) -> YAML:
    """Return the consistently configured minimal-diff YAML editor."""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.explicit_start = explicit_start
    return yaml


def _decode_yaml(value: str | bytes, *, source: str) -> str:
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise YamlError(f"failed to decode YAML from {source} as UTF-8: {exc}") from exc


def _read_yaml_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise YamlError(f"failed to decode YAML file {path} as UTF-8: {exc}") from exc
    except OSError as exc:
        raise YamlError(f"failed to read YAML file {path}: {exc}") from exc


def parse_yaml(value: str | bytes, *, source: str = "input") -> Any:
    """Parse one YAML document from UTF-8 text or bytes."""
    text = _decode_yaml(value, source=source)
    try:
        return _safe_yaml().load(text)
    except YAMLError as exc:
        raise YamlError(f"failed to parse YAML from {source}: {exc}") from exc


def parse_yaml_mapping(value: str | bytes, *, source: str = "input") -> dict[str, Any]:
    """Parse one mapping document; an empty document becomes an empty mapping."""
    document = parse_yaml(value, source=source)
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise YamlError(f"YAML from {source} must contain a mapping")
    return document


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read one UTF-8 YAML mapping; an empty file becomes an empty mapping."""
    return parse_yaml_mapping(_read_yaml_text(path), source=str(path))


def load_yaml_documents(path: Path) -> list[Any]:
    """Read all YAML documents from one UTF-8 file."""
    return parse_yaml_documents(_read_yaml_text(path), source=str(path))


def parse_yaml_documents(value: str | bytes, *, source: str = "input") -> list[Any]:
    """Parse every YAML document in UTF-8 text or bytes."""
    text = _decode_yaml(value, source=source)
    try:
        return list(_safe_yaml().load_all(text))
    except YAMLError as exc:
        raise YamlError(f"failed to parse YAML from {source}: {exc}") from exc


def dump_yaml(document: Any) -> str:
    """Serialize one YAML document with the shared stable output policy."""
    output = io.StringIO()
    try:
        _safe_yaml().dump(document, output)
    except (YAMLError, UnicodeError, TypeError, ValueError) as exc:
        raise YamlError(f"failed to serialize YAML: {exc}") from exc
    return output.getvalue()


def edit_yaml_documents(path: Path, editor: YamlDocumentEditor) -> bool:
    """Round-trip edit YAML documents and write only when their values change.

    The callback mutates the supplied document list. Equality with a deep copy
    taken before the callback determines whether a write is necessary.
    """
    text = _read_yaml_text(path)
    yaml = _round_trip_yaml(explicit_start=text.startswith("---"))
    try:
        documents = list(yaml.load_all(text))
    except YAMLError as exc:
        raise YamlError(f"failed to parse YAML from {path}: {exc}") from exc
    original = copy.deepcopy(documents)
    editor(documents)
    if documents == original:
        return False

    output = io.StringIO()
    try:
        yaml.dump_all(documents, output)
        path.write_text(output.getvalue(), encoding="utf-8")
    except (YAMLError, UnicodeError, TypeError, ValueError) as exc:
        raise YamlError(f"failed to serialize YAML for {path}: {exc}") from exc
    except OSError as exc:
        raise YamlError(f"failed to write YAML file {path}: {exc}") from exc
    return True


__all__ = [
    "dump_yaml",
    "edit_yaml_documents",
    "load_yaml_documents",
    "load_yaml_file",
    "parse_yaml",
    "parse_yaml_mapping",
]
