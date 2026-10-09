"""`shared/cluster`: provision, attach, find, stop and teardown one kind cluster."""

from __future__ import annotations

from pathlib import Path

import pytest

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.settings import Settings
from chart_manager.shared.cluster import session
from tests.conftest import FakeCommandRunner, Reply, kind_runner

READYZ = ("kubectl", "get", "--raw=/readyz", "--context", "kind-lab")


def _cluster(tmp_path: Path, hooks: dict[str, list[str]] | None = None) -> LocalCluster:
    (tmp_path / "kind.yaml").write_text("kind: Cluster\n", encoding="utf-8")
    cluster: dict[str, object] = {"config": "kind.yaml"}
    if hooks:
        cluster["hooks"] = hooks
    return LocalCluster.model_validate(
        {
            "apiVersion": "chartmanager.io/v1alpha1",
            "kind": "LocalCluster",
            "metadata": {"name": "default"},
            "spec": {"cluster": cluster, "bootstrap": {"releases": []}},
        }
    )


def test_provision_creates_an_absent_cluster_and_waits_for_its_apiserver(tmp_path: Path) -> None:
    runner = kind_runner()

    dev = session.provision(
        _cluster(tmp_path),
        root=tmp_path,
        name="lab",
        run_hooks=False,
        runner=runner,
        settings=Settings(),
        progress=[].append,
    )

    assert runner.calls == [
        ("kind", "get", "clusters"),
        ("kind", "create", "cluster", "--name", "lab", "--config", str(tmp_path / "kind.yaml")),
        READYZ,
    ]
    assert (dev.name, dev.context) == ("lab", "kind-lab")


def test_provision_runs_hooks_around_the_cluster_and_rewaits_after_the_post_hook(
    tmp_path: Path,
) -> None:
    runner = kind_runner("lab").respond(("docker", "ps"), stdout="")
    cluster = _cluster(
        tmp_path, {"preProvision": ["./scripts/pre"], "postProvision": ["post", "--x"]}
    )

    session.provision(
        cluster,
        root=tmp_path,
        name="lab",
        run_hooks=True,
        runner=runner,
        settings=Settings(),
        progress=[].append,
    )

    assert [call for call in runner.calls if call[0] != "docker"] == [
        ("./scripts/pre",),
        ("kind", "get", "clusters"),
        READYZ,
        ("post", "--x"),
        READYZ,
    ]
    pre, post = (r for r in runner.records if r.args[0] in {"./scripts/pre", "post"})
    assert pre.cwd == post.cwd == tmp_path.resolve()
    assert pre.env == {
        "CHART_MANAGER_HOOK_PHASE": "preProvision",
        "CHART_MANAGER_ROOT": str(tmp_path.resolve()),
        "CHART_MANAGER_CLUSTER_NAME": "lab",
        "CHART_MANAGER_KIND_CONFIG": str(tmp_path / "kind.yaml"),
    }
    assert post.env == {
        **pre.env,
        "CHART_MANAGER_HOOK_PHASE": "postProvision",
        "CHART_MANAGER_KUBE_CONTEXT": "kind-lab",
        "CHART_MANAGER_PROVIDER_TYPE": "kind",
    }


def test_provision_skips_authored_hooks_when_hooks_are_off(tmp_path: Path) -> None:
    runner = kind_runner("lab").respond(("docker", "ps"), stdout="")
    cluster = _cluster(tmp_path, {"preProvision": ["pre"], "postProvision": ["post"]})

    session.provision(
        cluster,
        root=tmp_path,
        name="lab",
        run_hooks=False,
        runner=runner,
        settings=Settings(),
        progress=[].append,
    )

    assert ("pre",) not in runner.calls
    assert ("post",) not in runner.calls


def test_provision_with_replace_deletes_the_cluster_after_the_pre_hook(tmp_path: Path) -> None:
    runner = (
        FakeCommandRunner()
        .respond_each(("kind", "get", "clusters"), Reply(stdout="lab"), Reply(stdout=""))
        .respond(READYZ, stdout="ok")
    )
    cluster = _cluster(tmp_path, {"preProvision": ["pre"]})

    session.provision(
        cluster,
        root=tmp_path,
        name="lab",
        run_hooks=True,
        runner=runner,
        settings=Settings(),
        replace=True,
        progress=[].append,
    )

    assert runner.calls == [
        ("pre",),
        ("kind", "get", "clusters"),
        ("kind", "delete", "cluster", "--name", "lab"),
        ("kind", "get", "clusters"),
        ("kind", "create", "cluster", "--name", "lab", "--config", str(tmp_path / "kind.yaml")),
        READYZ,
    ]


def test_find_returns_a_session_only_for_an_existing_cluster() -> None:
    runner = kind_runner("other", "lab")

    assert session.find("lab", runner=runner, settings=Settings()) is not None
    assert session.find("absent", runner=runner, settings=Settings()) is None


def test_teardown_deletes_an_existing_cluster_and_reports_an_absent_one() -> None:
    runner = kind_runner("lab")
    dev = session.attach("lab", runner=runner, settings=Settings())
    gone = session.attach("gone", runner=runner, settings=Settings())

    assert session.teardown(dev) is True
    assert session.teardown(gone) is False
    assert ("kind", "delete", "cluster", "--name", "lab") in runner.calls
    assert ("kind", "delete", "cluster", "--name", "gone") not in runner.calls


def test_a_failed_pre_hook_stops_provision_before_anything_is_deleted(tmp_path: Path) -> None:
    runner = kind_runner("lab").respond(("pre",), returncode=9, stderr="blocked")

    with pytest.raises(ExternalCommandError, match="blocked"):
        session.provision(
            _cluster(tmp_path, {"preProvision": ["pre"]}),
            root=tmp_path,
            name="lab",
            run_hooks=True,
            runner=runner,
            settings=Settings(),
            replace=True,
            progress=[].append,
        )

    assert not any(call[:2] == ("kind", "delete") for call in runner.calls)


@pytest.mark.parametrize("entry", ["provision", "attach"])
def test_a_session_addresses_its_own_context_and_the_configured_docker_host(
    tmp_path: Path, entry: str
) -> None:
    runner = kind_runner("lab")
    settings = Settings(
        kube_context="ambient", docker_host="tcp://remote:2375", command_timeout=30.0
    )

    if entry == "provision":
        dev = session.provision(
            _cluster(tmp_path),
            root=tmp_path,
            name="lab",
            run_hooks=False,
            runner=runner,
            settings=settings,
            progress=[].append,
        )
    else:
        dev = session.attach("lab", runner=runner, settings=settings)
        dev.kind.clusters()
        dev.kubectl.wait_apiserver_ready()
    dev.helm.upgrade_install("app", tmp_path, namespace="app", timeout=60.0)

    kind, kubectl, helm = (
        [r for r in runner.records if r.args[0] == tool] for tool in ("kind", "kubectl", "helm")
    )
    assert kind and all(r.env == {"DOCKER_HOST": "tcp://remote:2375"} for r in kind)
    assert [r.args[-2:] for r in kubectl] == [("--context", "kind-lab")]
    assert {r.args[-2:] for r in helm} == {("--kube-context", "kind-lab")}
    assert {r.timeout for r in kind + kubectl} == {30.0}
    # Installs keep helm's own --timeout; the command timeout would kill a slow --wait.
    assert {r.timeout for r in helm} == {None}
