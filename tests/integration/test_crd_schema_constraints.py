"""Exercise converted constraint fragments using the actual kubeconform validator."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from chart_manager.commands.validate.schemas.crd import _strict_schema
from chart_manager.integrations.kubeconform import Kubeconform

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("field,valid,invalid", [
    ({"type": "boolean", "nullable": True}, None, "false"),
    ({"type": "string", "nullable": True}, None, 7),
    ({"type": "integer", "nullable": True}, None, "7"),
    ({"type": "object", "nullable": True, "properties": {"limit": {"type": "integer"}}},
     None, {"unknown": 3}),
    ({"type": "object", "nullable": True, "properties": {"limit": {"type": "integer"}}},
     {"limit": 3}, {"limit": "bad"}),
    ({"type": "array", "nullable": True, "items": {"type": "integer"}}, None, ["bad"]),
    ({"type": "array", "items": {"type": "integer", "nullable": True}}, [None, 1], ["bad"]),
    ({"type": "object", "additionalProperties": {"type": "integer", "nullable": True}},
     {"key": None}, {"key": "bad"}),
    ({"type": "string", "nullable": True, "enum": ["A"]}, "A", None),
    ({"type": "string", "nullable": True, "enum": ["A", None]}, None, "B"),
    ({"type": "string", "nullable": False}, "A", None),
    ({"type": "object", "nullable": True, "oneOf": [{"required": ["a"]}]}, None, {}),
    ({"x-kubernetes-int-or-string": True}, 80, False),
    ({"x-kubernetes-int-or-string": True}, "http", {}),
    ({"x-kubernetes-int-or-string": True, "nullable": True}, None, 1.5),
])
def test_nullable_preserves_type_and_enum_constraints(tmp_path, field, valid, invalid):
    _assert_verdicts(tmp_path, {"properties": {"value": field}},
                     {"value": valid}, {"value": invalid})


@pytest.mark.parametrize("constraint,valid,invalid", [
    ({"not": {"properties": {"a": {"enum": ["bad"]}}, "required": ["a"]}},
     {"a": "good", "b": "present"}, {"a": "bad", "b": "present"}),
    ({"if": {"properties": {"a": {"enum": ["secure"]}}},
      "then": {"properties": {"b": {"enum": ["tls"]}}},
      "else": {"properties": {"b": {"enum": ["plain"]}}}},
     {"a": "secure", "b": "tls"}, {"a": "secure", "b": "plain"}),
    ({"if": {"properties": {"a": {"enum": ["secure"]}}},
      "then": {"properties": {"b": {"enum": ["tls"]}}},
      "else": {"properties": {"b": {"enum": ["plain"]}}}},
     {"a": "other", "b": "plain"}, {"a": "other", "b": "tls"}),
])
def test_conditionals_preserve_admission_semantics(
    tmp_path: Path, constraint: dict, valid: dict, invalid: dict,
) -> None:
    _assert_verdicts(tmp_path, constraint, valid, invalid)


@pytest.mark.parametrize("keyword", ["oneOf", "anyOf", "allOf"])
def test_nested_composition_preserves_parent_declared_siblings(tmp_path: Path, keyword: str) -> None:
    # The fragment describes only spec.a; the structural parent owns spec.b too.
    constraint = {keyword: [{"properties": {"spec": {
        "properties": {"a": {"enum": ["good"]}}, "required": ["a"],
    }}}]}
    _assert_verdicts(tmp_path, constraint, {"a": "good", "b": "present"},
                     {"a": "bad", "b": "present"}, root_constraint=True)


def _assert_verdicts(tmp_path, constraint, valid, invalid, *, root_constraint=False):
    if shutil.which("kubeconform") is None:
        pytest.skip("kubeconform is required")
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
        "properties": {"apiVersion": {"type": "string"}, "kind": {"type": "string"},
            "metadata": {"type": "object", "additionalProperties": True},
            "spec": {"type": "object", "properties": {
                "a": {"type": "string"}, "b": {"type": "string"}}}}}
    target = schema if root_constraint else schema["properties"]["spec"]
    target.update(deepcopy(constraint))
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(_strict_schema(schema)))
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    for name, spec in (("valid", valid), ("invalid", invalid)):
        (manifests / f"{name}.json").write_text(json.dumps({
            "apiVersion": "example.io/v1", "kind": "Widget",
            "metadata": {"name": name}, "spec": spec,
        }))
    report = Kubeconform().validate(
        manifests, schema_locations=[str(schema_path)], extra_args=["-verbose"],
    )
    assert {item.name: item.status for item in report.resources} == {
        "valid": "valid", "invalid": "invalid",
    }, report
