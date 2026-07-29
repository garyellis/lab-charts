"""Helm OCI package/push adapter contracts."""

from pathlib import Path

import pytest

from chart_manager.integrations.helm import Helm
from chart_manager.plumbing.errors import ExternalCommandError

from .conftest import FakeCommandRunner


def test_package_uses_override_without_editing_chart_yaml(tmp_path: Path) -> None:
    chart = tmp_path / "demo"
    chart.mkdir()
    metadata = chart / "Chart.yaml"
    metadata.write_text("apiVersion: v2\nname: demo\nversion: 1.2.3\n")
    output = tmp_path / "packages"
    runner = FakeCommandRunner(
        stdout=f"Successfully packaged chart and saved it to: {output}/demo-2.0.0.tgz\n"
    )

    result = Helm(runner=runner, binary="helm", context="ignored").package(
        chart, output, version="2.0.0"
    )

    assert runner.records[0].args == (
        "helm",
        "package",
        str(chart),
        "--destination",
        str(output),
        "--version",
        "2.0.0",
    )
    assert result.path == output / "demo-2.0.0.tgz"
    assert metadata.read_text() == "apiVersion: v2\nname: demo\nversion: 1.2.3\n"


def test_push_captures_full_reference_and_digest(tmp_path: Path) -> None:
    package = tmp_path / "demo-1.2.3.tgz"
    runner = FakeCommandRunner(
        stdout=(
            "Pushed: registry.local/library/demo:1.2.3\n"
            "Digest: sha256:0123456789\n"
        )
    )

    ca_file = tmp_path / "lab-ca.crt"
    result = Helm(runner=runner, binary="helm", context="ignored").push(
        package,
        "oci://registry.local/library/",
        ca_file=ca_file,
    )

    assert runner.records[0].args == (
        "helm",
        "push",
        str(package),
        "oci://registry.local/library",
        "--ca-file",
        str(ca_file),
    )
    assert result.reference == "oci://registry.local/library/demo:1.2.3"
    assert result.digest == "sha256:0123456789"


def test_push_reads_decorated_machine_output_from_stderr(tmp_path: Path) -> None:
    runner = FakeCommandRunner(
        stderr=(
            "\x1b[32m  Pushed: registry.local/library/demo:1.2.3\x1b[0m\n"
            "  Digest: sha256:0123456789\n"
        )
    )

    result = Helm(runner=runner, binary="helm").push(
        tmp_path / "demo-1.2.3.tgz",
        "oci://registry.local/library",
    )

    assert result.reference == "oci://registry.local/library/demo:1.2.3"
    assert result.digest == "sha256:0123456789"
    assert "Pushed:" in result.output


def test_push_uses_expected_reference_when_success_output_has_no_marker(
    tmp_path: Path,
) -> None:
    expected = "oci://registry.local/library/cert-manager:1.18.2"
    helm = Helm(runner=FakeCommandRunner(stdout="upload complete\n"), binary="helm")

    result = helm.push(
        tmp_path / "cert-manager-1.18.2.tgz",
        "oci://registry.local/library",
        expected_reference=expected,
    )

    assert result.reference == expected
    assert result.digest is None


def test_package_and_push_reject_missing_machine_output(tmp_path: Path) -> None:
    helm = Helm(runner=FakeCommandRunner(stdout="done\n"), binary="helm")
    with pytest.raises(ExternalCommandError, match="archive path"):
        helm.package(tmp_path, tmp_path / "out")

    with pytest.raises(ValueError, match="oci://"):
        helm.push(tmp_path / "demo.tgz", "registry.local/library")

    with pytest.raises(ExternalCommandError, match="pushed OCI reference"):
        helm.push(tmp_path / "demo.tgz", "oci://registry.local/library")
