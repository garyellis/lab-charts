"""Tests for the standalone ChartLifecycle intent boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from chart_manager.api.lifecycle.v1alpha1 import LIFECYCLE_API_VERSION, ChartLifecycle
from chart_manager.plumbing.errors import CapabilityUnavailableError, SpecError
from chart_manager.services.chart_config import (
    LIFECYCLE_FILENAME,
    CapabilityStatus,
    cluster_test_status,
    load_chart_lifecycle,
    load_optional_chart_lifecycle,
    require_cluster_test,
    require_validation,
    validate_chart_lifecycle_identity,
    validation_status,
)
from chart_manager.services.domain.cluster_test_policy import require_cluster_test_profile


def _write_lifecycle(tmp_path: Path, spec: str, *, name: str = "demo") -> Path:
    path = tmp_path / LIFECYCLE_FILENAME
    path.write_text(
        f"""
apiVersion: {LIFECYCLE_API_VERSION}
kind: ChartLifecycle
metadata:
  name: {name}
spec:
{spec}
""",
        encoding="utf-8",
    )
    return path


def _cluster_spec(*, root_enabled: bool = True, section_enabled: bool = True) -> str:
    return f"""  enabled: {str(root_enabled).lower()}
  clusterTest:
    enabled: {str(section_enabled).lower()}
    profiles:
      minimal:
        values: [values.yaml]
        helmTest: true
    dependentTests: []
