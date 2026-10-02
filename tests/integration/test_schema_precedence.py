"""Real kubeconform coverage for managed schema precedence and error outcomes."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from chart_manager.api.v1alpha1.chart_lifecycle import ManifestValidationSpec
from chart_manager.integrations.kubeconform import Kubeconform
from chart_manager.services.manifest_validation.validator_adapters import (
    KubeconformProvider,
    KubeconformValidator,
)
from chart_manager.services.manifest_validation.validators import (
    KubeconformConfig,
    KubeconformRuntimeInputs,
    ValidatorCompileContext,
)
from tests.conftest import POLICIES_DIR

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform missing"),
]


def _manifest(root: Path, text: str | None = None) -> Path:
    manifests = root / "manifests"
    manifests.mkdir(exist_ok=True)
    (manifests / "widget.yaml").write_text(
        text or "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\n"
    )
    return manifests


def test_managed_schema_precedence_with_real_kubeconform(tmp_path: Path) -> None:
    paths = {name: tmp_path / name / "widget.json" for name in ("generated", "local", "upstream")}
    for name, path in paths.items():
        path.parent.mkdir()
        path.write_text(json.dumps({"type": "object", "properties": {"source": {"enum": [name]}}}))
    spec = ManifestValidationSpec.model_validate(
        {
            "releaseName": "demo",
            "environments": {"dev": {"namespace": "dev"}},
            "schemaLocations": ["local/{{.ResourceKind}}.json"],
        }
    )
    invocation = KubeconformProvider().compile(
        ValidatorCompileContext(
            spec=spec,
            repo_root=tmp_path,
            chart_path=tmp_path / "charts/demo",
            spec_path=tmp_path / "charts/demo/chart-lifecycle.yaml",
            policies_dir=POLICIES_DIR,
            kubeconform=KubeconformRuntimeInputs(
                kubernetes_version="1.35.3",
                generated_schema_locations=(str(tmp_path / "generated/{{.ResourceKind}}.json"),),
                fallback_schema_locations=(str(tmp_path / "upstream/{{.ResourceKind}}.json"),),
            ),
        )
    )
    validator = KubeconformValidator(Kubeconform())
    for expected in ("generated", "local", "upstream"):
        manifests = _manifest(
            tmp_path,
            "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo}\n"
            f"source: {expected}\n",
        )
        result = validator.validate(manifests, invocation.config)
        assert result.status == "PASS", result.detail
        # Removing the higher-priority file must expose the next source.
        paths[expected].unlink()
    result = validator.validate(manifests, invocation.config)
    assert result.status == "FAIL"
    assert result.error_type == "tool"


@pytest.mark.parametrize("ignore_missing", [(), ("Widget",), ("example.io/v1/Widget",)])
@pytest.mark.parametrize("content", ['{"type":', '{"type":123}', '{"$ref":"#/missing"}'])
def test_malformed_schema_is_tool_failure_even_for_optional_kind(
    tmp_path: Path,
    content: str,
    ignore_missing: tuple[str, ...],
) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text(content)
    result = KubeconformValidator(Kubeconform()).validate(
        _manifest(tmp_path),
        KubeconformConfig("1.35.3", (str(schema),), ignore_missing),
    )
    assert result.status == "FAIL"
    assert result.error_type == "tool"
    assert "schema JSON" in result.detail
    assert "chart-manager schemas sync" in result.detail


@pytest.mark.parametrize(
    "manifest",
    [
        "apiVersion: example.io/v1\nkind: Widget\nmetadata: {name: demo\n",
        "apiVersion: example.io/v1\nmetadata: {name: demo}\n",
    ],
)
def test_malformed_resource_remains_chart_failure(tmp_path: Path, manifest: str) -> None:
    result = KubeconformValidator(Kubeconform()).validate(
        _manifest(tmp_path, manifest),
        KubeconformConfig("1.35.3", (str(tmp_path / "missing.json"),)),
    )
    assert result.status == "FAIL"
    assert result.error_type is None
    assert "chart-manager schemas sync" not in result.detail
