"""Current chart bytes, rather than a manual sync, control generated CRD schemas."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chart_manager.commands.validate.schemas import generated
from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaRenderError,
)
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.yaml_files import dump_yaml
from tests.conftest import crd_manifest

from .schema_fixtures import workspace


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
    """Render each chart's templates verbatim into <out>/<chart>/dev, as `run()` would."""

    def __init__(self):
        self.calls = []
        self.fail = False
        self.hydrate = None

    def __call__(self, charts, out):
        failures = []
        for chart in charts:
            self.calls.append(chart.name)
            if self.hydrate:
                self.hydrate(chart.path)
            output = out / chart.name / "dev"
            output.mkdir(parents=True)
            for path in (chart.path / "templates").glob("*.yaml"):
                (output / path.name).write_bytes(path.read_bytes())
            if self.fail:
                failures.append(f"{chart.name}/dev: specific failure")
        return failures


@pytest.fixture
def env(tmp_path, monkeypatch):
    # A stable executable identity without requiring Helm for unit tests.
    binary = tmp_path / "helm"
    binary.write_text("test-helm")
    monkeypatch.setattr(generated.shutil, "which", lambda _: str(binary))
    renderer = Renderer()

    def prepare():
        return generated.prepare(workspace(tmp_path), render=renderer, cache_root=tmp_path / "cache")

    return SimpleNamespace(root=tmp_path, renderer=renderer, prepare=prepare, binary=binary)


def schema_at(locations):
    return Path(locations[0].split("{{.Group}}")[0]) / "example.io/widget_v1.json"


def test_unchanged_charts_reuse_cache_but_modified_crd_is_immediately_current(env):
    provider = chart(env.root, "provider", crd_manifest(nested_type="string"))
    first = env.prepare()
    assert len(env.renderer.calls) == 1
    assert schema_at(first).is_file()
    assert env.prepare() == first
    assert len(env.renderer.calls) == 1
    (provider / "templates/resources.yaml").write_text(crd_manifest(nested_type="integer"))
    second = env.prepare()
    assert second != first
    assert len(env.renderer.calls) == 2
    schema = json.loads(schema_at(second).read_text())
    assert schema["properties"]["spec"]["properties"]["name"]["type"] == "integer"
    assert not (env.root / ".chart-manager/schemas.lock.yaml").exists()


def test_only_changed_chart_is_rendered_and_new_chart_is_discovered(env):
    chart(env.root, "provider", crd_manifest())
    app = chart(env.root, "app", "apiVersion: v1\nkind: ConfigMap\n")
    first = env.prepare()
    assert env.renderer.calls == ["provider"]
    (app / "templates/new.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\n")
    assert env.prepare() == first
    assert env.renderer.calls == ["provider"]
    chart(env.root, "new-provider", crd_manifest())
    env.prepare()
    assert env.renderer.calls[-1] == "new-provider"
    assert env.renderer.calls.count("provider") == 1


def test_provider_removal_never_retains_stale_generated_schemas(env):
    provider = chart(env.root, "provider", crd_manifest())
    env.prepare()
    (provider / "templates/resources.yaml").write_text("apiVersion: v1\nkind: ConfigMap\n")
    assert env.prepare() == ()


def test_conflicting_providers_fail_even_if_one_is_cached(env):
    chart(env.root, "first", crd_manifest())
    env.prepare()
    chart(env.root, "second", crd_manifest(nested_type="integer"))
    with pytest.raises(KubeconformSchemaConfigurationError, match="first; second"):
        env.prepare()


def test_corrupt_derived_cache_is_rebuilt(env):
    chart(env.root, "provider", crd_manifest())
    env.prepare()
    cache = next((env.root / "cache/v3/derived/charts").glob("*.json"))
    cache.write_text("{broken")
    env.prepare()
    assert len(env.renderer.calls) == 2


def test_helm_binary_change_invalidates_derived_cache(env):
    chart(env.root, "provider", crd_manifest())
    env.prepare()
    env.binary.write_text("new-helm")
    env.prepare()
    assert len(env.renderer.calls) == 2


def test_a_failed_provider_render_raises_and_caches_nothing(env):
    chart(env.root, "provider", crd_manifest())
    env.renderer.fail = True
    with pytest.raises(
        KubeconformSchemaRenderError, match="provider/dev: specific failure"
    ) as caught:
        env.prepare()
    assert caught.value.outcome is Outcome.FAILED
    assert not list((env.root / "cache").rglob("charts/*.json"))


def test_extra_schema_in_derived_output_is_removed(env):
    chart(env.root, "provider", crd_manifest())
    first = env.prepare()
    extra = schema_at(first).with_name("stale_v1.json")
    extra.write_text("{}")
    assert env.prepare() == first
    assert not extra.exists()
    assert len(env.renderer.calls) == 1


def test_symlinked_chart_inputs_disable_cache(env):
    provider = chart(env.root, "provider", crd_manifest())
    external = env.root / "external"
    external.mkdir()
    (provider / "linked").symlink_to(external, target_is_directory=True)
    env.prepare()
    env.prepare()
    assert len(env.renderer.calls) == 2


def test_shared_tool_bytes_are_read_once_per_preparation(env, monkeypatch):
    chart(env.root, "first", crd_manifest())
    chart(env.root, "second", crd_manifest())
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


def test_uncached_providers_render_in_one_batch(env):
    chart(env.root, "first", crd_manifest())
    chart(env.root, "second", crd_manifest())
    batches = []

    def render(charts, out):
        batches.append(tuple(chart.name for chart in charts))
        return env.renderer(charts, out)

    generated.prepare(workspace(env.root), render=render, cache_root=env.root / "cache")
    assert batches == [("first", "second")]


def test_unrelated_nonprovider_values_and_lifecycle_errors_are_not_loaded(env):
    chart(env.root, "provider", crd_manifest())
    unrelated = chart(env.root, "unrelated", "apiVersion: v1\nkind: ConfigMap\n")
    (unrelated / "values.yaml").write_text("[broken YAML")
    (unrelated / "chart-lifecycle.yaml").write_text("[broken YAML")
    assert schema_at(env.prepare()).is_file()
    assert env.renderer.calls == ["provider"]


def test_stale_dependencies_render_once_then_cache(env, monkeypatch):
    provider = chart(env.root, "provider", crd_manifest())
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

    def hydrate(path):
        (path / "charts").mkdir(exist_ok=True)
        (path / "charts/dep.txt").write_text("dependency bytes")
        fresh.add(path)

    # Stale dependencies make both possible providers; rendering hydrates them.
    env.renderer.hydrate = hydrate
    first = env.prepare()
    assert env.renderer.calls == ["provider", "unrelated"]
    assert list((env.root / "cache/v3/derived/charts").glob("*.json"))
    assert env.prepare() == first
    assert env.renderer.calls == ["provider", "unrelated"]


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

    nested = package("dep/crds/widget.yaml", crd_manifest().encode())
    outer = package("wrapper/charts/dep.tgz", nested)
    provider = chart(env.root, "provider", "apiVersion: v1\nkind: ConfigMap\n")
    (provider / "charts").mkdir()
    (provider / "charts/dep.tgz").write_bytes(outer)
    assert generated._possible_crd_provider(provider)
    assert not (env.root / "dep").exists()


def test_active_cache_manifest_drops_removed_provider_entries(env):
    provider = chart(env.root, "provider", crd_manifest())
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
