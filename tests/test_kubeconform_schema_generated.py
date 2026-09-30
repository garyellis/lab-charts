"""Current chart bytes, rather than a manual sync, control generated CRD schemas."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.yaml_files import dump_yaml
from chart_manager.services.kubeconform_schemas import generated
from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaRenderError,
)
from chart_manager.services.manifest_validation.models import (
    PhaseResult,
    RowResult,
    RunOutcome,
    RunResult,
    WorklistRow,
)

from .schema_fixtures import workspace
from .test_kubeconform_schema_inventory_crd import _crd


def chart(root, name, payload):
    directory = root / "charts" / name
    (directory / "templates").mkdir(parents=True)
    (directory / "Chart.yaml").write_text(
        dump_yaml({"apiVersion": "v2", "name": name, "version": "0.1.0"})
    )
    (directory / "values.yaml").write_text("{}\n")
    (directory / "templates/resources.yaml").write_text(payload)
    (directory / "chart-lifecycle.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "chartmanager.io/v1alpha1",
                "kind": "ChartLifecycle",
                "metadata": {"name": name},
                "spec": {
                    "enabled": True,
                    "validation": {
                        "enabled": True,
                        "releaseName": name,
                        "environments": {
                            "dev": {"namespace": "default", "values": ["values.yaml"]}
                        },
                    },
                },
            }
        )
    )
    return directory


class Renderer:
    def __init__(self):
        self.calls = []
        self.error_type = None
        self.fail = False

    def run(self, request):
        assert request.phases == frozenset({"render"})
        assert request.include_crds
        self.calls.extend(request.charts)
        name = request.charts[0]
        output = request.out / name / "dev"
        output.mkdir(parents=True)
        for path in (request.root / "charts" / name / "templates").glob("*.yaml"):
            (output / path.name).write_bytes(path.read_bytes())
        row = RowResult(
            row=WorklistRow(chart=name, env="dev", release=name, namespace="default"),
            phases={
                "render": PhaseResult(
                    phase="render",
                    status="FAIL" if self.fail else "PASS",
                    detail="specific failure" if self.fail else None,
                    error_type=self.error_type,
                )
            },
        )
        return RunOutcome(
            result=RunResult(rows=(row,), rendered_root=request.out), out_dir=request.out, keep=True
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    # A stable executable identity without requiring Helm for unit tests.
    binary = tmp_path / "helm"
    binary.write_text("test-helm")
    monkeypatch.setattr(generated.shutil, "which", lambda _: str(binary))
    renderer = Renderer()

    def prepare():
        return generated.prepare_generated_schemas(
            workspace(tmp_path), renderer, cache_root=tmp_path / "cache"
        )

    return SimpleNamespace(root=tmp_path, renderer=renderer, prepare=prepare, binary=binary)


def schema_at(locations):
    return Path(locations[0].split("{{.Group}}")[0]) / "example.io/widget_v1.json"


def test_unchanged_charts_reuse_cache_but_modified_crd_is_immediately_current(env):
    provider = chart(env.root, "provider", _crd(nested_type="string"))
    first = env.prepare()
    assert len(env.renderer.calls) == 1
    assert schema_at(first).is_file()
    assert env.prepare() == first
    assert len(env.renderer.calls) == 1
    (provider / "templates/resources.yaml").write_text(_crd(nested_type="integer"))
    second = env.prepare()
    assert second != first
    assert len(env.renderer.calls) == 2
    schema = json.loads(schema_at(second).read_text())
    assert schema["properties"]["spec"]["properties"]["name"]["type"] == "integer"
    assert not (env.root / ".chart-manager/schemas.lock.yaml").exists()


def test_only_changed_chart_is_rendered_and_new_chart_is_discovered(env):
    chart(env.root, "provider", _crd())
    app = chart(env.root, "app", "apiVersion: v1\nkind: ConfigMap\n")
    first = env.prepare()
    assert env.renderer.calls == ["app", "provider"]
    (app / "templates/new.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\n")
    assert env.prepare() == first
    assert env.renderer.calls == ["app", "provider", "app"]
    chart(env.root, "new-provider", _crd())
    env.prepare()
    assert env.renderer.calls[-1] == "new-provider"
    assert env.renderer.calls.count("provider") == 1


def test_provider_removal_never_retains_stale_generated_schemas(env):
    provider = chart(env.root, "provider", _crd())
    env.prepare()
    (provider / "templates/resources.yaml").write_text("apiVersion: v1\nkind: ConfigMap\n")
    assert env.prepare() == ()


def test_conflicting_providers_fail_even_if_one_is_cached(env):
    chart(env.root, "first", _crd())
    env.prepare()
    chart(env.root, "second", _crd(nested_type="integer"))
    with pytest.raises(KubeconformSchemaConfigurationError, match="first; second"):
        env.prepare()


def test_corrupt_derived_cache_is_rebuilt(env):
    chart(env.root, "provider", _crd())
    env.prepare()
    cache = next((env.root / "cache/v3/derived/charts").glob("*.json"))
    cache.write_text("{broken")
    env.prepare()
    assert len(env.renderer.calls) == 2


def test_helm_binary_change_invalidates_derived_cache(env):
    chart(env.root, "provider", _crd())
    env.prepare()
    env.binary.write_text("new-helm")
    env.prepare()
    assert len(env.renderer.calls) == 2


@pytest.mark.parametrize(
    "error_type,expected",
    [
        (None, Outcome.FAILED),
        ("spec", Outcome.SPEC),
        ("environment", Outcome.ENVIRONMENT),
        ("tool", Outcome.TOOL),
    ],
)
def test_provider_render_retains_original_failure_outcome(env, error_type, expected):
    chart(env.root, "provider", _crd())
    env.renderer.fail = True
    env.renderer.error_type = error_type
    with pytest.raises(
        KubeconformSchemaRenderError, match="provider/dev: specific failure"
    ) as caught:
        env.prepare()
    assert caught.value.outcome is expected
    assert not list((env.root / "cache").rglob("charts/*.json"))


def test_extra_schema_in_derived_output_is_removed(env):
    chart(env.root, "provider", _crd())
    first = env.prepare()
    extra = schema_at(first).with_name("stale_v1.json")
    extra.write_text("{}")
    assert env.prepare() == first
    assert not extra.exists()
    assert len(env.renderer.calls) == 1


def test_symlinked_chart_inputs_disable_cache(env):
    provider = chart(env.root, "provider", _crd())
    external = env.root / "external"
    external.mkdir()
    (provider / "linked").symlink_to(external, target_is_directory=True)
    env.prepare()
    env.prepare()
    assert len(env.renderer.calls) == 2


def test_shared_tool_bytes_are_read_once_per_preparation(env, monkeypatch):
    chart(env.root, "first", _crd())
    chart(env.root, "second", _crd())
    read_bytes = Path.read_bytes
    observed = {env.binary: 0, Path(generated.__file__).resolve(): 0}

    def read(path):
        if path in observed:
            observed[path] += 1
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    env.prepare()
    assert list(observed.values()) == [1, 1]
    env.prepare()
    assert list(observed.values()) == [2, 2]
    assert env.renderer.calls == ["first", "second"]
