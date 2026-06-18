from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.services.helmrelease.scanner import scan

_LOKI_HR = """\
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: loki
  namespace: loki
spec:
  interval: 10m
  chart:
    spec:
      chart: loki
      version: "0.1.1"
      sourceRef:
        kind: HelmRepository
        name: lab-charts
        namespace: flux-system
      interval: 10m
"""

_GRAFANA_HR = """\
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: grafana
spec:
  chart:
    spec:
      chart: grafana
      version: "1.2.3"
"""

_MULTI_DOC = """\
---
apiVersion: v1
kind: Namespace
metadata:
  name: loki
---
apiVersion: helm.toolkit.fluxcd.io/v2beta2
kind: HelmRelease
metadata:
  name: loki
  namespace: loki
spec:
  chart:
    spec:
      chart: loki
      version: "0.1.1"
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_scan_finds_match_in_nested_subdir(tmp_path: Path) -> None:
    _write(tmp_path / "prod" / "apps" / "loki.yaml", _LOKI_HR)
    matches = scan(tmp_path / "prod", chart_name="loki")

    assert len(matches) == 1
    m = matches[0]
    assert m.name == "loki"
    assert m.namespace == "loki"
    assert m.current_version == "0.1.1"
    assert m.path.name == "loki.yaml"


def test_scan_finds_match_in_multi_doc_file(tmp_path: Path) -> None:
    _write(tmp_path / "prod" / "loki.yaml", _MULTI_DOC)
    matches = scan(tmp_path / "prod", chart_name="loki")

    assert len(matches) == 1
    assert matches[0].doc_index == 1
    assert matches[0].current_version == "0.1.1"


def test_scan_skips_non_matching_chart(tmp_path: Path) -> None:
    _write(tmp_path / "prod" / "grafana.yaml", _GRAFANA_HR)
    matches = scan(tmp_path / "prod", chart_name="loki")
    assert matches == []


def test_scan_returns_match_when_already_at_target_version(tmp_path: Path) -> None:
    # Service-layer uses the difference between current_version and target
    # to decide no-op; scan itself must still surface the match.
    _write(tmp_path / "prod" / "loki.yaml", _LOKI_HR)
    matches = scan(tmp_path / "prod", chart_name="loki")
    assert len(matches) == 1
    assert matches[0].current_version == "0.1.1"


def test_scan_ignores_non_yaml_files(tmp_path: Path) -> None:
    _write(tmp_path / "prod" / "README.md", _LOKI_HR)
    matches = scan(tmp_path / "prod", chart_name="loki")
    assert matches == []


def test_scan_ignores_non_flux_kinds(tmp_path: Path) -> None:
    bogus = _LOKI_HR.replace("helm.toolkit.fluxcd.io/v2", "apps/v1")
    _write(tmp_path / "prod" / "loki.yaml", bogus)
    assert scan(tmp_path / "prod", chart_name="loki") == []


def test_scan_aggregates_across_files(tmp_path: Path) -> None:
    _write(tmp_path / "prod" / "a" / "loki.yaml", _LOKI_HR)
    _write(tmp_path / "prod" / "b" / "loki.yaml", _LOKI_HR)
    _write(tmp_path / "prod" / "b" / "grafana.yaml", _GRAFANA_HR)
    matches = scan(tmp_path / "prod", chart_name="loki")
    assert len(matches) == 2


def test_scan_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ChartManagerError, match="does not exist"):
        scan(tmp_path / "nope", chart_name="loki")
