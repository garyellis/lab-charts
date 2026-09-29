"""Pin the authored resource contracts in ``chart_manager.api``.

The authored kinds are what repository authors write, so their observable
behavior is pinned here: the checked-in documents parse, fixtures round-trip
through the authored spellings, ``default_factory`` defaults the schema cannot
show, strictness, rejection categories, discriminators and the JSON Schema
each root model generates.

Regenerate the schema snapshot in ``tests/fixtures/api/expected-schemas.json``
only when a reviewer has confirmed the diff is intentional::

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

from chart_manager.api.v1alpha1.chart_lifecycle import (
    ALL_ENVIRONMENTS,
    CHART_LIFECYCLE_KIND,
    MATCH_BY_BASENAME,
    ChartLifecycle,
    ChartLifecycleMetadata,
    ChartLifecycleSpec,
    ClusterTestProfile,
    ClusterTestSpec,
    ManifestValidationEnvironmentSpec,
    ManifestValidationPolicySpec,
    ManifestValidationSpec,
    ManifestValidationValidatorsSpec,
)
from chart_manager.api.v1alpha1.chart_workspace import (
    CHART_WORKSPACE_KIND,
    ChartWorkspace,
)
from chart_manager.api.v1alpha1.common import API_VERSION
from chart_manager.api.v1alpha1.local_cluster import (
    LOCAL_CLUSTER_KIND,
    LocalBootstrap,
    LocalCluster,
)
from chart_manager.api.v1alpha1.local_stack import LOCAL_STACK_KIND, LocalStack
from chart_manager.api.v1alpha1.releases import (
    BootstrapLifecycleRelease,
    BootstrapLocalChartRelease,
    BootstrapOciChartRelease,
    BootstrapRepoChartRelease,
    LifecycleRelease,
    OciChartRelease,
    RepoChartRelease,
    ResourceMetadata,
)
from chart_manager.domain.lifecycle_policy import LIFECYCLE_FILENAME
from chart_manager.domain.local_resources import DEFAULT_STACKS_DIR
from chart_manager.settings import DEFAULT_LOCAL_CONFIG

from .conftest import REPO_ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "api"
SCHEMA_SNAPSHOT = FIXTURES / "expected-schemas.json"

#: The root models whose JSON Schema is compared against the snapshot.
ROOT_MODELS: dict[str, type[BaseModel]] = {
    "ChartWorkspace": ChartWorkspace,
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
    assert API_VERSION == "chartmanager.io/v1alpha1"
    assert CHART_LIFECYCLE_KIND == "ChartLifecycle"
    assert LIFECYCLE_FILENAME == "chart-lifecycle.yaml"
    assert MATCH_BY_BASENAME == "match-by-basename"
    assert ALL_ENVIRONMENTS == "all-environments"
    assert LOCAL_CLUSTER_KIND == "LocalCluster"
    assert LOCAL_STACK_KIND == "LocalStack"
    assert CHART_WORKSPACE_KIND == "ChartWorkspace"


# --------------------------------------------------------------------------
# 2. every checked-in authored document still parses
# --------------------------------------------------------------------------

LIFECYCLE_DOCUMENTS = _discover(LIFECYCLE_FILENAME)
LOCAL_CLUSTER_DOCUMENT = REPO_ROOT / DEFAULT_LOCAL_CONFIG
WORKSPACE_DOCUMENT = REPO_ROOT / ".chart-manager/workspace.yaml"
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
    assert WORKSPACE_DOCUMENT.is_file()
    assert LOCAL_STACK_DOCUMENTS == []


@pytest.mark.parametrize("path", LIFECYCLE_DOCUMENTS, ids=_rel)
def test_every_checked_in_chart_lifecycle_parses(path: Path) -> None:
    resource = ChartLifecycle.model_validate(_read_yaml(path))

    assert resource.api_version == API_VERSION
    assert resource.kind == CHART_LIFECYCLE_KIND
    assert ChartLifecycle.model_validate(resource.model_dump(by_alias=True)) == resource


def test_repository_local_cluster_parses() -> None:
    resource = LocalCluster.model_validate(_read_yaml(LOCAL_CLUSTER_DOCUMENT))

    assert resource.api_version == API_VERSION
    assert resource.kind == LOCAL_CLUSTER_KIND
    assert LocalCluster.model_validate(resource.model_dump(by_alias=True)) == resource


def test_repository_chart_workspace_parses() -> None:
    resource = ChartWorkspace.model_validate(_read_yaml(WORKSPACE_DOCUMENT))

    assert resource.api_version == API_VERSION
    assert resource.kind == CHART_WORKSPACE_KIND
    assert ChartWorkspace.model_validate(resource.model_dump(by_alias=True)) == resource


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


def test_local_cluster_fixture_round_trips_through_authored_aliases() -> None:
    document = _read_yaml(FIXTURES / "local-cluster.yaml")
    resource = LocalCluster.model_validate(document)

    dumped = resource.model_dump(mode="json", by_alias=True)

    assert LocalCluster.model_validate(dumped) == resource
    _assert_authored_subset(document, dumped)

    releases = resource.spec.bootstrap.releases
    assert [type(release) for release in releases] == [
        BootstrapLifecycleRelease,
        BootstrapLocalChartRelease,
        BootstrapOciChartRelease,
        BootstrapOciChartRelease,
        BootstrapRepoChartRelease,
    ]
    # Repository-relative paths are typed as `Path` but serialize as the
    # authored POSIX spelling.
    assert dumped["spec"]["cluster"]["config"] == "kind-config.yaml"
    assert isinstance(resource.spec.cluster.config, Path)


def test_chart_workspace_fixture_round_trips_through_authored_aliases() -> None:
    document = _read_yaml(FIXTURES / "chart-workspace.yaml")
    resource = ChartWorkspace.model_validate(document)

    dumped = resource.model_dump(mode="json", by_alias=True)

    assert ChartWorkspace.model_validate(dumped) == resource
    _assert_authored_subset(document, dumped)


def test_local_stack_fixture_round_trips_through_authored_aliases() -> None:
    document = _read_yaml(FIXTURES / "local-stack.yaml")
    resource = LocalStack.model_validate(document)

    dumped = resource.model_dump(mode="json", by_alias=True)

    assert LocalStack.model_validate(dumped) == resource
    _assert_authored_subset(document, dumped)
    assert [type(release) for release in resource.spec.releases] == [
        LifecycleRelease,
        OciChartRelease,
        RepoChartRelease,
    ]


@pytest.mark.parametrize(
    ("model", "fixture"),
    [
        (ChartLifecycle, LIFECYCLE_FILENAME),
        (ChartWorkspace, "chart-workspace.yaml"),
        (LocalCluster, "local-cluster.yaml"),
        (LocalStack, "local-stack.yaml"),
    ],
    ids=["ChartLifecycle", "ChartWorkspace", "LocalCluster", "LocalStack"],
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


def _minimal_validation() -> ManifestValidationSpec:
    return ManifestValidationSpec.model_validate(
        {"releaseName": "demo", "environments": {"dev": {"namespace": "lab-dev"}}}
    )


def test_manifest_validation_spec_defaults() -> None:
    spec = _minimal_validation()

    assert spec.schema_locations == []
    assert spec.triggers == {}
    assert spec.trigger_ignores == []
    assert spec.validators == ManifestValidationValidatorsSpec(kubeconform=True, policy=True)
    assert spec.policies == ManifestValidationPolicySpec(extra=[])


def test_manifest_validation_environment_defaults() -> None:
    assert ManifestValidationEnvironmentSpec().values == []


def test_cluster_test_defaults() -> None:
    spec = ClusterTestSpec.model_validate({"profiles": {}})
    profile = ClusterTestProfile(namespace="demo")

    assert spec.dependent_tests == []
    assert profile.requires == []
    assert profile.values == ["values.yaml"]
    assert profile.hooks is None


def test_local_resource_defaults() -> None:
    release = BootstrapLifecycleRelease.model_validate(
        {"type": "lifecycle", "chart": "charts/demo", "profile": "minimal"}
    )
    assert LocalBootstrap().releases == []
    assert release.runtime_values == {}


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
        "apiVersion": API_VERSION,
        "kind": CHART_LIFECYCLE_KIND,
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


def _hooks(hooks: Any) -> dict[str, Any]:
    """A lifecycle whose one otherwise-valid profile carries ``hooks``."""
    return _lifecycle({"clusterTest": {"profiles": {"m": {"namespace": "d", "hooks": hooks}}}})


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
            _lifecycle({}, apiVersion="chartmanager.io/v1beta1"),
            "literal_error",
            id="wrong-apiVersion",
        ),
        pytest.param(
            _lifecycle({}, apiVersion="lifecycle.chartmanager.io/v1alpha1"),
            "literal_error",
            id="legacy-lifecycle-apiVersion",
        ),
        pytest.param(
            _lifecycle({}, apiVersion="local.chartmanager.io/v1alpha1"),
            "literal_error",
            id="legacy-local-apiVersion",
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
            _lifecycle(
                {"clusterTest": {"profiles": {"m": {"namespace": "d", "values": ["/etc/passwd"]}}}}
            ),
            "value_error",
            id="absolute-cluster-test-values-path",
        ),
        pytest.param(
            _lifecycle(
                {"clusterTest": {"profiles": {"m": {"namespace": "d", "values": ["../x.yaml"]}}}}
            ),
            "value_error",
            id="escaping-cluster-test-values-path",
        ),
        pytest.param(
            _lifecycle(
                {"clusterTest": {"profiles": {"m": {"namespace": "d", "helm_test": False}}}}
            ),
            "extra_forbidden",
            id="snake-case-helmTest",
        ),
        pytest.param(
            _lifecycle(
                {"clusterTest": {"profiles": {"m": {"namespace": "d", "requires": [{"bogus": 1}]}}}}
            ),
            "extra_forbidden",
            id="unknown-cluster-test-ref-field",
        ),
        pytest.param(
            _lifecycle({"clusterTest": {}}),
            "missing",
            id="missing-cluster-test-profiles",
        ),
        pytest.param(
            _lifecycle({"clusterTest": {"profiles": {"m": {}}}}),
            "missing",
            id="missing-cluster-test-namespace",
        ),
        pytest.param(
            _lifecycle({"clusterTest": {"profiles": {"m": {"namespace": ""}}}}),
            "string_too_short",
            id="empty-cluster-test-namespace",
        ),
        pytest.param(
            _hooks({"preInstall": []}),
            "value_error",
            id="empty-cluster-test-hook-argv",
        ),
        pytest.param(
            _hooks({"cleanup": [""]}),
            "value_error",
            id="empty-cluster-test-hook-arg",
        ),
        pytest.param(
            _hooks({"preInstall": "echo hi"}),
            "list_type",
            id="shell-string-cluster-test-hook",
        ),
        pytest.param(
            _hooks({"postInstall": [1]}),
            "string_type",
            id="non-string-cluster-test-hook-arg",
        ),
        pytest.param(
            _hooks({"pre_install": ["x"]}),
            "extra_forbidden",
            id="snake-case-preInstall",
        ),
        pytest.param(
            _hooks({"preUpgrade": ["x"]}),
            "extra_forbidden",
            id="unknown-cluster-test-hook",
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
        "apiVersion": API_VERSION,
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
            _cluster(_bootstrap(), apiVersion="chartmanager.io/v1beta1"),
            "literal_error",
            id="wrong-apiVersion",
        ),
        pytest.param(
            _cluster(_bootstrap(), apiVersion="lifecycle.chartmanager.io/v1alpha1"),
            "literal_error",
            id="legacy-lifecycle-apiVersion",
        ),
        pytest.param(
            _cluster(_bootstrap(), apiVersion="local.chartmanager.io/v1alpha1"),
            "literal_error",
            id="legacy-local-apiVersion",
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
        "apiVersion": API_VERSION,
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
            _stack(_oci(version="1.2.3"), apiVersion="chartmanager.io/v1beta1"),
            "literal_error",
            id="wrong-apiVersion",
        ),
        pytest.param(
            _stack(_oci(version="1.2.3"), apiVersion="lifecycle.chartmanager.io/v1alpha1"),
            "literal_error",
            id="legacy-lifecycle-apiVersion",
        ),
        pytest.param(
            _stack(_oci(version="1.2.3"), apiVersion="local.chartmanager.io/v1alpha1"),
            "literal_error",
            id="legacy-local-apiVersion",
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


def test_root_model_json_schemas_match_the_snapshot() -> None:
    """The checked-in snapshot is complete and exactly what the models generate.

    Byte equality with `_serialize_schemas` output pins every property, its
    type, `const`/`enum`, default and `required` list, and that the file is
    deterministic, so a regeneration diff is exactly the contract change.
    """
    assert SCHEMA_SNAPSHOT.read_text(encoding="utf-8") == _serialize_schemas(_generate_schemas())
