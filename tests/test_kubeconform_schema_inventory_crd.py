from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
)
from chart_manager.commands.validate.schemas.models import SchemaScope
from chart_manager.plumbing.yaml_files import dump_yaml, parse_yaml_mapping
from chart_manager.services.kubeconform_schemas.crd import generate_crd_schemas
from chart_manager.services.kubeconform_schemas.inventory import (
    scan_rendered_directory,
)


@pytest.mark.parametrize("document", ["metadata: {}", "- bad", "kind: List\nitems: [bad]"])
def test_bad_rendered_document_blames_chart_scope(tmp_path: Path, document: str) -> None:
    (tmp_path / "bad.yaml").write_text(document)
    with pytest.raises(KubeconformSchemaConfigurationError, match=r"demo/dev:.*bad.yaml"):
        scan_rendered_directory(tmp_path, scope=SchemaScope(chart="demo", environment="dev"))


def _crd(*, nested_type: str = "string") -> str:
    return f"""apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.io
spec:
  group: example.io
  names:
    kind: Widget
    plural: widgets
  scope: Namespaced
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                name:
                  type: {nested_type}
                labels:
                  type: object
                  additionalProperties:
                    type: string
                arbitrary:
                  type: object
                  x-kubernetes-preserve-unknown-fields: true
    - name: v1beta1
      served: false
      storage: false
      schema:
        openAPIV3Schema:
          type: object
"""


def test_inventory_expands_kubernetes_lists_and_collects_only_crds(tmp_path: Path) -> None:
    crd = parse_yaml_mapping(_crd())
    (tmp_path / "all.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "one"}},
                    crd,
                    crd,
                ],
            }
        )
        + "---\napiVersion: example.io/v1\nkind: Widget\nmetadata: {name: sample}\n"
    )
    scope = SchemaScope(chart="demo", environment="ci")

    crds = scan_rendered_directory(tmp_path, scope=scope)

    assert len(crds) == 2
    assert all(item.scope == scope and item.document == crd for item in crds)
    assert all(item.path == Path("all.yaml") and item.document_index == 0 for item in crds)
    # Duplicate definitions collapse during generation, not before conflicts can be checked.
    assert len(generate_crd_schemas(crds)) == 1


def test_crd_generation_closes_objects_but_preserves_maps_and_unknown_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "crd.yaml"
    path.write_text(_crd())
    crds = scan_rendered_directory(
        tmp_path,
        scope=SchemaScope(chart="operator", environment="ci"),
    )

    generated = generate_crd_schemas(crds)

    assert len(generated) == 1
    assert generated[0].gvk.key == "example.io/v1/Widget"
    schema = json.loads(generated[0].content)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["apiVersion"]["enum"] == ["example.io/v1"]
    assert schema["properties"]["kind"]["enum"] == ["Widget"]
    assert schema["properties"]["metadata"]["additionalProperties"] is True
    assert {"apiVersion", "kind", "metadata"}.issubset(schema["required"])
    spec = schema["properties"]["spec"]
    assert spec["additionalProperties"] is False
    assert spec["properties"]["labels"]["additionalProperties"] == {"type": "string"}
    assert "additionalProperties" not in spec["properties"]["arbitrary"]


def test_inventory_reads_json_manifests(tmp_path: Path) -> None:
    (tmp_path / "crd.json").write_text(json.dumps(parse_yaml_mapping(_crd())))

    crds = scan_rendered_directory(
        tmp_path,
        scope=SchemaScope(chart="demo", environment="ci"),
    )

    assert len(crds) == 1
    assert crds[0].path == Path("crd.json")
    assert generate_crd_schemas(crds)[0].gvk.key == "example.io/v1/Widget"


def test_conflicting_crds_in_one_scope_fail_instead_of_winning_by_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "one.yaml").write_text(_crd(nested_type="string"))
    (tmp_path / "two.yaml").write_text(_crd(nested_type="integer"))
    crds = scan_rendered_directory(
        tmp_path,
        scope=SchemaScope(chart="operator", environment="ci"),
    )

    with pytest.raises(
        KubeconformSchemaConfigurationError,
        match="conflicting rendered CRDs",
    ) as caught:
        generate_crd_schemas(crds)
    assert "operator/ci:one.yaml#document=1;version=v1" in str(caught.value)
    assert "operator/ci:two.yaml#document=1;version=v1" in str(caught.value)


def test_identical_crds_are_shared_across_chart_scopes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "crd.yaml").write_text(_crd())
    (second / "crd.yaml").write_text(_crd())
    crds = (
        *scan_rendered_directory(first, scope=SchemaScope(chart="operator", environment="ci")),
        *scan_rendered_directory(second, scope=SchemaScope(chart="consumer", environment="dev")),
    )

    generated = generate_crd_schemas(crds)

    assert len(generated) == 1
    assert generated[0].source_reference == "rendered CRD example.io/v1/Widget"


def test_strimzi_style_combinator_fragments_are_not_closed(tmp_path: Path) -> None:
    text = _crd().replace(
        "properties:\n                name:",
        "oneOf:\n                - properties:\n                    value: {type: string}\n"
        "                - properties:\n                    valueFrom:\n                      type: object\n"
        "                      properties:\n                        secretKeyRef: {type: object}\n"
        "              properties:\n                name:",
    )
    (tmp_path / "crd.yaml").write_text(text)
    crds = scan_rendered_directory(tmp_path, scope=SchemaScope(chart="strimzi", environment="ci"))

    schema = json.loads(generate_crd_schemas(crds)[0].content)
    branches = schema["properties"]["spec"]["oneOf"]

    assert "additionalProperties" not in branches[0]
    assert "additionalProperties" not in branches[1]
    assert "additionalProperties" not in branches[1]["properties"]["valueFrom"]
