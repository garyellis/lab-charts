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
from tests.conftest import CHARTS_DIR

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

    def prepare_schema_dependencies(self, targets):
        pass

    def run(self, request):
        assert request.phases == frozenset({"render"})
        assert request.include_crds
        self.calls.extend(request.charts)
        rows = []
        for name in request.charts:
            output = request.out / name / "dev"
            output.mkdir(parents=True)
            for path in (request.root / "charts" / name / "templates").glob("*.yaml"):
                (output / path.name).write_bytes(path.read_bytes())
            rows.append(
                RowResult(
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
            )
        return RunOutcome(
            result=RunResult(rows=tuple(rows), rendered_root=request.out),
            out_dir=request.out,
            keep=True,
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
    assert env.renderer.calls == ["provider"]
    (app / "templates/new.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\n")
    assert env.prepare() == first
    assert env.renderer.calls == ["provider"]
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


def test_uncached_providers_render_in_one_batch(env, monkeypatch):
    chart(env.root, "first", _crd())
    chart(env.root, "second", _crd())
    original = env.renderer.run
    batches = []

    def render(request):
        batches.append(request.charts)
        return original(request)

    monkeypatch.setattr(env.renderer, "run", render)
    env.prepare()
    assert batches == [("first", "second")]


def test_unrelated_nonprovider_values_and_lifecycle_errors_are_not_loaded(env):
    chart(env.root, "provider", _crd())
    unrelated = chart(env.root, "unrelated", "apiVersion: v1\nkind: ConfigMap\n")
    (unrelated / "values.yaml").write_text("[broken YAML")
    (unrelated / "chart-lifecycle.yaml").write_text("[broken YAML")
    assert schema_at(env.prepare()).is_file()
    assert env.renderer.calls == ["provider"]


def test_dependencies_hydrate_before_provider_scan_and_cache_on_first_run(env, monkeypatch):
    provider = chart(env.root, "provider", _crd())
    unrelated = chart(env.root, "unrelated", "apiVersion: v1\nkind: ConfigMap\n")
    dependency = {"name": "dep", "version": "1.0.0", "repository": "https://example.test"}
    for directory in (provider, unrelated):
        (directory / "Chart.yaml").write_text(
            dump_yaml(
                {
                    "apiVersion": "v2",
                    "name": directory.name,
                    "version": "0.1.0",
                    "dependencies": [dependency],
                }
            )
        )
    (unrelated / "values.yaml").write_text("[broken YAML")
    fresh = set()
    monkeypatch.setattr(generated, "deps_are_fresh", lambda path: path in fresh)

    def hydrate(targets):
        for target in targets:
            (target.path / "charts").mkdir(exist_ok=True)
            (target.path / "charts/dep.txt").write_text("dependency bytes")
            fresh.add(target.path)

    monkeypatch.setattr(env.renderer, "prepare_schema_dependencies", hydrate)
    first = env.prepare()
    assert env.renderer.calls == ["provider"]
    assert list((env.root / "cache/v3/derived/charts").glob("*.json"))
    assert env.prepare() == first
    assert env.renderer.calls == ["provider"]


@pytest.mark.parametrize(
    "name,data",
    [
        (
            "templates/resource.tpl",
            b'apiVersion: {{ print "apiextensions" ".k8s.io/v1" }}\nkind: {{ print "CustomResource" "Definition" }}',
        ),
        (
            "templates/resources.yaml",
            b'kind: ConfigMap\n---\n{{ tpl (.Files.Get "provider.txt") . }}',
        ),
        ("templates/resources.yaml", b'{{ include "provider" . }}'),
        ("templates/resources.yaml", b"kind: {{ .Values.kind }}"),
        ("values.yaml", b"kind: CustomResourceDefinition"),
        ("crds/something.yaml", b"apiVersion: example/v1\nkind: Something"),
    ],
)
def test_provider_detection_keeps_dynamic_templates_and_crd_sources(name, data):
    assert generated._possible_crd_bytes(name, data)


def test_nested_archives_are_scanned_without_extracting(env):
    import io
    import tarfile

    def package(name, content):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        return data.getvalue()

    nested = package("dep/crds/widget.yaml", _crd().encode())
    outer = package("wrapper/charts/dep.tgz", nested)
    provider = chart(env.root, "provider", "apiVersion: v1\nkind: ConfigMap\n")
    (provider / "charts").mkdir()
    (provider / "charts/dep.tgz").write_bytes(outer)
    assert generated._possible_crd_provider(provider)
    assert not (env.root / "dep").exists()


def test_active_cache_manifest_drops_removed_provider_entries(env):
    provider = chart(env.root, "provider", _crd())
    env.prepare()
    manifest = env.root / "cache/v3/derived/current-charts.json"
    active = json.loads(manifest.read_text())
    assert len(active) == 1
    assert (manifest.parent / "charts" / active[0]).is_file()
    (provider / "templates/resources.yaml").write_text("apiVersion: v1\nkind: ConfigMap\n")
    assert env.prepare() == ()
    assert json.loads(manifest.read_text()) == []


@pytest.mark.parametrize(
    "expansion",
    [
        b'{{ if true }}{{ tpl (.Files.Get "provider.txt") . }}{{ end }}',
        b'{{ tpl\n(.Files.Get "provider.txt") . }}',
        b"{{ $resource }}",
        b'{{ block "provider" . }}{{ end }}',
    ],
)
def test_mixed_templates_keep_chained_multiline_and_variable_output(expansion):
    assert generated._possible_crd_bytes(
        "templates/resources.yaml", b"kind: ConfigMap\n---\n" + expansion
    )


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("network", Outcome.ENVIRONMENT),
        ("missing-tool", Outcome.TOOL),
        ("spec", Outcome.SPEC),
    ],
)
def test_dependency_preparation_retains_failure_classification(env, monkeypatch, failure, expected):
    from chart_manager.plumbing.errors import ExternalCommandError, MissingToolError, SpecError
    from chart_manager.services.manifest_validation import app
    from chart_manager.services.manifest_validation.catalog import build_catalog

    provider = chart(env.root, "provider", _crd())
    (provider / "Chart.yaml").write_text(
        dump_yaml(
            {
                "apiVersion": "v2",
                "name": "provider",
                "version": "0.1.0",
                "dependencies": [
                    {"name": "dep", "version": "1.0.0", "repository": "https://example.test"}
                ],
            }
        )
    )
    errors = {
        "network": ExternalCommandError("offline"),
        "missing-tool": MissingToolError("helm"),
        "spec": SpecError("bad chart"),
    }

    class FakeHelm:
        def __init__(self, **kwargs):
            pass

        def dependency_update_if_stale(self, path, *, timeout):
            assert path == provider
            raise errors[failure]

    monkeypatch.setattr(app, "Helm", FakeHelm)
    service = app.ManifestValidationService(workspace=workspace(env.root))
    with pytest.raises(KubeconformSchemaRenderError) as caught:
        service.prepare_schema_dependencies(build_catalog(env.root, charts_dir=CHARTS_DIR).targets)
    assert caught.value.outcome is expected


