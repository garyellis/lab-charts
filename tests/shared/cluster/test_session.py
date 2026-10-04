"""`shared/cluster`: provision, attach, find, stop and teardown one kind cluster."""

from __future__ import annotations

from pathlib import Path

from chart_manager.api.v1alpha1.local_cluster import LocalCluster
from chart_manager.shared.cluster import session
from chart_manager.shared.settings import Settings
from tests.conftest import FakeCommandRunner, Reply

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


def _runner(*clusters: str) -> FakeCommandRunner:
    return (
        FakeCommandRunner()
        .respond(("kind", "get", "clusters"), stdout="\n".join(clusters))
        .respond(READYZ, stdout="ok")
    )


def test_provision_creates_an_absent_cluster_and_waits_for_its_apiserver(tmp_path: Path) -> None:
    runner = _runner()

    lab = session.provision(
        _cluster(tmp_path),
        root=tmp_path,
        name="lab",
        run_hooks=False,
        runner=runner,
        settings=Settings(),
    )

    assert runner.calls == [
        ("kind", "get", "clusters"),
        ("kind", "create", "cluster", "--name", "lab", "--config", str(tmp_path / "kind.yaml")),
        READYZ,
    ]
    assert (lab.name, lab.context) == ("lab", "kind-lab")


def test_provision_runs_hooks_around_the_cluster_and_rewaits_after_the_post_hook(
    tmp_path: Path,
) -> None:
    runner = _runner("lab").respond(("docker", "ps"), stdout="")
    cluster = _cluster(
        tmp_path, {"preProvision": ["./scripts/pre"], "postProvision": ["post", "--x"]}
    )

    session.provision(
        cluster, root=tmp_path, name="lab", run_hooks=True, runner=runner, settings=Settings()
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
    runner = _runner("lab").respond(("docker", "ps"), stdout="")
    cluster = _cluster(tmp_path, {"preProvision": ["pre"], "postProvision": ["post"]})

    session.provision(
        cluster, root=tmp_path, name="lab", run_hooks=False, runner=runner, settings=Settings()
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
    runner = _runner("other", "lab")

    assert session.find("lab", runner=runner, settings=Settings()) is not None
    assert session.find("absent", runner=runner, settings=Settings()) is None


def test_teardown_deletes_an_existing_cluster_and_reports_an_absent_one() -> None:
    runner = _runner("lab")
    lab = session.attach("lab", runner=runner, settings=Settings())
    gone = session.attach("gone", runner=runner, settings=Settings())

    assert session.teardown(lab) is True
    assert session.teardown(gone) is False
    assert ("kind", "delete", "cluster", "--name", "lab") in runner.calls
    assert ("kind", "delete", "cluster", "--name", "gone") not in runner.calls
