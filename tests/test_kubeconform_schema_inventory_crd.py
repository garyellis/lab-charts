from __future__ import annotations

import json
from pathlib import Path

import pytest

from chart_manager.services.kubeconform_schemas.crd import generate_crd_schemas
from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaIntegrityError,
)
from chart_manager.services.kubeconform_schemas.inventory import scan_rendered_directory
from chart_manager.services.kubeconform_schemas.models import SchemaScope


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


def test_inventory_deduplicates_gvks_and_expands_kubernetes_lists(tmp_path: Path) -> None:
    (tmp_path / "all.yaml").write_text(
        """apiVersion: v1
kind: List
items:
  - apiVersion: v1
    kind: ConfigMap
    metadata: {name: one}
  - apiVersion: v1
    kind: ConfigMap
    metadata: {name: two}
---
apiVersion: example.io/v1
kind: Widget
metadata: {name: sample}
"""
    )
    scope = SchemaScope(chart="demo", environment="ci")

    inventory = scan_rendered_directory(
        tmp_path,
        scope=scope,
        allow_missing=frozenset({"example.io/v1/Widget"}),
    )

    assert len(inventory.resources) == 3
    assert [(item.gvk.kind, item.allow_missing) for item in inventory.requirements] == [
        ("ConfigMap", False),
        ("Widget", True),
    ]


def test_crd_generation_closes_objects_but_preserves_maps_and_unknown_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "crd.yaml"
    path.write_text(_crd())
    inventory = scan_rendered_directory(
        tmp_path,
        scope=SchemaScope(chart="operator", environment="ci"),
    )

    generated = generate_crd_schemas(inventory.crds)

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
    (tmp_path / "configmap.json").write_text(
        '{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"demo"}}'
    )

    inventory = scan_rendered_directory(
        tmp_path,
        scope=SchemaScope(chart="demo", environment="ci"),
    )

    assert [item.gvk.key for item in inventory.requirements] == ["v1/ConfigMap"]


def test_conflicting_crds_in_one_scope_fail_instead_of_winning_by_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "one.yaml").write_text(_crd(nested_type="string"))
    (tmp_path / "two.yaml").write_text(_crd(nested_type="integer"))
    inventory = scan_rendered_directory(
        tmp_path,
        scope=SchemaScope(chart="operator", environment="ci"),
    )

    with pytest.raises(
        KubeconformSchemaIntegrityError,
        match="conflicting rendered CRDs",
    ):
        generate_crd_schemas(inventory.crds)
