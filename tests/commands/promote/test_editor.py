from __future__ import annotations

from pathlib import Path

from chart_manager.commands.promote.editor import set_version

_HR = """\
---
# Loki promotion target
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
"""


def test_set_version_updates_matching_chart(tmp_path: Path) -> None:
    f = tmp_path / "loki.yaml"
    f.write_text(_HR)

    set_version(f, chart_name="loki", new_version="0.1.2")

    text = f.read_text()
    assert 'version: "0.1.2"' in text
    # Comment preserved by round-trip loader.
    assert "# Loki promotion target" in text


def test_set_version_no_op_when_already_target(tmp_path: Path) -> None:
    f = tmp_path / "loki.yaml"
    original = _HR.replace('"0.1.1"', '"0.1.2"')
    f.write_text(original)

    set_version(f, chart_name="loki", new_version="0.1.2")

    assert f.read_text() == original


def test_set_version_skips_other_charts(tmp_path: Path) -> None:
    f = tmp_path / "grafana.yaml"
    f.write_text(_HR.replace("chart: loki", "chart: grafana"))

    set_version(f, chart_name="loki", new_version="9.9.9")

    assert "9.9.9" not in f.read_text()


def test_set_version_updates_only_matching_doc_in_multidoc(tmp_path: Path) -> None:
    f = tmp_path / "stack.yaml"
    second = _HR.replace("chart: loki", "chart: grafana").replace("name: loki", "name: grafana")
    f.write_text(_HR + second)

    set_version(f, chart_name="loki", new_version="0.2.0")

    text = f.read_text()
    assert text.count('version: "0.2.0"') == 1
    # grafana version unchanged
    assert text.count('version: "0.1.1"') == 1
