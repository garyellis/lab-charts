"""Unit tests for ValidateSpec parsing + validators."""
from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.validate_spec import (
    MATCH_BY_BASENAME,
    ValidateSpec,
    load_validate_spec,
    resolve_namespace,
)


def _write_spec(path: Path, body: str) -> Path:
    spec = path / "validate-spec.yaml"
    spec.write_text(body)
    return spec


def test_roundtrip_full_fixture(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        """
version: 1
release_name: demo
namespace_template: "lab-${env}"
helm_version: "4.1.3"
kubernetes_version: "1.31.2"
schema_locations:
  - default
environments:
  dev:
    values: [values.yaml, values-dev.yaml]
  prod:
    namespace: lab-prod
    values: [values.yaml]
triggers:
  "values.yaml": [dev, prod]
  "values-dev.yaml": [dev]
  "envs/*.yaml": match-by-basename
policies:
  extra: [extra/policies]
""",
    )
    s = load_validate_spec(spec)
    assert s.version == 1
    assert s.release_name == "demo"
    assert s.helm_version == "4.1.3"
    assert s.kubernetes_version == "1.31.2"
    assert set(s.environments) == {"dev", "prod"}
    assert s.environments["dev"].values == ["values.yaml", "values-dev.yaml"]
    assert s.triggers["envs/*.yaml"] == MATCH_BY_BASENAME
    assert s.policies.extra == ["extra/policies"]


def test_rejects_unknown_version(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        "version: 2\nrelease_name: x\nenvironments:\n  dev:\n    namespace: x\n",
    )
    with pytest.raises(SpecError, match="unsupported validate-spec version"):
        load_validate_spec(spec)


def test_rejects_helm_version_and_bin_both_set(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        """
version: 1
release_name: x
helm_version: "4.1.3"
helm_bin: /opt/helm
environments:
  dev:
    namespace: x
""",
    )
    with pytest.raises(SpecError, match="mutually exclusive"):
        load_validate_spec(spec)


def test_rejects_missing_release_name(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        "version: 1\nenvironments:\n  dev:\n    namespace: x\n",
    )
    with pytest.raises(SpecError):
        load_validate_spec(spec)


def test_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        """
version: 1
release_name: x
mystery: true
environments:
  dev:
    namespace: x
""",
    )
    with pytest.raises(SpecError, match=r"mystery|Extra"):
        load_validate_spec(spec)


def test_rejects_namespace_template_unset_and_env_namespace_unset(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        """
version: 1
release_name: x
environments:
  dev:
    values: [values.yaml]
""",
    )
    with pytest.raises(SpecError, match="namespace_template"):
        load_validate_spec(spec)


def test_namespace_template_substitution_and_override(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        """
version: 1
release_name: x
namespace_template: "lab-${env}"
environments:
  dev:
    values: [values.yaml]
  prod:
    namespace: lab-prod-explicit
""",
    )
    s = load_validate_spec(spec)
    assert resolve_namespace(s, "dev") == "lab-dev"
    assert resolve_namespace(s, "prod") == "lab-prod-explicit"


def test_resolve_namespace_unknown_env_raises() -> None:
    s = ValidateSpec.model_validate(
        {
            "version": 1,
            "release_name": "x",
            "namespace_template": "lab-${env}",
            "environments": {"dev": {"values": ["values.yaml"]}},
        }
    )
    with pytest.raises(SpecError, match="unknown environment"):
        resolve_namespace(s, "nope")


def test_trigger_string_must_be_match_by_basename(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        """
version: 1
release_name: x
environments:
  dev:
    namespace: x
triggers:
  "values.yaml": "bogus"
""",
    )
    with pytest.raises(SpecError, match="match-by-basename"):
        load_validate_spec(spec)


def test_trigger_env_must_exist(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        """
version: 1
release_name: x
environments:
  dev:
    namespace: x
triggers:
  "values.yaml": [staging]
""",
    )
    with pytest.raises(SpecError, match="unknown environment"):
        load_validate_spec(spec)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="missing validate spec"):
        load_validate_spec(tmp_path / "absent.yaml")


def test_empty_environments_rejected(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        "version: 1\nrelease_name: x\nenvironments: {}\n",
    )
    with pytest.raises(SpecError, match="at least one"):
        load_validate_spec(spec)