"""


def _validation_spec(*, root_enabled: bool = True, section_enabled: bool = True) -> str:
    return f"""  enabled: {str(root_enabled).lower()}
  validation:
    enabled: {str(section_enabled).lower()}
    releaseName: demo
    namespaceTemplate: "lab-${{env}}"
    environments:
      ci:
        values: [values.yaml]
    triggers:
      templates/**: [ci]
    unmatchedChanges: warn
"""


def test_loads_each_capability_from_chart_lifecycle(tmp_path: Path) -> None:
    cluster = load_chart_lifecycle(_write_lifecycle(tmp_path, _cluster_spec()))

    assert cluster.api_version == LIFECYCLE_API_VERSION
    assert cluster.kind == "ChartLifecycle"
    assert cluster.metadata.name == "demo"
    assert cluster.spec.cluster_test is not None
    assert require_cluster_test_profile(cluster.spec.cluster_test, "minimal").helm_test is True
    assert cluster_test_status(cluster) is CapabilityStatus.ENABLED

    validation = load_chart_lifecycle(_write_lifecycle(tmp_path, _validation_spec()))
    assert validation.spec.validation is not None
    assert validation.spec.validation.release_name == "demo"
    assert validation_status(validation) is CapabilityStatus.ENABLED


def test_both_capabilities_can_share_one_spec(tmp_path: Path) -> None:
    lifecycle = load_chart_lifecycle(
        _write_lifecycle(
            tmp_path,
            _validation_spec()
            + """  clusterTest:
    profiles:
      minimal: {}
""",
        )
    )

    assert validation_status(lifecycle) is CapabilityStatus.ENABLED
    assert cluster_test_status(lifecycle) is CapabilityStatus.ENABLED


def test_enabled_defaults_true_and_capabilities_are_optional(tmp_path: Path) -> None:
    lifecycle = load_chart_lifecycle(_write_lifecycle(tmp_path, "  {}\n"))

    assert lifecycle.spec.enabled is True
    assert validation_status(lifecycle) is CapabilityStatus.ABSENT
    assert cluster_test_status(lifecycle) is CapabilityStatus.ABSENT


@pytest.mark.parametrize(
    "body",
    [
        "kind: ChartLifecycle\nmetadata: {name: demo}\nspec: {}\n",
        (
            f"apiVersion: {LIFECYCLE_API_VERSION}\nkind: Wrong\n"
            "metadata: {name: demo}\nspec: {}\n"
        ),
        (
            f"apiVersion: {LIFECYCLE_API_VERSION}\nkind: ChartLifecycle\n"
            "metadata: {}\nspec: {}\n"
        ),
        (
            f"apiVersion: {LIFECYCLE_API_VERSION}\nkind: ChartLifecycle\n"
            "metadata: {name: ' demo'}\nspec: {}\n"
        ),
        (
            f"apiVersion: {LIFECYCLE_API_VERSION}\nkind: ChartLifecycle\n"
            "metadata: {name: demo, extra: true}\nspec: {}\n"
        ),
        (
            f"apiVersion: {LIFECYCLE_API_VERSION}\nkind: ChartLifecycle\n"
            "metadata: {name: demo}\nspec: {enabled: 'true'}\n"
        ),
        (
            f"apiVersion: {LIFECYCLE_API_VERSION}\nkind: ChartLifecycle\n"
            "metadata: {name: demo}\nspec: {}\nextra: true\n"
        ),
        "- apiVersion\n- lifecycle.cmg.io/v1alpha1\n",
    ],
)
def test_envelope_is_strict(tmp_path: Path, body: str) -> None:
    path = tmp_path / LIFECYCLE_FILENAME
    path.write_text(body, encoding="utf-8")

    with pytest.raises(SpecError, match="invalid chart lifecycle configuration"):
        load_chart_lifecycle(path)


def test_disabled_sections_remain_schema_validated(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match=r"releaseName"):
        load_chart_lifecycle(
            _write_lifecycle(
                tmp_path,
                """  validation:
    enabled: false
    environments:
      ci:
        namespace: demo
""",
            )
        )


def test_optional_loader_only_tolerates_absent_canonical_file(tmp_path: Path) -> None:
    path = tmp_path / LIFECYCLE_FILENAME
    assert load_optional_chart_lifecycle(path) is None

    path.write_text("not: [valid", encoding="utf-8")
    with pytest.raises(SpecError, match="invalid chart lifecycle configuration"):
        load_optional_chart_lifecycle(path)


def test_strict_loader_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="missing chart lifecycle configuration"):
        load_chart_lifecycle(tmp_path / LIFECYCLE_FILENAME)


def test_composition_requires_all_three_chart_names_to_match(tmp_path: Path) -> None:
    lifecycle = load_chart_lifecycle(_write_lifecycle(tmp_path, "  {}\n", name="other"))

    with pytest.raises(
        SpecError,
        match=r"metadata\.name 'other'.*directory.*demo.*Chart\.yaml name 'demo'",
    ):
        validate_chart_lifecycle_identity(
            lifecycle,
            chart_name="demo",
            chart_directory=tmp_path / "demo",
        )


def test_capability_status_distinguishes_absent_disabled_and_enabled() -> None:
    absent = ChartLifecycle.model_validate(
        {
            "apiVersion": LIFECYCLE_API_VERSION,
            "kind": "ChartLifecycle",
            "metadata": {"name": "demo"},
            "spec": {"enabled": False},
        }
    )
    disabled = ChartLifecycle.model_validate(
        {
            "apiVersion": LIFECYCLE_API_VERSION,
            "kind": "ChartLifecycle",
            "metadata": {"name": "demo"},
            "spec": {
                "clusterTest": {
                    "enabled": False,
                    "profiles": {"minimal": {}},
                }
            },
        }
    )
    enabled = ChartLifecycle.model_validate(
        {
            "apiVersion": LIFECYCLE_API_VERSION,
            "kind": "ChartLifecycle",
            "metadata": {"name": "demo"},
            "spec": {"clusterTest": {"profiles": {"minimal": {}}}},
        }
    )

    assert cluster_test_status(None) is CapabilityStatus.ABSENT
    assert cluster_test_status(absent) is CapabilityStatus.ABSENT
    assert cluster_test_status(disabled) is CapabilityStatus.DISABLED
    assert cluster_test_status(enabled) is CapabilityStatus.ENABLED


@pytest.mark.parametrize(
    ("lifecycle", "helper", "message"),
    [
        (
            None,
            require_validation,
            "chart 'demo' has no validation configuration in chart-lifecycle.yaml",
        ),
        (
            ChartLifecycle.model_validate(
                {
                    "apiVersion": LIFECYCLE_API_VERSION,
                    "kind": "ChartLifecycle",
                    "metadata": {"name": "demo"},
                    "spec": {"enabled": False},
                }
            ),
            require_validation,
            "chart 'demo' has no validation configuration in chart-lifecycle.yaml",
        ),
        (
            None,
            require_cluster_test,
            "chart 'demo' has no clusterTest configuration in chart-lifecycle.yaml",
        ),
        (
            ChartLifecycle.model_validate(
                {
                    "apiVersion": LIFECYCLE_API_VERSION,
                    "kind": "ChartLifecycle",
                    "metadata": {"name": "demo"},
                    "spec": {
                        "clusterTest": {
                            "enabled": False,
                            "profiles": {"minimal": {}},
                        }
                    },
                }
            ),
            require_cluster_test,
            "cluster tests are disabled for chart 'demo'",
        ),
    ],
)
def test_require_helpers_report_precise_unavailable_reason(
    lifecycle: ChartLifecycle | None,
    helper: Callable[..., object],
    message: str,
) -> None:
    with pytest.raises(CapabilityUnavailableError) as caught:
        helper(lifecycle, chart_name="demo")
    assert str(caught.value) == message
    assert not isinstance(caught.value, SpecError)
