"""Shared, strict expansion of the supported kubeconform location variables."""

from __future__ import annotations

import re

from chart_manager.plumbing.errors import SpecError

_VARIABLES = frozenset({"Group", "ResourceKind", "ResourceAPIVersion"})


def validate_schema_location(template: str) -> None:
    """Reject unsupported or malformed template expressions before schema lookup."""
    remainder = template
    for name in _VARIABLES:
        remainder = remainder.replace("{{." + name + "}}", "")
    if "{{" in remainder or "}}" in remainder:
        expressions = re.findall(r"\{\{.*?\}\}", remainder)
        raise SpecError(
            f"unsupported schema location expression in {template!r}: "
            f"{', '.join(expressions) or remainder}; supported variables: "
            + ", ".join("{{." + name + "}}" for name in sorted(_VARIABLES))
        )


def expand_schema_location(template: str, *, group: str, version: str, kind: str) -> str:
    """Expand one core or grouped GVK, rejecting unknown variables."""
    validate_schema_location(template)
    values = {
        "Group": group or version,
        "ResourceKind": kind.lower(),
        "ResourceAPIVersion": version,
    }
    for name, value in values.items():
        template = template.replace("{{." + name + "}}", value)
    return template
