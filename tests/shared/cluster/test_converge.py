"""`converge()`: the one release install, its readiness wait and its failure report."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.plumbing.errors import MissingToolError
from chart_manager.shared.cluster import session
from chart_manager.shared.cluster.converge import Release, ReleaseFailed, converge, installed
from chart_manager.shared.settings import Settings
from tests.conftest import FakeCommandRunner, argv_prefix, plain_argv

MANIFEST = """\
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
---
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: agent, namespace: kube-system}
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: widgets.example.com}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: settings}
"""


def _runner(manifest: str = MANIFEST, labelled: dict[str, str] | None = None) -> FakeCommandRunner:
    runner = FakeCommandRunner().respond(argv_prefix("helm", "get", "manifest"), stdout=manifest)
    for kind, names in (labelled or {}).items():
        runner.respond(argv_prefix("kubectl", "-n", "apps", "get", kind, "-l"), stdout=names)
    return runner


def _lab(runner: FakeCommandRunner) -> session.Session:
    return session.attach("lab", runner=runner, settings=Settings())


def _significant(runner: FakeCommandRunner) -> list[tuple[str, ...]]:
    """Every call except helm's revision lookups around the install."""
    return [
        plain_argv(argv) for argv in runner.calls if plain_argv(argv)[:2] != ("helm", "list")
    ]


def _chart(tmp_path: Path) -> Path:
    chart = tmp_path / "web"
    chart.mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: web\nversion: 0.1.0\n")
    return chart


def test_converge_installs_without_helm_wait_then_waits_on_the_manifest_and_instance_label(
    tmp_path: Path,
) -> None:
    runner = _runner(labelled={"deployment": "web made-by-operator"})
    chart = _chart(tmp_path)

    converge(_lab(runner), Release(name="web", chart=chart, namespace="apps", timeout="5m"))

    calls = _significant(runner)
    install = calls[0]
    assert install[:4] == ("helm", "upgrade", "--install", "web")
    assert "--wait" not in install
    assert install[install.index("--timeout") + 1] == "5m"
    assert calls[1] == ("helm", "get", "manifest", "web", "--namespace", "apps")
    waits = calls[2:]
    assert [c for c in waits if "rollout" in c or c[1] == "wait"] == [
        ("kubectl", "-n", "apps", "rollout", "status", "deployment/web", "--timeout=5m"),
        ("kubectl", "-n", "apps", "rollout", "status", "deployment/made-by-operator", "--timeout=5m"),
        ("kubectl", "-n", "kube-system", "rollout", "status", "daemonset/agent", "--timeout=5m"),
        (
            "kubectl", "wait", "--for=condition=Established",
            "customresourcedefinition/widgets.example.com", "--timeout=5m",
        ),
    ]


def test_a_failed_rollout_raises_release_failed_with_the_namespace_diagnostics(
    tmp_path: Path,
) -> None:
    runner = (
        _runner()
        .respond(argv_prefix("kubectl", "-n", "apps", "rollout"), returncode=1, stderr="timed out")
        .respond(argv_prefix("kubectl", "get", "pods", "-n", "apps"), stdout="web-0 CrashLoopBackOff")
    )

    with pytest.raises(ReleaseFailed) as failed:
        converge(_lab(runner), Release(name="web", chart=_chart(tmp_path), namespace="apps"))

    assert failed.value.step == "wait"
    assert "timed out" in str(failed.value)
    assert "web-0 CrashLoopBackOff" in failed.value.diagnostics


def test_a_failed_install_is_reported_with_diagnostics_and_nothing_is_awaited(
    tmp_path: Path,
) -> None:
    runner = _runner().respond(argv_prefix("helm", "upgrade"), returncode=1, stderr="conflict")

    with pytest.raises(ReleaseFailed) as failed:
        converge(_lab(runner), Release(name="web", chart=_chart(tmp_path), namespace="apps"))

    assert failed.value.step == "install"
    assert "## pods" in failed.value.diagnostics
    assert not any(plain_argv(argv)[:3] == ("helm", "get", "manifest") for argv in runner.calls)


def test_a_missing_tool_is_raised_as_itself() -> None:
    def helm_missing(argv: tuple[str, ...]) -> bool:
        if plain_argv(argv)[:2] == ("helm", "upgrade"):
            raise MissingToolError("helm not found")
        return False

    runner = FakeCommandRunner().respond(helm_missing)

    with pytest.raises(MissingToolError):
        converge(_lab(runner), Release(name="web", chart="oci://example/web", namespace="apps"))


def test_a_remote_chart_skips_the_dependency_update_and_passes_version_and_repo() -> None:
    runner = _runner(manifest="")

    status = converge(
        _lab(runner),
        Release(
            name="web", chart="web", namespace="apps", version="1.2.3", repo="https://charts.example"
        ),
    )

    install = _significant(runner)[0]
    assert install[:5] == ("helm", "upgrade", "--install", "web", "web")
    assert install[install.index("--version") + 1] == "1.2.3"
    assert install[install.index("--repo") + 1] == "https://charts.example"
    assert status == "applied"


def test_installed_maps_every_release_in_any_state_from_one_helm_list() -> None:
    runner = FakeCommandRunner().respond(
        argv_prefix("helm", "list"),
        stdout=(
            '[{"name": "web", "namespace": "apps", "revision": "2", "status": "deployed"},'
            ' {"name": "db", "namespace": "data", "revision": "1", "status": "pending-install"}]'
        ),
    )

    releases = installed(_lab(runner))

    assert releases == {("apps", "web"): "deployed", ("data", "db"): "pending-install"}
    assert [plain_argv(argv) for argv in runner.calls] == [("helm", "list", "-o", "json", "-A", "--all")]
