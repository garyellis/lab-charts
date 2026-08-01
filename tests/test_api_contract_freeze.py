"""Freeze the authored resource contracts before they move into ``api/``.

This is Phase 0 of ``docs/plans/2026-07-30-versioned-configuration-api-refactor-agent.md``.
The three authored kinds -- ``ChartLifecycle``, ``LocalCluster`` and
``LocalStack`` -- are about to be relocated out of the services that happen to
consume them and into ``chart_manager.api.<group>.v1alpha1``. Relocation is
supposed to change ownership and nothing else, so every observable property of
the models is pinned here first: accepted YAML, aliases, defaults (including
``default_factory`` results), strictness, discriminators, dump spellings and
the JSON Schema each root model generates.

The imports below deliberately name the *current* locations. When the models
move, this file's import block is the only part that should need editing -- if
an assertion has to change too, the move was not behavior preserving and needs
a reviewer, not a fixup.

The schema snapshot in ``tests/fixtures/api/expected-schemas.json`` is the
plan's "comparison input" (Phase 0 item 5), not the checked-in schema
deliverable of Phase 5. Regenerate it only when a reviewer has confirmed the
diff is intentional::

    uv run --extra dev python - <<'PY'
    import json
    from tests.test_api_contract_freeze import _generate_schemas, _serialize_schemas, SCHEMA_SNAPSHOT
    SCHEMA_SNAPSHOT.write_text(_serialize_schemas(_generate_schemas()), encoding="utf-8")
    PY
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from chart_manager.api.lifecycle.v1alpha1 import (
    LIFECYCLE_API_VERSION,
    LIFECYCLE_KIND,
    MATCH_BY_BASENAME,
    ChartLifecycle,
    ChartLifecycleMetadata,
    ChartLifecycleSpec,
    ClusterTestProfile,
    ClusterTestRef,
    ClusterTestSpec,
    ManifestValidationEnvironmentSpec,
    ManifestValidationPolicySpec,
    ManifestValidationSpec,
    ManifestValidationValidatorsSpec,
)
from chart_manager.api.local.v1alpha1 import (
    LOCAL_API_VERSION,
    LOCAL_CLUSTER_KIND,
    LOCAL_STACK_KIND,
    BootstrapLifecycleRelease,
    BootstrapLocalChartRelease,
    BootstrapOciChartRelease,
    BootstrapReadiness,
    LifecycleRelease,
    LocalBootstrap,
    LocalCluster,
    LocalStack,
    OciChartRelease,
    ResourceMetadata,
)
from chart_manager.services.chart_config import LIFECYCLE_FILENAME
from chart_manager.services.local_resources import DEFAULT_LOCAL_CONFIG, DEFAULT_STACKS_DIR

from .conftest import REPO_ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api"
SCHEMA_SNAPSHOT = FIXTURES / "expected-schemas.json"

#: The three root models whose JSON Schema is compared before and after the move.
ROOT_MODELS: dict[str, type[BaseModel]] = {
    "ChartLifecycle": ChartLifecycle,
    "LocalCluster": LocalCluster,
    "LocalStack": LocalStack,
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _discover(filename: str) -> list[Path]:
    """Every ``filename`` under the repository, skipping dot-directories.

    ``os.walk`` with in-place pruning rather than ``rglob`` so the sweep never
    descends into ``.git`` or the agent worktrees under ``.claude/``, which
    contain whole second copies of ``charts/``.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        if filename in filenames:
            found.append(Path(dirpath) / filename)
    return sorted(found)


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _error_types(exc_info: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    """The Pydantic error *categories* raised, which is what must not drift."""
    return {error["type"] for error in exc_info.value.errors()}


def _assert_authored_subset(authored: Any, dumped: Any, where: str = "$") -> None:
    """Every authored key/value is reproduced verbatim by an aliased dump.

    A dump is a superset of the authored document because it materializes
    defaults; what must hold is that nothing the author wrote is renamed,
    dropped or rewritten on the way back out.
    """
    if isinstance(authored, dict):
        assert isinstance(dumped, dict), f"{where}: expected a mapping, got {type(dumped)}"
        missing = sorted(set(authored) - set(dumped))
        assert not missing, f"{where}: authored keys missing from the aliased dump: {missing}"
        for key, value in authored.items():
            _assert_authored_subset(value, dumped[key], f"{where}.{key}")
        return
    if isinstance(authored, list):
        assert isinstance(dumped, list), f"{where}: expected a list, got {type(dumped)}"
        assert len(authored) == len(dumped), f"{where}: list length changed"
        for index, value in enumerate(authored):
            _assert_authored_subset(value, dumped[index], f"{where}[{index}]")
        return
    assert authored == dumped, f"{where}: authored {authored!r} dumped as {dumped!r}"


# --------------------------------------------------------------------------
# 1. public constants
# --------------------------------------------------------------------------


def test_authored_api_constants_are_frozen() -> None:
    """Group/version/kind strings appear verbatim in every authored document."""
    assert LIFECYCLE_API_VERSION == "lifecycle.chartmanager.io/v1alpha1"
    assert LIFECYCLE_KIND == "ChartLifecycle"
    assert LIFECYCLE_FILENAME == "chart-lifecycle.yaml"
    assert MATCH_BY_BASENAME == "match-by-basename"
    assert LOCAL_API_VERSION == "local.chartmanager.io/v1alpha1"
    assert LOCAL_CLUSTER_KIND == "LocalCluster"
    assert LOCAL_STACK_KIND == "LocalStack"


# --------------------------------------------------------------------------
# 2. every checked-in authored document still parses
# --------------------------------------------------------------------------

LIFECYCLE_DOCUMENTS = _discover(LIFECYCLE_FILENAME)
LOCAL_CLUSTER_DOCUMENT = REPO_ROOT / DEFAULT_LOCAL_CONFIG
LOCAL_STACK_DOCUMENTS = sorted(
    path
    for path in (REPO_ROOT / DEFAULT_LOCAL_CONFIG.parent / DEFAULT_STACKS_DIR).glob("*")
    if path.suffix in {".yaml", ".yml"}
)


def test_checked_in_documents_are_discoverable() -> None:
    """Guard the guard: an empty sweep would make the parse tests vacuous."""
    assert len(LIFECYCLE_DOCUMENTS) > 25, (
        f"suspiciously few lifecycle documents found: {[_rel(p) for p in LIFECYCLE_DOCUMENTS]}"
    )
    assert REPO_ROOT / "charts" / "harbor" / LIFECYCLE_FILENAME in LIFECYCLE_DOCUMENTS
    assert (
        REPO_ROOT / "tests" / "fixtures" / "charts" / "passing-app" / LIFECYCLE_FILENAME
        in LIFECYCLE_DOCUMENTS
    )
    assert FIXTURES / LIFECYCLE_FILENAME in LIFECYCLE_DOCUMENTS
    # The repository ships exactly one LocalCluster and, so far, no LocalStack.
    # `test_local_stack_fixture_round_trips_through_authored_aliases` is the
    # only authored example of that kind; this assertion is what will notice
    # when a real one is added and needs adding to the sweep.
    assert LOCAL_CLUSTER_DOCUMENT.is_file()
    assert LOCAL_STACK_DOCUMENTS == []


@pytest.mark.parametrize("path", LIFECYCLE_DOCUMENTS, ids=_rel)
def test_every_checked_in_chart_lifecycle_parses(path: Path) -> None:
    resource = ChartLifecycle.model_validate(_read_yaml(path))

    assert resource.api_version == LIFECYCLE_API_VERSION
    assert resource.kind == LIFECYCLE_KIND
    assert ChartLifecycle.model_validate(resource.model_dump(by_alias=True)) == resource


def test_repository_local_cluster_parses() -> None:
    resource = LocalCluster.model_validate(_read_yaml(LOCAL_CLUSTER_DOCUMENT))

    assert resource.api_version == LOCAL_API_VERSION
    assert resource.kind == LOCAL_CLUSTER_KIND
    assert LocalCluster.model_validate(resource.model_dump(by_alias=True)) == resource


@pytest.mark.parametrize("path", LOCAL_STACK_DOCUMENTS, ids=_rel)
def test_every_checked_in_local_stack_parses(path: Path) -> None:
    resource = LocalStack.model_validate(_read_yaml(path))

    assert resource.kind == LOCAL_STACK_KIND
    assert LocalStack.model_validate(resource.model_dump(by_alias=True)) == resource


# --------------------------------------------------------------------------
# 3. representative fixtures round-trip through the authored spellings
# --------------------------------------------------------------------------


def test_chart_lifecycle_fixture_round_trips_through_authored_aliases() -> None:
    document = _read_yaml(FIXTURES / LIFECYCLE_FILENAME)
    resource = ChartLifecycle.model_validate(document)

    dumped = resource.model_dump(mode="json", by_alias=True)

    assert ChartLifecycle.model_validate(dumped) == resource
    _assert_authored_subset(document, dumped)
    assert list(dumped) == ["apiVersion", "kind", "metadata", "spec"]
    assert set(dumped["spec"]) == {"enabled", "validation", "clusterTest"}
    assert set(dumped["spec"]["validation"]) == {
        "enabled",
        "releaseName",
        "namespaceTemplate",
        "helmVersion",
        "helmBinary",
        "kubernetesVersion",
        "schemaLocations",
        "environments",
        "triggers",
        "triggerIgnores",
        "unmatchedChanges",
        "validators",
        "policies",
    }
    assert set(dumped["spec"]["validation"]["environments"]["ci"]) == {"namespace", "values"}
    assert set(dumped["spec"]["validation"]["validators"]) == {"kubeconform", "policy"}
    assert set(dumped["spec"]["validation"]["policies"]) == {"extra"}
    assert set(dumped["spec"]["clusterTest"]) == {"enabled", "profiles", "dependentTests"}
    assert set(dumped["spec"]["clusterTest"]["profiles"]["minimal"]) == {
        "description",
        "namespace",
        "requires",
        "values",
        "helmTest",
        "timeout",
    }
    assert set(dumped["spec"]["clusterTest"]["dependentTests"][0]) == {"chart", "profile"}
    # The near-empty `routed` profile shows the profile defaults in the dump.
    assert dumped["spec"]["clusterTest"]["profiles"]["routed"] == {
        "description": None,
        "namespace": None,
        "requires": [],
        "values": ["values.yaml"],
        "helmTest": True,
        "timeout": "10m",
    }


def test_local_cluster_fixture_round_trips_through_authored_aliases() -> None:
    document = _read_yaml(FIXTURES / "local-cluster.yaml")
    resource = LocalCluster.model_validate(document)

    dumped = resource.model_dump(mode="json", by_alias=True)

    assert LocalCluster.model_validate(dumped) == resource
    _assert_authored_subset(document, dumped)
    assert list(dumped) == ["apiVersion", "kind", "metadata", "spec"]
    assert set(dumped["spec"]) == {"cluster", "bootstrap"}
    assert set(dumped["spec"]["cluster"]) == {"config"}
    assert set(dumped["spec"]["bootstrap"]) == {"releases"}

    releases = resource.spec.bootstrap.releases
    assert [type(release) for release in releases] == [
        BootstrapLifecycleRelease,
        BootstrapLocalChartRelease,
        BootstrapOciChartRelease,
        BootstrapOciChartRelease,
    ]
    assert [release.type for release in releases] == ["lifecycle", "local", "oci", "oci"]
    assert set(dumped["spec"]["bootstrap"]["releases"][0]) == {
        "type",
        "chart",
        "profile",
        "runtimeValues",
        "readiness",
    }
    assert set(dumped["spec"]["bootstrap"]["releases"][1]) == {
        "name",
        "namespace",
        "values",
        "timeout",
        "type",
        "chart",
        "runtimeValues",
        "readiness",
    }
    assert set(dumped["spec"]["bootstrap"]["releases"][2]) == {
        "name",
        "namespace",
        "values",
        "timeout",
        "type",
        "chart",
        "version",
        "digest",
        "runtimeValues",
        "readiness",
    }
    assert set(dumped["spec"]["bootstrap"]["releases"][0]["readiness"]) == {
        "nodesReady",
        "workloadsReady",
    }
    assert set(dumped["spec"]["bootstrap"]["releases"][0]["readiness"]["workloadsReady"]) == {
        "namespace",
        "timeout",
    }
    # Repository-relative paths are typed as `Path` but serialize as the
    # authored POSIX spelling.
    assert dumped["spec"]["cluster"]["config"] == "kind-config.yaml"
    assert isinstance(resource.spec.cluster.config, Path)


def test_local_stack_fixture_round_trips_through_authored_aliases() -> None:
    document = _read_yaml(FIXTURES / "local-stack.yaml")
    resource = LocalStack.model_validate(document)

    dumped = resource.model_dump(mode="json", by_alias=True)

    assert LocalStack.model_validate(dumped) == resource
    _assert_authored_subset(document, dumped)
    assert list(dumped) == ["apiVersion", "kind", "metadata", "spec"]
    assert set(dumped["spec"]) == {"releases"}
    assert [type(release) for release in resource.spec.releases] == [
        LifecycleRelease,
        OciChartRelease,
    ]
    # A stack release carries none of the bootstrap-only contracts.
    assert set(dumped["spec"]["releases"][0]) == {"type", "chart", "profile"}
    assert "runtimeValues" not in dumped["spec"]["releases"][1]
    assert "readiness" not in dumped["spec"]["releases"][1]


@pytest.mark.parametrize(
    ("model", "fixture"),
    [
        (ChartLifecycle, LIFECYCLE_FILENAME),
        (LocalCluster, "local-cluster.yaml"),
        (LocalStack, "local-stack.yaml"),
    ],
    ids=["ChartLifecycle", "LocalCluster", "LocalStack"],
)
def test_field_names_are_alias_only(model: type[BaseModel], fixture: str) -> None:
    """No ``populate_by_name``: the authored spelling is the only accepted one.

    ``model_dump()`` without ``by_alias`` emits Python attribute names
    (``api_version``, ``cluster_test``, ...). Feeding that back in must fail,
    otherwise the snake_case spellings would be a second, undocumented and
    unversioned authored surface.
    """
    resource = model.model_validate(_read_yaml(FIXTURES / fixture))

    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(resource.model_dump())

    assert {"missing", "extra_forbidden"} <= _error_types(exc_info)


# --------------------------------------------------------------------------
# 4. author-visible defaults
# --------------------------------------------------------------------------


def test_chart_lifecycle_spec_defaults() -> None:
    spec = ChartLifecycleSpec.model_validate({})

    assert spec.enabled is True
    assert spec.validation is None
    assert spec.cluster_test is None


def _minimal_validation() -> ManifestValidationSpec:
    return ManifestValidationSpec.model_validate(
        {"releaseName": "demo", "environments": {"dev": {"namespace": "lab-dev"}}}
    )


def test_manifest_validation_spec_defaults() -> None:
    spec = _minimal_validation()

    assert spec.enabled is True
    assert spec.namespace_template is None
    assert spec.helm_version is None
    assert spec.helm_binary is None
    assert spec.kubernetes_version is None
    assert spec.schema_locations == []
    assert spec.triggers == {}
    assert spec.trigger_ignores == []
    assert spec.unmatched_changes == "warn"
    assert spec.validators == ManifestValidationValidatorsSpec(kubeconform=True, policy=True)
    assert spec.policies == ManifestValidationPolicySpec(extra=[])


def test_manifest_validation_environment_defaults() -> None:
    environment = ManifestValidationEnvironmentSpec()

    assert environment.namespace is None
    assert environment.values == []


def test_manifest_validation_default_factories_are_per_instance() -> None:
    first = _minimal_validation()
    second = _minimal_validation()

    first.schema_locations.append("default")
    first.trigger_ignores.append("README.md")
    first.triggers["values.yaml"] = ["dev"]
    first.policies.extra.append("policies/extra")

    assert second.schema_locations == []
    assert second.trigger_ignores == []
    assert second.triggers == {}
    assert second.policies.extra == []
    assert first.validators is not second.validators
    assert first.policies is not second.policies


def test_cluster_test_defaults() -> None:
    spec = ClusterTestSpec.model_validate({"profiles": {}})
    profile = ClusterTestProfile()
    ref = ClusterTestRef(chart="istio-base")

    assert spec.enabled is True
    assert spec.dependent_tests == []
    assert profile.description is None
    assert profile.namespace is None
    assert profile.requires == []
    assert profile.values == ["values.yaml"]
    assert profile.helm_test is True
    assert profile.timeout == "10m"
    assert ref.profile == "minimal"


def test_cluster_test_default_factories_are_per_instance() -> None:
    first = ClusterTestProfile()
    second = ClusterTestProfile()

    first.values.append("values-ci.yaml")
    first.requires.append(ClusterTestRef(chart="istio-base"))

    assert second.values == ["values.yaml"]
    assert second.requires == []


def test_local_resource_defaults() -> None:
    bootstrap = LocalBootstrap()
    readiness = BootstrapReadiness()
    release = BootstrapLifecycleRelease.model_validate(
        {"type": "lifecycle", "chart": "charts/demo", "profile": "minimal"}
    )
    oci = OciChartRelease.model_validate(
        {
            "type": "oci",
            "name": "demo",
            "chart": "oci://example.test/charts/demo",
            "version": "1.0.0",
            "namespace": "demo",
            "values": [],
            "timeout": "1m",
        }
    )

    assert bootstrap.releases == []
    assert readiness.nodes_ready is False
    assert readiness.workloads_ready is None
    assert release.runtime_values == {}
    assert release.readiness is None
    assert oci.digest is None


def test_local_default_factories_are_per_instance() -> None:
    first = LocalBootstrap()
    second = LocalBootstrap()

    first.releases.append(
        BootstrapLifecycleRelease.model_validate(
            {"type": "lifecycle", "chart": "charts/demo", "profile": "minimal"}
        )
    )

    assert second.releases == []


# --------------------------------------------------------------------------
# 5. strictness asymmetry -- current behavior, deliberately pinned
# --------------------------------------------------------------------------


def test_lifecycle_envelope_is_strict_but_capability_specs_are_not() -> None:
    """A real asymmetry in today's contract; the move must not "tidy" it.

    ``ChartLifecycle``/``ChartLifecycleSpec``/``ChartLifecycleMetadata`` set
    ``strict=True``, so ``spec.enabled: "true"`` is rejected. The nested
    ``ManifestValidationSpec`` and ``ClusterTestSpec`` set only
    ``extra="forbid"``, so *their* ``enabled: "true"`` is coerced to ``True``.
    Making the nested models strict would reject YAML that parses today.
    """
    with pytest.raises(ValidationError) as exc_info:
        ChartLifecycleSpec.model_validate({"enabled": "true"})
    assert _error_types(exc_info) == {"bool_type"}

    validation = ManifestValidationSpec.model_validate(
        {"enabled": "true", "releaseName": "demo", "environments": {"dev": {"namespace": "d"}}}
    )
    cluster_test = ClusterTestSpec.model_validate({"enabled": 1, "profiles": {}})

    assert validation.enabled is True
    assert cluster_test.enabled is True


def test_cluster_test_timeout_is_an_unvalidated_string() -> None:
    """Unlike local releases, cluster-test timeouts are not shape-checked."""
    assert ClusterTestProfile.model_validate({"timeout": "not-a-duration"}).timeout == (
        "not-a-duration"
    )


def test_chart_lifecycle_metadata_name_rules() -> None:
    """Lifecycle names are "non-empty, not padded"; local names are DNS labels."""
    assert ChartLifecycleMetadata(name="Not_A_DNS_Label").name == "Not_A_DNS_Label"
    assert ResourceMetadata(name="dns-label").name == "dns-label"

    with pytest.raises(ValidationError):
        ResourceMetadata(name="Not_A_DNS_Label")


# --------------------------------------------------------------------------
# 6. negative cases -- each must keep failing, in the same category
# --------------------------------------------------------------------------


def _lifecycle(spec: dict[str, Any], **envelope: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "apiVersion": LIFECYCLE_API_VERSION,
        "kind": LIFECYCLE_KIND,
        "metadata": {"name": "demo"},
        "spec": spec,
    }
    document.update(envelope)
    return document


def _validation(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "releaseName": "demo",
        "environments": {"dev": {"namespace": "lab-dev"}},
    }
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            _lifecycle({}, status={}),
            "extra_forbidden",
            id="unknown-envelope-field",
        ),
        pytest.param(
            _lifecycle({"bogus": True}),
            "extra_forbidden",
            id="unknown-spec-field",
        ),
        pytest.param(
            _lifecycle({}, metadata={"name": "demo", "labels": {}}),
            "extra_forbidden",
            id="unknown-metadata-field",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(bogus=True)}),
            "extra_forbidden",
            id="unknown-validation-field",
        ),
        pytest.param(
            _lifecycle({"cluster_test": {"profiles": {}}}),
            "extra_forbidden",
            id="snake-case-clusterTest",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(release_name="demo")}),
            "extra_forbidden",
            id="snake-case-releaseName",
        ),
        pytest.param(
            _lifecycle({}, apiVersion="lifecycle.chartmanager.io/v1"),
            "literal_error",
            id="wrong-apiVersion",
        ),
        pytest.param(
            _lifecycle({}, apiVersion="local.chartmanager.io/v1alpha1"),
            "literal_error",
            id="other-group-apiVersion",
        ),
        pytest.param(
            _lifecycle({}, kind="Chart"),
            "literal_error",
            id="wrong-kind",
        ),
        pytest.param(
            _lifecycle({}, metadata={"name": " demo "}),
            "value_error",
            id="padded-metadata-name",
        ),
        pytest.param(
            _lifecycle({}, metadata={"name": ""}),
            "string_too_short",
            id="empty-metadata-name",
        ),
        pytest.param(
            _lifecycle({}, metadata={"name": 123}),
            "string_type",
            id="non-string-metadata-name",
        ),
        pytest.param(
            _lifecycle({}, metadata={}),
            "missing",
            id="missing-metadata-name",
        ),
        pytest.param(
            _lifecycle({"validation": {"environments": {"dev": {"namespace": "d"}}}}),
            "missing",
            id="missing-releaseName",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(environments={})}),
            "value_error",
            id="empty-environments",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(environments={"dev": {}})}),
            "value_error",
            id="environment-without-namespace-or-template",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(helmVersion="4.1.3", helmBinary="/opt/helm")}),
            "value_error",
            id="mutually-exclusive-helm-settings",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(triggers={"values.yaml": ["staging"]})}),
            "value_error",
            id="unknown-trigger-environment",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(triggers={"values.yaml": "bogus"})}),
            "literal_error",
            id="unknown-trigger-string",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(unmatchedChanges="ignore")}),
            "literal_error",
            id="unknown-unmatched-changes-policy",
        ),
        pytest.param(
            _lifecycle(
                {"validation": _validation(environments={"dev": {"values": ["/etc/passwd"]}})}
            ),
            "value_error",
            id="absolute-environment-values-path",
        ),
        pytest.param(
            _lifecycle(
                {"validation": _validation(environments={"dev": {"values": ["../secrets.yaml"]}})}
            ),
            "value_error",
            id="escaping-environment-values-path",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(triggerIgnores=["../README.md"])}),
            "value_error",
            id="escaping-trigger-ignore",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(policies={"extra": ["/etc/policies"]})}),
            "value_error",
            id="absolute-policy-path",
        ),
        pytest.param(
            _lifecycle({"validation": _validation(validators={"conftest": True})}),
            "extra_forbidden",
            id="unknown-validator",
        ),
        pytest.param(
            _lifecycle({"clusterTest": {"profiles": {"m": {"values": ["/etc/passwd"]}}}}),
            "value_error",
            id="absolute-cluster-test-values-path",
        ),
        pytest.param(
            _lifecycle({"clusterTest": {"profiles": {"m": {"values": ["../x.yaml"]}}}}),
            "value_error",
            id="escaping-cluster-test-values-path",
        ),
        pytest.param(
            _lifecycle({"clusterTest": {"profiles": {"m": {"helm_test": False}}}}),
            "extra_forbidden",
            id="snake-case-helmTest",
        ),
        pytest.param(
            _lifecycle({"clusterTest": {"profiles": {"m": {"requires": [{"bogus": 1}]}}}}),
            "extra_forbidden",
            id="unknown-cluster-test-ref-field",
        ),
        pytest.param(
            _lifecycle({"clusterTest": {}}),
            "missing",
            id="missing-cluster-test-profiles",
        ),
    ],
)
def test_chart_lifecycle_rejections_stay_in_category(
    document: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChartLifecycle.model_validate(document)

    assert expected in _error_types(exc_info)


def _cluster(spec: dict[str, Any], **envelope: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "apiVersion": LOCAL_API_VERSION,
        "kind": LOCAL_CLUSTER_KIND,
        "metadata": {"name": "demo"},
        "spec": spec,
    }
    document.update(envelope)
    return document


def _bootstrap(*releases: dict[str, Any]) -> dict[str, Any]:
    return {"cluster": {"config": "kind-config.yaml"}, "bootstrap": {"releases": list(releases)}}


def _oci(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "type": "oci",
        "name": "remote",
        "chart": "oci://example.test/charts/remote",
        "namespace": "remote",
        "values": [],
        "timeout": "1m",
    }
    raw.update(overrides)
    return raw


def _local(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "type": "local",
        "name": "demo",
        "chart": "charts/demo",
        "namespace": "demo",
        "values": [],
        "timeout": "1m",
    }
    raw.update(overrides)
    return raw


_DIGEST = "sha256:" + "0" * 64


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            _cluster(_bootstrap(), status={}),
            "extra_forbidden",
            id="unknown-envelope-field",
        ),
        pytest.param(
            _cluster(_bootstrap(), apiVersion="local.chartmanager.io/v1"),
            "literal_error",
            id="wrong-apiVersion",
        ),
        pytest.param(
            _cluster(_bootstrap(), apiVersion="lifecycle.chartmanager.io/v1alpha1"),
            "literal_error",
            id="other-group-apiVersion",
        ),
        pytest.param(
            _cluster(_bootstrap(), kind="LocalStack"),
            "literal_error",
            id="wrong-kind",
        ),
        pytest.param(
            _cluster(_bootstrap(), metadata={"name": "Default"}),
            "value_error",
            id="uppercase-metadata-name",
        ),
        pytest.param(
            _cluster(_bootstrap(), metadata={"name": "a" * 64}),
            "value_error",
            id="over-long-metadata-name",
        ),
        pytest.param(
            _cluster(_bootstrap(), metadata={"name": "trailing-"}),
            "value_error",
            id="non-dns-metadata-name",
        ),
        pytest.param(
            _cluster({"cluster": {"config": "/etc/kind.yaml"}, "bootstrap": {"releases": []}}),
            "value_error",
            id="absolute-cluster-config",
        ),
        pytest.param(
            _cluster({"cluster": {"config": "../kind.yaml"}, "bootstrap": {"releases": []}}),
            "value_error",
            id="escaping-cluster-config",
        ),
        pytest.param(
            _cluster({"cluster": {"config": "./kind.yaml"}, "bootstrap": {"releases": []}}),
            "value_error",
            id="dot-segment-cluster-config",
        ),
        pytest.param(
            _cluster({"cluster": {"config": ""}, "bootstrap": {"releases": []}}),
            "value_error",
            id="empty-cluster-config",
        ),
        pytest.param(
            _cluster({"cluster": {"config": "kind-config.yaml"}}),
            "missing",
            id="missing-bootstrap",
        ),
        pytest.param(
            _cluster(_bootstrap({"type": "helm", "chart": "x"})),
            "union_tag_invalid",
            id="unknown-release-type",
        ),
        pytest.param(
            _cluster(_bootstrap({"chart": "charts/demo", "profile": "minimal"})),
            "union_tag_not_found",
            id="release-without-discriminator",
        ),
        pytest.param(
            _cluster(_bootstrap(_local(chart="/etc/demo"))),
            "value_error",
            id="absolute-release-chart",
        ),
        pytest.param(
            _cluster(_bootstrap(_local(values=["../outside.yaml"]))),
            "value_error",
            id="escaping-release-values",
        ),
        pytest.param(
            _cluster(_bootstrap(_local(timeout="10 m"))),
            "value_error",
            id="malformed-helm-timeout",
        ),
        pytest.param(
            _cluster(_bootstrap(_local(timeout="0s"))),
            "value_error",
            id="zero-helm-timeout",
        ),
        pytest.param(
            _cluster(_bootstrap(_local(timeout=" 10m"))),
            "value_error",
            id="padded-helm-timeout",
        ),
        pytest.param(
            _cluster(_bootstrap(_oci())),
            "value_error",
            id="oci-without-a-pin",
        ),
        pytest.param(
            _cluster(_bootstrap(_oci(version="1.2.3", digest=_DIGEST))),
            "value_error",
            id="oci-with-two-pins",
        ),
        pytest.param(
            _cluster(_bootstrap(_oci(version="1.2"))),
            "value_error",
            id="oci-inexact-semver",
        ),
        pytest.param(
            _cluster(_bootstrap(_oci(version="latest"))),
            "value_error",
            id="oci-floating-tag",
        ),
        pytest.param(
            _cluster(_bootstrap(_oci(digest="sha256:ABC"))),
            "value_error",
            id="oci-short-digest",
        ),
        pytest.param(
            _cluster(_bootstrap(_oci(digest="sha256:" + "A" * 64))),
            "value_error",
            id="oci-uppercase-digest",
        ),
        pytest.param(
            _cluster(_bootstrap(_oci(version="1.2.3", chart="https://example.test/charts/remote"))),
            "value_error",
            id="oci-non-oci-reference",
        ),
        pytest.param(
            _cluster(
                _bootstrap(
                    _oci(version="1.2.3", chart=f"oci://example.test/charts/remote@{_DIGEST}")
                )
            ),
            "value_error",
            id="oci-digest-inlined-in-chart",
        ),
        pytest.param(
            _cluster(
                _bootstrap(
                    {
                        "type": "lifecycle",
                        "chart": "charts/demo",
                        "profile": "minimal",
                        "runtimeValues": {"a": "${kind.unknown}"},
                    }
                )
            ),
            "value_error",
            id="unknown-kind-runtime-placeholder",
        ),
        pytest.param(
            _cluster(
                _bootstrap(
                    {
                        "type": "lifecycle",
                        "chart": "charts/demo",
                        "profile": "minimal",
                        "readiness": {"nodesReady": 1},
                    }
                )
            ),
            "bool_type",
            id="non-bool-nodesReady",
        ),
        pytest.param(
            _cluster(
                _bootstrap(
                    {
                        "type": "lifecycle",
                        "chart": "charts/demo",
                        "profile": "minimal",
                        "readiness": {"workloadsReady": {"namespace": "BAD", "timeout": "1m"}},
                    }
                )
            ),
            "value_error",
            id="non-dns-workloads-namespace",
        ),
        pytest.param(
            _cluster(
                _bootstrap(
                    {
                        "type": "lifecycle",
                        "chart": "charts/demo",
                        "profile": "minimal",
                        "readiness": {"workloadsReady": {"namespace": "demo", "timeout": "0s"}},
                    }
                )
            ),
            "value_error",
            id="zero-workloads-timeout",
        ),
        pytest.param(
            _cluster(
                _bootstrap(
                    {
                        "type": "lifecycle",
                        "chart": "charts/demo",
                        "profile": "minimal",
                        "runtime_values": {},
                    }
                )
            ),
            "extra_forbidden",
            id="snake-case-runtimeValues",
        ),
    ],
)
def test_local_cluster_rejections_stay_in_category(
    document: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        LocalCluster.model_validate(document)

    assert expected in _error_types(exc_info)


def _stack(*releases: dict[str, Any], **envelope: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "apiVersion": LOCAL_API_VERSION,
        "kind": LOCAL_STACK_KIND,
        "metadata": {"name": "demo"},
        "spec": {"releases": list(releases)},
    }
    document.update(envelope)
    return document


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(_stack(), "too_short", id="no-releases"),
        pytest.param(
            _stack({"type": "lifecycle", "chart": "charts/demo", "profile": "minimal"}, status={}),
            "extra_forbidden",
            id="unknown-envelope-field",
        ),
        pytest.param(
            _stack(_local()),
            "union_tag_invalid",
            id="local-release-not-allowed-in-a-stack",
        ),
        pytest.param(
            _stack(_oci(version="1.2.3", runtimeValues={})),
            "extra_forbidden",
            id="bootstrap-only-runtimeValues",
        ),
        pytest.param(
            _stack(_oci(version="1.2.3", readiness={"nodesReady": True})),
            "extra_forbidden",
            id="bootstrap-only-readiness",
        ),
        pytest.param(
            _stack({"type": "lifecycle", "chart": "charts/demo", "profile": "Not-A-Label"}),
            "value_error",
            id="non-dns-release-profile",
        ),
        pytest.param(
            _stack({"type": "lifecycle", "chart": "charts/demo"}),
            "missing",
            id="lifecycle-release-without-profile",
        ),
        pytest.param(
            _stack(_oci(version="1.2.3"), kind="LocalCluster"),
            "literal_error",
            id="wrong-kind",
        ),
    ],
)
def test_local_stack_rejections_stay_in_category(
    document: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        LocalStack.model_validate(document)

    assert expected in _error_types(exc_info)


def test_discriminator_failures_name_the_tag_and_the_known_variants() -> None:
    """The discriminator message is what an author sees for a mistyped release."""
    with pytest.raises(ValidationError) as exc_info:
        LocalStack.model_validate(_stack(_local()))

    message = str(exc_info.value)
    assert "union_tag_invalid" in message
    assert "'local'" in message
    assert "'lifecycle', 'oci'" in message


# --------------------------------------------------------------------------
# 7. JSON Schema comparison input (plan Phase 0 item 5)
# --------------------------------------------------------------------------


def _generate_schemas() -> dict[str, Any]:
    return {name: model.model_json_schema() for name, model in sorted(ROOT_MODELS.items())}


def _serialize_schemas(schemas: dict[str, Any]) -> str:
    return json.dumps(schemas, indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize("name", sorted(ROOT_MODELS), ids=sorted(ROOT_MODELS))
def test_root_model_json_schema_matches_the_snapshot(name: str) -> None:
    """Relocating a model must not change the schema it generates.

    Pydantic's JSON Schema is derived from class names, docstrings, aliases,
    defaults and field order -- never from the module a class lives in -- so a
    pure move leaves this byte-identical.

    What this pins is the schema's *content*: every property, its type, its
    `const`/`enum`, its default, and the `required` list (a JSON array, so its
    order is compared). It does not pin property order -- the comparison is
    between parsed dicts and `_serialize_schemas` writes with `sort_keys=True`.
    Authored key order is covered instead by the alias round-trip tests, which
    assert exact key sequences at every level of a real document.
    """
    expected = json.loads(SCHEMA_SNAPSHOT.read_text(encoding="utf-8"))

    assert name in expected, f"{name} missing from {SCHEMA_SNAPSHOT.name}"
    assert ROOT_MODELS[name].model_json_schema() == expected[name]


def test_schema_snapshot_is_complete_and_deterministic() -> None:
    """The checked-in snapshot is exactly what the models generate, sorted."""
    assert SCHEMA_SNAPSHOT.read_text(encoding="utf-8") == _serialize_schemas(_generate_schemas())