def test_helm_notes_do_not_make_plain_charts_potential_providers():
    assert not generated._possible_crd_bytes(
        "dep/templates/NOTES.txt", b'{{ include "chart.name" . }} is installed'
    )


@pytest.mark.parametrize(
    "helper",
    [
        b'{{ include "app.labels" . | nindent 4 }}',
        b'{{- include "app.labels" . | indent 4 -}}',
    ],
)
def test_indented_helpers_inside_static_resources_are_not_providers(helper):
    assert not generated._possible_crd_bytes(
        "templates/configmap.yaml", b"kind: ConfigMap\nmetadata:\n  labels:\n" + helper
    )


def test_indented_whole_document_is_still_a_potential_provider():
    assert generated._possible_crd_bytes(
        "templates/resources.yaml",
        b'kind: ConfigMap\n---\n{{ include "provider" . | nindent 2 }}',
    )


def test_unindented_helpers_still_make_static_resources_potential_providers():
    assert generated._possible_crd_bytes(
        "templates/configmap.yaml", b'kind: ConfigMap\n{{ include "provider" . }}'
    )


def test_control_before_document_separator_keeps_indented_dynamic_provider():
    assert generated._possible_crd_bytes(
        "templates/resources.yaml",
        b"kind: ConfigMap\n{{ if true }}---\n{{ tpl .Values.resource . | nindent 4 }}\n{{ end }}",
    )
