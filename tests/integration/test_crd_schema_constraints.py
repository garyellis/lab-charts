"""Exercise converted constraint fragments using the actual kubeconform validator."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.services.kubeconform_schemas.crd import _strict_schema

pytestmark = pytest.mark.integration


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
