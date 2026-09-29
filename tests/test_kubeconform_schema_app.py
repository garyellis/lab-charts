"""Repository orchestration for eager schema inventory and synchronization."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from chart_manager.api.v1alpha1.chart_workspace import WorkspaceValidation
from chart_manager.domain.workspace import SCHEMA_LOCK_FILE, RepositoryWorkspace
from chart_manager.services.kubeconform_schemas.app import (
    RepositoryKubeconformSchemaService,
)
from chart_manager.services.manifest_validation.models import (
    PhaseResult,
    RowResult,
    RunOutcome,
    RunResult,
    WorklistRow,
)


class _Validation:
    def run(self, request):  # type: ignore[no-untyped-def]
        rendered = request.out / "demo" / "dev"
        rendered.mkdir(parents=True)
        (rendered / "resources.yaml").write_text(
            """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: widgets.example.io}
spec:
  group: example.io
  names: {kind: Widget, plural: widgets}
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
                enabled: {type: boolean}
---
apiVersion: example.io/v1
kind: Widget
metadata: {name: sample}
spec: {enabled: true}
""".lstrip()
        )
        row = WorklistRow(
            chart="demo",
            env="dev",
            release="demo",
            namespace="default",
        )
        return RunOutcome(
            result=RunResult(
                rows=(
                    RowResult(
                        row=row,
                        phases={
                            "render": PhaseResult(
                                phase="render",
                                status="PASS",
                                artifacts=(rendered,),
                            ),
                            "schema": PhaseResult(phase="schema", status="NOT_RUN"),
                            "policy": PhaseResult(phase="policy", status="NOT_RUN"),
                        },
                    ),
                ),
                rendered_root=request.out,
            ),
            out_dir=request.out,
            keep=True,
        )


class _Sync:
    def __init__(self) -> None:
        self.request = None

    def sync(self, request):  # type: ignore[no-untyped-def]
        self.request = request
        return SimpleNamespace(
            lock=SimpleNamespace(generation="sha256:" + "a" * 64),
            generation_path=Path("/cache/generation"),
            lock_updated=request.update,
            generation_published=True,
        )


def test_sync_renders_inventory_and_generates_exact_crd_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    policy = WorkspaceValidation.model_validate(
        {
            "kubernetesVersion": "1.35.3",
            "schemas": {
                "generateFromCRDs": True,
                "catalog": {"repository": "datreeio/CRDs-catalog", "track": "main"},
            },
        }
    )
    workspace = RepositoryWorkspace(
        root=tmp_path,
        name="lab-charts",
        validation=policy,
        authored=True,
    )
    target = SimpleNamespace(
        spec=SimpleNamespace(
            ignore_missing_schemas=[],
            schema_locations=[],
        )
    )
    monkeypatch.setattr(
        "chart_manager.services.kubeconform_schemas.app.build_catalog",
        lambda *_args, **_kwargs: SimpleNamespace(
            errors=(),
            targets=(target,),
            by_name=lambda: {"demo": target},
        ),
    )
    sync = _Sync()
    service = RepositoryKubeconformSchemaService(
        workspace=workspace,
        validation=_Validation(),  # type: ignore[arg-type]
        sync=sync,  # type: ignore[arg-type]
    )

    result = service.sync(update=True, workers=2)

    assert result.rows == 1
    assert result.generated == 1
    assert sync.request.workspace == "lab-charts"
    assert sync.request.lock_path == tmp_path / SCHEMA_LOCK_FILE
    assert sync.request.update is True
    generated = sync.request.materialized[0]
    assert generated.gvk.key == "example.io/v1/Widget"
    assert b'"additionalProperties": false' in generated.content
